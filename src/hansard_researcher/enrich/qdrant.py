"""Qdrant vector index — optional ANN backend for semantic search.

The default search backend scans the embeddings Parquet with DuckDB, which
is fine for a slice but brute-force over the full archive (6.4M paragraphs).
Qdrant gives indexed approximate search: ``hansard-researcher enrich index`` loads
the already-computed embeddings into a collection, and
``enrich search --backend qdrant`` queries it.

Plain REST via httpx — no extra client dependency. One collection per
embedding model (``parlhansard__{model_slug}``); the point id is the
deterministic silver ``text_id`` (already a UUID), so re-indexing is an
idempotent upsert. Upserts never delete: ``enrich prune`` reconciles
retired text_ids (revised drafts, identity fixes) out of the index.
Payloads carry join keys plus citation metadata (subject heading, debate
context — proceeding/subproceeding/bill/committee names — member name,
party, electorate, turn kind, page/time anchors, parliament/session and
review stage, subject uid / extract index for deep links, harvested source
URL) — **no Hansard prose ever enters Qdrant**; result text is hydrated
from local silver at query time. Headings and member names are official
public record, already exposed via the gold marts.

Server: ``docker compose --profile enrich up -d`` (or any Qdrant); URL from
``HANSARD_RESEARCHER_QDRANT_URL`` (default ``http://localhost:6333``).
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from pathlib import Path

import duckdb
import httpx
import pyarrow.dataset as ds
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from hansard_researcher.enrich.embed import model_slug
from hansard_researcher.enrich.providers import ProviderError


def _retryable(exc: BaseException) -> bool:
    # transient transport aborts (observed: WinError 10053 under parallel
    # bulk load) and server-side pressure; safe because every operation
    # here is idempotent — points are keyed by text_id
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code >= 500 or exc.response.status_code == 429
    )


_RETRY = retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, max=8),
    reraise=True,
)

DEFAULT_URL = "http://localhost:6333"

#: Qdrant's default optimizer indexing_threshold (KB of unindexed vectors per
#: segment before HNSW building starts) — restored after a bulk load.
INDEXING_THRESHOLD_DEFAULT = 20_000

_PAYLOAD_KEYS = ("jurisdiction", "date", "house", "subject_id", "talker_id", "model")

#: citation metadata joined from silver at index time; nulls are dropped from
#: the payload rather than stored (talker fields are structurally null on
#: procedural/chair text). Deep links are constructed from these at API
#: response time — never baked in (a link-format fix must not cost a
#: full re-index). All values are structural metadata / official public
#: record — never Hansard prose.
_CITATION_KEYS = (
    # subject / debate context
    "subject_uid", "subject_name", "proceeding_name", "subproceeding_name",
    "committee_name", "bill_names", "extract_index",
    # speaker
    "speaker", "party", "party_abbreviation", "electorate", "role", "talker_kind",
    # citation position
    "text_kind", "page_no", "time_anchor",
    # sitting formalities
    "parliament_num", "session_num", "review_stage",
    # provenance
    "source_url",
)

def _payload_value(value):
    """Payload values must be JSON-native; timestamps become ISO-8601 UTC
    (DuckDB hands timestamptz back in the session timezone — normalize so
    the payload does not depend on the indexing machine's clock settings)."""
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat()
    return value


#: payload fields with keyword indexes — filtered search AND the prune scroll
#: degrade to full per-page scans without them (observed: ~1 s per 1,000-id
#: scroll page over 6.5M points; indexed: milliseconds)
_PAYLOAD_INDEXES = ("jurisdiction", "house", "date")


def collection_name(model: str) -> str:
    # frozen "parlhansard" prefix: existing collections keep working after the
    # rename to hansard-researcher (a new prefix would force a full re-index)
    return f"parlhansard__{model_slug(model)}"


class QdrantIndex:
    def __init__(
        self,
        url: str | None = None,
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        env_url = os.environ.get("HANSARD_RESEARCHER_QDRANT_URL")
        self.url = (url or env_url or DEFAULT_URL).rstrip("/")
        self._client = httpx.Client(base_url=self.url, timeout=timeout, transport=transport)

    @_RETRY
    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    def ensure_collection(self, name: str, dim: int) -> bool:
        """Create the collection if missing; returns True when created."""
        response = self._client.get(f"/collections/{name}")
        if response.status_code == 200:
            existing = (
                response.json()["result"]["config"]["params"]["vectors"].get("size")
            )
            if existing != dim:
                raise ProviderError(
                    f"collection {name!r} exists with dim {existing}, embeddings have "
                    f"dim {dim} — different model output? delete the collection first"
                )
            return False
        self._request(
            "PUT", f"/collections/{name}",
            json={"vectors": {"size": dim, "distance": "Cosine"}},
        )
        return True

    def upsert(self, name: str, points: list[dict], *, wait: bool = True) -> None:
        """Upsert points; ``wait=False`` returns on WAL write (bulk loads)."""
        self._request(
            "PUT", f"/collections/{name}/points",
            params={"wait": "true" if wait else "false"},
            json={"points": points},
        )

    def set_indexing_threshold(self, name: str, threshold: int) -> None:
        """Pause (0) or resume (INDEXING_THRESHOLD_DEFAULT) HNSW index building."""
        self._request(
            "PATCH", f"/collections/{name}",
            json={"optimizers_config": {"indexing_threshold": threshold}},
        )

    def collection_exists(self, name: str) -> bool:
        return self._client.get(f"/collections/{name}").status_code == 200

    def ensure_payload_indexes(self, name: str) -> None:
        """Keyword indexes on the filterable payload fields (idempotent;
        Qdrant builds them online in the background)."""
        for field in _PAYLOAD_INDEXES:
            self._request(
                "PUT", f"/collections/{name}/index",
                params={"wait": "false"},
                json={"field_name": field, "field_schema": "keyword"},
            )

    def iter_point_ids(
        self, name: str, *, jurisdiction: str | None = None, page_size: int = 10_000
    ) -> Iterator[str]:
        """Scroll every point id (no payloads/vectors — ids are cheap)."""
        body: dict = {"limit": page_size, "with_payload": False, "with_vector": False}
        if jurisdiction:
            body["filter"] = {
                "must": [{"key": "jurisdiction", "match": {"value": jurisdiction}}]
            }
        offset = None
        while True:
            page = dict(body, **({"offset": offset} if offset is not None else {}))
            response = self._request(
                "POST", f"/collections/{name}/points/scroll", json=page
            )
            result = response.json()["result"]
            for point in result["points"]:
                yield str(point["id"])
            offset = result.get("next_page_offset")
            if offset is None:
                return

    def delete_points(self, name: str, ids: list[str], *, wait: bool = True) -> None:
        self._request(
            "POST", f"/collections/{name}/points/delete",
            params={"wait": "true" if wait else "false"},
            json={"points": ids},
        )

    def search(
        self,
        name: str,
        vector: list[float],
        *,
        k: int = 10,
        jurisdiction: str | None = None,
    ) -> list[dict]:
        """Top-k points: ``[{id, score, payload}, ...]``."""
        body: dict = {"vector": vector, "limit": k, "with_payload": True}
        if jurisdiction:
            body["filter"] = {
                "must": [{"key": "jurisdiction", "match": {"value": jurisdiction}}]
            }
        response = self._request("POST", f"/collections/{name}/points/search", json=body)
        return response.json()["result"]


def index_embeddings(
    data_dir: Path,
    jurisdiction: str,
    index: QdrantIndex,
    model: str,
    *,
    batch_size: int = 512,
    workers: int = 4,
    log=print,
) -> dict[str, int]:
    """Load ``data/enriched/embeddings`` for one model+jurisdiction into Qdrant.

    Bulk-load shape (per Qdrant's own guidance): the jurisdiction filter is
    pushed into the parquet scan (partition pruning — other jurisdictions'
    files are never read), HNSW building is paused for the duration, and
    ``workers`` threads send batches with ``wait=false``. Safe because points
    are idempotent by id — no batch depends on another. On return Qdrant
    resumes indexing in the background; unindexed points are still
    searchable meanwhile (exact scan on unindexed segments).

    Citation metadata (subject heading/uid/extract index, debate context,
    member name/party/electorate, page/time anchors, sitting formalities,
    harvested source URL — see ``_CITATION_KEYS``) is joined from local
    silver per batch via a streaming DuckDB scan, so payloads are
    self-sufficient for a hosted API that has no silver.
    """
    embeddings_dir = (
        Path(data_dir) / "enriched" / "embeddings" / f"model_slug={model_slug(model)}"
    )
    if not embeddings_dir.is_dir():
        raise ProviderError(
            f"no embeddings for model {model!r} under {embeddings_dir} — "
            "run 'hansard-researcher enrich embed' first"
        )
    silver = Path(data_dir) / "silver"

    def table_or_empty(table: str, columns: dict[str, str]) -> str:
        """A silver table as a DuckDB relation — typed and empty when the
        table was never written (write_silver skips row-less tables; small
        archives may have no subproceedings or bill_refs at all)."""
        path = silver / table
        if any(path.rglob("*.parquet")):
            return f"read_parquet('{path.as_posix()}/**/*.parquet', hive_partitioning = true)"
        typed_nulls = ", ".join(f"null::{sql_type} as {name}" for name, sql_type in columns.items())
        return f"(select {typed_nulls} where false)"

    subproceedings_rel = table_or_empty(
        "subproceedings",
        {"subproceeding_id": "varchar", "name": "varchar", "jurisdiction": "varchar"},
    )
    bill_refs_rel = table_or_empty(
        "bill_refs",
        {"subject_id": "varchar", "name": "varchar", "jurisdiction": "varchar"},
    )
    # all join keys come from silver texts via text_id — NEVER from the
    # embeddings' own subject/talker/fragment columns, which are NULL in
    # pre-provenance slices (and go stale when a revised day shifts ids)
    sql = f"""
        select e.text_id, e.embedding, e.dim,
               e.jurisdiction, e.date, e.house,
               t.subject_id, t.talker_id,
               s.uid           as subject_uid,
               s.name          as subject_name,
               s.extract_index as extract_index,
               p.name          as proceeding_name,
               sp.name         as subproceeding_name,
               coalesce(s.committee_name, f.committee_name) as committee_name,
               b.bill_names    as bill_names,
               tk.name         as speaker,
               tk.party        as party,
               tk.party_abbreviation as party_abbreviation,
               tk.electorate   as electorate,
               tk.role         as role,
               tk.kind         as talker_kind,
               t.kind          as text_kind,
               t.page_no       as page_no,
               t.time_anchor   as time_anchor,
               f.parliament_num as parliament_num,
               f.session_num   as session_num,
               f.review_stage  as review_stage,
               f.source_url    as source_url
        from read_parquet('{embeddings_dir.as_posix()}/**/*.parquet',
                          hive_partitioning = true) e
        left join read_parquet('{(silver / "texts").as_posix()}/**/*.parquet',
                               hive_partitioning = true) t
               on e.text_id = t.text_id and t.jurisdiction = ?
        left join read_parquet('{(silver / "subjects").as_posix()}/**/*.parquet',
                               hive_partitioning = true) s
               on t.subject_id = s.subject_id and s.jurisdiction = ?
        left join read_parquet('{(silver / "proceedings").as_posix()}/**/*.parquet',
                               hive_partitioning = true) p
               -- texts only carry proceeding_id when they sit directly under
               -- the proceeding; subject-level text routes via the subject
               on coalesce(t.proceeding_id, s.proceeding_id) = p.proceeding_id
                  and p.jurisdiction = ?
        left join {subproceedings_rel} sp
               on t.subproceeding_id = sp.subproceeding_id and sp.jurisdiction = ?
        left join (
            select jurisdiction, subject_id,
                   list(distinct name order by name) as bill_names
            from {bill_refs_rel}
            where name is not null
            group by jurisdiction, subject_id
        ) b on t.subject_id = b.subject_id and b.jurisdiction = ?
        left join read_parquet('{(silver / "talkers").as_posix()}/**/*.parquet',
                               hive_partitioning = true) tk
               on t.talker_id = tk.talker_id and tk.jurisdiction = ?
        left join read_parquet('{(silver / "fragments").as_posix()}/**/*.parquet',
                               hive_partitioning = true) f
               on t.fragment_id = f.fragment_id and f.jurisdiction = ?
        where e.jurisdiction = ?
    """
    name = collection_name(model)
    created = None
    points_written = 0
    reader = (
        duckdb.connect()
        .execute(sql, [jurisdiction] * 8)
        .to_arrow_reader(batch_size)
    )
    in_flight: set[Future] = set()

    def drain(down_to: int) -> None:
        nonlocal in_flight
        while len(in_flight) > down_to:
            done, in_flight = wait_futures(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch in reader:
                rows = batch.to_pylist()
                if not rows:
                    continue
                if created is None:
                    created = index.ensure_collection(name, rows[0]["dim"])
                    index.ensure_payload_indexes(name)
                    index.set_indexing_threshold(name, 0)
                points = [
                    {
                        "id": row["text_id"],
                        "vector": row["embedding"],
                        "payload": {
                            **{
                                k: str(row[k])
                                for k in _PAYLOAD_KEYS
                                if k != "model" and row[k] is not None
                            },
                            "model": model,
                            **{
                                k: _payload_value(row[k])
                                for k in _CITATION_KEYS
                                if row[k] is not None
                            },
                        },
                    }
                    for row in rows
                ]
                # bound memory: at most ~2 encoded batches queued per worker
                drain(workers * 2)
                in_flight.add(pool.submit(index.upsert, name, points, wait=False))
                points_written += len(rows)
                if points_written % (batch_size * 20) < batch_size:
                    log(f"  {points_written:,} points indexed")
            drain(0)
    finally:
        if created is not None:
            index.set_indexing_threshold(name, INDEXING_THRESHOLD_DEFAULT)
    return {"points": points_written, "created": int(bool(created))}


def _sweep_dead_partitions(base_jur: Path, silver_jur: Path, log, label: str) -> int:
    """Remove house-day partition dirs under ``base_jur`` with no matching
    silver ``texts`` partition (hive dir names are pyarrow-encoded
    identically on both sides)."""
    removed = 0
    if not base_jur.is_dir():
        return 0
    for date_dir in sorted(base_jur.glob("date=*")):
        for house_dir in sorted(date_dir.glob("house=*")):
            if not (silver_jur / date_dir.name / house_dir.name).is_dir():
                shutil.rmtree(house_dir)
                removed += 1
                log(f"  dead {label} partition removed: {date_dir.name}/{house_dir.name}")
        if not any(date_dir.iterdir()):
            date_dir.rmdir()
    return removed


def prune_index(
    data_dir: Path,
    jurisdiction: str,
    index: QdrantIndex,
    model: str,
    *,
    delete_batch: int = 512,
    log=print,
) -> dict[str, int]:
    """Flow deletions downstream: silver -> enriched parquet -> Qdrant.

    Upserts never delete, so anything that retires a ``text_id`` — a revised
    house-day (uncorrected -> corrected), an identity fix — strands points in
    Qdrant (dead ids waste top-k slots; hydration drops them silently) and can
    strand whole enriched partitions whose silver partition moved. Three
    reconciliations, each against the layer above:

    1. embeddings house-day partitions (this model) with no matching silver
       ``texts`` partition are deleted (they would otherwise re-seed dead
       points on every future ``enrich index`` run);
    2. themes house-day partitions with no matching silver partition are
       deleted for ALL theme model_slugs — themes are not tied to the Qdrant
       collection, and stale ones pollute the next aggregate;
    3. Qdrant points whose id is no longer in the embeddings dataset are
       deleted.

    Within-partition staleness (a revised day not yet re-embedded) is not
    prune's job — re-embedding replaces that slice atomically; run prune
    after ``enrich embed``.
    """
    silver_jur = Path(data_dir) / "silver" / "texts" / f"jurisdiction={jurisdiction}"
    model_dir = Path(data_dir) / "enriched" / "embeddings" / f"model_slug={model_slug(model)}"
    embeddings_jur = model_dir / f"jurisdiction={jurisdiction}"

    partitions_removed = _sweep_dead_partitions(
        embeddings_jur, silver_jur, log, "embeddings"
    )
    theme_partitions_removed = sum(
        _sweep_dead_partitions(
            theme_model_dir / f"jurisdiction={jurisdiction}", silver_jur, log, "themes"
        )
        for theme_model_dir in (Path(data_dir) / "enriched" / "themes").glob("model_slug=*")
    )

    name = collection_name(model)
    stats = {
        "partitions_removed": partitions_removed,
        "theme_partitions_removed": theme_partitions_removed,
        "points_checked": 0,
        "points_deleted": 0,
    }
    if not index.collection_exists(name):
        return stats

    current: set[str] = set()
    if embeddings_jur.is_dir() and any(embeddings_jur.rglob("*.parquet")):
        dataset = ds.dataset(model_dir, format="parquet", partitioning="hive")
        for batch in dataset.to_batches(
            columns=["text_id"], filter=ds.field("jurisdiction") == jurisdiction
        ):
            current.update(batch.column("text_id").to_pylist())

    orphans: list[str] = []
    for point_id in index.iter_point_ids(name, jurisdiction=jurisdiction):
        stats["points_checked"] += 1
        if point_id not in current:
            orphans.append(point_id)
            if len(orphans) >= delete_batch:
                index.delete_points(name, orphans)
                stats["points_deleted"] += len(orphans)
                orphans = []
    if orphans:
        index.delete_points(name, orphans)
        stats["points_deleted"] += len(orphans)
    return stats

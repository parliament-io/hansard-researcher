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
Payloads carry join keys only — **no Hansard prose ever enters Qdrant**;
result text is hydrated from local silver at query time.

Server: ``docker compose --profile enrich up -d`` (or any Qdrant); URL from
``HANSARD_RESEARCHER_QDRANT_URL`` (default ``http://localhost:6333``).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from pathlib import Path

import httpx
import pyarrow.dataset as ds

from hansard_researcher.enrich.embed import model_slug
from hansard_researcher.enrich.providers import ProviderError

DEFAULT_URL = "http://localhost:6333"

#: Qdrant's default optimizer indexing_threshold (KB of unindexed vectors per
#: segment before HNSW building starts) — restored after a bulk load.
INDEXING_THRESHOLD_DEFAULT = 20_000

_PAYLOAD_KEYS = ("jurisdiction", "date", "house", "subject_id", "talker_id", "model")


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
        response = self._client.put(
            f"/collections/{name}",
            json={"vectors": {"size": dim, "distance": "Cosine"}},
        )
        response.raise_for_status()
        return True

    def upsert(self, name: str, points: list[dict], *, wait: bool = True) -> None:
        """Upsert points; ``wait=False`` returns on WAL write (bulk loads)."""
        response = self._client.put(
            f"/collections/{name}/points",
            params={"wait": "true" if wait else "false"},
            json={"points": points},
        )
        response.raise_for_status()

    def set_indexing_threshold(self, name: str, threshold: int) -> None:
        """Pause (0) or resume (INDEXING_THRESHOLD_DEFAULT) HNSW index building."""
        response = self._client.patch(
            f"/collections/{name}",
            json={"optimizers_config": {"indexing_threshold": threshold}},
        )
        response.raise_for_status()

    def collection_exists(self, name: str) -> bool:
        return self._client.get(f"/collections/{name}").status_code == 200

    def iter_point_ids(
        self, name: str, *, jurisdiction: str | None = None, page_size: int = 1000
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
            response = self._client.post(f"/collections/{name}/points/scroll", json=page)
            response.raise_for_status()
            result = response.json()["result"]
            for point in result["points"]:
                yield str(point["id"])
            offset = result.get("next_page_offset")
            if offset is None:
                return

    def delete_points(self, name: str, ids: list[str], *, wait: bool = True) -> None:
        response = self._client.post(
            f"/collections/{name}/points/delete",
            params={"wait": "true" if wait else "false"},
            json={"points": ids},
        )
        response.raise_for_status()

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
        response = self._client.post(f"/collections/{name}/points/search", json=body)
        response.raise_for_status()
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
    """
    embeddings_dir = (
        Path(data_dir) / "enriched" / "embeddings" / f"model_slug={model_slug(model)}"
    )
    if not embeddings_dir.is_dir():
        raise ProviderError(
            f"no embeddings for model {model!r} under {embeddings_dir} — "
            "run 'hansard-researcher enrich embed' first"
        )
    name = collection_name(model)
    created = None
    points_written = 0
    dataset = ds.dataset(embeddings_dir, format="parquet", partitioning="hive")
    batches = dataset.to_batches(
        columns=["text_id", "embedding", "dim", *(k for k in _PAYLOAD_KEYS if k != "model")],
        batch_size=batch_size,
        filter=ds.field("jurisdiction") == jurisdiction,
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
            for batch in batches:
                rows = batch.to_pylist()
                if not rows:
                    continue
                if created is None:
                    created = index.ensure_collection(name, rows[0]["dim"])
                    index.set_indexing_threshold(name, 0)
                points = [
                    {
                        "id": row["text_id"],
                        "vector": row["embedding"],
                        "payload": {
                            **{k: str(row[k]) for k in _PAYLOAD_KEYS if k != "model"},
                            "model": model,
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


def prune_index(
    data_dir: Path,
    jurisdiction: str,
    index: QdrantIndex,
    model: str,
    *,
    delete_batch: int = 512,
    log=print,
) -> dict[str, int]:
    """Flow deletions downstream: silver -> embeddings parquet -> Qdrant.

    Upserts never delete, so anything that retires a ``text_id`` — a revised
    house-day (uncorrected -> corrected), an identity fix — strands points in
    Qdrant (dead ids waste top-k slots; hydration drops them silently) and can
    strand whole embeddings partitions whose silver partition moved. Two
    reconciliations, each against the layer above:

    1. embeddings house-day partitions with no matching silver ``texts``
       partition are deleted (they would otherwise re-seed dead points on
       every future ``enrich index`` run);
    2. Qdrant points whose id is no longer in the embeddings dataset are
       deleted.

    Within-partition staleness (a revised day not yet re-embedded) is not
    prune's job — re-embedding replaces that slice atomically; run prune
    after ``enrich embed``.
    """
    silver_jur = Path(data_dir) / "silver" / "texts" / f"jurisdiction={jurisdiction}"
    model_dir = Path(data_dir) / "enriched" / "embeddings" / f"model_slug={model_slug(model)}"
    embeddings_jur = model_dir / f"jurisdiction={jurisdiction}"

    partitions_removed = 0
    if embeddings_jur.is_dir():
        # hive dir names are pyarrow-encoded identically on both sides
        for date_dir in sorted(embeddings_jur.glob("date=*")):
            for house_dir in sorted(date_dir.glob("house=*")):
                if not (silver_jur / date_dir.name / house_dir.name).is_dir():
                    shutil.rmtree(house_dir)
                    partitions_removed += 1
                    log(f"  dead partition removed: {date_dir.name}/{house_dir.name}")
            if not any(date_dir.iterdir()):
                date_dir.rmdir()

    name = collection_name(model)
    stats = {"partitions_removed": partitions_removed, "points_checked": 0, "points_deleted": 0}
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

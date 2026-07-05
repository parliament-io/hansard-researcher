"""Paragraph embeddings: silver ``texts`` -> ``data/enriched/embeddings``.

Optional Tier 3 — runs only when the user configures a provider (see
:mod:`hansard_researcher.enrich.providers`). Embedding rows carry **no Hansard
prose**: vectors + join keys only; display text is joined back from the
local silver tables (licensing stance, LICENSES-DATA.md — vectors are
non-expressive derived data).

Layout mirrors silver: hive-partitioned by (model_slug, jurisdiction, date,
house) and written with ``delete_matching``, so re-embedding a house-day for
one model atomically replaces exactly that slice and never touches another
model's vectors. ``text_id`` is deterministic from silver, so
(``model``, ``text_id``) is a stable dedup key — switching providers or
re-running stays coherent.

Incremental runs are content-aware: each embedded partition carries a
``_signature.json`` sidecar (the ``_`` prefix hides it from parquet dataset
discovery) hashing the embeddable (text_id, clean_text) pairs. A revised
house-day (draft -> corrected) re-embeds without ``--force``; a silver
rewrite with identical content (full re-normalize) stays skipped.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote

import pyarrow as pa
import pyarrow.dataset as ds

SCHEMA = pa.schema(
    [
        ("text_id", pa.string()),
        ("fragment_id", pa.string()),
        ("talker_id", pa.string()),
        ("subject_id", pa.string()),
        ("model", pa.string()),
        ("provider", pa.string()),
        ("dim", pa.int32()),
        ("embedding", pa.list_(pa.float32())),
        ("model_slug", pa.string()),
        ("jurisdiction", pa.string()),
        ("date", pa.string()),
        ("house", pa.string()),
    ]
)

_PARTITIONING = ds.partitioning(
    pa.schema(
        [
            ("model_slug", pa.string()),
            ("jurisdiction", pa.string()),
            ("date", pa.string()),
            ("house", pa.string()),
        ]
    ),
    flavor="hive",
)


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def model_slug(model: str) -> str:
    """Filesystem-safe partition value for a model id (raw id stays in ``model``)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model)


@dataclass(frozen=True)
class HouseDay:
    """One silver house-day partition. Values are hive-decoded; ``path`` keeps
    the encoded directory names, so an output path built from ``path`` parts
    matches what pyarrow will write for the same values."""

    jurisdiction: str
    date: str
    house: str
    path: Path


_SIGNATURE_NAME = "_signature.json"


def _embeddable(silver_day_dir: Path) -> list[tuple[str, str]]:
    """Sorted (text_id, clean_text) pairs a run of embed_day would produce."""
    table = ds.dataset(silver_day_dir, format="parquet").to_table(
        columns=["text_id", "clean_text"]
    )
    return sorted(
        (row["text_id"], row["clean_text"])
        for row in table.to_pylist()
        if row["clean_text"] and row["clean_text"].strip()
    )


def _signature_of(pairs: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for text_id, clean_text in pairs:
        digest.update(text_id.encode())
        digest.update(b"\x00")
        digest.update(clean_text.encode())
        digest.update(b"\x00")
    return digest.hexdigest()


def _write_signature(partition: Path, signature: str, texts: int) -> None:
    (partition / _SIGNATURE_NAME).write_text(
        json.dumps({"version": 1, "sha256": signature, "texts": texts}),
        encoding="utf-8",
    )


def _is_fresh(silver_day_dir: Path, partition: Path) -> bool:
    """True when the existing embeddings partition still matches silver.

    mtime is only a fast-path gate, never the decider: a full re-normalize
    rewrites every silver partition with mostly identical content, and an
    mtime-only check would answer that with a full archive re-embed. When
    silver is newer, the stored content signature decides; a signature-less
    partition (pre-dating sidecars) falls back to comparing text_id sets —
    equal sets are baselined as in-sync (matching the historical skip
    behaviour), different sets mean the day was revised.
    """
    signature_path = partition / _SIGNATURE_NAME
    silver_mtime = max(
        (f.stat().st_mtime for f in silver_day_dir.glob("*.parquet")), default=0.0
    )
    if signature_path.is_file() and signature_path.stat().st_mtime >= silver_mtime:
        return True
    pairs = _embeddable(silver_day_dir)
    signature = _signature_of(pairs)
    if signature_path.is_file():
        stored = json.loads(signature_path.read_text(encoding="utf-8"))["sha256"]
        if stored == signature:
            signature_path.touch()  # re-arm the mtime fast path
            return True
        return False
    embedded_ids = set(
        ds.dataset(partition, format="parquet")
        .to_table(columns=["text_id"])
        .column("text_id")
        .to_pylist()
    )
    if embedded_ids == {text_id for text_id, _ in pairs}:
        _write_signature(partition, signature, len(pairs))
        return True
    return False


def iter_house_days(
    texts_dir: Path,
    jurisdiction: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
):
    """Walk silver's hive layout without reading any data files."""
    for date_dir in sorted((texts_dir / f"jurisdiction={jurisdiction}").glob("date=*")):
        date = date_dir.name.split("=", 1)[1]
        if (start and date < start.isoformat()) or (end and date > end.isoformat()):
            continue
        for house_dir in sorted(date_dir.glob("house=*")):
            house = unquote(house_dir.name.split("=", 1)[1])
            yield HouseDay(jurisdiction, date, house, house_dir)


def embed_texts(
    data_dir: Path,
    jurisdiction: str,
    embedder: Embedder,
    *,
    provider: str,
    model: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    batch_size: int = 96,
    workers: int = 1,
    force: bool = False,
    log=print,
) -> dict[str, int]:
    """Embed every non-empty silver paragraph; incremental per (model, house-day)."""
    texts_dir = data_dir / "silver" / "texts"
    out_dir = data_dir / "enriched" / "embeddings"
    slug = model_slug(model)

    def partition_for(day: HouseDay) -> Path:
        return (
            out_dir / f"model_slug={slug}" / day.path.parent.parent.name
            / day.path.parent.name / day.path.name
        )

    def embed_day(day: HouseDay) -> int:
        """Embed one house-day; returns vectors written (0 = no text)."""
        rows = [
            row
            for row in ds.dataset(day.path, format="parquet")
            .to_table(columns=["text_id", "fragment_id", "talker_id", "subject_id", "clean_text"])
            .to_pylist()
            if row["clean_text"] and row["clean_text"].strip()
        ]
        if not rows:
            return 0
        embeddings: list[list[float]] = []
        for i in range(0, len(rows), batch_size):
            embeddings.extend(embedder.embed([r["clean_text"] for r in rows[i : i + batch_size]]))
        out_rows = [
            {
                "text_id": row["text_id"],
                "fragment_id": row["fragment_id"],
                "talker_id": row["talker_id"],
                "subject_id": row["subject_id"],
                "model": model,
                "provider": provider,
                "dim": len(vector),
                "embedding": vector,
                "model_slug": slug,
                "jurisdiction": day.jurisdiction,
                "date": day.date,
                "house": day.house,
            }
            for row, vector in zip(rows, embeddings, strict=True)
        ]
        # each house-day writes its own partition dir, so concurrent
        # writes from the worker pool never touch the same files
        ds.write_dataset(
            pa.Table.from_pylist(out_rows, schema=SCHEMA),
            base_dir=str(out_dir),
            format="parquet",
            partitioning=_PARTITIONING,
            existing_data_behavior="delete_matching",
            basename_template="part-{i}.parquet",
        )
        pairs = sorted((row["text_id"], row["clean_text"]) for row in rows)
        _write_signature(partition_for(day), _signature_of(pairs), len(pairs))
        log(f"  {day.date} {day.house}: {len(out_rows)} vectors")
        return len(out_rows)

    days = vectors = skipped = 0
    pending: list[HouseDay] = []
    for day in iter_house_days(texts_dir, jurisdiction, start, end):
        partition = partition_for(day)
        if partition.is_dir() and not force and _is_fresh(day.path, partition):
            skipped += 1
        else:
            pending.append(day)

    if workers <= 1:
        for day in pending:
            written = embed_day(day)
            days += 1 if written else 0
            vectors += written
    else:
        # embed calls are HTTP-bound: a thread pool keeps `workers` house-days
        # in flight so the provider is never idle between parquet reads/writes
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(embed_day, day) for day in pending]
            for future in as_completed(futures):
                written = future.result()
                days += 1 if written else 0
                vectors += written
    return {"days": days, "skipped": skipped, "vectors": vectors}

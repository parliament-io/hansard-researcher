"""Paragraph embeddings: silver ``texts`` -> ``data/enriched/embeddings``.

Optional Tier 3 — runs only when the user configures a provider (see
:mod:`parlhansard.enrich.providers`). Embedding rows carry **no Hansard
prose**: vectors + join keys only; display text is joined back from the
local silver tables (licensing stance, LICENSES-DATA.md — vectors are
non-expressive derived data).

Layout mirrors silver: hive-partitioned by (model_slug, jurisdiction, date,
house) and written with ``delete_matching``, so re-embedding a house-day for
one model atomically replaces exactly that slice and never touches another
model's vectors. ``text_id`` is deterministic from silver, so
(``model``, ``text_id``) is a stable dedup key — switching providers or
re-running stays coherent.
"""

from __future__ import annotations

import datetime as dt
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
    force: bool = False,
    log=print,
) -> dict[str, int]:
    """Embed every non-empty silver paragraph; incremental per (model, house-day)."""
    texts_dir = data_dir / "silver" / "texts"
    out_dir = data_dir / "enriched" / "embeddings"
    slug = model_slug(model)

    days = vectors = skipped = 0
    for day in iter_house_days(texts_dir, jurisdiction, start, end):
        partition = (
            out_dir / f"model_slug={slug}" / day.path.parent.parent.name
            / day.path.parent.name / day.path.name
        )
        if partition.exists() and not force:
            skipped += 1
            continue
        rows = [
            row
            for row in ds.dataset(day.path, format="parquet")
            .to_table(columns=["text_id", "fragment_id", "talker_id", "subject_id", "clean_text"])
            .to_pylist()
            if row["clean_text"] and row["clean_text"].strip()
        ]
        if not rows:
            continue
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
        ds.write_dataset(
            pa.Table.from_pylist(out_rows, schema=SCHEMA),
            base_dir=str(out_dir),
            format="parquet",
            partitioning=_PARTITIONING,
            existing_data_behavior="delete_matching",
            basename_template="part-{i}.parquet",
        )
        days += 1
        vectors += len(out_rows)
        log(f"  {day.date} {day.house}: {len(out_rows)} vectors")
    return {"days": days, "skipped": skipped, "vectors": vectors}

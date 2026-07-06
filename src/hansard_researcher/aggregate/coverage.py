"""Pipeline coverage — which house-days exist at each stage.

Two consumers:

- the ``pipeline_coverage`` gold cube (built with the rest in
  :mod:`hansard_researcher.aggregate.cubes`): silver house-day grain with harvest
  info attached at day grain — raw stores house *codes* (``lh``, ``senate``)
  while silver stores house *names*, so the two sides only align per date.
  The full join means a harvested-but-never-normalized day still surfaces.
- ``hansard-researcher status`` via :func:`collect_status`: a live report from the
  raw directory layout + parquet footers (no Hansard prose is read).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from urllib.parse import unquote

import duckdb
import pyarrow as pa

RAW_DAYS_SCHEMA = pa.schema(
    [
        ("jurisdiction", pa.string()),
        ("date", pa.date32()),
        ("house_code", pa.string()),
        ("documents", pa.int64()),
        ("harvested_at", pa.string()),
    ]
)

# join-key projection of data/enriched/embeddings (never the vectors)
EMBEDDING_INDEX_SCHEMA = pa.schema(
    [
        ("model_slug", pa.string()),
        ("jurisdiction", pa.string()),
        ("date", pa.date32()),
        ("house", pa.string()),
    ]
)


def scan_raw(raw_dir: Path | None) -> pa.Table:
    """Walk ``raw/{jurisdiction}/{date}/{house_code}/meta.json``.

    A house-day directory without meta.json is deliberately excluded: the
    harvester only writes meta once a day yielded documents, so its absence
    means "not (yet) harvested — will re-probe" (see ``cmd_harvest``).
    """
    rows: list[dict] = []
    raw_dir = Path(raw_dir) if raw_dir else None
    if raw_dir and raw_dir.is_dir():
        for jur_dir in sorted(p for p in raw_dir.iterdir() if p.is_dir()):
            for date_dir in sorted(p for p in jur_dir.iterdir() if p.is_dir()):
                try:
                    date = dt.date.fromisoformat(date_dir.name)
                except ValueError:
                    continue
                for house_dir in sorted(p for p in date_dir.iterdir() if p.is_dir()):
                    meta_path = house_dir / "meta.json"
                    if not meta_path.is_file():
                        continue
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        meta = {}
                    rows.append(
                        {
                            "jurisdiction": jur_dir.name,
                            "date": date,
                            "house_code": house_dir.name,
                            "documents": meta.get("documents"),
                            "harvested_at": meta.get("harvested_at"),
                        }
                    )
    return pa.Table.from_pylist(rows, schema=RAW_DAYS_SCHEMA)


def _iter_hive(table_dir: Path):
    """Yield (jurisdiction, date, house) from silver-style hive directory
    names — no file opens, so a full-archive walk is near-instant."""
    if not table_dir.is_dir():
        return
    for jur_dir in sorted(table_dir.glob("jurisdiction=*")):
        jur = jur_dir.name.split("=", 1)[1]
        for date_dir in sorted(jur_dir.glob("date=*")):
            date = date_dir.name.split("=", 1)[1]
            for house_dir in sorted(date_dir.glob("house=*")):
                yield jur, date, unquote(house_dir.name.split("=", 1)[1])


def collect_status(data_dir: Path, *, counts: bool = False) -> dict:
    """Live pipeline status: raw -> silver -> enriched -> gold -> reference.

    The default reads directory layouts only (a partition directory is the
    stage's own done-marker). ``counts=True`` additionally reads parquet
    footers for row counts — much slower on a full archive.
    """
    data_dir = Path(data_dir)
    jurisdictions: dict[str, dict] = {}

    def jur(code: str) -> dict:
        return jurisdictions.setdefault(
            code,
            {
                "raw_days": 0,
                "raw_documents": 0,
                "raw_first_date": None,
                "raw_last_date": None,
                "silver_days": 0,
                "silver_house_days": 0,
                "first_date": None,
                "last_date": None,
                "pending_normalize_days": 0,
            },
        )

    raw_dates: dict[str, set[str]] = {}
    for row in scan_raw(data_dir / "raw").to_pylist():
        code = row["jurisdiction"]
        raw_dates.setdefault(code, set()).add(row["date"].isoformat())
        jur(code)["raw_documents"] += row["documents"] or 0
    for code, dates in raw_dates.items():
        j = jur(code)
        j["raw_days"] = len(dates)
        j["raw_first_date"] = min(dates)
        j["raw_last_date"] = max(dates)

    silver_dates: dict[str, set[str]] = {}
    total_house_days = 0
    for code, date, _house in _iter_hive(data_dir / "silver" / "fragments"):
        j = jur(code)
        j["silver_house_days"] += 1
        total_house_days += 1
        silver_dates.setdefault(code, set()).add(date)
        j["first_date"] = min(j["first_date"] or date, date)
        j["last_date"] = max(j["last_date"] or date, date)
    for code, dates in silver_dates.items():
        jur(code)["silver_days"] = len(dates)
    for code, dates in raw_dates.items():
        jur(code)["pending_normalize_days"] = len(dates - silver_dates.get(code, set()))

    embeddings: dict[str, dict] = {}
    themes: dict[str, dict] = {}
    for kind, out in (("embeddings", embeddings), ("themes", themes)):
        base = data_dir / "enriched" / kind
        if base.is_dir():
            for model_dir in sorted(base.glob("model_slug=*")):
                slug = unquote(model_dir.name.split("=", 1)[1])
                out[slug] = {"house_days": sum(1 for _ in _iter_hive(model_dir))}

    enrichment: dict = {
        "silver_house_days": total_house_days,
        "embeddings": embeddings,
        "themes": themes,
    }

    if counts:
        con = duckdb.connect()

        def _agg(table_dir: Path, sql: str) -> list[tuple]:
            if not (table_dir.is_dir() and any(table_dir.rglob("*.parquet"))):
                return []
            src = (
                f"read_parquet('{table_dir.as_posix()}/**/*.parquet',"
                f" hive_partitioning=1)"
            )
            return con.execute(sql.format(src=src)).fetchall()

        silver = data_dir / "silver"
        for table, key in (
            ("subjects", "subjects"),
            ("talkers", "talker_turns"),
            ("texts", "texts"),
            ("divisions", "divisions"),
        ):
            by_jur = dict(
                _agg(silver / table, "select jurisdiction, count(*) from {src} group by 1")
            )
            for code in jurisdictions:
                jur(code)[key] = by_jur.get(code, 0)
        enrichment["silver_subjects"] = sum(
            j.get("subjects", 0) for j in jurisdictions.values()
        )
        for slug, vectors in _agg(
            data_dir / "enriched" / "embeddings",
            "select model_slug, count(*) from {src} group by 1",
        ):
            embeddings.setdefault(slug, {})["vectors"] = vectors
        for slug, labels, subjects in _agg(
            data_dir / "enriched" / "themes",
            "select model_slug, count(*), count(distinct subject_id)"
            " from {src} group by 1",
        ):
            themes.setdefault(slug, {}).update(labels=labels, subjects=subjects)

    gold_files = sorted((data_dir / "gold").glob("*.parquet"))
    gold = {
        "cubes": len(gold_files),
        "built_at": (
            dt.datetime.fromtimestamp(
                max(f.stat().st_mtime for f in gold_files)
            ).isoformat(timespec="seconds")
            if gold_files
            else None
        ),
    }

    members_dir = data_dir / "reference" / "members"
    members: dict[str, int] = {}
    if members_dir.is_dir() and any(members_dir.rglob("*.parquet")):
        con = duckdb.connect()
        members = dict(
            con.execute(
                f"select jurisdiction, count(*) from read_parquet("
                f"'{members_dir.as_posix()}/**/*.parquet', hive_partitioning=1)"
                f" group by 1"
            ).fetchall()
        )

    return {
        "data_dir": str(data_dir),
        "jurisdictions": dict(sorted(jurisdictions.items())),
        "enrichment": enrichment,
        "gold": gold,
        "reference": {"members": members},
    }

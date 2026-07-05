"""normalize_day: one raw house-day -> its silver partition.

Provenance (``source_url``/``retrieved_at``) comes from the day's
``meta.json`` — written at harvest time — so local re-normalizes carry the
official fetch URL through to silver fragments.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import pyarrow.dataset as ds

from hansard_researcher.normalize.runner import _day_provenance, normalize_day

FIXTURES = Path(__file__).parent / "fixtures"

DAY_URL = "https://www.parliament.wa.gov.au/hansard/api/hansard/lh/2026-03-04/toc"


def _stage_day(tmp_path: Path, meta: dict | None) -> list[str]:
    day = tmp_path / "raw" / "wa" / "2026-03-04" / "lh"
    day.mkdir(parents=True)
    files = []
    for i in (1, 2):
        target = day / f"subject_{i:04d}.xml"
        shutil.copy(FIXTURES / f"extract_{i:04d}.xml", target)
        files.append(str(target))
    if meta is not None:
        (day / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return files


def _fragment_rows(out_dir: Path) -> list[dict]:
    dataset = ds.dataset(out_dir / "fragments", format="parquet", partitioning="hive")
    return dataset.to_table().to_pylist()


def test_normalize_day_carries_meta_provenance(tmp_path):
    files = _stage_day(
        tmp_path,
        {
            "event": {"date": "2026-03-04", "house": "lh", "url": DAY_URL},
            "documents": 2,
            "harvested_at": "2026-07-02T00:00:00+00:00",
        },
    )
    counts = normalize_day("wa", "2026-03-04", "lh", files, str(tmp_path / "silver"))
    assert counts["fragments"] == 1
    (row,) = _fragment_rows(tmp_path / "silver")
    assert row["source_url"] == DAY_URL
    assert row["retrieved_at"] == dt.datetime(2026, 7, 2, tzinfo=dt.UTC)


def test_normalize_day_without_meta_leaves_provenance_null(tmp_path):
    files = _stage_day(tmp_path, meta=None)
    normalize_day("wa", "2026-03-04", "lh", files, str(tmp_path / "silver"))
    (row,) = _fragment_rows(tmp_path / "silver")
    assert row["source_url"] is None
    assert row["retrieved_at"] is None


def test_day_provenance_tolerates_bad_meta(tmp_path):
    (tmp_path / "meta.json").write_text("{not json", encoding="utf-8")
    assert _day_provenance(tmp_path) == (None, None)

    (tmp_path / "meta.json").write_text(
        json.dumps({"event": {"date": "2026-03-04"}, "harvested_at": "yesterday"}),
        encoding="utf-8",
    )
    assert _day_provenance(tmp_path) == (None, None)

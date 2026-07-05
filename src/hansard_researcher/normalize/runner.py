"""Parallel normalization runner.

Each (jurisdiction, date, house) is an independent unit of work: it reads its
own raw files, parses/stitches, and writes only its own silver partition
(``jurisdiction=/date=/house=``), so house-days parallelize across a process
pool with no shared state. The worker lives at module level so it is
importable under Windows ``spawn``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _day_provenance(day_dir: Path) -> tuple[str | None, dt.datetime | None]:
    """(source url, harvested-at) from the day's ``meta.json``, if present.

    Both feed volatile fragment fields (never the content hash): the source
    URL lets external consumers validate provenance against the official API.
    """
    meta_path = day_dir / "meta.json"
    if not meta_path.is_file():
        return None, None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    url = (meta.get("event") or {}).get("url")
    retrieved_at = None
    if harvested := meta.get("harvested_at"):
        try:
            retrieved_at = dt.datetime.fromisoformat(harvested)
        except ValueError:
            pass
    return url, retrieved_at


def normalize_day(
    jurisdiction: str, date: str, house: str, files: list[str], out_dir: str
) -> dict[str, int]:
    """Parse one house-day's raw XML and write its silver partition."""
    from hansard_researcher.harvest import get_adapter
    from hansard_researcher.harvest.base import RawDocument, SittingEvent
    from hansard_researcher.model.canonical import Jurisdiction
    from hansard_researcher.normalize.silver import write_silver

    event = SittingEvent(
        jurisdiction=Jurisdiction(jurisdiction),
        date=dt.date.fromisoformat(date),
        house=house,
    )
    url, retrieved_at = _day_provenance(Path(files[0]).parent)
    docs = [
        RawDocument(
            event=event,
            content=Path(f).read_bytes(),
            media_type="text/xml",
            name=Path(f).name,
            url=url,
            retrieved_at=retrieved_at,
        )
        for f in files
    ]
    adapter = get_adapter(event.jurisdiction)
    fragments = list(adapter.normalize(docs))
    # day-level passes, after stitching: anchor wall-clock turn times to the
    # sitting's zone (rolling past-midnight readings onto the next date) and
    # type untyped follow-on turns from the same member's lead turn
    from hansard_researcher.normalize.clock import apply_running_clock
    from hansard_researcher.normalize.kinds import apply_kind_inference

    for fragment in fragments:
        apply_running_clock(fragment, jurisdiction)
        apply_kind_inference(fragment)
    return write_silver(fragments, Path(out_dir))

"""Parallel normalization runner.

Each (jurisdiction, date, house) is an independent unit of work: it reads its
own raw files, parses/stitches, and writes only its own silver partition
(``jurisdiction=/date=/house=``), so house-days parallelize across a process
pool with no shared state. The worker lives at module level so it is
importable under Windows ``spawn``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path


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
    docs = [
        RawDocument(
            event=event,
            content=Path(f).read_bytes(),
            media_type="text/xml",
            name=Path(f).name,
            url=None,
        )
        for f in files
    ]
    adapter = get_adapter(event.jurisdiction)
    fragments = list(adapter.normalize(docs))
    # day-level pass, after stitching: anchors wall-clock turn times to the
    # sitting's zone and rolls past-midnight readings onto the next date
    from hansard_researcher.normalize.clock import apply_running_clock

    for fragment in fragments:
        apply_running_clock(fragment, jurisdiction)
    return write_silver(fragments, Path(out_dir))

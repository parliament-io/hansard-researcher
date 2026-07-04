"""Day-level running clock — timezone anchoring and midnight rollover.

Sittings run past midnight, and three of the four live sources mark most turn
times as wall-clock readings (``Time-H``/``HPS-Time`` spans, HH:MM only) with
the calendar date implied by the sitting header. This pass runs once per
house-day, after the per-subject extracts are stitched into the daily
fragment (so the clock survives fragment boundaries — a fragment whose only
reading is 02:10 needs the 20:13 from an earlier extract to know the sitting
crossed midnight); AU's whole-day document goes through the identical walk.

Rules (agreed 2026-07-04, see backlog.md):

- An explicit timestamp from the XML is the truth — never adjusted — and
  re-anchors the running clock. Parsers mark clock-derived times with
  ``extensions["time_source"] = "clock"``; anything unmarked is a document
  value.
- Naive datetimes are wall-clock local time: they get the document's UTC
  offset when the header carries one, else the jurisdiction's IANA zone
  (DST-correct for the sitting date), recorded in
  ``fragment.extensions["clock_tz"]``.
- A clock-derived reading earlier than the last time seen means the sitting
  crossed midnight: the date advances one day (flagged
  ``extensions["clock_rolled"]``).

Silver stores UTC (``pa.timestamp("us", tz="UTC")``); pyarrow treats naive
datetimes as already-UTC, so localizing here is also what makes the stored
instants correct.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from zoneinfo import ZoneInfo

from hansard_researcher.model.canonical import Fragment, Talker

#: a backwards clock reading only counts as a midnight wrap when the
#: regression is bigger than this; smaller regressions are out-of-order
#: transcript blocks and stay on their day
_ROLL_THRESHOLD = dt.timedelta(hours=12)

#: jurisdiction -> IANA zone, the fallback when no document offset exists.
#: Federal parliament sits in Canberra (Sydney offsets).
ZONES = {
    "wa": "Australia/Perth",
    "sa": "Australia/Adelaide",
    "nsw": "Australia/Sydney",
    "au": "Australia/Sydney",
    "nz": "Pacific/Auckland",
    "scot": "Europe/London",
}


def _iter_talkers(fragment: Fragment) -> Iterator[Talker]:
    for proc in fragment.proceedings:
        yield from proc.talkers
        for subj in proc.subjects:
            yield from subj.talkers
            for div in subj.divisions:
                yield from div.talkers
            for sub in subj.subproceedings:
                yield from sub.talkers
                for div in sub.divisions:
                    yield from div.talkers
                for clause in sub.clauses:
                    yield from clause.talkers
                    for div in clause.divisions:
                        yield from div.talkers
            for clause in subj.clauses:
                yield from clause.talkers
                for div in clause.divisions:
                    yield from div.talkers


def _document_tz(fragment: Fragment) -> dt.tzinfo | None:
    """The document-stated UTC offset, when a sitting-local time carries one.

    Only sitting-local times qualify: ``date_modified`` is deliberately
    excluded — it is a publishing-system timestamp (SA emits it in UTC) and
    its offset says nothing about where the chamber sits.
    """
    for value in (fragment.start_time, fragment.end_time):
        if value is not None and value.tzinfo is not None:
            return value.tzinfo
    for talker in _iter_talkers(fragment):
        if talker.start_time is not None and talker.start_time.tzinfo is not None:
            return talker.start_time.tzinfo
    return None


def apply_running_clock(fragment: Fragment, jurisdiction: str) -> None:
    """Anchor every naive talker time to the sitting's zone, rolling midnight.

    Mutates the fragment in place. Aware datetimes pass through verbatim.
    """
    tz = _document_tz(fragment)
    if tz is not None:
        fragment.extensions["clock_tz"] = "document"
    else:
        zone = ZONES.get(jurisdiction, "Australia/Sydney")
        tz = ZoneInfo(zone)
        fragment.extensions["clock_tz"] = f"zone:{zone}"

    last: dt.datetime | None = None
    for talker in sorted(_iter_talkers(fragment), key=lambda t: t.document_order):
        start = talker.start_time
        if start is None:
            continue
        if start.tzinfo is not None:
            last = start  # document truth: keep verbatim, re-anchor the clock
            continue
        candidate = start.replace(tzinfo=tz)
        if talker.extensions.get("time_source") == "clock" and last is not None:
            # a LARGE regression (23:50 -> 00:10 reads as ~-23:40) means the
            # sitting crossed midnight; a small one (14:00 -> 09:00) is an
            # out-of-order transcript block (written answers, corrections)
            # and must NOT roll — unguarded rolling cascaded real NSW days
            # up to +3 days. Bounded: at most two genuine wraps.
            for _ in range(2):
                if candidate >= last or last - candidate <= _ROLL_THRESHOLD:
                    break
                candidate += dt.timedelta(days=1)
                talker.extensions["clock_rolled"] = "1"
        talker.start_time = candidate
        last = candidate

    # header times: localize, and a sitting that ends before it starts ran
    # past midnight
    if fragment.start_time is not None and fragment.start_time.tzinfo is None:
        fragment.start_time = fragment.start_time.replace(tzinfo=tz)
    if fragment.end_time is not None and fragment.end_time.tzinfo is None:
        fragment.end_time = fragment.end_time.replace(tzinfo=tz)
    if (
        fragment.start_time is not None
        and fragment.end_time is not None
        and fragment.end_time < fragment.start_time
    ):
        fragment.end_time += dt.timedelta(days=1)
        fragment.extensions["clock_rolled"] = "1"

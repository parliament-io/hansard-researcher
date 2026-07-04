"""Day-level running clock: zone anchoring and midnight rollover."""

import datetime as dt

from hansard_researcher.model.canonical import (
    Fragment,
    Jurisdiction,
    Proceeding,
    Subject,
    Talker,
)
from hansard_researcher.normalize.clock import apply_running_clock

AEDT = dt.timezone(dt.timedelta(hours=11))


def _clock_talker(order: int, hour: int, minute: int, date=dt.date(2023, 11, 30)) -> Talker:
    t = Talker(
        document_order=order,
        start_time=dt.datetime.combine(date, dt.time(hour, minute)),
    )
    t.extensions["time_source"] = "clock"
    return t


def _day(talkers, jurisdiction=Jurisdiction.NSW, **fragment_kwargs) -> Fragment:
    subject = Subject(document_order=0, talkers=talkers)
    return Fragment(
        fragment_id="test-day",
        jurisdiction=jurisdiction,
        date=dt.date(2023, 11, 30),
        house="Legislative Assembly",
        proceedings=[Proceeding(document_order=0, subjects=[subject])],
        **fragment_kwargs,
    )


def test_clock_readings_roll_past_midnight():
    """20:13 then 02:10 => the second reading lands on the next day."""
    day = _day([_clock_talker(1, 20, 13), _clock_talker(2, 2, 10)])
    apply_running_clock(day, "nsw")
    first, second = day.proceedings[0].subjects[0].talkers
    assert first.start_time == dt.datetime(2023, 11, 30, 20, 13, tzinfo=AEDT)
    assert second.start_time == dt.datetime(2023, 12, 1, 2, 10, tzinfo=AEDT)
    assert second.extensions["clock_rolled"] == "1"
    assert "clock_rolled" not in first.extensions


def test_document_offset_wins_over_zone():
    """A header time with an offset supplies the tz for clock readings."""
    day = _day(
        [_clock_talker(1, 10, 0)],
        start_time=dt.datetime(2023, 11, 30, 9, 30, tzinfo=AEDT),
    )
    apply_running_clock(day, "nsw")
    assert day.extensions["clock_tz"] == "document"
    (talker,) = day.proceedings[0].subjects[0].talkers
    assert talker.start_time.utcoffset() == dt.timedelta(hours=11)


def test_zone_fallback_is_dst_correct():
    """No document offset: the jurisdiction's IANA zone supplies it —
    Adelaide is +10:30 during DST, +9:30 outside it."""
    day = _day([_clock_talker(1, 14, 0)], jurisdiction=Jurisdiction.SA)
    apply_running_clock(day, "sa")
    assert day.extensions["clock_tz"] == "zone:Australia/Adelaide"
    (talker,) = day.proceedings[0].subjects[0].talkers
    assert talker.start_time.utcoffset() == dt.timedelta(hours=10, minutes=30)


def test_explicit_document_times_are_truth():
    """Aware timestamps pass through verbatim and re-anchor the clock:
    a 23:35 document time followed by a 00:05 clock reading rolls over."""
    explicit = Talker(
        document_order=1,
        start_time=dt.datetime(2023, 11, 30, 23, 35, tzinfo=AEDT),
    )
    day = _day([explicit, _clock_talker(2, 0, 5)])
    apply_running_clock(day, "nsw")
    first, second = day.proceedings[0].subjects[0].talkers
    assert first.start_time == dt.datetime(2023, 11, 30, 23, 35, tzinfo=AEDT)
    assert second.start_time == dt.datetime(2023, 12, 1, 0, 5, tzinfo=AEDT)


def test_document_naive_times_localize_without_rollover():
    """A naive document time (no time_source mark) is truth for its date:
    localized, never shifted, and it re-anchors the running clock."""
    doc = Talker(
        document_order=1,
        start_time=dt.datetime(2023, 11, 30, 9, 0),
    )
    day = _day([doc])
    apply_running_clock(day, "nsw")
    (talker,) = day.proceedings[0].subjects[0].talkers
    assert talker.start_time == dt.datetime(2023, 11, 30, 9, 0, tzinfo=AEDT)
    assert "clock_rolled" not in talker.extensions


def test_sitting_end_before_start_rolls():
    """Header end 02:10 with start 10:00 => the sitting ran past midnight."""
    day = _day(
        [],
        start_time=dt.datetime(2023, 11, 30, 10, 0),
        end_time=dt.datetime(2023, 11, 30, 2, 10),
    )
    apply_running_clock(day, "nsw")
    assert day.end_time - day.start_time == dt.timedelta(hours=16, minutes=10)
    assert day.extensions["clock_rolled"] == "1"


def test_system_timestamp_never_defines_the_zone():
    """SA's dateModified is a UTC publishing-system timestamp — it must not
    hijack the sitting's offset (regression: real SA 2008-10-29 talkers were
    stamped +00:00)."""
    day = _day(
        [Talker(document_order=1, start_time=dt.datetime(2023, 11, 30, 11, 1))],
        jurisdiction=Jurisdiction.SA,
        date_modified=dt.datetime(2023, 12, 15, 0, 37, tzinfo=dt.timezone.utc),
    )
    apply_running_clock(day, "sa")
    assert day.extensions["clock_tz"] == "zone:Australia/Adelaide"
    (talker,) = day.proceedings[0].subjects[0].talkers
    assert talker.start_time.utcoffset() == dt.timedelta(hours=10, minutes=30)


def test_missing_times_stay_missing():
    """The pass backfills formatting, it never invents timestamps."""
    day = _day([Talker(document_order=1)])
    apply_running_clock(day, "nsw")
    (talker,) = day.proceedings[0].subjects[0].talkers
    assert talker.start_time is None

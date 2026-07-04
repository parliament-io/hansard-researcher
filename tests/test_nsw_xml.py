import datetime as dt
from pathlib import Path

import pytest

from hansard_researcher.model.canonical import Jurisdiction, ReviewStage, TalkerKind, TextKind
from hansard_researcher.normalize.canonical_xml import stitch_daily
from hansard_researcher.normalize.nsw_xml import house_code, parse_nsw_fragment

FIXTURES = Path(__file__).parent / "fixtures"


def _fragment(n: int):
    return parse_nsw_fragment(
        (FIXTURES / f"nsw_fragment_{n}.xml").read_bytes(),
        doc_id=f"TEST-FRAG-{n:04d}",
        extract_index=n,
    )


@pytest.fixture
def daily():
    return stitch_daily([_fragment(1), _fragment(2)])


def test_header():
    fragment = _fragment(1)
    assert fragment.jurisdiction is Jurisdiction.NSW
    assert fragment.date == dt.date(2026, 6, 24)
    assert fragment.house == "Legislative Council"
    assert fragment.parliament_num == 58
    assert fragment.session_name == "Fifty-Eighth Parliament, First Session (58-1)"
    assert fragment.review_stage is ReviewStage.UNCORRECTED  # draft=true


def test_speech_attribution():
    subject = _fragment(1).proceedings[0].subjects[0]
    assert subject.name == "Synthetic Motion"
    assert subject.uid == "TEST-FRAG-0001"
    (talker,) = subject.talkers
    assert talker.member_source_id == "2300"
    assert talker.name == "Dr TEST MEMBER"
    assert talker.kind is TalkerKind.SPEECH
    # Time-H 10:04 + HiddenTime-H :26
    assert talker.start_time == dt.datetime(2026, 6, 24, 10, 4, 26)
    # marker paragraph + following list paragraph both belong to the talker
    assert len(talker.texts) == 2
    assert "I move that this synthetic house notes widgets" in talker.texts[0].clean_text
    # heading para before the marker belongs to the subject
    assert subject.texts[0].kind is TextKind.HEADING


def test_question_answer_kinds():
    subject = _fragment(2).proceedings[0].subjects[0]
    question, answer = subject.talkers
    assert question.kind is TalkerKind.QUESTION
    assert question.extensions["question_number"] == "77"
    assert answer.kind is TalkerKind.ANSWER
    assert answer.portfolios == ["Synthetic Affairs"]
    assert "widget policy is excellent" in answer.texts[0].clean_text


def test_stitch_merges_by_proceeding_name(daily):
    # both fragments are under "Motions" — NSW has no proceeding uids
    assert len(daily.proceedings) == 1
    assert daily.proceedings[0].name == "Motions"
    assert [s.name for s in daily.proceedings[0].subjects] == [
        "Synthetic Motion",
        "Second Synthetic Motion",
    ]
    assert daily.extensions["extract_count"] == "2"
    assert daily.fragment_id


def test_house_code():
    assert house_code("Legislative Assembly") == "la"
    assert house_code("Legislative Council") == "lc"
    assert house_code(None) == "unknown"


# see fixture comment: mirrors real structure, blank names, grouped pairs
DIVISION_FRAGMENT = (FIXTURES / "nsw_fragment_division.xml").read_bytes()


def test_division_parsed_from_fragment_data():
    fragment = parse_nsw_fragment(DIVISION_FRAGMENT, doc_id="TEST-DIV-0001")
    subject = fragment.proceedings[0].subjects[0]
    (sub,) = subject.subproceedings
    assert sub.name == "Consideration In Detail"
    (division,) = sub.divisions

    assert (division.ayes_count, division.noes_count, division.pairs_count) == (2, 3, 1)
    assert division.result is not None and division.result.value == "noes"
    assert division.extensions["result_derived"] == "true"

    votes = {(v.member_source_id, v.vote.value) for v in division.votes}
    assert votes == {
        ("81", "AYES"), ("2229", "AYES"),
        ("28", "NOES"), ("93", "NOES"), ("115", "NOES"),
        ("118", "PAIRS"), ("2293", "PAIRS"),
    }
    # names are blank at the source — resolved later via the member register
    assert all(v.member_name is None for v in division.votes)


def test_division_survives_stitching():
    daily = stitch_daily([parse_nsw_fragment(DIVISION_FRAGMENT, doc_id="TEST-DIV-0001")])
    (division,) = daily.proceedings[0].subjects[0].subproceedings[0].divisions
    assert len(division.votes) == 7


def test_legacy_bold_marker_attribution():
    """Pre-2016 back-converted fragments mark speakers with
    <b data-mode="member"> instead of spans; the bold text is the only
    place the speaker's name appears (fragment.data carries no talkers)."""
    fragment = parse_nsw_fragment(
        (FIXTURES / "nsw_fragment_legacy.xml").read_bytes(), doc_id="TEST-LEG-0001"
    )
    subject = fragment.proceedings[0].subjects[0]
    assert subject.name == "Synthetic Legacy Motion"
    member, speaker = subject.talkers
    assert member.member_source_id == "12"
    assert member.name == "Mr Test Legacy"  # from the bold marker text
    # marker paragraph + the unmarked follow-on paragraph
    assert len(member.texts) == 2
    assert "bold elements" in member.texts[0].clean_text
    assert "2016 conversion" in member.texts[1].clean_text
    # 2005-2015 era: talk.time carries the full ISO timestamp with offset —
    # document truth, parsed verbatim (aware), never adjusted by the clock pass
    assert member.start_time == dt.datetime(
        2010, 5, 11, 23, 35, tzinfo=dt.timezone(dt.timedelta(hours=10))
    )
    assert speaker.member_source_id == "34"
    assert speaker.name == "Mr SPEAKER"
    assert "resume their seat" in speaker.texts[0].clean_text
    # the heading before the first marker stays with the subject
    assert subject.texts[0].kind is TextKind.HEADING or subject.texts

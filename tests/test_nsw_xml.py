import datetime as dt
from pathlib import Path

import pytest

from parlhansard.model.canonical import Jurisdiction, ReviewStage, TalkerKind, TextKind
from parlhansard.normalize.canonical_xml import stitch_daily
from parlhansard.normalize.nsw_xml import house_code, parse_nsw_fragment

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

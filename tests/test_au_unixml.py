import datetime as dt
from pathlib import Path

import pytest

from parlhansard.model.canonical import (
    Jurisdiction,
    ReviewStage,
    TalkerKind,
    VoteValue,
)
from parlhansard.normalize.au_unixml import parse_daily

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def daily():
    return parse_daily((FIXTURES / "au_daily.xml").read_bytes())


def test_header(daily):
    assert daily.jurisdiction is Jurisdiction.AU
    assert daily.date == dt.date(2026, 3, 11)
    assert daily.house == "Senate"
    assert daily.parliament_num == 3
    assert daily.session_num == 1
    assert daily.review_stage is ReviewStage.UNCORRECTED  # proof=1
    assert daily.schema_version == "2.2"


def test_structure_mapping(daily):
    assert [p.name for p in daily.proceedings] == ["QUESTIONS WITHOUT NOTICE", "BILLS"]
    qwn = daily.proceedings[0]
    assert qwn.extensions["debate_type"] == "QUESTIONS WITHOUT NOTICE"
    assert qwn.subjects[0].name == "Synthetic Widgets"
    bills = daily.proceedings[1]
    assert bills.subjects[0].name == "Synthetic Standards Bill 2026"
    assert bills.subjects[0].subproceedings[0].name == "Second Reading"
    assert len(daily.texts) == 1  # business.start


def test_talkers_with_nested_interjection_and_continue(daily):
    talkers = daily.proceedings[0].subjects[0].talkers
    kinds = [(t.kind, t.name, t.continued) for t in talkers]
    assert kinds == [
        (TalkerKind.QUESTION, "Tester, Sen Alice", False),
        (TalkerKind.ANSWER, "Sample, Sen Bob", False),
        (TalkerKind.INTERJECTION, "Heckler, Sen Carol", False),
        (TalkerKind.ANSWER, "Sample, Sen Bob", True),  # <continue> flattened
    ]
    answer = talkers[1]
    assert answer.member_source_id == "900002"
    assert answer.party == "GP"
    assert answer.extensions["party_abbreviation"] == "GP"
    assert answer.extensions["in_gov"] == "1"
    orders = [t.document_order for t in talkers]
    assert orders == sorted(orders)


def test_first_speech_flag(daily):
    speech = daily.proceedings[1].subjects[0].subproceedings[0].talkers[0]
    assert speech.first_speech is True
    assert speech.kind is TalkerKind.SPEECH


def test_time_from_body_span(daily):
    question = daily.proceedings[0].subjects[0].talkers[0]
    assert question.texts[0].time_anchor == dt.datetime(2026, 3, 11, 14, 1)


def test_page_forward_fill(daily):
    interjection = daily.proceedings[0].subjects[0].talkers[2]
    assert interjection.texts[0].page_no == "12"


def test_division(daily):
    division = daily.proceedings[1].subjects[0].divisions[0]
    assert division.ayes_count == 2
    assert division.noes_count == 1
    assert division.result is not None and division.result.value == "ayes"
    assert division.extensions["result_derived"] == "true"
    assert division.extensions["result_text"] == "Question agreed to."
    votes = {(v.member_name, v.vote, v.teller) for v in division.votes}
    assert votes == {
        ("Tester, A.", VoteValue.AYES, True),   # "(Teller)" suffix stripped
        ("Newbie, D.", VoteValue.AYES, False),
        ("Sample, B.", VoteValue.NOES, False),
    }
    # division preamble clock "[14:33]" advanced the running time
    assert division.texts[0].raw_text.startswith("The Senate divided.")


def test_no_unhandled_elements(daily):
    unhandled = {k: v for k, v in daily.extensions.items() if k.startswith("unhandled:")}
    assert unhandled == {}

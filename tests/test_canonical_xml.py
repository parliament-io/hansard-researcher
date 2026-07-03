import datetime as dt
from pathlib import Path

import pytest

from parlhansard.model.canonical import (
    Jurisdiction,
    ReviewStage,
    TalkerKind,
    TalkerRole,
    TextKind,
    VoteValue,
)
from parlhansard.normalize.canonical_xml import parse_extract, stitch_daily

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def extract1():
    return parse_extract(
        (FIXTURES / "extract_0001.xml").read_bytes(),
        jurisdiction=Jurisdiction.WA,
        extract_index=1,
    )


@pytest.fixture
def extract2():
    return parse_extract(
        (FIXTURES / "extract_0002.xml").read_bytes(),
        jurisdiction=Jurisdiction.WA,
        extract_index=2,
    )


@pytest.fixture
def daily(extract1, extract2):
    return stitch_daily([extract1, extract2])


def test_header_metadata(extract1):
    assert extract1.date == dt.date(2026, 3, 4)
    assert extract1.house == "Synthetic Assembly"
    assert extract1.parliament_num == 42
    assert extract1.session_num == 1
    assert extract1.review_stage is ReviewStage.PUBLISHED
    assert extract1.schema_version == "4.0"
    assert extract1.lang == "en-AU"
    assert extract1.start_page == "100"
    assert extract1.start_time == dt.datetime.fromisoformat("2026-03-04T09:00:00+08:00")


def test_talker_parsing(extract1):
    subject = extract1.proceedings[0].subjects[0]
    question, answer = subject.talkers
    assert question.kind is TalkerKind.QUESTION
    assert question.role is TalkerRole.MEMBER
    assert question.member_source_id == "m-100"
    assert question.member_reference_id == "ref-100"
    assert question.party == "Example Party"
    assert question.extensions["party_abbreviation"] == "EX"
    assert question.electorate == "Testville"
    assert answer.kind is TalkerKind.ANSWER
    assert answer.portfolios == ["Widgets"]


def test_text_kinds_and_cleaning(extract1):
    subject = extract1.proceedings[0].subjects[0]
    heading = subject.texts[0]
    assert heading.kind is TextKind.HEADING
    answer = subject.talkers[1]
    para = answer.texts[0]
    assert para.kind is TextKind.PARAGRAPH
    # double-escaped entity &amp;#x9; resolved and whitespace-normalized
    assert "widget policy seriously" in para.clean_text
    assert "\t" not in para.clean_text and "&#x9;" not in para.clean_text
    item = answer.texts[1]
    assert item.kind is TextKind.ITEM


def test_page_and_time_forward_fill(extract1):
    answer = extract1.proceedings[0].subjects[0].talkers[1]
    # <page num="101"/> inside the answer talker forward-fills to its texts
    assert answer.texts[0].page_no == "101"
    question = extract1.proceedings[0].subjects[0].talkers[0]
    # timeStamp anchors the question text
    assert question.texts[0].time_anchor == dt.datetime.fromisoformat("2026-03-04T09:05:00+08:00")
    assert question.texts[0].page_no == "100"  # from startPage


def test_division_parsing(extract2):
    division = extract2.proceedings[0].subjects[0].divisions[0]
    assert division.ayes_count == 2
    assert division.noes_count == 1
    assert division.result is not None and division.result.value == "ayes"
    assert len(division.votes) == 3
    teller = next(v for v in division.votes if v.teller)
    assert teller.member_source_id == "m-100"
    assert teller.vote is VoteValue.AYES


def test_bill_refs_and_first_speech(extract2):
    subject = extract2.proceedings[0].subjects[0]
    assert subject.bill_refs[0].name == "Gadget Standards Bill 2026"
    assert subject.bill_refs[0].source_id == "bill-9"
    speaker = subject.subproceedings[0].talkers[0]
    assert speaker.first_speech is True


def test_stitch_merges_proceedings_by_uid(daily):
    # extracts are the day split BY SUBJECT; proceeding proc-1 spans both
    assert len(daily.proceedings) == 1
    proceeding = daily.proceedings[0]
    assert [s.uid for s in proceeding.subjects] == ["subj-1", "subj-2"]
    # repeated proceeding heading (tx-0001 in both extracts) deduped
    assert len([t for t in proceeding.texts if t.source_id == "tx-0001"]) == 1


def test_stitch_document_order_global_and_unique(daily):
    from parlhansard.normalize.canonical_xml import _iter_nodes

    orders = [n.document_order for n in _iter_nodes(daily)]
    assert len(orders) == len(set(orders)), "document_order must be unique across the day"
    subj1 = daily.proceedings[0].subjects[0]
    subj2 = daily.proceedings[0].subjects[1]
    assert subj1.document_order < subj2.document_order, "extract 1 content precedes extract 2"


def test_stitch_daily_identity(daily):
    assert daily.extensions["extract_count"] == "2"
    assert daily.fragment_id  # deterministic day id assigned
    assert daily.source_doc_id is None  # per-extract ids are not the day's id
    subj1 = daily.proceedings[0].subjects[0]
    assert subj1.extensions["extract_index"] == "1"


def test_leading_comment_is_tolerated():
    # real payloads carry the licence comment before the root element
    content = (FIXTURES / "extract_0001.xml").read_bytes()
    assert b"<!--" in content.split(b"<hansard")[0]
    parse_extract(content, jurisdiction=Jurisdiction.WA)

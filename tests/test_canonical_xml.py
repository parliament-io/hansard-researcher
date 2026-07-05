import datetime as dt
from pathlib import Path

import pytest

from hansard_researcher.model.canonical import (
    Jurisdiction,
    ReviewStage,
    TalkerKind,
    TalkerRole,
    TextKind,
    VoteValue,
)
from hansard_researcher.normalize.canonical_xml import parse_extract, stitch_daily

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
    from hansard_researcher.normalize.canonical_xml import _iter_nodes

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


@pytest.fixture
def event_fragment():
    return parse_extract(
        (FIXTURES / "extract_events.xml").read_bytes(),
        jurisdiction=Jurisdiction.WA,
        extract_index=1,
    )


def test_interjection_event_becomes_sibling_talker(event_fragment):
    subject = event_fragment.proceedings[0].subjects[0]
    host, quoted, stub = subject.talkers[:3]
    assert host.name == "Mr Sample"
    assert quoted.kind is TalkerKind.INTERJECTION
    assert quoted.role is TalkerRole.MEMBER
    assert quoted.member_source_id == "m-100"
    assert quoted.member_reference_id == "ref-100"
    assert quoted.name == "Ms Example"
    assert "kind_inferred" not in quoted.extensions  # source markup, not inference
    # quoted words move to the interjector; leading ": " separator stripped
    assert quoted.texts[0].clean_text == "Under whose policy?"
    assert quoted.texts[0].source_id == "tx-3"
    # the interrupted speaker no longer carries the interjection paragraphs
    assert [t.source_id for t in host.texts] == ["tx-2", "tx-5", "tx-6"]


def test_stub_interjection_keeps_verbatim_sentence(event_fragment):
    subject = event_fragment.proceedings[0].subjects[0]
    stub = subject.talkers[2]
    assert stub.kind is TalkerKind.INTERJECTION
    assert stub.name == "Ms Example"
    assert stub.texts[0].clean_text == "Ms Example interjected."


def test_subject_level_interjection_event(event_fragment):
    subject = event_fragment.proceedings[0].subjects[0]
    outside = subject.talkers[3]
    assert outside.kind is TalkerKind.INTERJECTION
    assert outside.member_source_id == "m-300"
    assert outside.name == "Mr Third"


def test_sa_interjecting_label_form(event_fragment):
    """SA labels read "X interjecting:" (census 2026-07-05) — the name must
    still parse out and the trailing colon must not survive into it."""
    subject = event_fragment.proceedings[0].subjects[0]
    sa_form = subject.talkers[4]
    assert sa_form.kind is TalkerKind.INTERJECTION
    assert sa_form.member_source_id == "9"
    assert sa_form.name == "The Hon. P.F. Conlon"
    assert sa_form.texts[0].clean_text == "The Hon. P.F. Conlon interjecting:"


def test_interjection_document_order_reflects_flow(event_fragment):
    subject = event_fragment.proceedings[0].subjects[0]
    host, quoted = subject.talkers[0], subject.talkers[1]
    before = next(t for t in host.texts if t.source_id == "tx-2")
    after = next(t for t in host.texts if t.source_id == "tx-6")
    assert before.document_order < quoted.document_order < after.document_order


def test_meetingtimestamp_anchors_running_clock(event_fragment):
    subject = event_fragment.proceedings[0].subjects[0]
    host = subject.talkers[0]
    label = next(t for t in host.texts if t.source_id == "tx-5")
    resumed = next(t for t in host.texts if t.source_id == "tx-6")
    # the label paragraph is kept verbatim and both anchor to the event time
    assert label.clean_text == "9:35:00 AM"
    assert label.time_anchor == dt.datetime.fromisoformat("2026-03-04T09:35:00+08:00")
    assert resumed.time_anchor == dt.datetime.fromisoformat("2026-03-04T09:35:00+08:00")
    # a running-clock reading is not a sitting-phase mark
    assert all(m.kind != "meetingtimestamp" for m in event_fragment.meeting_time_marks)


def test_meeting_phase_events_become_time_marks(event_fragment):
    opened, suspended = (
        [m for m in event_fragment.meeting_time_marks if m.kind == kind]
        for kind in ("meetingopened", "meetingsuspended")
    )
    assert opened[0].time == dt.datetime.fromisoformat("2026-03-04T09:00:00+08:00")
    assert opened[0].label == "The Synthetic Assembly met at 9:00 am."
    assert suspended[0].time == dt.datetime.fromisoformat("2026-03-04T12:00:00+08:00")
    # the announcement paragraphs stay in the verbatim record
    subject = event_fragment.proceedings[0].subjects[0]
    assert any(t.source_id == "tx-1" for t in subject.texts)
    assert any(t.source_id == "tx-8" for t in subject.texts)


def test_split_event_label_reassembles(event_fragment):
    """WA splits some labels across adjacent events; identity comes from
    whichever fragment carries the member id (8 texts, census 2026-07-05)."""
    subject = event_fragment.proceedings[0].subjects[0]
    split = subject.talkers[5]
    assert split.kind is TalkerKind.INTERJECTION
    assert split.name == "Ms Split"
    assert split.member_source_id == "m-500"
    assert split.texts[0].clean_text == "Ms Split interjected."


def test_nested_item_event_with_words(event_fragment):
    subject = event_fragment.proceedings[0].subjects[0]
    nested = subject.talkers[6]
    assert nested.kind is TalkerKind.INTERJECTION
    assert nested.member_source_id == "m-400"
    assert nested.name == "Hon Nested Quoted"
    assert nested.texts[0].clean_text == "Not likely!"


def test_sa_kindless_suspension_label(event_fragment):
    """SA suspensions are kindless, timeless events — the label's 24h
    readings become a mark (suspension start) and re-anchor the clock at
    the resumption."""
    suspended = [m for m in event_fragment.meeting_time_marks if m.kind == "meetingsuspended"]
    assert suspended[1].time == dt.datetime(2026, 3, 4, 12, 47)
    assert suspended[1].label == "[Sitting suspended from 12:47 to 14:00]"
    subject = event_fragment.proceedings[0].subjects[0]
    resumed = next(t for t in subject.texts if t.source_id == "tx-14")
    assert resumed.time_anchor == dt.datetime(2026, 3, 4, 14, 0)
    # the stage direction stays in the verbatim record
    assert any(t.source_id == "tx-13" for t in subject.texts)


def test_sa_kindless_committee_met_label(event_fragment):
    opened = [m for m in event_fragment.meeting_time_marks if m.kind == "meetingopened"]
    assert opened[1].time == dt.datetime(2026, 3, 4, 9, 12)
    assert opened[1].label == "The committee met at 09:12"


def test_kindless_stage_direction_stays_verbatim(event_fragment):
    assert event_fragment.extensions["unhandled:event:<none>"] == "1"
    subject = event_fragment.proceedings[0].subjects[0]
    quorum = next(t for t in subject.texts if t.source_id == "tx-15")
    assert quorum.clean_text == "A quorum having been formed:"


def test_unknown_event_kind_is_noted_not_dropped(event_fragment):
    assert event_fragment.extensions["unhandled:event:somethingnew"] == "1"
    subject = event_fragment.proceedings[0].subjects[0]
    mystery = next(t for t in subject.texts if t.source_id == "tx-9")
    assert mystery.clean_text == "Mystery."


def test_committee_volume_identity_from_name():
    """SA committee volumes carry the parent chamber in <house> and their
    identity in <name>: both volumes of one date must not collide into one
    fragment_id / silver partition (real data loss — ~14k SA rows)."""
    def volume(name: str, house: str) -> bytes:
        return f"""<hansard id="x" schemaVersion="1.0">
          <name>{name}</name>
          <date date="2026-06-18" />
          <house>{house}</house>
          <proceeding></proceeding>
        </hansard>""".encode()

    committee = parse_extract(
        volume("Estimates Committee A", "House of Assembly"),
        jurisdiction=Jurisdiction.SA, extract_index=1,
    )
    chamber = parse_extract(
        volume("House of Assembly", "House of Assembly"),
        jurisdiction=Jurisdiction.SA, extract_index=1,
    )
    assert committee.house == "Estimates Committee A"
    assert committee.committee_name == "Estimates Committee A"
    assert committee.extensions["parent_house"] == "House of Assembly"
    assert chamber.house == "House of Assembly"
    assert "parent_house" not in chamber.extensions

    committee_daily = stitch_daily([committee])
    chamber_daily = stitch_daily([chamber])
    assert committee_daily.fragment_id != chamber_daily.fragment_id
    assert committee_daily.extensions["parent_house"] == "House of Assembly"

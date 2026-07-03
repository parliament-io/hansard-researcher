"""Canonical Hansard document model.

Mirrors the analytics-relevant structure of ``schemas/Hansard_1_0.xsd`` — the
SA/WA "Hansard Fragment" schema family, which is also the normalization target
for the NSW, Federal uniXML, NZ and Scottish sources (see docs/ROADMAP.md for
the source notes; the Federal mapping table lives in
``normalize/au_unixml.py``).

Design notes:

- The XML body is an ordered mixed sequence (``subject`` interleaves talkers,
  paragraphs, divisions, subproceedings, clauses). Rather than a recursive
  union, each child model carries ``document_order`` — its 0-based position in
  the fragment's overall reading order — so the interleaving is reconstructable
  from typed lists, and every list flattens cleanly to a Parquet table.
- Source identifiers are preserved verbatim: ``source_id`` is the XML ``@id``
  (the parliament's internal record id) and ``uid`` the XML ``@uid`` (unique
  within the document). Deterministic pipeline ids are derived separately via
  :func:`parlhansard.model.ids.deterministic_id`.
- Fields the canonical XSD lacks but a source provides (e.g. federal
  ``in.gov``) live in the ``extensions`` dict rather than being dropped.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Jurisdiction(StrEnum):
    WA = "wa"
    SA = "sa"
    NSW = "nsw"
    AU = "au"
    NZ = "nz"
    SCOT = "scot"


class ReviewStage(StrEnum):
    PUBLISHED = "published"
    UNCORRECTED = "uncorrected"


class TalkerRole(StrEnum):
    MEMBER = "member"
    MINISTER = "minister"
    OFFICE = "office"
    VISITOR = "visitor"


class TalkerKind(StrEnum):
    SPEECH = "speech"
    QUESTION = "question"
    ANSWER = "answer"
    INTERJECTION = "interjection"
    PAPER = "paper"
    PETITION = "petition"


class TextKind(StrEnum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    SUBHEADING = "subheading"
    ITEM = "item"
    BOOKMARK = "bookmark"


class VoteValue(StrEnum):
    AYES = "AYES"
    NOES = "NOES"
    PAIRS = "PAIRS"
    ABSTENTIONS = "ABSTENTIONS"


class DivisionResult(StrEnum):
    AYES = "ayes"
    NOES = "noes"


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_order: int = 0
    extensions: dict[str, str] = Field(default_factory=dict)


class TextPara(_Node):
    """One paragraph (or heading/item/bookmark) of official Hansard text."""

    source_id: str | None = None
    kind: TextKind = TextKind.PARAGRAPH
    raw_text: str = ""
    clean_text: str = ""
    para_index: int = 0
    page_no: str | None = None
    time_anchor: dt.datetime | None = None
    style: str | None = None
    mapped_style: str | None = None


class DivisionVote(_Node):
    """A single member's recorded vote in a division."""

    member_source_id: str | None = None
    member_name: str | None = None
    vote: VoteValue
    teller: bool = False
    proxy: bool = False
    proxy_source_id: str | None = None
    proxy_name: str | None = None
    party: str | None = None


class Talker(_Node):
    """A speaking turn — one person and the text of what they said."""

    uid: str | None = None
    member_source_id: str | None = None
    member_reference_id: str | None = None
    name: str | None = None
    role: TalkerRole | None = None
    kind: TalkerKind | None = None
    party: str | None = None
    party_source_id: str | None = None
    electorate: str | None = None
    portfolios: list[str] = Field(default_factory=list)
    first_speech: bool = False
    continued: bool = False
    start_time: dt.datetime | None = None
    texts: list[TextPara] = Field(default_factory=list)


class Division(_Node):
    """A formal recorded vote, including per-member vote data."""

    uid: str | None = None
    source_id: str | None = None
    result: DivisionResult | None = None
    ayes_count: int | None = None
    noes_count: int | None = None
    pairs_count: int | None = None
    abstentions_count: int | None = None
    texts: list[TextPara] = Field(default_factory=list)
    talkers: list[Talker] = Field(default_factory=list)
    votes: list[DivisionVote] = Field(default_factory=list)


class BillRef(_Node):
    """A reference to a bill attached to a subject."""

    uid: str | None = None
    source_id: str | None = None
    reference_id: str | None = None
    name: str | None = None


class Clause(_Node):
    """A clause/section/amendment (e.g. committee stage of a bill)."""

    uid: str | None = None
    name: str | None = None
    talkers: list[Talker] = Field(default_factory=list)
    texts: list[TextPara] = Field(default_factory=list)
    divisions: list[Division] = Field(default_factory=list)


class Subproceeding(_Node):
    """A sub-phase of a subject (e.g. a bill reading stage)."""

    uid: str | None = None
    name: str | None = None
    talkers: list[Talker] = Field(default_factory=list)
    texts: list[TextPara] = Field(default_factory=list)
    divisions: list[Division] = Field(default_factory=list)
    clauses: list[Clause] = Field(default_factory=list)


class Subject(_Node):
    """One subject of debate within a proceeding — the primary debate unit."""

    uid: str | None = None
    name: str | None = None
    names: list[str] = Field(default_factory=list)
    bill_refs: list[BillRef] = Field(default_factory=list)
    committee_name: str | None = None
    talkers: list[Talker] = Field(default_factory=list)
    texts: list[TextPara] = Field(default_factory=list)
    divisions: list[Division] = Field(default_factory=list)
    subproceedings: list[Subproceeding] = Field(default_factory=list)
    clauses: list[Clause] = Field(default_factory=list)


class Proceeding(_Node):
    """A parliamentary proceeding — a normalized order-of-business item."""

    uid: str | None = None
    name: str | None = None
    continued: bool = False
    subjects: list[Subject] = Field(default_factory=list)
    talkers: list[Talker] = Field(default_factory=list)
    texts: list[TextPara] = Field(default_factory=list)


class Attendee(_Node):
    kind: str | None = None
    name: str | None = None
    reference_id: str | None = None


class MeetingTimeMark(_Node):
    kind: str | None = None
    time: dt.datetime | None = None
    label: str | None = None


class Fragment(BaseModel):
    """One sitting-day Hansard record (or fragment thereof) for one house.

    The pipeline grain: ``(jurisdiction, date, house)`` plus ``source_doc_id``
    where a source publishes multiple fragments per day (SA subjects, WA
    extracts). Federal/NSW daily files normalize to a single fragment.
    """

    model_config = ConfigDict(extra="forbid")

    # identity
    fragment_id: str
    jurisdiction: Jurisdiction
    source_doc_id: str | None = None
    schema_version: str | None = None

    # sitting metadata (XSD header block)
    name: str | None = None
    date: dt.date
    house: str | None = None
    committee_name: str | None = None
    venue: str | None = None
    parliament_num: int | None = None
    session_num: int | None = None
    parliament_name: str | None = None
    session_name: str | None = None
    meeting_number: str | None = None
    review_stage: ReviewStage | None = None
    start_time: dt.datetime | None = None
    end_time: dt.datetime | None = None
    start_page: str | None = None
    end_page: str | None = None
    date_modified: dt.datetime | None = None
    lang: str = "en"

    # provenance (volatile — excluded from content hash)
    source_url: str | None = None
    retrieved_at: dt.datetime | None = None

    # body
    proceedings: list[Proceeding] = Field(default_factory=list)
    texts: list[TextPara] = Field(default_factory=list)
    attendees: list[Attendee] = Field(default_factory=list)
    meeting_time_marks: list[MeetingTimeMark] = Field(default_factory=list)
    extensions: dict[str, str] = Field(default_factory=dict)

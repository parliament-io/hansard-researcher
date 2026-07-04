"""Parser for NSW Parliament Hansard fragment XML.

NSW publishes exactly the schema preserved at
``schemas/federal/ExtractSchema_v1.xsd`` — the v1 extract format:
``hansard.header`` + ``chamber.xscript`` → ``fragment.data`` (structural
speaker metadata) + ``fragment.text`` (XHTML prose). Like WA/SA, the day is
split **by subject**: the daily ToC lists ``topic`` elements with ``@uid``
(the fragment documentId) and ``@ref`` (1-based order), and each fragment is
one topic. :func:`parlhansard.normalize.canonical_xml.stitch_daily` reassembles
the daily (proceedings merge by name — NSW carries no proceeding uids).

Speaker attribution inside ``fragment.text``: paragraphs open with
``<span data-mode="member" data-value="{member id}" data-article="{kind}">``
markers; paragraphs are assigned to the most recent marker. ``Time-H`` +
``HiddenTime-H`` spans carry the clock (HH:MM + :SS).

Divisions ARE structured in ``fragment.data`` (a source extension beyond the
v1 XSD; the prose body only carries a "[Deferred division]" marker):
``topic/subproceeding/division`` with ``ayes``/``noes`` (``text``, ``count``,
per-member ``aye``/``noe`` of ``id`` + ``name``) and ``pairs`` (``group`` →
two ``pair`` members). **The member ``name`` elements are blank** — ``id``
is the parliament ``pk`` (same id space as the member register and talker
``data-value``), so names/party resolve via ``data/reference/members``.
"""

from __future__ import annotations

import datetime as dt

from lxml import etree

from parlhansard.model.canonical import (
    Division,
    DivisionResult,
    DivisionVote,
    Fragment,
    Jurisdiction,
    Proceeding,
    ReviewStage,
    Subject,
    Subproceeding,
    Talker,
    TalkerKind,
    TextKind,
    TextPara,
    VoteValue,
)
from parlhansard.normalize.canonical_xml import _clean

_TALK_KINDS = {
    "speech": TalkerKind.SPEECH,
    "question": TalkerKind.QUESTION,
    "answer": TalkerKind.ANSWER,
    "interjection": TalkerKind.INTERJECTION,
}

_HOUSE_CODES = {"legislative assembly": "la", "legislative council": "lc"}


def _local(el) -> str | None:
    return etree.QName(el).localname if isinstance(el.tag, str) else None


class _Ctx:
    def __init__(self, date: dt.date) -> None:
        self.date = date
        self.order = 0
        self.time: dt.datetime | None = None

    def next_order(self) -> int:
        self.order += 1
        return self.order - 1


def house_code(chamber: str | None) -> str:
    return _HOUSE_CODES.get((chamber or "").strip().lower(), "unknown")


def _parse_meta_talkers(data: etree._Element, ctx: _Ctx) -> list[Talker]:
    """Structural speaker metadata from fragment.data, in document order."""
    talkers: list[Talker] = []
    for talk_el in data.iter():
        tag = _local(talk_el)
        if tag not in _TALK_KINDS:
            continue
        meta = talk_el.find("talk.start/talker")
        talker = Talker(document_order=ctx.next_order(), kind=_TALK_KINDS[tag])
        if meta is not None:
            talker.member_source_id = (meta.findtext("id") or "").strip() or None
            talker.name = _clean(meta.findtext("name") or "") or None
            talker.electorate = _clean(meta.findtext("electorate") or "") or None
            portfolios = meta.find("portfolios")
            if portfolios is not None:
                talker.portfolios = [
                    _clean("".join(p.itertext()))
                    for p in portfolios.findall("portfolio")
                    if _clean("".join(p.itertext()))
                ]
            qnum = (meta.findtext("question.number") or "").strip()
            if qnum:
                talker.extensions["question_number"] = qnum
            qdate = (meta.findtext("question.date") or "").strip()
            if qdate:
                talker.extensions["question_date"] = qdate
        talkers.append(talker)
    return talkers


_VOTE_TAGS = {"aye": VoteValue.AYES, "noe": VoteValue.NOES, "pair": VoteValue.PAIRS}
_COUNT_FIELDS = {"ayes": "ayes_count", "noes": "noes_count", "abstentions": "abstentions_count"}


def _parse_division(div_el: etree._Element, ctx: _Ctx) -> Division:
    """One structured NSW division: counts + per-member votes (blank names)."""
    division = Division(document_order=ctx.next_order())
    for group_el in div_el:
        tag = _local(group_el)
        if tag in ("ayes", "noes", "pairs", "abstentions"):
            count_text = (group_el.findtext("count") or "").strip()
            if tag in _COUNT_FIELDS and count_text.isdigit():
                setattr(division, _COUNT_FIELDS[tag], int(count_text))
            preamble = _clean(group_el.findtext("text") or "")
            if preamble:
                division.texts.append(
                    TextPara(
                        document_order=ctx.next_order(),
                        raw_text=preamble,
                        clean_text=preamble,
                    )
                )
            for member_el in group_el.iter():
                member_tag = _local(member_el)
                if member_tag not in _VOTE_TAGS:
                    continue
                member_id = (member_el.findtext("id") or "").strip() or None
                # names are blank at the source; the register fills them in
                name = _clean(member_el.findtext("name") or "") or None
                if member_id or name:
                    division.votes.append(
                        DivisionVote(
                            document_order=ctx.next_order(),
                            member_source_id=member_id,
                            member_name=name,
                            vote=_VOTE_TAGS[member_tag],
                        )
                    )
            if tag == "pairs":
                groups = group_el.findall("group")
                if count_text.isdigit():
                    division.pairs_count = int(count_text)
                elif groups:
                    division.pairs_count = len(groups)
        elif tag == "questionresolved":
            resolved = _clean("".join(group_el.itertext()))
            if resolved:
                division.extensions["question_resolved"] = resolved
    if (
        division.ayes_count is not None
        and division.noes_count is not None
        and division.ayes_count != division.noes_count
    ):
        division.result = (
            DivisionResult.AYES
            if division.ayes_count > division.noes_count
            else DivisionResult.NOES
        )
        division.extensions["result_derived"] = "true"
    return division


def _marker_of(p: etree._Element) -> str | None:
    """Member id if this paragraph opens a new speaker's turn."""
    for span in p.iter():
        if _local(span) == "span" and span.get("data-mode") == "member":
            return (span.get("data-value") or "").strip() or None
    return None


def _clock_of(p: etree._Element, date: dt.date) -> dt.datetime | None:
    """NSW time spans: Time-H holds HH:MM, HiddenTime-H holds :SS."""
    time_part = seconds_part = ""
    for span in p.iter():
        if _local(span) != "span":
            continue
        cls = span.get("class") or ""
        if cls == "Time-H":
            time_part = "".join(span.itertext()).strip()
        elif cls == "HiddenTime-H":
            seconds_part = "".join(span.itertext()).strip()
    if not time_part or ":" not in time_part:
        return None
    try:
        hour, minute = (int(x) for x in time_part.split(":", 1))
        second = int(seconds_part.lstrip(":")) if seconds_part.startswith(":") else 0
        return dt.datetime.combine(date, dt.time(hour, minute, second))
    except ValueError:
        return None


def parse_nsw_fragment(
    content: bytes,
    *,
    doc_id: str | None = None,
    extract_index: int | None = None,
    source_url: str | None = None,
    retrieved_at: dt.datetime | None = None,
) -> Fragment:
    """Parse one NSW per-subject fragment into a partial daily fragment."""
    root = etree.fromstring(content)
    if _local(root) != "hansard":
        raise ValueError(f"expected <hansard> root, got <{root.tag}>")

    header = root.find("hansard.header")
    if header is None:
        raise ValueError("fragment has no <hansard.header>")
    date = dt.date.fromisoformat((header.findtext("date") or "").strip()[:10])
    chamber = _clean(header.findtext("chamber") or "") or None
    draft = (header.findtext("draft") or "").strip().lower() == "true"

    def _num(tag: str) -> int | None:
        value = (header.findtext(tag) or "").strip()
        return int(value) if value.isdigit() else None

    fragment = Fragment(
        fragment_id="",  # assigned by stitch_daily
        jurisdiction=Jurisdiction.NSW,
        source_doc_id=doc_id,
        date=date,
        house=chamber,
        parliament_num=_num("parliament.number"),
        session_num=_num("session.number"),
        session_name=_clean(header.findtext("parliament.session.name") or "") or None,
        review_stage=ReviewStage.UNCORRECTED if draft else ReviewStage.PUBLISHED,
        lang="en-AU",
        source_url=source_url,
        retrieved_at=retrieved_at,
    )
    if extract_index is not None:
        fragment.extensions["extract_index"] = str(extract_index)

    ctx = _Ctx(date)
    xscript = root.find("chamber.xscript")
    if xscript is None:
        return fragment

    # structure: proceeding name + subject name + speaker metadata
    proceeding = Proceeding(document_order=ctx.next_order())
    subject = Subject(document_order=ctx.next_order(), uid=doc_id)
    data = xscript.find("fragment.data")
    meta_talkers: list[Talker] = []
    if data is not None:
        proc_el = data.find("proceeding")
        if proc_el is not None:
            proceeding.name = _clean(proc_el.findtext("proceedinginfo/text") or "") or None
            topic = proc_el.find("topic")
            if topic is not None:
                subject.name = _clean(topic.findtext("topicinfo/text") or "") or None
                if subject.name:
                    subject.names = [subject.name]
                for sub_el in topic.findall("subproceeding"):
                    sub = Subproceeding(
                        document_order=ctx.next_order(),
                        name=_clean(sub_el.findtext("subproceedinginfo/text") or "") or None,
                    )
                    sub.divisions = [
                        _parse_division(div_el, ctx)
                        for div_el in sub_el.findall("division")
                    ]
                    if sub.name or sub.divisions:
                        subject.subproceedings.append(sub)
                subject.divisions = [
                    _parse_division(div_el, ctx) for div_el in topic.findall("division")
                ]
        meta_talkers = _parse_meta_talkers(data, ctx)

    # prose: assign paragraphs to speakers via data-value markers
    by_id: dict[str, list[Talker]] = {}
    for talker in meta_talkers:
        by_id.setdefault(talker.member_source_id or "", []).append(talker)

    def claim(member_id: str) -> Talker:
        queue = by_id.get(member_id)
        if queue:
            return queue.pop(0)
        orphan = Talker(
            document_order=ctx.next_order(),
            kind=TalkerKind.SPEECH,
            member_source_id=member_id or None,
        )
        meta_talkers.append(orphan)
        return orphan

    current: Talker | None = None
    body = xscript.find("fragment.text/body")
    if body is not None:
        subject_para = talker_para = 0
        for p in body.iter():
            if _local(p) != "p":
                continue
            marker = _marker_of(p)
            if marker is not None:
                current = claim(marker)
                talker_para = 0
                clock = _clock_of(p, date)
                if clock is not None:
                    ctx.time = clock
                    current.start_time = clock
            raw = "".join(p.itertext())
            cls = p.get("class") or None
            kind = TextKind.HEADING if (cls or "").endswith("-H") else TextKind.PARAGRAPH
            text = TextPara(
                document_order=ctx.next_order(),
                kind=kind,
                raw_text=raw,
                clean_text=_clean(raw),
                para_index=talker_para if current is not None else subject_para,
                time_anchor=ctx.time,
                mapped_style=cls,
            )
            if current is not None:
                current.texts.append(text)
                talker_para += 1
            else:
                subject.texts.append(text)
                subject_para += 1

    subject.talkers = meta_talkers
    proceeding.subjects.append(subject)
    fragment.proceedings.append(proceeding)
    return fragment

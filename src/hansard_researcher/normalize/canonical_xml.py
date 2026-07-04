"""Parser for the canonical ``Hansard_1_0.xsd`` schema family (WA/SA native).

Two-step model, mirroring how the source publishes:

- :func:`parse_extract` — parse ONE per-subject extract (ToC ref 1, 2, 3 … =
  extract 001, 002, 003 …) into a partial :class:`Fragment`. Every extract
  carries the whole-day header plus one proceeding/subject slice of the day.
- :func:`stitch_daily` — reassemble the extracts (in ToC index order) into the
  single daily fragment the analytics grain expects — the public-API
  equivalent of the ``Daily.xml`` the source systems build at end of day.
  Proceedings spanning multiple extracts are merged by ``uid``; proceeding-
  level heading texts repeated in each extract are deduped by source id;
  ``document_order`` is renumbered across the whole day.
"""

from __future__ import annotations

import datetime as dt
import html
import re

from lxml import etree

from hansard_researcher.model.canonical import (
    Attendee,
    BillRef,
    Clause,
    Division,
    DivisionResult,
    DivisionVote,
    Fragment,
    Jurisdiction,
    MeetingTimeMark,
    Proceeding,
    ReviewStage,
    Subject,
    Subproceeding,
    Talker,
    TalkerKind,
    TalkerRole,
    TextKind,
    TextPara,
    VoteValue,
)
from hansard_researcher.model.ids import deterministic_id

_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Whitespace-normalize and resolve double-escaped entities (``&amp;#x9;``)."""
    return _WS.sub(" ", html.unescape(text)).strip()


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _bool_attr(el: etree._Element, name: str) -> bool:
    return (el.get(name) or "").strip().lower() in ("true", "1", "yes")


def _count_of(el: etree._Element) -> int | None:
    """Vote-count value: the XSD specifies ``@num`` but live WA/SA payloads
    put the number in the element's text content — accept both."""
    return _parse_int(el.get("num")) if el.get("num") else _parse_int((el.text or "").strip())


def _child_text(el: etree._Element, tag: str) -> str | None:
    child = el.find(tag)
    if child is None:
        return None
    text = "".join(child.itertext()).strip()
    return text or None


def _enum_or_ext(value: str | None, enum_cls, node, ext_key: str):
    """Coerce an attribute into an enum; preserve unknown values in extensions."""
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        node.extensions[ext_key] = value
        return None


class _Ctx:
    """Forward-fill state while walking one extract in document order."""

    def __init__(self) -> None:
        self.order = 0
        self.page: str | None = None
        self.time: dt.datetime | None = None

    def next_order(self) -> int:
        self.order += 1
        return self.order - 1


def _note_unhandled(fragment: Fragment, tag: str) -> None:
    key = f"unhandled:{tag}"
    fragment.extensions[key] = str(int(fragment.extensions.get(key, "0")) + 1)


def _parse_text(el: etree._Element, ctx: _Ctx, para_index: int) -> TextPara:
    kind = TextKind.PARAGRAPH
    if el.find("heading") is not None:
        kind = TextKind.HEADING
    elif el.find("subheading") is not None:
        kind = TextKind.SUBHEADING
    elif el.find("item") is not None:
        kind = TextKind.ITEM

    stamp = el.find("timeStamp")
    if stamp is not None:
        anchored = _parse_datetime(stamp.get("time"))
        if anchored is not None:
            ctx.time = anchored

    raw = "".join(el.itertext())
    return TextPara(
        document_order=ctx.next_order(),
        source_id=el.get("id"),
        kind=kind,
        raw_text=raw,
        clean_text=_clean(raw),
        para_index=para_index,
        page_no=ctx.page,
        time_anchor=ctx.time,
        style=el.get("style"),
        mapped_style=el.get("mappedstyle"),
    )


def _parse_bookmark(el: etree._Element, ctx: _Ctx, para_index: int) -> TextPara:
    raw = "".join(el.itertext()) or (el.get("name") or "")
    return TextPara(
        document_order=ctx.next_order(),
        source_id=el.get("id"),
        kind=TextKind.BOOKMARK,
        raw_text=raw,
        clean_text=_clean(raw),
        para_index=para_index,
        page_no=ctx.page,
        time_anchor=ctx.time,
    )


def _parse_talker(el: etree._Element, ctx: _Ctx, fragment: Fragment) -> Talker:
    talker = Talker(
        document_order=ctx.next_order(),
        uid=el.get("uid"),
        member_source_id=el.get("id"),
        member_reference_id=el.get("referenceid"),
        first_speech=_bool_attr(el, "firstSpeech"),
        continued=_bool_attr(el, "continued"),
    )
    talker.role = _enum_or_ext(el.get("role"), TalkerRole, talker, "role")
    talker.kind = _enum_or_ext(el.get("kind"), TalkerKind, talker, "kind")

    para_index = 0
    for child in el:
        if child.tag is etree.Comment:
            continue
        tag = child.tag
        if tag == "name" and talker.name is None:
            talker.name = _clean("".join(child.itertext()))
        elif tag == "electorate":
            talker.electorate = _clean("".join(child.itertext())) or None
        elif tag == "party":
            talker.party = _clean("".join(child.itertext())) or None
            talker.party_source_id = child.get("id")
            if child.get("abbreviation"):
                talker.extensions["party_abbreviation"] = child.get("abbreviation")
        elif tag == "portfolios":
            talker.portfolios = [
                _clean("".join(p.itertext()))
                for p in child.findall("portfolio")
                if _clean("".join(p.itertext()))
            ]
        elif tag == "startTime":
            talker.start_time = _parse_datetime(child.get("time"))
            if talker.start_time is not None:
                ctx.time = talker.start_time
        elif tag == "page":
            ctx.page = child.get("num")
        elif tag == "text":
            talker.texts.append(_parse_text(child, ctx, para_index))
            para_index += 1
        elif tag == "bookmark":
            talker.texts.append(_parse_bookmark(child, ctx, para_index))
            para_index += 1
        elif tag == "clause":
            # rare: clause inside talker — surface its name + texts inline
            name = _child_text(child, "name")
            if name:
                talker.texts.append(
                    TextPara(
                        document_order=ctx.next_order(),
                        kind=TextKind.HEADING,
                        raw_text=name,
                        clean_text=_clean(name),
                        para_index=para_index,
                        page_no=ctx.page,
                        time_anchor=ctx.time,
                    )
                )
                para_index += 1
            for t in child.findall("text"):
                talker.texts.append(_parse_text(t, ctx, para_index))
                para_index += 1
        elif tag in ("house", "committeeName", "papers", "petitions", "questions", "object"):
            pass  # metadata/attachments not needed for Tier 1
        else:
            _note_unhandled(fragment, f"talker/{tag}")
    return talker


# historic (pre-2026) SA/WA divisions are presentational: counts as
# "Ayes<TAB>3" text lines and votes as <table> blocks titled AYES/NOES/PAIRS
_COUNT_LINE = re.compile(r"^(ayes|noes|pairs|abstentions|majority)\s+(\d+)$", re.IGNORECASE)
_TELLER_SUFFIX = re.compile(r"\s*\((?:teller)\)\s*$", re.IGNORECASE)
_VOTE_TITLES = {
    "AYES": VoteValue.AYES,
    "NOES": VoteValue.NOES,
    "PAIRS": VoteValue.PAIRS,
    "ABSTENTIONS": VoteValue.ABSTENTIONS,
}


def _vote_table_group(text_el: etree._Element) -> tuple[etree._Element, VoteValue] | None:
    """(table, vote group) if this text wraps a division vote table."""
    table = text_el.find("table")
    if table is None:
        return None
    rowtitle = table.find("rowtitle")
    if rowtitle is None:
        return None
    title = _clean("".join(rowtitle.itertext())).upper()
    group = _VOTE_TITLES.get(title)
    return (table, group) if group else None


def _parse_division(el: etree._Element, ctx: _Ctx, fragment: Fragment) -> Division:
    division = Division(
        document_order=ctx.next_order(),
        uid=el.get("uid"),
        source_id=el.get("id"),
    )
    division.result = _enum_or_ext(el.get("vote"), DivisionResult, division, "vote")

    para_index = 0
    for child in el:
        if child.tag is etree.Comment:
            continue
        tag = child.tag
        if tag == "page":
            ctx.page = child.get("num")
        elif tag == "text":
            vote_table = _vote_table_group(child)
            if vote_table is not None:
                table, group = vote_table
                for cell in table.iter("cell"):
                    name = _clean("".join(cell.itertext()))
                    if not name or name.upper() in _VOTE_TITLES:
                        continue
                    division.votes.append(
                        DivisionVote(
                            document_order=ctx.next_order(),
                            member_name=_TELLER_SUFFIX.sub("", name).strip(),
                            vote=group,
                            teller=bool(_TELLER_SUFFIX.search(name)),
                        )
                    )
                continue
            text = _parse_text(child, ctx, para_index)
            count_line = _COUNT_LINE.match(text.clean_text)
            if count_line:
                label, value = count_line.group(1).lower(), int(count_line.group(2))
                if label == "ayes":
                    division.ayes_count = value
                elif label == "noes":
                    division.noes_count = value
                elif label == "pairs":
                    division.pairs_count = value
                elif label == "abstentions":
                    division.abstentions_count = value
                # "majority" is derivable — margin — so not stored
            division.texts.append(text)
            para_index += 1
        elif tag == "talker":
            division.talkers.append(_parse_talker(child, ctx, fragment))
        elif tag == "ayesCount":
            division.ayes_count = _count_of(child)
        elif tag == "noesCount":
            division.noes_count = _count_of(child)
        elif tag == "pairsCount":
            division.pairs_count = _count_of(child)
        elif tag == "abstentionsCount":
            division.abstentions_count = _count_of(child)
        elif tag == "data":
            for member in child.findall("member"):
                vote = _enum_or_ext(member.get("vote"), VoteValue, division, "member-vote")
                if vote is None:
                    continue
                division.votes.append(
                    DivisionVote(
                        document_order=ctx.next_order(),
                        member_source_id=member.get("id"),
                        member_name=member.get("name"),
                        vote=vote,
                        teller=_bool_attr(member, "teller"),
                        proxy=_bool_attr(member, "proxy"),
                        proxy_source_id=member.get("proxyId"),
                        proxy_name=member.get("proxyName"),
                    )
                )
        else:
            _note_unhandled(fragment, f"division/{tag}")

    # live payloads often omit @vote — derive the result from the counts
    if (
        division.result is None
        and division.ayes_count is not None
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


def _parse_container_children(
    el: etree._Element,
    node: Subproceeding | Clause,
    ctx: _Ctx,
    fragment: Fragment,
    *,
    allow_clauses: bool,
) -> None:
    para_index = 0
    for child in el:
        if child.tag is etree.Comment:
            continue
        tag = child.tag
        if tag == "name":
            if node.name is None:
                node.name = _clean("".join(child.itertext())) or None
        elif tag == "page":
            ctx.page = child.get("num")
        elif tag == "text":
            node.texts.append(_parse_text(child, ctx, para_index))
            para_index += 1
        elif tag == "bookmark":
            node.texts.append(_parse_bookmark(child, ctx, para_index))
            para_index += 1
        elif tag == "talker":
            node.talkers.append(_parse_talker(child, ctx, fragment))
        elif tag == "division":
            node.divisions.append(_parse_division(child, ctx, fragment))
        elif tag == "clause" and allow_clauses:
            clause = Clause(document_order=ctx.next_order(), uid=child.get("uid"))
            _parse_container_children(child, clause, ctx, fragment, allow_clauses=False)
            node.clauses.append(clause)  # type: ignore[union-attr]
        else:
            _note_unhandled(fragment, f"{el.tag}/{tag}")


def _parse_subject(el: etree._Element, ctx: _Ctx, fragment: Fragment) -> Subject:
    subject = Subject(document_order=ctx.next_order(), uid=el.get("uid"))
    para_index = 0
    for child in el:
        if child.tag is etree.Comment:
            continue
        tag = child.tag
        if tag == "name":
            name = _clean("".join(child.itertext())) or None
            if name:
                subject.names.append(name)
                if subject.name is None:
                    subject.name = name
        elif tag == "bills":
            for bill in child.findall("bill"):
                subject.bill_refs.append(
                    BillRef(
                        document_order=ctx.next_order(),
                        uid=bill.get("uid"),
                        source_id=bill.get("id"),
                        reference_id=bill.get("referenceid"),
                        name=_clean("".join(bill.itertext())) or None,
                    )
                )
        elif tag == "committee":
            subject.committee_name = _clean("".join(child.itertext())) or None
        elif tag == "page":
            ctx.page = child.get("num")
        elif tag == "text":
            subject.texts.append(_parse_text(child, ctx, para_index))
            para_index += 1
        elif tag == "bookmark":
            subject.texts.append(_parse_bookmark(child, ctx, para_index))
            para_index += 1
        elif tag == "talker":
            subject.talkers.append(_parse_talker(child, ctx, fragment))
        elif tag == "division":
            subject.divisions.append(_parse_division(child, ctx, fragment))
        elif tag == "subproceeding":
            sub = Subproceeding(document_order=ctx.next_order(), uid=child.get("uid"))
            _parse_container_children(child, sub, ctx, fragment, allow_clauses=True)
            subject.subproceedings.append(sub)
        elif tag == "clause":
            clause = Clause(document_order=ctx.next_order(), uid=child.get("uid"))
            _parse_container_children(child, clause, ctx, fragment, allow_clauses=False)
            subject.clauses.append(clause)
        elif tag in ("papers", "petitions", "questions"):
            pass  # attachment refs — not needed for Tier 1
        else:
            _note_unhandled(fragment, f"subject/{tag}")
    return subject


def _parse_proceeding(el: etree._Element, ctx: _Ctx, fragment: Fragment) -> Proceeding:
    proceeding = Proceeding(
        document_order=ctx.next_order(),
        uid=el.get("uid"),
        continued=_bool_attr(el, "continued"),
    )
    para_index = 0
    for child in el:
        if child.tag is etree.Comment:
            continue
        tag = child.tag
        if tag == "name":
            if proceeding.name is None:
                proceeding.name = _clean("".join(child.itertext())) or None
        elif tag == "page":
            ctx.page = child.get("num")
        elif tag == "text":
            proceeding.texts.append(_parse_text(child, ctx, para_index))
            para_index += 1
        elif tag == "talker":
            proceeding.talkers.append(_parse_talker(child, ctx, fragment))
        elif tag == "subject":
            proceeding.subjects.append(_parse_subject(child, ctx, fragment))
        else:
            _note_unhandled(fragment, f"proceeding/{tag}")
    return proceeding


def parse_extract(
    content: bytes,
    *,
    jurisdiction: Jurisdiction,
    extract_index: int | None = None,
    source_url: str | None = None,
    retrieved_at: dt.datetime | None = None,
) -> Fragment:
    """Parse one per-subject extract into a partial daily fragment."""
    root = etree.fromstring(content)
    if root.tag != "hansard":
        raise ValueError(f"expected <hansard> root, got <{root.tag}>")

    date_value = None
    date_el = root.find("date")
    if date_el is not None:
        parsed = _parse_datetime(date_el.get("date"))
        if parsed is not None:
            date_value = parsed.date()
    if date_value is None:
        raise ValueError("fragment has no parseable <date date=...>")

    start_el, end_el = root.find("startTime"), root.find("endTime")
    modified_el = root.find("dateModified")
    start_page, end_page = root.find("startPage"), root.find("endPage")

    fragment = Fragment(
        fragment_id="",  # assigned by stitch_daily / caller
        jurisdiction=jurisdiction,
        source_doc_id=root.get("id"),
        schema_version=root.get("schemaVersion"),
        name=_child_text(root, "name"),
        date=date_value,
        house=_child_text(root, "house"),
        committee_name=_child_text(root, "committeeName"),
        venue=_child_text(root, "venue"),
        parliament_num=_parse_int(_child_text(root, "parliamentNum")),
        session_num=_parse_int(_child_text(root, "sessionNum")),
        parliament_name=_child_text(root, "parliamentName"),
        session_name=_child_text(root, "sessionName"),
        start_time=_parse_datetime(start_el.get("time")) if start_el is not None else None,
        end_time=_parse_datetime(end_el.get("time")) if end_el is not None else None,
        start_page=start_page.get("num") if start_page is not None else None,
        end_page=end_page.get("num") if end_page is not None else None,
        date_modified=_parse_datetime(modified_el.get("time")) if modified_el is not None else None,
        lang=root.get("{http://www.w3.org/XML/1998/namespace}lang") or "en",
        source_url=source_url,
        retrieved_at=retrieved_at,
    )
    stage = _child_text(root, "reviewStage")
    fragment.review_stage = _enum_or_ext(stage, ReviewStage, fragment, "reviewStage")
    if extract_index is not None:
        fragment.extensions["extract_index"] = str(extract_index)

    ctx = _Ctx()
    ctx.page = fragment.start_page
    ctx.time = fragment.start_time

    para_index = 0
    for child in root:
        if child.tag is etree.Comment:
            continue
        tag = child.tag
        if tag == "page":
            ctx.page = child.get("num")
        elif tag == "proceeding":
            fragment.proceedings.append(_parse_proceeding(child, ctx, fragment))
        elif tag == "text":
            fragment.texts.append(_parse_text(child, ctx, para_index))
            para_index += 1
        elif tag == "attendance":
            for att in child.findall("attendee"):
                fragment.attendees.append(
                    Attendee(
                        document_order=ctx.next_order(),
                        kind=att.get("kind"),
                        name=_clean("".join(att.itertext())) or None,
                        reference_id=att.get("referenceid"),
                    )
                )
        elif tag == "meetingTimeSummary":
            for mark in child.iter("timeMark"):
                fragment.meeting_time_marks.append(
                    MeetingTimeMark(
                        document_order=ctx.next_order(),
                        kind=mark.get("kind"),
                        time=_parse_datetime(mark.get("time")),
                        label=_clean("".join(mark.itertext())) or None,
                    )
                )
        elif tag in (
            "name", "date", "sessionName", "parliamentNum", "sessionNum",
            "parliamentName", "house", "committeeName", "meetingNumber", "venue",
            "reviewStage", "isTranscribing", "startTime", "endTime", "startPage",
            "endPage", "dateModified", "broadcasts",
        ):
            pass  # header handled above; broadcasts out of Tier 1 scope
        else:
            _note_unhandled(fragment, tag)
    return fragment


def _iter_nodes(fragment: Fragment):
    """Yield every ordered node of a fragment in its current local order."""
    yield from fragment.texts
    yield from fragment.attendees
    yield from fragment.meeting_time_marks
    for proc in fragment.proceedings:
        yield proc
        yield from proc.texts
        for talker in proc.talkers:
            yield talker
            yield from talker.texts
        for subject in proc.subjects:
            yield subject
            yield from subject.texts
            yield from subject.bill_refs
            for talker in subject.talkers:
                yield talker
                yield from talker.texts
            for division in subject.divisions:
                yield division
                yield from division.texts
                for talker in division.talkers:
                    yield talker
                    yield from talker.texts
                yield from division.votes
            for sub in subject.subproceedings:
                yield sub
                yield from sub.texts
                for talker in sub.talkers:
                    yield talker
                    yield from talker.texts
                for division in sub.divisions:
                    yield division
                    yield from division.texts
                    for talker in division.talkers:
                        yield talker
                        yield from talker.texts
                    yield from division.votes
                for clause in sub.clauses:
                    yield clause
                    yield from clause.texts
                    for talker in clause.talkers:
                        yield talker
                        yield from talker.texts
                    for division in clause.divisions:
                        yield division
                        yield from division.texts
                        for talker in division.talkers:
                            yield talker
                            yield from talker.texts
                        yield from division.votes
            for clause in subject.clauses:
                yield clause
                yield from clause.texts
                for talker in clause.talkers:
                    yield talker
                    yield from talker.texts
                for division in clause.divisions:
                    yield division
                    yield from division.texts
                    for talker in division.talkers:
                        yield talker
                        yield from talker.texts
                    yield from division.votes


def stitch_daily(extracts: list[Fragment]) -> Fragment:
    """Reassemble per-subject extracts (in ToC order) into the daily fragment.

    The public-API equivalent of the source system's end-of-day ``Daily.xml``:
    extract 001, 002, 003 … are the day split by ToC ref, each carrying the
    whole-day header. Metadata comes from the first extract; proceedings that
    span extracts are merged by ``uid`` (falling back to name); repeated
    proceeding-level heading texts are deduped by source id; every node's
    ``document_order`` is renumbered across the day.
    """
    if not extracts:
        raise ValueError("no extracts to stitch")

    extracts = sorted(
        extracts, key=lambda f: int(f.extensions.get("extract_index", "0"))
    )
    first = extracts[0]
    daily = first.model_copy(
        update={
            "fragment_id": deterministic_id(
                first.jurisdiction.value, first.date.isoformat(), first.house or ""
            ),
            "source_doc_id": None,
            "proceedings": [],
            "texts": [],
            "attendees": [],
            "meeting_time_marks": [],
            "extensions": {"extract_count": str(len(extracts))},
        }
    )

    by_uid: dict[str, Proceeding] = {}
    seen_text_ids: set[str] = set()
    order = 0

    def renumber(nodes) -> None:
        nonlocal order
        for node in sorted(nodes, key=lambda n: n.document_order):
            node.document_order = order
            order += 1

    for extract in extracts:
        extract_nodes = list(_iter_nodes(extract))
        renumber(extract_nodes)

        for text in extract.texts:
            if text.source_id and text.source_id in seen_text_ids:
                continue
            if text.source_id:
                seen_text_ids.add(text.source_id)
            daily.texts.append(text)
        daily.attendees.extend(extract.attendees)
        daily.meeting_time_marks.extend(extract.meeting_time_marks)

        for proc in extract.proceedings:
            key = proc.uid or f"name:{proc.name}"
            target = by_uid.get(key)
            if target is None:
                by_uid[key] = proc
                daily.proceedings.append(proc)
                target = proc
            else:
                for text in proc.texts:
                    if text.source_id and text.source_id in seen_text_ids:
                        continue
                    if text.source_id:
                        seen_text_ids.add(text.source_id)
                    target.texts.append(text)
                target.talkers.extend(proc.talkers)
                target.subjects.extend(proc.subjects)
            for text in target.texts:
                if text.source_id:
                    seen_text_ids.add(text.source_id)

        # mark each subject with the extract it came from (API deep-link)
        idx = extract.extensions.get("extract_index")
        if idx is not None:
            for proc in extract.proceedings:
                for subject in proc.subjects:
                    subject.extensions.setdefault("extract_index", idx)

    return daily

"""Parser for Australian Federal Parliament daily uniXML (``hansard.xsd`` 2.2).

One request returns the whole sitting day (via
``aph.gov.au/api/hansard/link/?id=chamber/{coll}/{docId}/toc&linktype=xml&fulltranscript=True``),
so there is no stitching step. Mapping to the canonical model:

    session.header                  -> fragment metadata (proof=1 -> uncorrected)
    debate / debateinfo             -> proceeding
    subdebate.1                     -> subject
    subdebate.2                     -> subproceeding
    speech|question|answer          -> talker (kind), with NESTED interjection
                                       and continue elements flattened into
                                       sibling talkers in document order
    talker: name.id, party,
            electorate, first.speech,
            in.gov                  -> member_source_id, party, electorate,
                                       first_speech, extensions["in_gov"]
    talk.text XHTML body p/span     -> text paragraphs (span class HPS-Time
                                       anchors the running clock)
    division: division.data ayes/
              noes/pairs names      -> division + per-member votes ("(Teller)"
                                       name suffix -> teller flag); result
                                       derived from counts (result_derived)
"""

from __future__ import annotations

import datetime as dt
import re

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
from parlhansard.model.ids import deterministic_id
from parlhansard.normalize.canonical_xml import _clean  # shared text normalizer

_TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
_TELLER = re.compile(r"\s*\((?:teller)\)\s*$", re.IGNORECASE)

_HOUSE_MAP = {
    "reps": "House of Representatives",
    "house of reps": "House of Representatives",
    "house of representatives": "House of Representatives",
    "senate": "Senate",
}

_TALK_KINDS = {
    "speech": TalkerKind.SPEECH,
    "talk": TalkerKind.SPEECH,  # rare bare variant observed 2025+
    "question": TalkerKind.QUESTION,
    "answer": TalkerKind.ANSWER,
    "interjection": TalkerKind.INTERJECTION,
}

_VOTE_GROUPS = (("ayes", VoteValue.AYES), ("noes", VoteValue.NOES), ("pairs", VoteValue.PAIRS))


def _local(el) -> str | None:
    return etree.QName(el).localname if isinstance(el.tag, str) else None


class _Ctx:
    def __init__(self, date: dt.date) -> None:
        self.date = date
        self.order = 0
        self.page: str | None = None
        self.time: dt.datetime | None = None

    def next_order(self) -> int:
        self.order += 1
        return self.order - 1

    def clock(self, text: str | None) -> None:
        """Advance the running clock from an HH:MM(:SS) fragment."""
        if not text:
            return
        m = _TIME.search(text)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            second = int(m.group(3) or 0)
            if hour < 24:
                self.time = dt.datetime.combine(self.date, dt.time(hour, minute, second))


def _note_unhandled(fragment: Fragment, tag: str) -> None:
    key = f"unhandled:{tag}"
    fragment.extensions[key] = str(int(fragment.extensions.get(key, "0")) + 1)


def _body_paras(body: etree._Element | None, ctx: _Ctx, start_index: int = 0) -> list[TextPara]:
    """Flatten a Word-derived XHTML ``body`` into text paragraphs."""
    paras: list[TextPara] = []
    if body is None:
        return paras
    index = start_index
    for child in body.iter():
        if _local(child) != "p":
            continue
        # spans with a Time-ish class advance the running clock
        for span in child.iter():
            cls = span.get("class") or ""
            if _local(span) == "span" and "time" in cls.lower():
                ctx.clock("".join(span.itertext()))
        raw = "".join(child.itertext())
        cls = child.get("class") or None
        paras.append(
            TextPara(
                document_order=ctx.next_order(),
                kind=TextKind.PARAGRAPH,
                raw_text=raw,
                clean_text=_clean(raw),
                para_index=index,
                page_no=ctx.page,
                time_anchor=ctx.time,
                mapped_style=cls,
            )
        )
        index += 1
    return paras


def _parse_talker_meta(talk_el: etree._Element, ctx: _Ctx) -> Talker:
    talker = Talker(document_order=ctx.next_order())
    meta = talk_el.find("talk.start/talker")
    if meta is None:
        return talker
    names = meta.findall("name")
    preferred = next((n for n in names if n.get("role") == "metadata"), names[0] if names else None)
    if preferred is not None:
        talker.name = _clean("".join(preferred.itertext())) or None
    talker.member_source_id = (meta.findtext("name.id") or "").strip() or None
    talker.electorate = _clean(meta.findtext("electorate") or "") or None
    party = _clean(meta.findtext("party") or "") or None
    if party:
        talker.party = party
        talker.extensions["party_abbreviation"] = party  # federal party is the abbreviation
    page = (meta.findtext("page.no") or "").strip()
    if page:
        ctx.page = page
    ctx.clock(meta.findtext("time.stamp"))
    in_gov = (meta.findtext("in.gov") or "").strip()
    if in_gov:
        talker.extensions["in_gov"] = in_gov
    first = (meta.findtext("first.speech") or "").strip().lower()
    talker.first_speech = first in ("1", "true", "yes")
    return talker


def _parse_talk(el: etree._Element, kind: TalkerKind, ctx: _Ctx) -> list[Talker]:
    """Parse a speech/question/answer/interjection into 1+ talkers.

    ``interjection`` and ``continue`` are nested inside the parent speech;
    they become sibling talkers so document order reflects the actual flow.
    """
    main = _parse_talker_meta(el, ctx)
    main.kind = kind
    talkers = [main]
    para_index = 0
    for child in el:
        tag = _local(child)
        if tag == "talk.start":
            continue  # consumed by _parse_talker_meta
        if tag == "talk.text":
            paras = _body_paras(child.find("body"), ctx, para_index)
            main.texts.extend(paras)
            para_index += len(paras)
        elif tag == "interjection":
            talkers.extend(_parse_talk(child, TalkerKind.INTERJECTION, ctx))
        elif tag == "continue":
            continuation = _parse_talk(child, kind, ctx)
            for t in continuation:
                if t.kind is kind:
                    t.continued = True
            talkers.extend(continuation)
    return talkers


def _parse_division(el: etree._Element, ctx: _Ctx) -> Division:
    division = Division(document_order=ctx.next_order())
    header_body = el.find("division.header/body")
    if header_body is not None:
        division.texts.extend(_body_paras(header_body, ctx))
        ctx.clock(" ".join(t.raw_text for t in division.texts))  # "[09:13]" preamble
    data = el.find("division.data")
    if data is not None:
        for group, vote in _VOTE_GROUPS:
            group_el = data.find(group)
            if group_el is None:
                continue
            count = (group_el.findtext("num.votes") or "").strip()
            if count.isdigit():
                value = int(count)
                if vote is VoteValue.AYES:
                    division.ayes_count = value
                elif vote is VoteValue.NOES:
                    division.noes_count = value
                else:
                    division.pairs_count = value
            names_el = group_el.find("names")
            if names_el is None:
                continue
            for name_el in names_el.findall("name"):
                raw_name = _clean("".join(name_el.itertext()))
                if not raw_name:
                    continue
                teller = bool(_TELLER.search(raw_name))
                division.votes.append(
                    DivisionVote(
                        document_order=ctx.next_order(),
                        member_name=_TELLER.sub("", raw_name).strip(),
                        vote=vote,
                        teller=teller,
                    )
                )
    result_el = el.find("division.result")
    if result_el is not None:
        result_text = _clean("".join(result_el.itertext()))
        if result_text:
            division.extensions["result_text"] = result_text
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


def _parse_subdebate(
    el: etree._Element,
    node: Subject | Subproceeding,
    info_tag: str,
    ctx: _Ctx,
    fragment: Fragment,
) -> None:
    for child in el:
        tag = _local(child)
        if tag == info_tag:
            node.name = node.name or _clean(child.findtext("title") or "") or None
            page = (child.findtext("page.no") or "").strip()
            if page:
                ctx.page = page
        elif tag and tag.endswith(".text"):
            node.texts.extend(_body_paras(child.find("body"), ctx, len(node.texts)))
        elif tag in _TALK_KINDS:
            node.talkers.extend(_parse_talk(child, _TALK_KINDS[tag], ctx))
        elif tag == "division":
            node.divisions.append(_parse_division(child, ctx))
        elif tag == "subdebate.2" and isinstance(node, Subject):
            sub = Subproceeding(document_order=ctx.next_order())
            _parse_subdebate(child, sub, "subdebateinfo", ctx, fragment)
            node.subproceedings.append(sub)
        else:
            _note_unhandled(fragment, f"{_local(el)}/{tag}")


def _parse_debate(el: etree._Element, ctx: _Ctx, fragment: Fragment) -> Proceeding:
    proceeding = Proceeding(document_order=ctx.next_order())
    implicit_subject: Subject | None = None

    def ensure_subject() -> Subject:
        nonlocal implicit_subject
        if implicit_subject is None:
            implicit_subject = Subject(
                document_order=ctx.next_order(), name=proceeding.name
            )
            proceeding.subjects.append(implicit_subject)
        return implicit_subject

    for child in el:
        tag = _local(child)
        if tag == "debateinfo":
            proceeding.name = _clean(child.findtext("title") or "") or None
            debate_type = _clean(child.findtext("type") or "")
            if debate_type:
                proceeding.extensions["debate_type"] = debate_type
            page = (child.findtext("page.no") or "").strip()
            if page:
                ctx.page = page
        elif tag == "debate.text":
            proceeding.texts.extend(_body_paras(child.find("body"), ctx, len(proceeding.texts)))
        elif tag in ("subdebate.1", "subdebate.2"):
            # 2017-era files occasionally hang subdebate.2 directly off the
            # debate (no subdebate.1 parent) — both map to a subject here
            subject = Subject(document_order=ctx.next_order())
            _parse_subdebate(child, subject, "subdebateinfo", ctx, fragment)
            proceeding.subjects.append(subject)
        elif tag in _TALK_KINDS:
            proceeding.talkers.extend(_parse_talk(child, _TALK_KINDS[tag], ctx))
        elif tag == "division":
            # divisions occasionally sit at debate level — attach to an
            # implicit subject named after the debate so the grain holds
            ensure_subject().divisions.append(_parse_division(child, ctx))
        else:
            _note_unhandled(fragment, f"debate/{tag}")
    return proceeding


def parse_daily(
    content: bytes,
    *,
    source_url: str | None = None,
    retrieved_at: dt.datetime | None = None,
) -> Fragment:
    """Parse one federal daily uniXML document into a canonical fragment."""
    root = etree.fromstring(content)
    if _local(root) != "hansard":
        raise ValueError(f"expected <hansard> root, got <{root.tag}>")

    header = root.find("session.header")
    if header is None:
        raise ValueError("daily has no <session.header>")
    date_text = (header.findtext("date") or "").strip()
    date = dt.date.fromisoformat(date_text[:10])
    chamber = _clean(header.findtext("chamber") or "")
    house = _HOUSE_MAP.get(chamber.lower(), chamber or None)
    proof = (header.findtext("proof") or "").strip()

    def _num(tag: str) -> int | None:
        value = (header.findtext(tag) or "").strip()
        return int(value) if value.isdigit() else None

    fragment = Fragment(
        fragment_id=deterministic_id("au", date.isoformat(), house or ""),
        jurisdiction=Jurisdiction.AU,
        schema_version=root.get("version"),
        date=date,
        house=house,
        parliament_num=_num("parliament.no"),
        session_num=_num("session.no"),
        review_stage=ReviewStage.UNCORRECTED if proof == "1" else ReviewStage.PUBLISHED,
        lang="en-AU",
        source_url=source_url,
        retrieved_at=retrieved_at,
    )

    ctx = _Ctx(date)
    xscript = root.find("chamber.xscript")
    if xscript is None:
        return fragment
    for child in xscript:
        tag = _local(child)
        if tag == "business.start":
            fragment.texts.extend(_body_paras(child.find("body"), ctx, len(fragment.texts)))
        elif tag == "debate":
            fragment.proceedings.append(_parse_debate(child, ctx, fragment))
        elif tag == "adjournment":
            # adjournment wraps debate-like content
            fragment.proceedings.append(_parse_debate(child, ctx, fragment))
        else:
            _note_unhandled(fragment, f"chamber.xscript/{tag}")
    return fragment

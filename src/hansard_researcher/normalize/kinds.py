"""Contribution-kind inference for lead-turn-only sources (WA/SA).

The Hansard_1_0-family producers stamp ``kind`` only on the *lead* turn of an
exchange (verified against the full WA archive: raw kind matches silver
exactly; 32,464 of WA's 32,998 untyped talkers are followers). The
conversational tail is recoverable from the typed turns around it:

- SA question subjects type the asker (``kind="question"``) and the
  minister's first turns (``kind="answer"``); the minister's continuations
  and the asker's supplementaries follow untyped.
- WA debate subproceedings/clauses type the lead speech; the same speaker's
  later turns follow untyped.

One rule, deliberately narrow: **an untyped turn inherits the kind of the
same member's earlier typed turn in the same container** (subject,
subproceeding, clause or division talker list). Members with no typed turn in
the container — the chair's interventions, other members' interjections in a
debate — stay honestly null: speech-vs-interjection cannot be told from
structure, and guessing would poison the substantive-turn analytics.
Inferred kinds carry ``extensions["kind_inferred"]`` so derived counts can
always separate source markup from inference. Fully-typed sources (AU/NSW
element names) pass through untouched.
"""

from __future__ import annotations

from collections.abc import Iterator

from hansard_researcher.model.canonical import Fragment, Talker, TalkerKind

#: kinds that propagate to the same member's untyped follow-on turns.
#: INTERJECTION deliberately doesn't — one heckle shouldn't type a member's
#: later substantive turns.
_PROPAGATES = (TalkerKind.SPEECH, TalkerKind.QUESTION, TalkerKind.ANSWER)


def _containers(fragment: Fragment) -> Iterator[list[Talker]]:
    """Each talker list where an exchange plays out, innermost included."""
    for proc in fragment.proceedings:
        yield proc.talkers
        for subj in proc.subjects:
            yield subj.talkers
            for div in subj.divisions:
                yield div.talkers
            for sub in subj.subproceedings:
                yield sub.talkers
                for div in sub.divisions:
                    yield div.talkers
                for clause in sub.clauses:
                    yield clause.talkers
                    for div in clause.divisions:
                        yield div.talkers
            for clause in subj.clauses:
                yield clause.talkers
                for div in clause.divisions:
                    yield div.talkers


def apply_kind_inference(fragment: Fragment) -> int:
    """Type untyped follow-on turns from the same member's lead turn.

    Mutates in place; returns the number of talkers inferred.
    """
    inferred = 0
    for talkers in _containers(fragment):
        last_kind: dict[str, TalkerKind] = {}
        for talker in sorted(talkers, key=lambda t: t.document_order):
            if talker.kind is not None:
                if talker.kind in _PROPAGATES and talker.member_source_id:
                    last_kind[talker.member_source_id] = talker.kind
                continue
            if talker.extensions.get("kind"):
                continue  # unknown source kind (e.g. petition) — not ours to type
            member = talker.member_source_id
            if member and member in last_kind:
                talker.kind = last_kind[member]
                talker.extensions["kind_inferred"] = "same-member"
                inferred += 1
    return inferred

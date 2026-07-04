"""Kind inference: same-member propagation from lead turns, nothing else."""

import datetime as dt

from hansard_researcher.model.canonical import (
    Fragment,
    Jurisdiction,
    Proceeding,
    Subject,
    Talker,
    TalkerKind,
)
from hansard_researcher.normalize.kinds import apply_kind_inference


def _talker(order: int, member: str | None, kind: TalkerKind | None = None) -> Talker:
    return Talker(document_order=order, member_source_id=member, kind=kind)


def _day(talkers) -> Fragment:
    return Fragment(
        fragment_id="test-day",
        jurisdiction=Jurisdiction.SA,
        date=dt.date(2008, 10, 29),
        house="House of Assembly",
        proceedings=[
            Proceeding(
                document_order=0,
                subjects=[Subject(document_order=0, talkers=talkers)],
            )
        ],
    )


def test_qa_exchange_infers_supplementaries_and_continuations():
    """The SA question-time shape: asker typed question, minister typed
    answer, then both continue untyped; the chair never gets typed."""
    day = _day(
        [
            _talker(1, "546", TalkerKind.QUESTION),
            _talker(2, "1806", TalkerKind.ANSWER),
            _talker(3, "627"),  # the chair — no typed turn, stays null
            _talker(4, "546"),  # supplementary
            _talker(5, "1806"),  # answer continuation
        ]
    )
    assert apply_kind_inference(day) == 2
    talkers = day.proceedings[0].subjects[0].talkers
    assert talkers[2].kind is None and "kind_inferred" not in talkers[2].extensions
    assert talkers[3].kind is TalkerKind.QUESTION
    assert talkers[3].extensions["kind_inferred"] == "same-member"
    assert talkers[4].kind is TalkerKind.ANSWER


def test_debate_followers_by_other_members_stay_null():
    """The WA debate shape: only the lead speaker's own continuations are
    typed; another member's untyped turn is speech-vs-interjection ambiguous."""
    day = _day(
        [
            _talker(1, "lead", TalkerKind.SPEECH),
            _talker(2, "other"),  # ambiguous — stays null
            _talker(3, "lead"),  # the lead speaker resuming
        ]
    )
    assert apply_kind_inference(day) == 1
    talkers = day.proceedings[0].subjects[0].talkers
    assert talkers[1].kind is None
    assert talkers[2].kind is TalkerKind.SPEECH


def test_interjections_do_not_propagate():
    """One heckle must not type a member's later substantive turns."""
    day = _day(
        [
            _talker(1, "m1", TalkerKind.INTERJECTION),
            _talker(2, "m1"),
        ]
    )
    assert apply_kind_inference(day) == 0
    assert day.proceedings[0].subjects[0].talkers[1].kind is None


def test_unknown_source_kinds_are_not_ours_to_type():
    """A talker whose source kind fell to extensions (petition) stays put."""
    petition = _talker(2, "m1")
    petition.extensions["kind"] = "petition"
    day = _day([_talker(1, "m1", TalkerKind.SPEECH), petition])
    assert apply_kind_inference(day) == 0
    assert petition.kind is None
    assert "kind_inferred" not in petition.extensions


def test_inference_is_container_scoped():
    """A kind never leaks across subjects: the same member untyped in the
    next subject stays null."""
    subject_a = Subject(
        document_order=0, talkers=[_talker(1, "m1", TalkerKind.QUESTION)]
    )
    subject_b = Subject(document_order=2, talkers=[_talker(3, "m1")])
    day = Fragment(
        fragment_id="test-day",
        jurisdiction=Jurisdiction.SA,
        date=dt.date(2008, 10, 29),
        house="House of Assembly",
        proceedings=[Proceeding(document_order=0, subjects=[subject_a, subject_b])],
    )
    assert apply_kind_inference(day) == 0
    assert subject_b.talkers[0].kind is None

from __future__ import annotations

import datetime as dt

import pytest

from hansard_researcher.model.canonical import (
    Division,
    DivisionResult,
    DivisionVote,
    Fragment,
    Jurisdiction,
    Proceeding,
    ReviewStage,
    Subject,
    Talker,
    TalkerKind,
    TalkerRole,
    TextPara,
    VoteValue,
)
from hansard_researcher.model.ids import deterministic_id


@pytest.fixture
def synthetic_fragment() -> Fragment:
    """A small, fully synthetic fragment — no real Hansard text (see LICENSES-DATA.md)."""
    return Fragment(
        fragment_id=deterministic_id("test", "2026-03-04", "lh"),
        jurisdiction=Jurisdiction.WA,
        source_doc_id="test-0001",
        schema_version="1.0",
        name="Synthetic Assembly",
        date=dt.date(2026, 3, 4),
        house="Synthetic Assembly",
        parliament_num=41,
        session_num=1,
        review_stage=ReviewStage.UNCORRECTED,
        start_page="1",
        proceedings=[
            Proceeding(
                uid="p1",
                name="Questions Without Notice",
                document_order=0,
                subjects=[
                    Subject(
                        uid="s1",
                        name="Widget Regulation",
                        document_order=1,
                        talkers=[
                            Talker(
                                uid="t1",
                                document_order=2,
                                member_source_id="m-100",
                                name="Ms Example",
                                role=TalkerRole.MEMBER,
                                kind=TalkerKind.QUESTION,
                                party="Example Party",
                                electorate="Testville",
                                texts=[
                                    TextPara(
                                        document_order=3,
                                        para_index=0,
                                        raw_text="Will the minister regulate widgets?",
                                        clean_text="Will the minister regulate widgets?",
                                    )
                                ],
                            ),
                            Talker(
                                uid="t2",
                                document_order=4,
                                member_source_id="m-200",
                                name="Mr Sample",
                                role=TalkerRole.MINISTER,
                                kind=TalkerKind.ANSWER,
                                portfolios=["Widgets"],
                                texts=[
                                    TextPara(
                                        document_order=5,
                                        para_index=0,
                                        raw_text="Yes.",
                                        clean_text="Yes.",
                                    )
                                ],
                            ),
                        ],
                        divisions=[
                            Division(
                                uid="d1",
                                document_order=6,
                                result=DivisionResult.AYES,
                                ayes_count=1,
                                noes_count=1,
                                votes=[
                                    DivisionVote(
                                        member_source_id="m-100",
                                        member_name="Ms Example",
                                        vote=VoteValue.AYES,
                                        teller=True,
                                    ),
                                    DivisionVote(
                                        member_source_id="m-200",
                                        member_name="Mr Sample",
                                        vote=VoteValue.NOES,
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
    )

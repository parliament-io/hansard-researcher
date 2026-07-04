"""Curated stage vocabulary — YAML validity and loader invariants."""

from __future__ import annotations

from hansard_researcher.reference.stages import STAGE_ORDER, load_stage_vocab


def test_vocab_loads_all_live_jurisdictions():
    rows = load_stage_vocab()
    assert {r["jurisdiction"] for r in rows} == {"au", "nsw", "sa", "wa"}
    assert all(r["stage"] in STAGE_ORDER for r in rows)
    assert all(r["name_lower"] == r["name_lower"].lower() for r in rows)


def test_no_duplicate_names_within_a_jurisdiction():
    rows = load_stage_vocab()
    keys = [(r["jurisdiction"], r["name_lower"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_stage_order_reflects_progression():
    rows = {(r["jurisdiction"], r["name_lower"]): r["stage_order"] for r in load_stage_vocab()}
    # second reading precedes committee precedes third reading, everywhere mapped
    assert rows[("sa", "second reading")] < rows[("sa", "committee stage")]
    assert rows[("sa", "committee stage")] < rows[("sa", "third reading")]
    # NSW LA's "Agreement in Principle" is its second-reading equivalent
    assert rows[("nsw", "agreement in principle")] == rows[("nsw", "second reading")]

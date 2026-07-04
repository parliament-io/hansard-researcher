from pathlib import Path

import duckdb
import pytest

from parlhansard.aggregate.cubes import GOLD_QUERIES, build_db, build_gold
from parlhansard.model.canonical import Jurisdiction
from parlhansard.normalize.canonical_xml import parse_extract, stitch_daily
from parlhansard.normalize.silver import write_silver

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def gold(tmp_path):
    extracts = [
        parse_extract(
            (FIXTURES / f"extract_{i:04d}.xml").read_bytes(),
            jurisdiction=Jurisdiction.WA,
            extract_index=i,
        )
        for i in (1, 2)
    ]
    write_silver([stitch_daily(extracts)], tmp_path / "silver")
    counts = build_gold(tmp_path / "silver", tmp_path / "gold")
    return tmp_path / "gold", counts


def _q(gold_dir: Path, sql: str):
    con = duckdb.connect()
    con.execute(f"set file_search_path='{gold_dir.as_posix()}'")
    return con.execute(sql)


def test_all_cubes_written(gold):
    gold_dir, counts = gold
    assert set(counts) == set(GOLD_QUERIES)
    for name in GOLD_QUERIES:
        assert (gold_dir / f"{name}.parquet").exists()


def test_member_activity(gold):
    gold_dir, _ = gold
    rows = _q(
        gold_dir,
        "select member_source_id, questions, answers, speeches, division_votes,"
        " gave_first_speech from 'member_activity.parquet' order by member_source_id",
    ).fetchall()
    by_id = {r[0]: r[1:] for r in rows}
    assert by_id["m-100"] == (1, 0, 0, 1, False)  # asked 1 question, voted once
    assert by_id["m-200"] == (0, 1, 0, 1, False)  # answered, voted
    assert by_id["m-300"] == (0, 0, 1, 1, True)   # first speech + vote


def test_qa_pairing(gold):
    gold_dir, _ = gold
    (row,) = _q(
        gold_dir,
        "select question_member, answer_member, answered from 'qa_pairs.parquet'",
    ).fetchall()
    assert row == ("Ms Example", "Mr Sample", True)


def test_subject_occurrence(gold):
    gold_dir, _ = gold
    rows = _q(
        gold_dir,
        "select subject_name, talker_turns, divisions, bill_names, extract_index"
        " from 'subject_occurrence.parquet' order by extract_index",
    ).fetchall()
    assert rows[0][0] == "Widget Regulation"
    assert rows[0][1] == 2  # question + answer
    assert rows[1] == ("Gadget Standards Bill", 1, 1, "Gadget Standards Bill 2026", 2)


def test_division_summary_and_votes(gold):
    gold_dir, _ = gold
    (division,) = _q(
        gold_dir,
        "select subject_name, result, ayes_count, noes_count, margin, recorded_votes"
        " from 'division_summary.parquet'",
    ).fetchall()
    assert division == ("Gadget Standards Bill", "ayes", 2, 1, 1, 3)
    votes = _q(
        gold_dir,
        "select member_name, voted_with_result from 'division_votes_detail.parquet'"
        " order by member_name",
    ).fetchall()
    assert dict(votes) == {"Ms Example": True, "Dr Newcomer": True, "Mr Sample": False}


def test_sitting_days(gold):
    gold_dir, _ = gold
    (day,) = _q(
        gold_dir,
        "select date, duration_minutes, subjects, distinct_speakers, divisions"
        " from 'sitting_days.parquet'",
    ).fetchall()
    import datetime as dt

    assert day == (dt.date(2026, 3, 4), 480, 2, 3, 1)


def test_gold_contains_no_hansard_prose(gold):
    """Licensing invariant: gold cubes must never carry text columns."""
    gold_dir, _ = gold
    con = duckdb.connect()
    for parquet in gold_dir.glob("*.parquet"):
        columns = [
            r[0]
            for r in con.execute(
                f"select name from parquet_schema('{parquet.as_posix()}')"
            ).fetchall()
        ]
        assert not {"raw_text", "clean_text"} & set(columns), parquet.name


def test_register_backfills_nsw_division_names_and_party(tmp_path):
    """NSW division votes come with blank names + no party; the member
    register fills both in gold (source values always win when present)."""
    from parlhansard.normalize.nsw_xml import parse_nsw_fragment
    from parlhansard.reference.register import member_id, write_register

    fragment_xml = (FIXTURES / "nsw_fragment_division.xml").read_bytes()
    daily = stitch_daily([parse_nsw_fragment(fragment_xml, doc_id="TEST-DIV-0001")])
    write_silver([daily], tmp_path / "silver")
    write_register(
        [
            {
                "member_id": member_id("nsw", "81"),
                "source_member_id": "81",
                "display_name": "Kevin Anderson",
                "party_name": "The National Party",
                "is_current": True,
                "jurisdiction": "nsw",
            }
        ],
        tmp_path / "reference",
    )
    build_gold(tmp_path / "silver", tmp_path / "gold", tmp_path / "reference")

    votes = dict(
        _q(
            tmp_path / "gold",
            "select member_source_id, member_name from 'division_votes_detail.parquet'",
        ).fetchall()
    )
    assert len(votes) == 7
    assert votes["81"] == "Kevin Anderson"  # register fills the blank name
    assert votes["28"] is None  # not in this register -> stays null

    (party,) = _q(
        tmp_path / "gold",
        "select party from 'division_votes_detail.parquet' where member_source_id = '81'",
    ).fetchone()
    assert party == "The National Party"

    # a division-only member (never speaks) still gets a name in member_activity
    activity = dict(
        _q(
            tmp_path / "gold",
            "select member_source_id, member_name from 'member_activity.parquet'",
        ).fetchall()
    )
    assert activity["81"] == "Kevin Anderson"


def test_bill_journey_and_bills_cubes(tmp_path):
    """A bill-shaped NSW subject yields a journey row with a canonical stage
    (via the curated vocabulary) and a one-row bills summary."""
    from parlhansard.normalize.nsw_xml import parse_nsw_fragment

    fragment_xml = (FIXTURES / "nsw_fragment_division.xml").read_bytes()
    daily = stitch_daily([parse_nsw_fragment(fragment_xml, doc_id="TEST-DIV-0001")])
    write_silver([daily], tmp_path / "silver")
    build_gold(tmp_path / "silver", tmp_path / "gold")

    (journey,) = _q(
        tmp_path / "gold",
        "select bill_name, house, stage_labels, furthest_stage, divisions,"
        " division_results from 'bill_journey.parquet'",
    ).fetchall()
    assert journey[0] == "Synthetic Memorial Bill 2025"
    assert journey[1] == "Legislative Assembly"
    assert journey[2] == "Consideration In Detail"
    assert journey[3] == "committee"  # vocabulary match is case-insensitive
    assert (journey[4], journey[5]) == (1, "noes")

    (bill,) = _q(
        tmp_path / "gold",
        "select bill_key, house_names, latest_stage, divisions from 'bills.parquet'",
    ).fetchall()
    assert bill == ("synthetic memorial bill 2025", "Legislative Assembly", "committee", 1)


def test_theme_cubes_from_enriched_assignments(tmp_path):
    """The C# aggregator's theme cube set, fed by 'enrich themes' output:
    a themed NSW bill subject with a division populates all six cubes."""
    from parlhansard.enrich.themes import classify_themes
    from parlhansard.normalize.nsw_xml import parse_nsw_fragment
    from parlhansard.reference.themes import Theme
    from test_enrich import FakeEmbedder

    taxonomy = [
        Theme("en-AU", 1, "memorials", "Memorials", "Synthetic Memorial war memorial bills"),
        Theme("en-AU", 1, "detail-stages", "Detail Stages", "Consideration In Detail Bills"),
    ]
    fragment_xml = (FIXTURES / "nsw_fragment_division.xml").read_bytes()
    daily = stitch_daily([parse_nsw_fragment(fragment_xml, doc_id="TEST-DIV-0001")])
    write_silver([daily], tmp_path / "silver")
    classify_themes(
        tmp_path, "nsw", engine="embedding", model="fake/embed-v1", provider="test",
        embedder=FakeEmbedder(), themes=taxonomy, min_score=0.01, log=lambda *_: None,
    )
    build_gold(
        tmp_path / "silver", tmp_path / "gold",
        enriched_dir=tmp_path / "enriched",
    )

    by_week = _q(
        tmp_path / "gold",
        "select theme_id, iso_year, subject_occurrences from 'theme_by_week.parquet'"
        " order by theme_id",
    ).fetchall()
    assert {r[0] for r in by_week} == {"memorials", "detail-stages"}
    assert all(r[1] == 2025 for r in by_week)

    (pair,) = _q(
        tmp_path / "gold",
        "select theme_id_a, theme_id_b, cooccurrences from 'theme_cooccurrence.parquet'",
    ).fetchall()
    assert pair == ("detail-stages", "memorials", 1)  # ordered a < b

    (bill_link,) = _q(
        tmp_path / "gold",
        "select bill_name, theme_id from 'bill_theme_link.parquet' where theme_id = 'memorials'",
    ).fetchall()
    assert bill_link[0] == "Synthetic Memorial Bill 2025"

    votes = _q(
        tmp_path / "gold",
        "select member_source_id, vote, votes from 'member_vote_by_theme.parquet'"
        " where theme_id = 'memorials' order by member_source_id",
    ).fetchall()
    assert ("81", "AYES", 1) in votes and ("28", "NOES", 1) in votes

    # the division fixture has no speaking turns -> no member_theme_rank rows,
    # and every subject got a theme -> no candidates
    for empty_cube in ("member_theme_rank", "theme_candidates"):
        (count,) = _q(
            tmp_path / "gold", f"select count(*) from '{empty_cube}.parquet'"
        ).fetchone()
        assert count == 0, empty_cube


def test_theme_cubes_empty_without_enrichment(gold):
    """Tier 1 invariant: no provider, no themes — cubes exist but are empty."""
    gold_dir, counts = gold
    for cube in (
        "theme_by_week", "theme_cooccurrence", "member_theme_rank",
        "bill_theme_link", "member_vote_by_theme", "theme_candidates",
    ):
        assert counts[cube] == 0
        assert (gold_dir / f"{cube}.parquet").exists()


def test_build_db(gold, tmp_path):
    gold_dir, _ = gold
    out = tmp_path / "hansard.duckdb"
    tables = build_db(gold_dir, out)
    assert set(tables) == set(GOLD_QUERIES)
    con = duckdb.connect(str(out), read_only=True)
    (count,) = con.execute("select count(*) from member_activity").fetchone()
    assert count == 3

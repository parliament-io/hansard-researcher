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


def test_build_db(gold, tmp_path):
    gold_dir, _ = gold
    out = tmp_path / "hansard.duckdb"
    tables = build_db(gold_dir, out)
    assert set(tables) == set(GOLD_QUERIES)
    con = duckdb.connect(str(out), read_only=True)
    (count,) = con.execute("select count(*) from member_activity").fetchone()
    assert count == 3

from pathlib import Path

import duckdb
import pytest

from hansard_researcher.model.canonical import Jurisdiction
from hansard_researcher.normalize.canonical_xml import parse_extract, stitch_daily
from hansard_researcher.normalize.silver import fragment_rows, write_silver

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def daily():
    extracts = [
        parse_extract(
            (FIXTURES / f"extract_{i:04d}.xml").read_bytes(),
            jurisdiction=Jurisdiction.WA,
            extract_index=i,
        )
        for i in (1, 2)
    ]
    return stitch_daily(extracts)


def test_row_counts(daily):
    rows = fragment_rows(daily)
    assert len(rows["fragments"]) == 1
    assert len(rows["proceedings"]) == 1
    assert len(rows["subjects"]) == 2
    assert len(rows["talkers"]) == 3
    assert len(rows["divisions"]) == 1
    assert len(rows["division_votes"]) == 3
    assert len(rows["bill_refs"]) == 1
    assert len(rows["subproceedings"]) == 1
    # texts: proceeding heading (deduped) + 2 subject headings + 4 talker texts + 1 division text
    assert len(rows["texts"]) == 8


def test_rows_are_deterministic(daily):
    first = fragment_rows(daily)
    second = fragment_rows(daily)
    assert first == second


def test_parent_ids_link(daily):
    rows = fragment_rows(daily)
    subject_ids = {r["subject_id"] for r in rows["subjects"]}
    for talker in rows["talkers"]:
        parent_keys = [
            talker[k]
            for k in ("proceeding_id", "subject_id", "subproceeding_id", "clause_id", "division_id")
            if talker.get(k)
        ]
        assert parent_keys, "every talker row must link to a container"
    for vote in rows["division_votes"]:
        assert vote["division_id"] == rows["divisions"][0]["division_id"]
    assert {r["subject_id"] for r in rows["bill_refs"]} <= subject_ids


def test_write_silver_and_query_with_duckdb(daily, tmp_path):
    counts = write_silver([daily], tmp_path)
    assert counts["talkers"] == 3

    con = duckdb.connect()
    talkers = con.execute(
        f"select kind, count(*) from read_parquet('{tmp_path.as_posix()}/talkers/**/*.parquet', "
        "hive_partitioning=1) group by kind order by kind"
    ).fetchall()
    assert dict(talkers) == {"answer": 1, "question": 1, "speech": 1}


def test_pass_provenance_reaches_silver(daily, tmp_path):
    """The day-pass flags (kind inference / clock provenance) must survive
    the parquet roundtrip — they are what separates source markup from
    inference downstream."""
    from hansard_researcher.normalize.clock import _iter_talkers
    from hansard_researcher.normalize.kinds import apply_kind_inference

    talkers = sorted(_iter_talkers(daily), key=lambda t: t.document_order)
    talkers[0].extensions["time_source"] = "clock"
    talkers[0].extensions["clock_rolled"] = "1"
    talkers[1].kind = None  # same member as a typed lead? force inference path
    talkers[1].member_source_id = talkers[0].member_source_id
    apply_kind_inference(daily)
    write_silver([daily], tmp_path)

    con = duckdb.connect()
    (clock_rows, inferred_rows) = con.execute(
        f"select count(*) filter (time_source = 'clock' and clock_rolled), "
        f"count(*) filter (kind_inferred) "
        f"from read_parquet('{tmp_path.as_posix()}/talkers/**/*.parquet', hive_partitioning=1)"
    ).fetchone()
    assert clock_rows == 1
    assert inferred_rows == 1

    votes = con.execute(
        f"select vote, count(*) from read_parquet('{tmp_path.as_posix()}/division_votes"
        "/**/*.parquet', hive_partitioning=1) group by vote order by vote"
    ).fetchall()
    assert dict(votes) == {"AYES": 2, "NOES": 1}


def test_two_houses_same_date_both_survive(daily, tmp_path):
    """Regression: house must be in the partition key — both chambers usually
    sit the same date, and a (jurisdiction, date) partition would let the
    second house's write delete the first's rows."""
    other_house = daily.model_copy(deep=True)
    other_house.house = "Synthetic Council"
    other_house.fragment_id = daily.fragment_id + "-uh"
    write_silver([daily], tmp_path)
    write_silver([other_house], tmp_path)
    con = duckdb.connect()
    rows = con.execute(
        f"select house, count(*) from read_parquet('{tmp_path.as_posix()}/fragments/**/*.parquet', "
        "hive_partitioning=1) group by house order by house"
    ).fetchall()
    assert rows == [("Synthetic Assembly", 1), ("Synthetic Council", 1)]


def test_rewrite_day_is_idempotent(daily, tmp_path):
    write_silver([daily], tmp_path)
    write_silver([daily], tmp_path)  # delete_matching replaces the partition
    con = duckdb.connect()
    (count,) = con.execute(
        f"select count(*) from read_parquet('{tmp_path.as_posix()}/fragments/**/*.parquet', "
        "hive_partitioning=1)"
    ).fetchone()
    assert count == 1

"""SA member register builder — offline normalization over synthetic snapshots."""

from __future__ import annotations

import datetime as dt
import json

import pyarrow.dataset as ds
import pytest

from hansard_researcher.reference.register import member_id, write_register
from hansard_researcher.reference.sa import _parse_date, build_rows, fetch_raw

CURRENT = [
    {
        "pm_Id": 5413,
        "pm_FirstName": "Emily",
        "pm_LastName": "Bourke",
        "pm_OtherNames": "Sarah",
        "pm_Title": "Hon",
        "pm_DateOfBirth": "1975-01-02T00:00:00",
        "mb_ElectedDate": "Mar 17 2018 12:00AM",
        "pm_ArchiveDate": None,
        "pm_Deceased": None,
        "electorate": None,
        "houseName": "Legislative Council",
        "pp_name": "Australian Labor Party",
    },
]
FORMER = [
    {
        "pm_Id": 2483,
        "pm_FirstName": "Roy ",  # trailing space appears in the real feed
        "pm_LastName": "Abbott",
        "pm_OtherNames": "Kitto",
        "pm_Title": "Hon",
        "pm_DateOfBirth": "1927-08-30T00:00:00",
        "mb_ElectedDate": "Jul 12 1975 12:00AM",
        "pm_ArchiveDate": "1989-11-24T00:00:00",
        "pm_Deceased": True,
        "electorate": "Spence",
        "houseName": "House of Assembly",
        "pp_name": "Australian Labor Party",
    },
    # also present in current: the current row must win
    {
        "pm_Id": 5413,
        "pm_FirstName": "Emily",
        "pm_LastName": "Bourke",
        "houseName": "Legislative Council",
        "pp_name": "Old Party",
        "mb_ElectedDate": None,
    },
]


@pytest.fixture
def raw_dir(tmp_path):
    raw = tmp_path / "reference" / "raw" / "sa"
    raw.mkdir(parents=True)
    (raw / "members_current.json").write_text(json.dumps(CURRENT), encoding="utf-8")
    (raw / "members_former.json").write_text(json.dumps(FORMER), encoding="utf-8")
    (raw / "meta.json").write_text(
        json.dumps({"retrieved_at": "2026-07-04T00:00:00+00:00"}), encoding="utf-8"
    )
    return raw


def test_parse_both_source_date_shapes():
    assert _parse_date("1989-11-24T00:00:00") == dt.date(1989, 11, 24)
    assert _parse_date("Jul 12 1975 12:00AM") == dt.date(1975, 7, 12)
    assert _parse_date(None) is None
    assert _parse_date("  ") is None
    assert _parse_date("unknown") is None


def test_build_rows_normalizes_and_dedupes(raw_dir):
    rows = {r["source_member_id"]: r for r in build_rows(raw_dir)}
    assert len(rows) == 2

    abbott = rows["2483"]
    assert abbott["display_name"] == "Roy Abbott"  # whitespace cleaned
    assert abbott["house"] == "House of Assembly"  # verbatim silver house name
    assert abbott["elected_date"] == dt.date(1975, 7, 12)
    assert abbott["archived_date"] == dt.date(1989, 11, 24)
    assert (abbott["is_current"], abbott["deceased"]) == (False, True)
    assert abbott["member_id"] == member_id("sa", "2483")  # deterministic

    bourke = rows["5413"]  # in both lists: current row wins
    assert bourke["is_current"] is True
    assert bourke["party_name"] == "Australian Labor Party"
    assert bourke["elected_date"] == dt.date(2018, 3, 17)


def test_register_parquet_joins_on_member_source_id(raw_dir, tmp_path):
    count = write_register(build_rows(raw_dir), tmp_path / "reference")
    assert count == 2
    dataset = ds.dataset(
        tmp_path / "reference" / "members", format="parquet", partitioning="hive"
    )
    table = dataset.to_table()
    # join key is the Hansard talker id (pm_Id), as a string like silver
    assert set(table.column("source_member_id").to_pylist()) == {"2483", "5413"}
    assert set(table.column("jurisdiction").to_pylist()) == {"sa"}


def test_fetch_raw_snapshots_all_sources(tmp_path, monkeypatch):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["memberType"] in ("current", "former")
            return httpx.Response(200, json=CURRENT if body["memberType"] == "current" else FORMER)
        return httpx.Response(200, json={"memberContacts": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw = tmp_path / "raw" / "sa"
    fetch_raw(raw, client=client)
    for name in (
        "members_current.json",
        "members_former.json",
        "ha_contacts.json",
        "lc_contacts.json",
        "meta.json",
    ):
        assert (raw / name).exists(), name
    meta = json.loads((raw / "meta.json").read_text(encoding="utf-8"))
    assert dt.datetime.fromisoformat(meta["retrieved_at"]).tzinfo is not None

"""NSW member register builder — parsing over synthetic HTML snapshots."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from parlhansard.reference.nsw import build_rows, parse_current, parse_former_index
from parlhansard.reference.register import member_id

# mirrors the real all-members table: hidden data columns after the visible ones
CURRENT_HTML = b"""
<html><body><table>
  <tr><th>Name</th><th>Position</th><th>Contact Details</th><th>Photo</th>
      <th>House</th><th>Surname</th><th>Party</th><th>Gender</th>
      <th>Ministry</th><th>IsParliamentarySecretaries</th></tr>
  <tr>
    <td><a class="prl-name-link" href="/members/Pages/Member-details.aspx?pk=120">
        Aitchison,\n Jenny</a></td>
    <td><div>MP (Legislative Assembly)</div><div>Member for Maitland</div>
        <div>Minister for Roads</div></td>
    <td>Phone</td><td></td>
    <td>LA</td><td>Aitchison</td><td>Australian Labor Party</td><td>Female</td>
    <td>Minister for Roads</td><td>false</td>
  </tr>
  <tr>
    <td><a class="prl-name-link" href="/members/Pages/Member-details.aspx?pk=28">
        Sharpe, Penny</a></td>
    <td><div>MLC</div><div>Leader of the Government in the Legislative Council</div></td>
    <td></td><td></td>
    <td>LC</td><td>Sharpe</td><td>Australian Labor Party</td><td>Female</td>
    <td></td><td>false</td>
  </tr>
</table></body></html>
"""

FORMER_HTML = b"""
<html><body><table id="formerMembersTable">
  <tr><th>Name</th><th>Date of Birth</th><th>Status</th><th>Gender</th></tr>
  <tr>
    <td><a href="/members/formermembers/Pages/former-member-details.aspx?pk=17">
        Aplin, Mr Gregory John</a></td>
    <td>01/01/1950</td><td></td><td>Male</td>
  </tr>
  <tr>
    <td><a href="/members/formermembers/Pages/former-member-details.aspx?pk=381">
        A'BECKETT, Dr Arthur Martin</a></td>
    <td>01/01/1812</td><td>Deceased</td><td>Male</td>
  </tr>
  <!-- same person also listed as current: the current row must win -->
  <tr>
    <td><a href="/members/formermembers/Pages/former-member-details.aspx?pk=28">
        Sharpe, The Hon. Penny</a></td>
    <td></td><td></td><td>Female</td>
  </tr>
</table></body></html>
"""


def test_parse_current_members():
    rows = {r["source_member_id"]: r for r in parse_current(CURRENT_HTML)}
    assert set(rows) == {"120", "28"}

    aitchison = rows["120"]
    assert aitchison["display_name"] == "Jenny Aitchison"
    assert aitchison["house"] == "Legislative Assembly"  # LA -> silver house name
    assert aitchison["electorate"] == "Maitland"  # from the Position cell
    assert aitchison["party_name"] == "Australian Labor Party"
    assert aitchison["is_current"] is True

    sharpe = rows["28"]
    assert sharpe["house"] == "Legislative Council"
    assert sharpe["electorate"] is None  # LC members have no electorate


def test_parse_former_index_identity_only():
    rows = {r["source_member_id"]: r for r in parse_former_index(FORMER_HTML)}
    assert set(rows) == {"17", "381", "28"}

    aplin = rows["17"]
    assert aplin["display_name"] == "Gregory John Aplin"
    assert aplin["title"] == "Mr"
    assert aplin["date_of_birth"] == dt.date(1950, 1, 1)
    assert aplin["deceased"] is None
    # party is deliberately absent: NSW former-member party is free text
    assert "party_name" not in aplin

    abeckett = rows["381"]
    assert abeckett["title"] == "Dr"
    assert abeckett["deceased"] is True


@pytest.fixture
def raw_dir(tmp_path):
    raw = tmp_path / "reference" / "raw" / "nsw"
    raw.mkdir(parents=True)
    (raw / "all_members.html").write_bytes(CURRENT_HTML)
    (raw / "former_index_A.html").write_bytes(FORMER_HTML)
    (raw / "meta.json").write_text(
        json.dumps({"retrieved_at": "2026-07-04T00:00:00+00:00"}), encoding="utf-8"
    )
    return raw


def test_build_rows_current_wins_over_former(raw_dir):
    rows = {r["source_member_id"]: r for r in build_rows(raw_dir)}
    assert len(rows) == 4  # 120, 28, 17, 381

    sharpe = rows["28"]  # in both snapshots
    assert sharpe["is_current"] is True
    assert sharpe["party_name"] == "Australian Labor Party"

    assert rows["17"]["is_current"] is False
    for row in rows.values():
        assert row["jurisdiction"] == "nsw"
        assert row["member_id"] == member_id("nsw", row["source_member_id"])

"""Live payloads deviate from the XSD: counts as element text, no @vote."""

from hansard_researcher.model.canonical import DivisionResult, Jurisdiction
from hansard_researcher.normalize.canonical_xml import parse_extract

REAL_STYLE = b"""<?xml version="1.0" encoding="utf-8"?>
<hansard id="frag-x" tocId="" schemaVersion="4.0" xml:lang="en-AU">
  <name>Synthetic Assembly</name>
  <date date="2026-03-04T09:00:00+08:00" />
  <parliamentNum>42</parliamentNum>
  <sessionNum>1</sessionNum>
  <house>Synthetic Assembly</house>
  <reviewStage>published</reviewStage>
  <startPage num="1" />
  <proceeding uid="p1">
    <name>Bills</name>
    <subject uid="s1">
      <name>Test Bill</name>
      <division uid="d1">
        <ayesCount>12</ayesCount>
        <noesCount>34</noesCount>
        <pairsCount>0</pairsCount>
        <data>
          <member id="m1" vote="AYES" name="One, Member (Teller)" teller="true" />
          <member id="m2" vote="NOES" name="Two, Member" />
        </data>
      </division>
    </subject>
  </proceeding>
</hansard>
"""


def test_counts_as_text_content_and_derived_result():
    fragment = parse_extract(REAL_STYLE, jurisdiction=Jurisdiction.WA)
    division = fragment.proceedings[0].subjects[0].divisions[0]
    assert division.ayes_count == 12
    assert division.noes_count == 34
    assert division.pairs_count == 0
    assert division.result is DivisionResult.NOES
    assert division.extensions["result_derived"] == "true"


HISTORIC_STYLE = b"""<?xml version="1.0" encoding="utf-8"?>
<hansard id="frag-h" tocId="" schemaVersion="4.0" xml:lang="en-AU">
  <name>Synthetic Council</name>
  <date date="2015-12-08T09:00:00+10:30" />
  <parliamentNum>53</parliamentNum>
  <sessionNum>1</sessionNum>
  <house>Synthetic Council</house>
  <reviewStage>published</reviewStage>
  <startPage num="1" />
  <proceeding uid="p1">
    <name>Bills</name>
    <subject uid="s1">
      <name>Historic Bill</name>
      <division>
        <page num="2583" />
        <text id="t1">Ayes&amp;#x9;3</text>
        <text id="t2">Noes&amp;#x9;2</text>
        <text id="t3">Majority&amp;#x9;1</text>
        <text id="t4">
          <table>
            <rowtitle><cell colspan="3">AYES</cell></rowtitle>
            <row><cell>Darley, J.A.</cell><cell>Hood, D.G.E.</cell>
            <cell>Parnell, M.C. (teller)</cell></row>
          </table>
        </text>
        <text id="t5">
          <table>
            <rowtitle><cell colspan="3">NOES</cell></rowtitle>
            <row><cell>Gago, G.E. (teller)</cell><cell>Lucas, R.I.</cell><cell /></row>
          </table>
        </text>
        <text id="t6">
          <table>
            <rowtitle><cell colspan="3">PAIRS</cell></rowtitle>
            <row><cell>Franks, T.A.</cell><cell /><cell /></row>
          </table>
        </text>
        <text id="t7">Motion thus carried.</text>
      </division>
    </subject>
  </proceeding>
</hansard>
"""


def test_historic_presentational_division():
    """Pre-2026 SA/WA divisions: counts in text lines, votes in tables."""
    fragment = parse_extract(HISTORIC_STYLE, jurisdiction=Jurisdiction.SA)
    division = fragment.proceedings[0].subjects[0].divisions[0]
    assert division.ayes_count == 3
    assert division.noes_count == 2
    assert division.result is DivisionResult.AYES  # derived from counts
    votes = {(v.member_name, v.vote.value, v.teller) for v in division.votes}
    assert votes == {
        ("Darley, J.A.", "AYES", False),
        ("Hood, D.G.E.", "AYES", False),
        ("Parnell, M.C.", "AYES", True),
        ("Gago, G.E.", "NOES", True),
        ("Lucas, R.I.", "NOES", False),
        ("Franks, T.A.", "PAIRS", False),
    }
    # narrative texts kept; vote tables consumed as data, not prose
    clean = [t.clean_text for t in division.texts]
    assert "Motion thus carried." in clean
    assert not any("Darley" in c for c in clean)


def test_explicit_vote_attr_is_not_overridden():
    content = REAL_STYLE.replace(b'<division uid="d1">', b'<division uid="d1" vote="ayes">')
    fragment = parse_extract(content, jurisdiction=Jurisdiction.WA)
    division = fragment.proceedings[0].subjects[0].divisions[0]
    assert division.result is DivisionResult.AYES
    assert "result_derived" not in division.extensions

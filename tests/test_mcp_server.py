"""MCP server tool surface — ArchiveTools is plain-callable, no transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hansard_researcher.aggregate.cubes import build_gold
from hansard_researcher.mcp_server import ArchiveTools
from hansard_researcher.model.canonical import Jurisdiction
from hansard_researcher.normalize.canonical_xml import parse_extract, stitch_daily
from hansard_researcher.normalize.silver import write_silver

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def data_dir(tmp_path):
    extracts = [
        parse_extract(
            (FIXTURES / f"extract_{i:04d}.xml").read_bytes(),
            jurisdiction=Jurisdiction.WA,
            extract_index=i,
        )
        for i in (1, 2)
    ]
    write_silver([stitch_daily(extracts)], tmp_path / "silver")
    build_gold(tmp_path / "silver", tmp_path / "gold")
    return tmp_path


def test_list_tables_and_schema(data_dir):
    tools = ArchiveTools(data_dir)
    tables = tools.list_tables()
    assert "member_activity  [gold]" in tables
    assert "talkers  [silver/reference]" in tables  # served by default (local)
    schema = tools.table_schema("member_activity")
    assert "member_source_id" in schema and "division_votes" in schema
    assert "unknown table" in tools.table_schema("no_such_table")


def test_no_silver_mode_hides_full_text(data_dir):
    tables = ArchiveTools(data_dir, include_silver=False).list_tables()
    assert "texts" not in tables.split() and "member_activity  [gold]" in tables


def test_query_returns_json_lines(data_dir):
    tools = ArchiveTools(data_dir)
    out = tools.query(
        "select member_source_id, questions from member_activity order by 1"
    )
    rows = [json.loads(line) for line in out.splitlines()]
    assert {r["member_source_id"] for r in rows} == {"m-100", "m-200", "m-300"}

    limited = tools.query("select * from member_activity", limit=1)
    assert "truncated at 1 rows" in limited


def test_query_is_read_only(data_dir):
    tools = ArchiveTools(data_dir)
    assert tools.query("drop view member_activity").startswith("rejected")
    assert tools.query("select 1; select 2").startswith("rejected")
    assert tools.query("select nope from missing").startswith("query error")


def test_semantic_search_without_provider_is_instructive(data_dir, monkeypatch):
    for var in ("HANSARD_RESEARCHER_ENRICH_PROVIDER", "HANSARD_RESEARCHER_ENRICH_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    out = ArchiveTools(data_dir).semantic_search("water infrastructure")
    assert out.startswith("no enrichment provider")


def test_pipeline_status_tool(data_dir):
    status = json.loads(ArchiveTools(data_dir).pipeline_status())
    assert status["jurisdictions"]["wa"]["silver_house_days"] == 1


def test_build_server_registers_tools(data_dir):
    pytest.importorskip("mcp")
    from hansard_researcher.mcp_server import build_server

    server = build_server(data_dir)
    # FastMCP exposes registered tools synchronously via its tool manager
    names = {t.name for t in server._tool_manager.list_tools()}
    assert names == {
        "list_tables", "table_schema", "query", "semantic_search", "pipeline_status",
    }

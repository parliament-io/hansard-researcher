"""MCP server — the local archive as tools for AI agents (optional extra).

``hansard-researcher mcp`` runs a stdio Model Context Protocol server exposing:

- ``list_tables`` / ``table_schema`` — discover the data model
- ``query`` — read-only SQL over the gold cubes and (local-only) silver
  full-text tables, live against the Parquet on disk
- ``semantic_search`` — cosine search over enriched embeddings, prose
  hydrated from local silver (needs a configured enrichment provider)
- ``pipeline_status`` — the ``hansard-researcher status`` report as JSON

Install: ``uv sync --extra mcp`` (or ``pip install 'hansard-researcher[mcp]'``).
Register with an agent, e.g. ``claude mcp add hansard-researcher -- uv run
hansard-researcher mcp`` (this repo also ships ``.mcp.json``).

Everything is local: the server reads the data directory and never talks to
a network itself (semantic_search calls whatever enrichment provider the
user configured, exactly like ``hansard-researcher enrich search``). Silver full
text is exposed by default because MCP is a local surface — the licensing
stance (LICENSES-DATA.md) restricts *distribution*, not local analysis;
pass ``--no-silver`` to serve derived gold facts only.

The tool logic lives on :class:`ArchiveTools` (plain methods, unit-testable
without the mcp package); :func:`build_server` wraps it in FastMCP.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb

_READONLY = re.compile(r"^\s*(select|with|describe|show|summarize|explain)\b", re.IGNORECASE)
_MAX_ROWS = 1000

_INSTRUCTIONS = """\
hansard-researcher: analytics over Parliamentary Hansard for WA, SA, NSW and federal
Australia (~21 years, 5,200+ house-days). Grain to know: a sitting date has
one row per (jurisdiction, date, house). Gold cubes are derived facts
(publishable); silver tables carry the full debate text (local use only).
Start with list_tables, then query (DuckDB SQL). Member/party gaps in silver
are normal — the member register backfills them in the gold cubes.
"""


class ArchiveTools:
    """The MCP tool surface as plain methods over the data directory."""

    def __init__(self, data_dir: Path, include_silver: bool = True) -> None:
        self.data_dir = Path(data_dir)
        self.include_silver = include_silver
        self._connection: duckdb.DuckDBPyConnection | None = None

    def _con(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            from hansard_researcher.aggregate.cubes import _attach_reference, _attach_silver

            con = duckdb.connect()
            for parquet in sorted((self.data_dir / "gold").glob("*.parquet")):
                con.execute(
                    f"create or replace view {parquet.stem} as "
                    f"select * from '{parquet.as_posix()}'"
                )
            if self.include_silver:
                _attach_silver(con, self.data_dir / "silver")
            _attach_reference(
                con, self.data_dir / "reference", self.data_dir / "enriched"
            )
            self._connection = con
        return self._connection

    def _tables(self) -> list[str]:
        return [
            row[0]
            for row in self._con()
            .execute("select view_name from duckdb_views() where not internal order by 1")
            .fetchall()
            if not row[0].startswith("_")
        ]

    def list_tables(self) -> str:
        """List the queryable tables: gold cubes (derived facts) and, when
        served, the silver full-text tables. Table names are DuckDB views —
        query them directly by name."""
        from hansard_researcher.aggregate.cubes import GOLD_QUERIES

        lines = []
        gold = set(GOLD_QUERIES)
        for name in self._tables():
            tier = "gold" if name in gold else "silver/reference"
            lines.append(f"{name}  [{tier}]")
        return "\n".join(lines)

    def table_schema(self, table: str) -> str:
        """Column names and types for one table (use list_tables for names)."""
        if table not in self._tables():
            return f"unknown table {table!r} — see list_tables"
        rows = self._con().execute(f"describe {table}").fetchall()
        return "\n".join(f"{r[0]}  {r[1]}" for r in rows)

    def query(self, sql: str, limit: int = 100) -> str:
        """Run read-only DuckDB SQL over the archive (SELECT/WITH/DESCRIBE/
        SHOW/SUMMARIZE/EXPLAIN; one statement). Results return as JSON lines,
        capped at `limit` rows (max 1000)."""
        if not _READONLY.match(sql):
            return "rejected: read-only server — start with SELECT/WITH/DESCRIBE/SHOW/SUMMARIZE"
        if ";" in sql.rstrip().rstrip(";"):
            return "rejected: one statement per call"
        limit = max(1, min(int(limit), _MAX_ROWS))
        try:
            result = self._con().execute(sql)
            columns = [d[0] for d in result.description]
            rows = result.fetchmany(limit + 1)
        except duckdb.Error as exc:
            return f"query error: {exc}"
        truncated = len(rows) > limit
        lines = [
            json.dumps(dict(zip(columns, row, strict=True)), default=str, ensure_ascii=False)
            for row in rows[:limit]
        ]
        if truncated:
            lines.append(f"... truncated at {limit} rows (raise `limit` up to {_MAX_ROWS})")
        return "\n".join(lines) if lines else "(0 rows)"

    def semantic_search(self, query: str, k: int = 10, jurisdiction: str | None = None) -> str:
        """Semantic search over the enriched paragraph embeddings; prose is
        joined back from local silver. Needs the enrichment provider that
        produced the embeddings (HANSARD_RESEARCHER_ENRICH_* env)."""
        from hansard_researcher.enrich.providers import ProviderError, get_embedder, resolve_config
        from hansard_researcher.enrich.search import search

        try:
            config = resolve_config()
            query_vector = get_embedder(config).embed([query])[0]
        except ProviderError as exc:
            return f"no enrichment provider: {exc}"
        hits = search(
            self.data_dir, query_vector, config.embed_model, k=k, jurisdiction=jurisdiction
        )
        if not hits:
            return "no matches — has 'hansard-researcher enrich embed' run for this model?"
        return "\n".join(
            json.dumps(
                {
                    "score": round(hit.score, 3),
                    "jurisdiction": hit.jurisdiction,
                    "date": str(hit.date),
                    "house": hit.house,
                    "subject": hit.subject_name,
                    "speaker": hit.speaker,
                    "text": hit.text,
                },
                default=str,
                ensure_ascii=False,
            )
            for hit in hits
        )

    def pipeline_status(self) -> str:
        """Coverage of the local archive per pipeline stage (harvest ->
        normalize -> enrich per model -> gold), as JSON."""
        from hansard_researcher.aggregate.coverage import collect_status

        return json.dumps(collect_status(self.data_dir), indent=2, default=str)


def build_server(data_dir: Path, include_silver: bool = True):
    """FastMCP server wrapping :class:`ArchiveTools` (stdio via .run())."""
    from mcp.server.fastmcp import FastMCP

    tools = ArchiveTools(data_dir, include_silver=include_silver)
    server = FastMCP("hansard-researcher", instructions=_INSTRUCTIONS)
    for method in (
        tools.list_tables,
        tools.table_schema,
        tools.query,
        tools.semantic_search,
        tools.pipeline_status,
    ):
        server.tool()(method)
    return server

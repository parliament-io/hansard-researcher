"""Semantic search over paragraph embeddings.

Embeds the query with the same provider/model used for ``enrich embed``,
scores cosine similarity in DuckDB over the enriched vectors, and joins the
matching paragraphs back from **local** silver (results contain Hansard
text — local analysis only, never republish; see LICENSES-DATA.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb


@dataclass(frozen=True)
class Hit:
    score: float
    jurisdiction: str
    date: str
    house: str
    subject_name: str | None
    speaker: str | None
    text: str


def search(
    data_dir: Path,
    query_vector: list[float],
    model: str,
    *,
    k: int = 10,
    jurisdiction: str | None = None,
) -> list[Hit]:
    embeddings = (Path(data_dir) / "enriched" / "embeddings").as_posix()
    silver = Path(data_dir) / "silver"
    params: list[object] = [query_vector, model]
    jurisdiction_filter = ""
    if jurisdiction:
        jurisdiction_filter = "and jurisdiction = ?"
        params.append(jurisdiction)
    sql = f"""
        with hits as (
            select text_id, subject_id, jurisdiction, date, house,
                   list_cosine_similarity(embedding, ?::FLOAT[]) as score
            from read_parquet('{embeddings}/**/*.parquet', hive_partitioning = true)
            where model = ? {jurisdiction_filter}
            order by score desc
            limit {int(k)}
        )
        select
            h.score, h.jurisdiction, h.date, h.house,
            s.name       as subject_name,
            tk.name      as speaker,
            t.clean_text as text
        from hits h
        join read_parquet('{(silver / "texts").as_posix()}/**/*.parquet',
                          hive_partitioning = true) t using (text_id)
        left join read_parquet('{(silver / "subjects").as_posix()}/**/*.parquet',
                               hive_partitioning = true) s
               on h.subject_id = s.subject_id
        left join read_parquet('{(silver / "talkers").as_posix()}/**/*.parquet',
                               hive_partitioning = true) tk
               on t.talker_id = tk.talker_id
        order by h.score desc
    """
    rows = duckdb.connect().execute(sql, params).fetchall()
    return [Hit(*row) for row in rows]


def search_qdrant(
    data_dir: Path,
    query_vector: list[float],
    model: str,
    *,
    k: int = 10,
    jurisdiction: str | None = None,
    index=None,
) -> list[Hit]:
    """ANN search via Qdrant (see :mod:`parlhansard.enrich.qdrant`), then
    hydrate text/subject/speaker from local silver — prose never lives in
    the index."""
    from parlhansard.enrich.qdrant import QdrantIndex, collection_name

    index = index or QdrantIndex()
    results = index.search(
        collection_name(model), query_vector, k=k, jurisdiction=jurisdiction
    )
    if not results:
        return []
    score_by_id = {str(r["id"]): r["score"] for r in results}
    silver = Path(data_dir) / "silver"
    placeholders = ", ".join("?" for _ in score_by_id)
    sql = f"""
        select
            t.text_id, t.jurisdiction, t.date, t.house,
            s.name       as subject_name,
            tk.name      as speaker,
            t.clean_text as text
        from read_parquet('{(silver / "texts").as_posix()}/**/*.parquet',
                          hive_partitioning = true) t
        left join read_parquet('{(silver / "subjects").as_posix()}/**/*.parquet',
                               hive_partitioning = true) s
               on t.subject_id = s.subject_id
        left join read_parquet('{(silver / "talkers").as_posix()}/**/*.parquet',
                               hive_partitioning = true) tk
               on t.talker_id = tk.talker_id
        where t.text_id in ({placeholders})
    """
    rows = duckdb.connect().execute(sql, list(score_by_id)).fetchall()
    hits = [
        Hit(score_by_id[row[0]], row[1], str(row[2]), row[3], row[4], row[5], row[6])
        for row in rows
    ]
    return sorted(hits, key=lambda hit: hit.score, reverse=True)

---
license: cc-by-4.0
pretty_name: Parliamentary Hansard — Embeddings & Derived Analytics
language:
  - en
tags:
  - parliament
  - hansard
  - australia
  - embeddings
  - semantic-search
  - civic-tech
  - open-data
size_categories:
  - 1M<n<10M
---

# Hansard Embeddings & Derived Analytics

Text embeddings and derived analytics cubes covering the parliamentary
Hansard of four Australian jurisdictions: the Commonwealth (`au`), New
South Wales (`nsw`), South Australia (`sa`), and Western Australia (`wa`).

**No Hansard prose is in this dataset.** It ships vectors, join keys, and
derived metadata (subject headings, member names, parties, counts). The
official record remains with each parliament; to hydrate the text, run the
open-source harvester against the official sources yourself —
[hansard-researcher](https://github.com/parliament-io/hansard-researcher).

Over the last 20 years, some Parliaments have published their Hansard in a specific Hansard XML format. The complication at the moment is that you have to go to each Parliament to access their records. This dataset is derived from those XML sources, but the pipeline is designed to be agnostic to the source format. The harvester extracts the prose and metadata, the normalizer converts it into a common schema, and the enrichment step produces embeddings and derived analytics cubes. The cubes are designed to support research and analysis without ever needing to ship the prose itself.

## What's in the repo

```
embeddings/   one vector per text unit, hive-partitioned:
              model_slug=<slug>/jurisdiction=<j>/year=<yyyy>/part-*.parquet
gold/         derived analytics cubes (*.parquet), the join target for
              subject_id / member ids
qdrant/       (optional) Qdrant collection snapshot — restore and search
              without re-indexing
```

## Vector-space contract

Query vectors are only comparable if produced identically:

| | |
|---|---|
| Model | `nomic-embed-text` (137M) served via Ollama |
| Dimensions | 768 |
| Distance | cosine |
| Task prefix | **none** — embed queries with no `search_query:` prefix |

The `model_slug` hive partition versions the vector space. If the corpus is
ever re-embedded with a different model or serving stack, the new vectors
publish **alongside** under a new `model_slug` — different implementations
are not the same vector space, so never mix partitions in one index.

## `embeddings/` schema

One row per embedded text unit (e.g. "a contiguous spoken passage within a subject").

| column | type | notes |
|---|---|---|
| `text_id` | VARCHAR (UUID) | primary key of the text unit |
| `fragment_id` | VARCHAR (UUID) | the silver fragment the text came from |
| `talker_id` | VARCHAR (UUID) | speaker turn; joins `gold/qa_pairs.question_talker_id` / `.answer_talker_id`. ~84% populated — NULL is structural (procedural/chair text carries no speaker attribution) |
| `subject_id` | VARCHAR (UUID) | debate subject; joins the subject-grain gold cubes. ~99% populated — NULL is procedural preamble before the first subject |
| `model` | VARCHAR | raw model id (`nomic-embed-text`) |
| `provider` | VARCHAR | serving stack that produced the vector (`ollama`) |
| `dim` | INTEGER | 768 |
| `embedding` | FLOAT[] | the vector |
| `date` | DATE | sitting date |
| `house` | VARCHAR | chamber (or committee volume) |
| `jurisdiction` | VARCHAR | `au` / `nsw` / `sa` / `wa` (also a hive partition) |
| `model_slug` | VARCHAR | filesystem-safe model id (also a hive partition) |
| `year` | INTEGER | hive partition, derived from `date` |

## Join contract

The vectors are deliberately prose-free; the gold cubes make them usable:

- `subject_id` → `gold/subject_occurrence`, `gold/contributions`,
  `gold/qa_pairs`, `gold/division_summary`, `gold/division_votes_detail` —
  subject headings, who spoke, Q&A pairings, votes.
- `talker_id` → `gold/qa_pairs` question/answer talker ids.
- Member identity across cubes is `member_source_id` (per-jurisdiction
  official id) + `member_name`.
- `text_id` / `fragment_id` → resolve to prose **only** via a local run of
  the harvester + normalizer (silver layer). Prose never ships here.

Example — label a nearest-neighbour hit without any prose:

```sql
-- duckdb
SELECT e.text_id, s.subject_name, s.date, s.house, s.jurisdiction
FROM read_parquet('hf://datasets/parliament-data/hansard-embeddings/embeddings/**/*.parquet', hive_partitioning=1) e
JOIN read_parquet('hf://datasets/parliament-data/hansard-embeddings/gold/subject_occurrence.parquet') s
  USING (subject_id)
LIMIT 10;
```

## `gold/` cubes

These are as of 2026-07-06

| cube | rows | grain |
|---|---|---|
| `contributions` | 629,054 | member × subject × house-day: turns/speeches/questions/answers/interjections/words |
| `subject_occurrence` | 353,285 | subject × house-day: participation counts, first-spoken time, linked bills |
| `qa_pairs` | 135,677 | paired question & answer with members, parties, portfolios, word counts |
| `division_votes_detail` | 891,335 | individual member vote in each division |
| `division_summary` | 14,055 | division outcomes with ayes/noes/margin |
| `bills` | 7,339 | bill: houses, sitting span, latest stage, debate volume |
| `bill_journey` | 31,484 | bill × house-day: stages reached, debate/divisions that day |
| `bill_theme_link` | 36,885 | bill × theme association |
| `member_activity` | 1,255 | member career totals |
| `member_activity_by_week` | 98,417 | member × ISO week activity |
| `member_theme_rank` | 25,790 | member ranking within each theme |
| `member_vote_by_theme` | 19,042 | member vote tallies by theme |
| `theme_by_week` | 42,971 | theme prevalence per week |
| `theme_cooccurrence` | 1,288 | theme pair co-occurrence |
| `theme_share_by_jurisdiction` | 104 | theme share per jurisdiction |
| `theme_subject_names` | 1,040 | top subject names per theme |
| `theme_coverage` | 4 | classification coverage per jurisdiction |
| `theme_candidates` | 0 | low-confidence classifications flagged for curation (empty when the classifier places everything above the cutoff) |
| `sitting_days` | 5,243 | house-day: session, times, duration, volume |
| `pipeline_coverage` | 5,243 | per house-day provenance: harvested/normalized/embedded/themed |

Theme cubes carry `engine`/`model` columns — theme labels are themselves
model-derived; treat them as annotations, not ground truth.

Every cube is self-describing — read the column list straight off the
parquet:

```sql
-- duckdb
DESCRIBE SELECT * FROM read_parquet(
  'hf://datasets/parliament-data/hansard-embeddings/gold/contributions.parquet');
```

## Qdrant snapshot

`qdrant/` holds a collection snapshot for batteries-included semantic
search: restore it into a Qdrant instance and query — no re-indexing of
6.3M points. Point payloads carry `jurisdiction`, `date`, `house`,
`subject_id`, `talker_id`, plus citation metadata so search results cite
the official record without any prose: subject/debate context
(`subject_name`, `subject_uid`, `proceeding_name`, `subproceeding_name`,
`committee_name`, `bill_names`, `extract_index`), speaker (`speaker`,
`party`, `party_abbreviation`, `electorate`, `role`, `talker_kind`),
citation position (`text_kind`, `page_no`, `time_anchor` — ISO-8601 UTC),
sitting formalities (`parliament_num`, `session_num`, `review_stage`) and
provenance (`source_url`). Nulls are dropped per point, not stored;
speaker fields are structurally absent on procedural/chair text.

```bash
curl -X POST 'http://localhost:6333/collections/<name>/snapshots/upload' \
  -H 'Content-Type: multipart/form-data' \
  -F 'snapshot=@hansard-embeddings.snapshot'
```

## Provenance & licensing

- Derived from the official Hansard published by each parliament. The
  parliamentary record itself remains subject to each parliament's terms:

  | Jurisdiction | Official source | Terms on the record |
  |---|---|---|
  | Australia (Federal) | [parlinfo.aph.gov.au](https://parlinfo.aph.gov.au) | CC BY-NC-ND 4.0 |
  | New South Wales | [parliament.nsw.gov.au/hansard](https://www.parliament.nsw.gov.au/hansard) (API: api.parliament.nsw.gov.au) | Parliamentary copyright |
  | South Australia | [hansardsearch.parliament.sa.gov.au](https://hansardsearch.parliament.sa.gov.au) | Parliamentary copyright |
  | Western Australia | [parliament.wa.gov.au/hansard](https://www.parliament.wa.gov.au/hansard) (public API) | CC BY-ND 4.0 |

  Those terms bind the *prose*, which is why none ships here; the full
  per-jurisdiction analysis is in the pipeline repo's
  [LICENSES-DATA.md](https://github.com/parliament-io/hansard-researcher/blob/main/LICENSES-DATA.md).
- Vectors and derived cubes in this dataset: **CC-BY-4.0**
- Pipeline: harvest → normalize → enrich (embed/themes) → aggregate, all
  open source at
  [parliament-io/hansard-researcher](https://github.com/parliament-io/hansard-researcher).
  `gold/pipeline_coverage` records per-house-day provenance including
  `harvested_at` timestamps.
- Updated weekly by an incremental upload from the maintainer's
  pipeline; the current year's shards churn, historical shards are stable.

## Citation

```bibtex
@misc{novaworks2026hansard,
  author    = {{NovaWorks Group Pty Ltd}},
  title     = {Parliamentary Hansard --- Embeddings \& Derived Analytics},
  year      = {2026},
  publisher = {Hugging Face},
  url       = {https://huggingface.co/datasets/parliament-data/hansard-embeddings},
  note      = {Project: \url{https://parliament.io}. Updated continuously; cite the access date.}
}
```

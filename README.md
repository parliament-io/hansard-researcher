# Hansard Researcher

Open-source analytics extraction for Parliamentary Hansard daily XML.

Six target jurisdictions: **Western Australia, South Australia, New South
Wales, Australian Federal Parliament, New Zealand, Scottish Parliament** —
harvested from official sources, normalized into one canonical model
(descended from the `Hansard_1_0.xsd` schema family that WA and SA publish
natively), and aggregated into analysis-ready open datasets anyone can query
with DuckDB or a notebook.

**Status: core pipeline complete and the full backfill executed
(2026-07-03).** Four jurisdictions are live end-to-end; NZ and Scotland are
researched placeholders. See [docs/ROADMAP.md](docs/ROADMAP.md) for
per-jurisdiction source research, verified data boundaries, operational
notes, and what's next.

## Current archive (backfilled 2026-07-03, local `data/`, ~4.5 GB raw)

| | WA | SA | AU Federal | NSW | Total |
|---|---|---|---|---|---|
| Coverage | 2025→ | ~2008→ | 2017-02-07→ | 2005→ | ~21 years |
| House-days | 153 | 1,952 | 1,021 | 2,117 | **5,243** |
| Speaking turns | 46k | 651k | 304k | 246k | **1.25M** |
| Text paragraphs | 157k | 2.16M | 1.62M | 2.52M | **6.4M** |
| Division member votes | 4.6k | 32k | 513k | 342k | **891k** |

Boundaries are *moving*: parliaments upload historic conversions over time
(NSW migrates old Word documents; WA/SA are deepening). Harvesters probe—
never hardcode—and a re-run picks up newly available days automatically.

## Design in one paragraph

Per-jurisdiction **harvest adapters** fetch official XML into an immutable raw
store; a shared **normalizer** maps every source into the canonical model
(silver Parquet, hive-partitioned `jurisdiction/date/house`); optional
**enrichment** (themes, embeddings, entity links) is pluggable and never
required; an **aggregator** produces gold cubes (member activity, Q→A pairs,
divisions, sitting rhythm) published as language-neutral Parquet + DuckDB.
Tier 1 analytics require no API keys and no models — the whole pipeline runs
on a laptop.

## Quickstart

```bash
uv sync
uv run pytest                        # 108 tests
uv run hansard-researcher sources           # adapter status per jurisdiction
uv run hansard-researcher schema            # emit canonical JSON Schema

# incremental harvest (skips already-fetched days; re-probes missing ones;
# --refresh-window re-fetches recent days so proofs converge to corrected)
uv run hansard-researcher harvest wa --start 2026-06-01 --end 2026-07-03 --refresh-window 45

# raw XML -> silver Parquet (parallel across house-days; default CPU-1 workers)
uv run hansard-researcher normalize wa --workers 8

# member register (Tier 2 reference data; SA live) — raw snapshots are
# stored under data/reference/raw so --offline rebuilds without network
uv run hansard-researcher reference sa

# silver -> gold cubes (full recompute, seconds) + self-contained DuckDB
uv run hansard-researcher aggregate
uv run hansard-researcher db --out data/hansard.duckdb

# query anything — no server needed
uv run python -c "import duckdb; print(duckdb.sql(
  \"select member_name, words from 'data/gold/member_activity.parquet' order by words desc limit 5\"))"
```

Full backfill = the same `harvest` command with a wide date range; it is
idempotent and newest-first, and interrupting/resuming is always safe.

## Enrichment (Tier 3 — optional, bring your own processing)

Embeddings + semantic search never run unless you configure a provider, and
the structural pipeline above never needs one. Point at **any**
OpenAI-compatible endpoint — a local server (no key) or a hosted API with
your own key — or run embeddings in-process:

```bash
# everything local via Docker: Ollama (models) + Qdrant (vector search),
# published on localhost only
docker compose --profile enrich up -d
docker compose exec ollama ollama pull nomic-embed-text

uv run hansard-researcher enrich embed wa --provider ollama
uv run hansard-researcher enrich search "housing affordability" --provider ollama

# at archive scale, index into Qdrant for fast ANN search (join keys only —
# no Hansard prose enters the index; text hydrates from local silver)
uv run hansard-researcher enrich index wa --provider ollama
uv run hansard-researcher enrich search "housing affordability" --provider ollama --backend qdrant

# theme classification against the seed taxonomy (reference/themes/*.yaml):
# embedding engine = cheap, works with any provider; llm engine = higher
# quality via a chat model. Re-running `aggregate` then populates the six
# theme gold cubes (theme_by_week, theme_cooccurrence, member_theme_rank,
# bill_theme_link, member_vote_by_theme, theme_candidates) + /themes page
uv run hansard-researcher enrich themes wa --provider ollama
uv run hansard-researcher enrich themes wa --provider ollama --engine llm

# hosted, BYO key
export HANSARD_RESEARCHER_ENRICH_API_KEY=sk-...
uv run hansard-researcher enrich embed wa --provider openai

# in-process sentence-transformers (no server at all; pulls torch)
uv sync --extra local
uv run hansard-researcher enrich embed wa --provider local

# anything else that speaks the OpenAI API
export HANSARD_RESEARCHER_ENRICH_BASE_URL=https://my-gateway/v1   # + _API_KEY, _EMBED_MODEL
```

Vectors land in `data/enriched/` (join keys only — no Hansard prose) with
the model id in every dedup key, so re-running with a different model or
provider is incremental and coherent. Keys live only in your environment.

## Docker (sandboxed)

The image holds code only; your local `data/` archive mounts at `/data`.
The default `pipeline` service has **no network**, a read-only root
filesystem and no capabilities — it can only read/write the data volume.
Version tags publish the image to GHCR
(`docker pull ghcr.io/parliament-io/hansard-researcher`), or build locally:

```bash
docker compose pull pipeline   # use the published image (no toolchain needed)
docker compose build           # …or build locally; same image name either way
docker compose run --rm pipeline normalize wa --workers 8   # fully offline
docker compose run --rm pipeline aggregate
docker compose run --rm harvest harvest wa --start 2026-06-01 --end 2026-07-04
```

Pin a release with `HANSARD_RESEARCHER_TAG=0.0.1 docker compose pull` —
otherwise `latest` is used. Note compose prefers building when no local
image exists, so run `pull` explicitly to skip the toolchain.

An optional `--profile ollama` adds a local model server on an
internal-only network, so Tier 3 runs with Hansard text never leaving the
machine (commands in `compose.yaml`).

## Dashboards

`dashboards/` is an [Evidence.dev](https://evidence.dev) project — pages are
markdown with SQL blocks, built into a fully static site from the **gold
cubes only** (derived facts; no Hansard prose can reach the site):

```bash
uv run hansard-researcher db --out dashboards/sources/hansard/hansard.duckdb
cd dashboards && npm install && npm run sources && npm run dev   # or: npm run build
```

The daily `publish.yml` workflow harvests all four live jurisdictions,
aggregates, and redeploys the site to GitHub Pages automatically.

## Data licensing

We publish **code and derived statistics only** — no transformed Hansard
text. Full-text tables are rebuilt locally by each user from the official
source. See [LICENSES-DATA.md](LICENSES-DATA.md) for per-jurisdiction terms.
`data/` is gitignored: raw + silver contain Hansard text and stay local;
gold is publishable everywhere (enforced by test — gold cubes carry no
`raw_text`/`clean_text` columns).

## Published dataset (Hugging Face)

The open-data artifacts publish to
[`parliament-data/hansard-embeddings`](https://huggingface.co/datasets/parliament-data/hansard-embeddings):
embeddings consolidated into jurisdiction-year shards (vectors + join keys
only), the gold cubes they join against, and optionally a Qdrant collection
snapshot for batteries-included restore-and-search. No Hansard prose ships —
hydrating text means running the harvester yourself (the dataset card
documents the join contract).

[`scripts/hf_publish.py`](scripts/hf_publish.py) does the work: staging,
join-contract guard (aborts if `subject_id` coverage drops below the
measured-healthy floor), and a resumable `upload_large_folder` — re-running
it is the incremental daily publish.

```bash
uv run scripts/hf_publish.py --dry-run   # stage + report, no upload
uv run scripts/hf_publish.py             # stage + upload (HF_TOKEN)
```

## Layout

```
schemas/          canonical XSD + generated JSON Schema + source-schema lineage
                  (wa/sa swagger copies, federal ExtractSchema v1)
src/hansard_researcher/
  model/          canonical Pydantic model, deterministic ids, content hashing
  harvest/        adapters: wa, sa (shared API), nsw, au + nz, scot stubs
  normalize/      canonical_xml (WA/SA + stitch_daily), au_unixml, nsw_xml,
                  silver writer, parallel runner
  aggregate/      gold cube SQL (cubes.py) + hansard.duckdb builder
  reference/      member registers (sa, nsw live) + curated YAML: stage
                  vocabulary (names -> bill stages) and seed theme taxonomy
                  (per-locale debate categories for Tier 3 classification)
  enrich/         optional Tier 3: providers (BYO key / local model),
                  embeddings, semantic search
  cli.py          harvest | normalize | aggregate | db | reference | enrich |
                  sources | schema
samples/          redistributable source samples (federal CC BY-NC-ND verbatim)
scripts/          hf_publish.py — stage + publish open-data artifacts to Hugging Face
dashboards/       Evidence.dev site (gold-only)
data/             local only, gitignored: raw/ silver/ gold/ enriched/ + logs
Dockerfile        pipeline image (code only; data mounts at /data)
compose.yaml      sandboxed services: pipeline (no network), harvest, ollama profile
.github/workflows publish.yml (daily pipeline + Pages), ci.yml (lint/test/
                  schema-drift), release.yml (GHCR image on version tags)
```

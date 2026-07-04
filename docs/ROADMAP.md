# Roadmap & source notes

Status (2026-07-03): **core pipeline complete; full backfill executed.** Four
jurisdictions live end-to-end (WA, SA, NSW, AU Federal); NZ and Scotland are
researched placeholders. ~21 years / 5,243 house-days / 1.25M speaking turns
/ 891k division member votes in the archive (NSW's 342k unlocked 2026-07-04
when the structured division blocks were found in fragment.data).

## Architecture

```
harvest (per-jurisdiction adapters, raw immutable store)
  -> normalize (source XML -> canonical model -> silver Parquet,
                hive-partitioned jurisdiction/date/house)
  -> aggregate (gold cubes: derived facts only, publishable)
  -> dashboards (Evidence.dev static site from gold)
```

Principles: raw is immutable; ids are deterministic and content-hashed
(idempotent re-runs); enrichment (themes/embeddings) is optional-by-design —
the structural pipeline needs no API keys; the canonical model follows the
`Hansard_1_0.xsd` schema family that WA and SA publish natively.

## Jurisdiction sources (verified 2026-07-03)

| # | Jurisdiction | Access | XML from | Licence |
|---|---|---|---|---|
| 1 | WA | Shared Hansard Public API: `parliament.wa.gov.au/hansard/api` (swagger copy in `schemas/`) | 2025→ | CC BY-ND 4.0 (declared by the API) |
| 2 | SA | Same API product: `hansardsearch.parliament.sa.gov.au/api` | ~2008→ | parliamentary copyright, API "as is" |
| 3 | NSW | `api.parliament.nsw.gov.au/api/hansard/search` — year listing → ToC → per-subject fragment XML | 2005→ verified (documented 1991→) | parliamentary copyright, API "as is" |
| 4 | AU Federal | Calendar-page discovery + `aph.gov.au/api/hansard/link/?id=chamber/{coll}/{id}/toc&linktype=xml&fulltranscript=True` (full daily) | 2017-02-07→ in the modern format | CC BY-NC-ND 4.0 |
| 5 | NZ | No working public XML/API (data.parliament.nz broken as checked); HTML only | — | TBC |
| 6 | Scotland | `data.parliament.scot/api/*` (members/bills, OGL) + TheyWorkForYou `sp-new` debate XML | — | OGL |

Boundaries move: parliaments upload historic conversions over time. The
harvesters **probe rather than hardcode** — a day whose XML isn't available
yet is skipped without being marked harvested, so later runs re-probe it.

## Operational facts (hard-won; keep them true)

- WA and SA run the identical API product — one adapter, two base URLs.
  Subject indexes are **1-based**; `contentType=text/xml` exactly (bare
  `xml` silently returns a JSON rendering).
- WA/SA/NSW publish the day **split by subject** ("extracts"; ToC ref n =
  extract n). `stitch_daily` reassembles the daily: proceedings merge by uid
  (by name for NSW, which has no proceeding uids), heading texts dedupe by
  source id, `document_order` renumbers across the day.
- Federal doc ids come in three era shapes — numeric (~2021→), GUID
  (~2012–2021), date-based (2000–2011) — all accepted by the same download
  URL. Pre-2018 calendar cells have **no aria-label**: dates are parsed
  structurally (year heading + month row + day-number link text). The
  aph.gov.au WAF requires a browser user-agent.
- NSW fragments follow the v1 extract schema (`schemas/federal/
  ExtractSchema_v1.xsd`): `fragment.data` (structure) + `fragment.text`
  (XHTML prose; speaker markers `data-mode="member" data-value={id}`;
  clock in `Time-H` + `HiddenTime-H` spans).
- Historic (pre-2026) SA/WA divisions are presentational: counts as
  "Ayes<TAB>n" text lines, member votes in AYES/NOES/PAIRS tables with
  "(teller)" suffixes — handled in `canonical_xml._parse_division`.
- Individual source documents can be broken (e.g. a 400 on one NSW
  fragment): adapters skip 4xx per-document, retry 5xx with backoff, and
  never mark a zero-document day as harvested.
- Silver's partition key must be (jurisdiction, date, **house**) — both
  chambers usually sit the same date (regression-tested).
- `normalize --workers N` parallelizes across house-days (Windows caps
  process pools at 61). The full archive re-normalizes in ~3 minutes.
- `harvest --refresh-window N` re-fetches recent days so uncorrected proofs
  converge to the corrected record (the daily workflow uses 45).

## Remaining work

**Bill journeys (shipped 2026-07-04):** two gold cubes port the internal
Explorer's bill views — `bills` (one row per bill: houses, first→last
sitting, furthest stage, volume, divisions) and `bill_journey` (bill ×
house-day: stages as published + canonical stage, speakers, words, division
results) — plus a `/bills` dashboard page (picker → journey table + debate-
volume chart). Bill identity = normalized bill *name* + jurisdiction: WA/SA
`bill_refs` uids do NOT track across houses (verified: 13 uids for one SA
bill), while the name string does; NSW/AU carry the bill name as the subject
name. **Stage vocabulary** is curated YAML shipped in-package
(`src/parlhansard/reference/stages/{jur}.yaml`), seeded from the observed
subproceeding-name distribution — maps e.g. NSW LA's "Agreement in
Principle" to `second_reading`. Coverage: SA 97.6% / AU 93.9% / NSW 81.8% /
WA 76.6% of journey rows mapped; unmapped names keep their raw label
(crowd-source additions by PR — a test validates the YAML). The **theme
taxonomy** (open seed YAML, per-locale, exemplar phrases written fresh —
never Hansard text) follows the same in-repo curated pattern and lands with
the Phase 4 theme classifier.

**Enrichment (in progress; optional-by-design):** decided (2026-07-03):
**bring your own processing** — no default model stack, multiple options.
*Shipped (2026-07-04):* the provider layer (`enrich/providers.py` — one
OpenAI-compatible HTTP client covering local servers [Ollama, LM Studio,
vLLM — no key] and hosted BYO-key endpoints, plus in-process
sentence-transformers behind the `local` extra), paragraph embeddings
(`enrich embed` → `data/enriched/`, incremental per model × house-day,
vectors + join keys only — no prose) and semantic search (`enrich search`).
Base URL + key come from `--provider` presets / `PARLHANSARD_ENRICH_*` env;
the project never ships, requires, or defaults to a key. Model id is part of
the dedup keys, so re-running with a different provider is coherent.
*Shipped (2026-07-04):* the **open seed theme taxonomy** — ≤30 broad
categories per locale as versioned in-repo YAML
(`src/parlhansard/reference/themes/{en-AU,en-NZ,en-GB}.yaml`, ported from
the proven internal catalog; descriptions freshly authored, never Hansard
text). Every jurisdiction maps to a locale list (wa/sa/nsw/au→en-AU,
nz→en-NZ, scot→en-GB) because locale-mismatched label spaces poison
classifier accuracy; the taxonomy version joins the model id in enrichment
dedup keys. *Shipped (2026-07-04):* the **theme classifier** (`enrich
themes`) — subject-baseline tier, two engines: `embedding` (cosine vs theme
"name — description" vectors; one call per subject; any provider incl.
`local`) and `llm` (chat model picks ≤k ids from the catalog; ids validated
against the taxonomy). Output: `data/enriched/themes/` (theme ids + join
keys, no prose), incremental per engine+model × house-day. *Shipped
(2026-07-04):* **Qdrant ANN backend** — `enrich index` loads computed
embeddings into a per-model collection (REST via httpx, point id =
deterministic text_id → idempotent re-index; payload = join keys only, no
prose; text hydrates from local silver at query time) and
`enrich search --backend qdrant` queries it; `compose.yaml --profile
enrich` runs Ollama + Qdrant on localhost. Verified live end-to-end on WA
2026-06-18 (2,177 vectors, nomic-embed-text). *Shipped (2026-07-04):* the
full **theme gold cube set** from the C# aggregator — theme_by_week,
theme_cooccurrence, member_theme_rank (dense rank within theme),
bill_theme_link, member_vote_by_theme, plus theme_candidates (curator-queue
port: unclassified/low-confidence subjects on classified days) — built by
`aggregate` from `data/enriched/themes` (empty until `enrich themes` runs;
every cube carries engine+model so providers never mix), and a `/themes`
dashboard page. *Next:* paragraph-tier refinement and bill-baseline
provenance, NER. Until canonical
member ids land everywhere, member×theme cubes key on source talker ids
(upgraded in place later).

**Reference data (SA live):** member registers per jurisdiction →
canonical member ids + party. Unlocks party facets for NSW/SA (their talker
XML carries no party; WA/AU do).

- *Shipped (2026-07-04):* `parlhansard reference sa` —
  `membersapp.parliament.sa.gov.au/api/members` (POST; memberType
  current/former; one row per person with party, house, electorate, DOB,
  elected→archived span) + contact-details API snapshots. Raw JSON is stored
  under `data/reference/raw/sa/` so `--offline` rebuilds without network.
  **`pm_Id` IS the Hansard talker id** — verified: 176/177 SA speaker ids
  resolve by direct join (99.8% of 555k turns; the one miss is an
  empty-`@id` talker, for the name-match fallback).
- *Shipped (2026-07-04):* `parlhansard reference nsw` — scrapes
  `all-members.aspx` (current: pk, party, house, electorate, 135 members)
  + `former-members-index.aspx?filter=A..Z` (former: identity only — pk,
  name, DOB; ~2,170). **NSW `pk` IS the Hansard talker id** (verified:
  Sharpe=28, Mitchell=93, Tudehope=115, Graham=2224). Former-member party
  is free text on the site (unreliable, operator-confirmed) → backfill via
  curated table / Wikidata is future work. Register total: 2,307 people.
- *Corrected:* the WA/SA Hansard API "sessional member index" is a speech
  index (name → referenceid + speech/question counts) — **no party,
  electorate, or dates**. Not a register; useful only as a name↔referenceid
  bridge. WA needs another source (parliament website / curated).
- *Corrected:* NSW divisions are NOT prose-only — modern fragments carry
  structured `topic/subproceeding/division` blocks (ayes/noes counts,
  per-member `aye`/`noe`/`pair` with **pk ids and blank names**). Now parsed
  by `nsw_xml._parse_division`; names/party resolve via the register.
- *Next:* entity-link stage (name fallback for empty ids), gold party join +
  register name fill for NSW division votes, former-NSW party backfill,
  Scotland `/api/members`, TWFY people for federal, WA scrape/curate.

**NZ + Scotland activation:** Scotland via TheyWorkForYou `sp-new` XML + OGL
reference API; NZ pending a working official source (or an HTML scraper).

**Known gaps:**
- NSW divisions appear only in prose — body-text division parser needed.
- AU 2000–2016 files use legacy `para`/`quote`/`motion` text elements —
  structural parse works, speech text is empty; parser branch needed before
  deepening the AU backfill.
- A tranche of older SA sittings (and WA pre-2025) await source-side
  conversion — re-running the backfill picks them up automatically.

## Licensing stance

Code + derived statistics are public; transformed Hansard text is never
redistributed — each user rebuilds full text locally from official sources.
Enforced structurally: `data/` is gitignored and a test fails if any gold
cube grows a text column. Per-jurisdiction terms: [LICENSES-DATA.md](../LICENSES-DATA.md).

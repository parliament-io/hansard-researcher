# Roadmap & source notes

Status (2026-07-03): **core pipeline complete; full backfill executed.** Four
jurisdictions live end-to-end (WA, SA, NSW, AU Federal); NZ and Scotland are
researched placeholders. ~21 years / 5,243 house-days / 1.25M speaking turns
/ 549k division member votes in the archive.

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

**Reference data (next):** member registers per jurisdiction →
canonical member ids + time-sliced party. Unlocks party facets for NSW/SA
(their talker XML carries no party; WA/AU do). Sources already identified:
WA/SA sessional member indexes (same API), Scotland `/api/members`,
TheyWorkForYou people data for federal.

**Enrichment (optional-by-design):** pluggable theme classification,
embeddings (DuckDB VSS default), NER. Open decision: default model stack
(local-only vs hosted).

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

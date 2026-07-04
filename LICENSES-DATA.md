# Data licensing — per jurisdiction

The code in this repository is MIT-licensed. The Hansard **text and data** it
harvests are not ours to licence: each parliament's terms apply to what you
fetch and what you may redistribute. This project's stance (see
[docs/ROADMAP.md](docs/ROADMAP.md)): **we publish code and derived statistics
only; full text is rebuilt locally by each user from the official source.**

| Jurisdiction | Source | Terms | Practical effect |
|---|---|---|---|
| Western Australia | parliament.wa.gov.au/hansard/api (public API) | API declares **CC BY-ND 4.0** (verified 2026-07-03). Additionally, per-file embedded conditions: attribution, fair/accurate use, no satire/misrepresentation; user assumes defamation liability. | Verbatim copies redistributable with attribution (ND blocks *adaptations* — don't redistribute transformed text). Derived statistics OK. |
| South Australia | hansardsearch.parliament.sa.gov.au (public API) | Parliamentary copyright; API offered "as is". | Don't redistribute full text without confirmation. Derived statistics OK. |
| New South Wales | api.parliament.nsw.gov.au | Parliamentary copyright; API offered "as is"; catalogued on Data.NSW. | Don't redistribute full text without confirmation. Derived statistics OK. |
| Australia (Federal) | parlinfo.aph.gov.au | **CC BY-NC-ND 4.0** | Verbatim, unmodified copies may be shared non-commercially with attribution (ND blocks *adaptations*, NC blocks commercial use). Transformed text must not be redistributed. Derived statistics OK. |
| New Zealand | hansard.parliament.nz | Reportedly free of copyright restriction under NZ statutory exception — **unverified**; confirm before the NZ adapter ships. | TBD. |
| Scotland | parliament.scot / data.parliament.scot | **Open Government Licence (OGL)** | Fully open — full text and derived data may be redistributed with attribution. |

Notes for downstream users:

- Derived tables published by this project (counts, votes, timings, activity
  metrics) are facts about parliamentary proceedings; no Hansard prose is
  included.
- Optional enrichment outputs stay local and carry no prose either:
  embeddings (`data/enriched/`, and any Qdrant collection built from them)
  hold vectors + row keys — vectors are non-expressive and do not reconstruct
  text (a considered judgement call, noted here for transparency); theme
  assignments hold taxonomy ids + keys. Display text always hydrates from
  your local silver build. If you point enrichment at a **hosted** provider,
  you are transmitting Hansard text to that provider under your own
  arrangement with it — the local-server path (Ollama/Qdrant via
  `docker compose --profile enrich`) keeps text on your machine.
- Member register data (`hansard-researcher reference`) is compiled from official
  parliamentary websites/APIs: names, party, electorate, service dates —
  facts, published with attribution to the source parliament.
- If you rebuild the full-text tables locally (`hansard-researcher harvest` +
  `normalize`), your local copy is obtained from the official source under
  that parliament's terms — including, for federal data, the **non-commercial**
  restriction, which binds your use, not just ours.
- This document is a good-faith engineering summary, not legal advice.

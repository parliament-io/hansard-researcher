"""New Zealand Parliament adapter — placeholder.

Status (verified 2026-07-03): **no working public XML/API.**

- Hansard is HTML at https://hansard.parliament.nz/
  (``/hansard-transcript/{yyyy-MM-dd}/{slug}``).
- The official developer portal https://data.parliament.nz/ was broken when
  checked (invalid certificate, HTTP 404) — re-check before implementing.
- Community prior art: https://github.com/kayakr/parliament.nz scrapes the
  HTML into Akoma Ntoso; an interim HTML scraper in that style is the fallback
  if the portal doesn't recover.

Licensing: reportedly free of copyright restriction under NZ statutory
exception — unverified; confirm before shipping (see LICENSES-DATA.md).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator

from parlhansard.harvest.base import HarvestAdapter, RawDocument, SittingEvent, register
from parlhansard.model.canonical import Fragment, Jurisdiction

_MESSAGE = (
    "NZ is a placeholder: no working public XML/API as of 2026-07-03 "
    "— see docs/ROADMAP.md"
)


@register
class NzAdapter(HarvestAdapter):
    jurisdiction = Jurisdiction.NZ
    status = "placeholder"
    source = "no working public XML/API as of 2026-07-03 - HTML scrape or data.parliament.nz"

    def discover(self, start: dt.date, end: dt.date) -> Iterator[SittingEvent]:
        raise NotImplementedError(_MESSAGE)

    def fetch(self, event: SittingEvent) -> Iterator[RawDocument]:
        raise NotImplementedError(_MESSAGE)

    def normalize(self, docs: Iterable[RawDocument]) -> Iterator[Fragment]:
        raise NotImplementedError(_MESSAGE)

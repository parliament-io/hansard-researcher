"""Scottish Parliament adapter — placeholder (reference-data API is live).

Status (verified 2026-07-03):

- Official Report text: HTML/PDF only (per-meeting PDF via
  ``https://www.parliament.scot/api/sitecore/CustomMedia/OfficialReport?meetingId={id}``);
  no official debate XML.
- **Open data API works now** (anonymous JSON, OGL):
  ``https://data.parliament.scot/api/{members,bills,committees,events,sessions}``
  — the members endpoint feeds the reference-data stage (see docs/ROADMAP.md).
- Debate text route: TheyWorkForYou/mySociety parlparse output at
  ``https://www.theyworkforyou.com/pwdata/scrapedxml/`` (``sp/``, ``sp-new/``,
  ``sp-motions/``, ``sp-questions/``, ``sp-written/``), parser at
  https://github.com/mysociety/parlparse.

Licensing: Open Government Licence — fully open (see LICENSES-DATA.md).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator

from parlhansard.harvest.base import HarvestAdapter, RawDocument, SittingEvent, register
from parlhansard.model.canonical import Fragment, Jurisdiction

DATA_API_BASE = "https://data.parliament.scot/api"

_MESSAGE = "Scotland debate-text adapter is not implemented yet — see docs/ROADMAP.md"


@register
class ScotAdapter(HarvestAdapter):
    jurisdiction = Jurisdiction.SCOT
    status = "placeholder"
    source = "data.parliament.scot JSON API (OGL) + TheyWorkForYou sp-new debate XML"

    def discover(self, start: dt.date, end: dt.date) -> Iterator[SittingEvent]:
        raise NotImplementedError(_MESSAGE)

    def fetch(self, event: SittingEvent) -> Iterator[RawDocument]:
        raise NotImplementedError(_MESSAGE)

    def normalize(self, docs: Iterable[RawDocument]) -> Iterator[Fragment]:
        raise NotImplementedError(_MESSAGE)

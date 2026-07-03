"""Australian Federal Parliament adapter — full daily uniXML via aph.gov.au.

Discovery (verified 2026-07-03): the two chamber calendar pages contain the
complete sitting-day history back to 2000, each date cell linking
``Hansard_Display?bid=chamber/{coll}/{docId}/`` with an ``aria-label`` date:

- House: https://www.aph.gov.au/Parliamentary_Business/Hansard/Hansreps_2011
- Senate: https://www.aph.gov.au/Parliamentary_Business/Hansard/Hanssen261110

Doc ids come in three era-dependent shapes, all accepted by the download API:
numeric (~2021→, e.g. 29139), GUID (~2012–2021), and date-based (2000–2011,
via legacy ParlInfo links where the date IS the id).

Fetch: one request returns the full daily transcript XML —
``https://www.aph.gov.au/api/hansard/link/?id=chamber/{coll}/{docId}/toc&linktype=xml&fulltranscript=True``
(requires a browser user-agent; the WAF blocks default HTTP-client UAs).

Coverage: 2000 → present via this adapter. 1981–1997 has no XML anywhere;
1901–1980 historical XML exists at github.com/wragge/hansard-xml (backfill,
different format). Licensing: CC BY-NC-ND 4.0 — see LICENSES-DATA.md.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Iterator

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from parlhansard.harvest.base import (
    BROWSER_USER_AGENT,
    HarvestAdapter,
    RawDocument,
    SittingEvent,
    register,
)
from parlhansard.model.canonical import Fragment, Jurisdiction
from parlhansard.normalize.au_unixml import parse_daily

CALENDARS = {
    "reps": ("hansardr", "https://www.aph.gov.au/Parliamentary_Business/Hansard/Hansreps_2011"),
    "senate": ("hansards", "https://www.aph.gov.au/Parliamentary_Business/Hansard/Hanssen261110"),
}

DOWNLOAD_URL = (
    "https://www.aph.gov.au/api/hansard/link/"
    "?id=chamber/{coll}/{doc_id}/toc&linktype=xml&fulltranscript=True"
)

# Hansard_Display anchors: ~2018+ carry the date in aria-label (tolerating
# &amp; and label suffixes injected by accessibility tooling); pre-2018 cells
# have NO aria-label — the date is structural: <h3><a name="{year}"> section
# headings + <p>{Month}</p> row labels + the day number as the link text.
_ANCHOR = re.compile(
    r'<a href="[^"]*Hansard_Display\?bid=chamber/(hansardr|hansards)/'
    r'([0-9a-fA-F-]+)/&(?:amp;)?sid=0000"([^>]*)>(\d{1,2})</a>'
)
_ARIA_DATE = re.compile(r'aria-label="(\d{2}-[A-Za-z]{3}-\d{4})')
_YEAR_MARK = re.compile(r'<a name="(\d{4})">')
_MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}
_MONTH_MARK = re.compile(r"<p>\s*(" + "|".join(_MONTHS) + r")\s*</p>")
# legacy ParlInfo links (2000-2011): the date IS the id
_LEGACY = re.compile(r"Id%3A%22chamber/(hansardr|hansards)/(\d{4}-\d{2}-\d{2})/0000%22")

_COLL_TO_HOUSE = {"hansardr": "reps", "hansards": "senate"}


def _last_before(marks: list[tuple[int, int]], pos: int) -> int | None:
    """Value of the latest marker positioned before ``pos``."""
    import bisect

    index = bisect.bisect_left(marks, (pos,)) - 1
    return marks[index][1] if index >= 0 else None


@register
class AuAdapter(HarvestAdapter):
    jurisdiction = Jurisdiction.AU
    status = "live"
    source = "aph.gov.au calendar scrape + full-daily uniXML link API (CC BY-NC-ND)"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=120.0,
            follow_redirects=True,
        )

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    def _get(self, url: str) -> httpx.Response:
        response = self._client.get(url)
        if response.status_code >= 500:
            response.raise_for_status()
        return response

    def _calendar_events(self, house: str) -> Iterator[SittingEvent]:
        coll, url = CALENDARS[house]
        response = self._get(url)
        response.raise_for_status()
        html = response.text
        years = [(m.start(), int(m.group(1))) for m in _YEAR_MARK.finditer(html)]
        months = [(m.start(), _MONTHS[m.group(1)]) for m in _MONTH_MARK.finditer(html)]
        seen: set[dt.date] = set()
        for match in _ANCHOR.finditer(html):
            aria = _ARIA_DATE.search(match.group(3))
            if aria:
                date = dt.datetime.strptime(aria.group(1), "%d-%b-%Y").date()
            else:
                year = _last_before(years, match.start())
                month = _last_before(months, match.start())
                if year is None or month is None:
                    continue
                try:
                    date = dt.date(year, month, int(match.group(4)))
                except ValueError:
                    continue
            if date in seen:
                continue
            seen.add(date)
            yield SittingEvent(
                jurisdiction=self.jurisdiction,
                date=date,
                house=house,
                source_doc_id=match.group(2),
                url=DOWNLOAD_URL.format(coll=match.group(1), doc_id=match.group(2)),
            )
        for match in _LEGACY.finditer(html):
            date = dt.date.fromisoformat(match.group(2))
            if date in seen:
                continue
            seen.add(date)
            yield SittingEvent(
                jurisdiction=self.jurisdiction,
                date=date,
                house=house,
                source_doc_id=match.group(2),  # the date IS the id
                url=DOWNLOAD_URL.format(coll=match.group(1), doc_id=match.group(2)),
            )

    def discover(self, start: dt.date, end: dt.date) -> Iterator[SittingEvent]:
        for house in CALENDARS:
            for event in self._calendar_events(house):
                if start <= event.date <= end:
                    yield event

    def fetch(self, event: SittingEvent) -> Iterator[RawDocument]:
        response = self._get(event.url)
        if 400 <= response.status_code < 500:
            return  # not (yet) available or broken — re-probe on a later run
        response.raise_for_status()
        yield RawDocument(
            event=event,
            content=response.content,
            media_type="text/xml",
            url=event.url,
            retrieved_at=dt.datetime.now(dt.UTC),
            name="daily.xml",
        )

    def normalize(self, docs: Iterable[RawDocument]) -> Iterator[Fragment]:
        for doc in docs:
            if doc.media_type != "text/xml":
                continue
            yield parse_daily(
                doc.content, source_url=doc.url, retrieved_at=doc.retrieved_at
            )

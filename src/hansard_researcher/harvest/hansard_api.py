"""Shared adapter for the "Parliamentary Hansard Public API" product.

WA and SA run the **same public REST API** (verified 2026-07-03 — identical
endpoint surfaces; swaggers preserved at ``schemas/wa.swagger.json`` and
``schemas/sa.swagger.json``), serving Hansard split **by subject** into
extracts in the canonical ``Hansard_1_0.xsd`` schema family. The source
systems build a combined ``Daily.xml`` at end of day; the API exposes the same
content as ToC + per-subject extracts (ToC ref 1, 2, 3 … = extract 001, 002,
003 …), so normalization stitches the extracts back into the daily fragment.

Endpoints (all GET, no auth; live-probed facts):

- ``/hansard/houses`` — house codes (WA: lh, uh, esta, estb;
  SA: lh, uh, eca, ecaatq, ecb, ecbatq)
- ``/hansard/events/{year}`` — sitting-day events
  (houseName, houseCode, date, pdfUrl, subjectCount, tocUrl)
- ``/hansard/{houseCode}/{date}/toc`` — proceedings tree; subjects carry
  their 1-based ``index`` and content links (**indexes are 1-based; 0 → 404**)
- ``/hansard/{houseCode}/{date}/subject/{i}?contentType=text/xml`` — extract
  XML (``contentType`` must be ``text/xml`` — bare ``xml`` falls back to a
  JSON rendering)
- ``/hansard/parliaments``, ``/hansard/indicies/{house}/{parl}/{sess}/members``
  — reference data for member/party linking (roadmap)
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from hansard_researcher import __version__
from hansard_researcher.harvest.base import (  # noqa: F401
    HarvestAdapter,
    RawDocument,
    SittingEvent,
    register,
)
from hansard_researcher.model.canonical import Fragment
from hansard_researcher.normalize.canonical_xml import parse_extract, stitch_daily

USER_AGENT = f"hansard-researcher/{__version__} (open-source Hansard analytics; polite harvester)"


class HansardPublicApiAdapter(HarvestAdapter):
    """Base adapter for parliaments running the shared Hansard Public API."""

    #: absolute API root, e.g. ``https://www.parliament.wa.gov.au/hansard/api``
    base_url: str
    #: concurrent subject fetches per day — a day is 50-100+ fragments, so
    #: sequential fetching pays full round-trip latency per fragment; a small
    #: pool stays polite while cutting per-day wall clock ~4x
    fetch_concurrency: int = 4

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=60.0,
            follow_redirects=True,
        )

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    def _get(self, url: str, **params: str) -> httpx.Response:
        response = self._client.get(url, params=params or None)
        if response.status_code >= 500:
            response.raise_for_status()  # retryable
        return response

    # -- discover ----------------------------------------------------------

    def discover(self, start: dt.date, end: dt.date) -> Iterator[SittingEvent]:
        for year in range(start.year, end.year + 1):
            response = self._get(f"{self.base_url}/hansard/events/{year}")
            if response.status_code == 404:
                continue
            response.raise_for_status()
            for event in response.json().get("events", []):
                date = dt.date.fromisoformat(event["date"])
                if not (start <= date <= end):
                    continue
                yield SittingEvent(
                    jurisdiction=self.jurisdiction,
                    date=date,
                    house=event["houseCode"],
                    url=event.get("tocUrl"),
                    extra={
                        "houseName": event.get("houseName", ""),
                        "subjectCount": str(event.get("subjectCount", "")),
                    },
                )

    # -- fetch -------------------------------------------------------------

    def _toc_url(self, event: SittingEvent) -> str:
        return event.url or (
            f"{self.base_url}/hansard/{event.house}/{event.date.isoformat()}/toc"
        )

    @staticmethod
    def _subject_indexes(toc: dict) -> list[int]:
        indexes = [
            subject["index"]
            for proceeding in toc.get("proceedings", [])
            for subject in proceeding.get("subjects", [])
            if "index" in subject
        ]
        return sorted(set(indexes))

    def fetch(self, event: SittingEvent) -> Iterator[RawDocument]:
        toc_url = self._toc_url(event)
        response = self._get(toc_url)
        if response.status_code == 404:
            # sitting listed but XML not (yet) available — parliaments upload
            # historic conversions over time, so skip and re-probe next run
            return
        response.raise_for_status()
        retrieved_at = dt.datetime.now(dt.UTC)
        yield RawDocument(
            event=event,
            content=response.content,
            media_type="application/json",
            url=toc_url,
            retrieved_at=retrieved_at,
            name="toc.json",
        )
        def get_subject(index: int) -> tuple[int, str, httpx.Response]:
            url = (
                f"{self.base_url}/hansard/{event.house}/{event.date.isoformat()}"
                f"/subject/{index}"
            )
            return index, url, self._get(url, contentType="text/xml")

        indexes = self._subject_indexes(response.json())
        with ThreadPoolExecutor(max_workers=self.fetch_concurrency) as pool:
            for index, url, subject_response in pool.map(get_subject, indexes):
                if 400 <= subject_response.status_code < 500:
                    # individual extracts can be broken at the source — skip,
                    # don't fail the day
                    continue
                subject_response.raise_for_status()
                yield RawDocument(
                    event=event,
                    content=subject_response.content,
                    media_type="text/xml",
                    url=f"{url}?contentType=text/xml",
                    retrieved_at=dt.datetime.now(dt.UTC),
                    name=f"subject_{index:04d}.xml",
                )

    # -- normalize ---------------------------------------------------------

    def normalize(self, docs: Iterable[RawDocument]) -> Iterator[Fragment]:
        """Stitch one day's extracts into the daily fragment.

        ``docs`` must belong to a single (date, house); the ToC document is
        optional and skipped.
        """
        extracts: list[Fragment] = []
        for doc in docs:
            if doc.media_type != "text/xml":
                continue
            index = None
            stem = doc.name.rsplit(".", 1)[0]
            if stem.startswith("subject_"):
                index = int(stem.split("_")[1])
            extracts.append(
                parse_extract(
                    doc.content,
                    jurisdiction=self.jurisdiction,
                    extract_index=index,
                    source_url=doc.url,
                    retrieved_at=doc.retrieved_at,
                )
            )
        if extracts:
            yield stitch_daily(extracts)

    # -- reference data (member/party linking, roadmap) ---------------------

    def members_index(self, house: str, parliament: int, session: int) -> dict:
        """Sessional members index — reference data for entity linking."""
        response = self._get(
            f"{self.base_url}/hansard/indicies/{house}/{parliament}/{session}/members",
            contentType="text/json",
        )
        response.raise_for_status()
        return json.loads(response.text)

"""New South Wales Parliament adapter — open Hansard API.

Endpoints (verified live 2026-07-03; docs at
https://parliament-api-docs.readthedocs.io/en/latest/new-south-wales/):

- ``/api/hansard/search/year/{year}`` — JSON: per sitting date, ``Events``
  per chamber with ``TocDocId``/``PdfDocId`` and an ``Uncorrected`` flag
- ``/api/hansard/search/daily/tableofcontents/{tocDocId}`` — ``hansard.toc``
  XML; ``topic`` elements carry ``@uid`` (fragment documentId) and ``@ref``
  (1-based order)
- ``/api/hansard/search/daily/fragment/{documentId}`` — fragment XML in the
  v1 extract schema (``schemas/federal/ExtractSchema_v1.xsd``):
  ``fragment.data`` (structure) + ``fragment.text`` (XHTML prose)

Like WA/SA the day is split **by subject** (one fragment per ToC topic), so
normalize parses each fragment and stitches the daily. Coverage: September
1991 → present. Licensing: NSW parliamentary copyright, API "as is".
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
from lxml import etree
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from hansard_researcher.harvest.base import (
    BROWSER_USER_AGENT,
    HarvestAdapter,
    RawDocument,
    SittingEvent,
    register,
)
from hansard_researcher.model.canonical import Fragment, Jurisdiction
from hansard_researcher.normalize.canonical_xml import stitch_daily
from hansard_researcher.normalize.nsw_xml import house_code, parse_nsw_fragment

BASE_URL = "https://api.parliament.nsw.gov.au/api/hansard/search"

_FRAGMENT_NAME = re.compile(r"^subject_(\d+)_(.+)\.xml$")

logger = logging.getLogger(__name__)


@register
class NswAdapter(HarvestAdapter):
    jurisdiction = Jurisdiction.NSW
    status = "live"
    source = "api.parliament.nsw.gov.au Hansard API (v1 extract schema, 1991-present)"
    #: concurrent fragment fetches per day (~100 fragments/day)
    fetch_concurrency: int = 8

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=60.0,
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

    def discover(self, start: dt.date, end: dt.date) -> Iterator[SittingEvent]:
        for year in range(start.year, end.year + 1):
            response = self._get(f"{BASE_URL}/year/{year}")
            if response.status_code == 404:
                continue
            response.raise_for_status()
            for day in response.json():
                date = dt.date.fromisoformat(day["date"])
                if not (start <= date <= end):
                    continue
                for event in day.get("Events", []):
                    toc_doc_id = event.get("TocDocId")
                    if not toc_doc_id:
                        continue
                    yield SittingEvent(
                        jurisdiction=self.jurisdiction,
                        date=date,
                        house=house_code(event.get("Chamber")),
                        source_doc_id=toc_doc_id,
                        url=f"{BASE_URL}/daily/tableofcontents/{toc_doc_id}",
                        extra={
                            "chamber": event.get("Chamber", ""),
                            "uncorrected": str(event.get("Uncorrected", "")),
                        },
                    )

    @staticmethod
    def _topics(toc_xml: bytes) -> list[tuple[int, str]]:
        """(ref, fragment documentId) pairs from the daily ToC."""
        root = etree.fromstring(toc_xml)
        topics: list[tuple[int, str]] = []
        for topic in root.iter("topic"):
            uid = (topic.get("uid") or "").strip()
            ref = (topic.get("ref") or "").strip()
            if uid and ref.isdigit():
                topics.append((int(ref), uid))
        return sorted(topics)

    def fetch(self, event: SittingEvent) -> Iterator[RawDocument]:
        response = self._get(event.url)
        if response.status_code == 404:
            return  # not (yet) migrated — NSW back-converts historic Word docs
        response.raise_for_status()
        yield RawDocument(
            event=event,
            content=response.content,
            media_type="application/xml",
            url=event.url,
            retrieved_at=dt.datetime.now(dt.UTC),
            name="toc.xml",
        )
        def get_fragment(topic: tuple[int, str]) -> tuple[int, str, str, httpx.Response]:
            ref, uid = topic
            url = f"{BASE_URL}/daily/fragment/{uid}"
            return ref, uid, url, self._get(url)

        # a day is ~100 fragments; the pool cuts wall clock proportionally
        with ThreadPoolExecutor(max_workers=self.fetch_concurrency) as pool:
            for ref, uid, url, fragment_response in pool.map(
                get_fragment, self._topics(response.content)
            ):
                if 400 <= fragment_response.status_code < 500:
                    # individual documents can be broken/unmigrated at the
                    # source (observed: 400 on a 2014 fragment) — skip, don't
                    # fail the day
                    logger.warning(
                        "skipping fragment %s (%s %s)",
                        uid, fragment_response.status_code, event.date,
                    )
                    continue
                fragment_response.raise_for_status()
                yield RawDocument(
                    event=event,
                    content=fragment_response.content,
                    media_type="text/xml",
                    url=url,
                    retrieved_at=dt.datetime.now(dt.UTC),
                    name=f"subject_{ref:04d}_{uid}.xml",
                )

    def normalize(self, docs: Iterable[RawDocument]) -> Iterator[Fragment]:
        extracts: list[Fragment] = []
        for doc in docs:
            if doc.media_type != "text/xml" or doc.name.startswith("toc"):
                continue
            match = _FRAGMENT_NAME.match(doc.name)
            ref = int(match.group(1)) if match else None
            uid = match.group(2) if match else None
            extracts.append(
                parse_nsw_fragment(
                    doc.content,
                    doc_id=uid,
                    extract_index=ref,
                    source_url=doc.url,
                    retrieved_at=doc.retrieved_at,
                )
            )
        if extracts:
            yield stitch_daily(extracts)

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from hansard_researcher.harvest.wa import WaAdapter

FIXTURES = Path(__file__).parent / "fixtures"

EVENTS_2026 = {
    "events": [
        {
            "houseName": "Synthetic Assembly",
            # uppercase on purpose: the live calendar occasionally emits it
            # (SA 2022-09-06 "UH") — discover must canonicalize to lowercase
            "houseCode": "LH",
            "date": "2026-03-04",
            "pdfUrl": "https://example.invalid/pdf",
            "subjectCount": 2,
            "tocUrl": "https://www.parliament.wa.gov.au/hansard/api/hansard/lh/2026-03-04/toc",
        },
        {
            "houseName": "Synthetic Council",
            "houseCode": "uh",
            "date": "2026-05-01",  # outside requested range
            "pdfUrl": "https://example.invalid/pdf",
            "subjectCount": 5,
            "tocUrl": "https://example.invalid/toc",
        },
    ]
}

TOC = {
    "date": "2026-03-04",
    "houseCode": "lh",
    "proceedings": [
        {
            "name": "Questions Without Notice",
            "subjects": [{"type": "subject", "index": 1}, {"type": "subject", "index": 2}],
        }
    ],
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/hansard/events/2026"):
        return httpx.Response(200, json=EVENTS_2026)
    if path.endswith("/lh/2026-03-04/toc"):
        return httpx.Response(200, json=TOC)
    if "/lh/2026-03-04/subject/" in path:
        assert request.url.params["contentType"] == "text/xml"
        index = int(path.rsplit("/", 1)[1])
        content = (FIXTURES / f"extract_{index:04d}.xml").read_bytes()
        return httpx.Response(200, content=content, headers={"content-type": "text/xml"})
    return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def adapter():
    return WaAdapter(client=httpx.Client(transport=httpx.MockTransport(_handler)))


def test_discover_filters_range(adapter):
    events = list(adapter.discover(dt.date(2026, 3, 1), dt.date(2026, 3, 31)))
    assert len(events) == 1
    assert events[0].house == "lh"
    assert events[0].date == dt.date(2026, 3, 4)


def test_fetch_yields_toc_and_subjects(adapter):
    event = next(iter(adapter.discover(dt.date(2026, 3, 1), dt.date(2026, 3, 31))))
    docs = list(adapter.fetch(event))
    assert [d.name for d in docs] == ["toc.json", "subject_0001.xml", "subject_0002.xml"]
    toc = json.loads(docs[0].content)
    assert toc["houseCode"] == "lh"


def test_end_to_end_normalize(adapter):
    event = next(iter(adapter.discover(dt.date(2026, 3, 1), dt.date(2026, 3, 31))))
    fragments = list(adapter.normalize(adapter.fetch(event)))
    assert len(fragments) == 1
    daily = fragments[0]
    assert daily.date == dt.date(2026, 3, 4)
    assert len(daily.proceedings) == 1  # merged across the two extracts
    assert len(daily.proceedings[0].subjects) == 2
    assert daily.extensions["extract_count"] == "2"

import datetime as dt
from pathlib import Path

import httpx
import pytest

from hansard_researcher.harvest.nsw import NswAdapter

FIXTURES = Path(__file__).parent / "fixtures"

YEAR_2026 = [
    {
        "date": "2026-06-24",
        "Events": [
            {
                "Chamber": "Legislative Council",
                "PdfDocId": "TEST-PDF-1",
                "TocDocId": "TEST-TOC-1",
                "Uncorrected": True,
            }
        ],
    },
    {
        "date": "2026-02-01",  # outside requested range
        "Events": [
            {"Chamber": "Legislative Assembly", "TocDocId": "TEST-TOC-X", "Uncorrected": False}
        ],
    },
]


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/year/2026"):
        return httpx.Response(200, json=YEAR_2026)
    if path.endswith("/tableofcontents/TEST-TOC-1"):
        return httpx.Response(
            200,
            content=(FIXTURES / "nsw_toc.xml").read_bytes(),
            headers={"content-type": "application/xml"},
        )
    if path.endswith("/fragment/TEST-FRAG-0001"):
        return httpx.Response(200, content=(FIXTURES / "nsw_fragment_1.xml").read_bytes())
    if path.endswith("/fragment/TEST-FRAG-0002"):
        return httpx.Response(200, content=(FIXTURES / "nsw_fragment_2.xml").read_bytes())
    return httpx.Response(404)


@pytest.fixture
def adapter():
    return NswAdapter(client=httpx.Client(transport=httpx.MockTransport(_handler)))


def test_discover(adapter):
    events = list(adapter.discover(dt.date(2026, 6, 1), dt.date(2026, 6, 30)))
    assert len(events) == 1
    assert events[0].house == "lc"
    assert events[0].source_doc_id == "TEST-TOC-1"


def test_fetch_yields_toc_then_fragments(adapter):
    (event,) = adapter.discover(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    docs = list(adapter.fetch(event))
    assert [d.name for d in docs] == [
        "toc.xml",
        "subject_0001_TEST-FRAG-0001.xml",
        "subject_0002_TEST-FRAG-0002.xml",
    ]


def test_end_to_end_normalize(adapter):
    (event,) = adapter.discover(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    (daily,) = adapter.normalize(adapter.fetch(event))
    assert daily.date == dt.date(2026, 6, 24)
    assert len(daily.proceedings) == 1  # both fragments merged under "Motions"
    subjects = daily.proceedings[0].subjects
    assert [s.uid for s in subjects] == ["TEST-FRAG-0001", "TEST-FRAG-0002"]
    assert daily.extensions["extract_count"] == "2"

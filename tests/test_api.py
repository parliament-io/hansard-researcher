"""Tier 2 search API — deep-link construction, service logic, HTTP surface.

The invariant under test everywhere: responses carry metadata and
official-source links only, never Hansard prose.
"""

from __future__ import annotations

import httpx
import pytest

from hansard_researcher.api import MAX_K, SearchService, build_app, official_url
from hansard_researcher.enrich.embed import embed_texts
from hansard_researcher.enrich.qdrant import QdrantIndex, collection_name, index_embeddings
from hansard_researcher.normalize.silver import write_silver
from test_enrich import FakeEmbedder
from test_qdrant import FakeQdrant

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


# --- deep links: constructed at response time, never stored ------------------


def test_official_url_nsw_subject_permalink():
    payload = {"jurisdiction": "nsw", "subject_uid": "HANSARD-1323879322-167240"}
    assert official_url(payload) == (
        "https://www.parliament.nsw.gov.au/Hansard/Pages/HansardResult.aspx"
        "#/docid/HANSARD-1323879322-167240"
    )


def test_official_url_falls_back_to_harvested_source():
    toc = "https://hansardsearch.parliament.sa.gov.au/api/hansard/lh/2026-06-25/toc"
    assert official_url({"jurisdiction": "sa", "subject_uid": "s1", "source_url": toc}) == toc
    # NSW uid that is not a HANSARD- doc id falls back too
    assert official_url({"jurisdiction": "nsw", "subject_uid": "s1"}) is None
    assert official_url({"jurisdiction": "wa"}) is None


# --- service + HTTP surface ---------------------------------------------------

MODEL = "fake/embed-v1"


@pytest.fixture
def service(synthetic_fragment, tmp_path):
    """A SearchService over the synthetic WA fixture, Qdrant mocked in-memory."""
    write_silver([synthetic_fragment], tmp_path / "silver")
    embed_texts(
        tmp_path, "wa", FakeEmbedder(), provider="test", model=MODEL,
        log=lambda *_: None,
    )
    qdrant = FakeQdrant()
    index = QdrantIndex("http://qdrant.test", transport=httpx.MockTransport(qdrant.handler))
    index_embeddings(tmp_path, "wa", index, MODEL, log=lambda *_: None)
    return SearchService(
        embedder=FakeEmbedder(), model=MODEL, index=index,
        collection=collection_name(MODEL),
    )


@pytest.fixture
def client(service):
    return TestClient(build_app(service=service))


def test_search_returns_citation_metadata_never_prose(client):
    response = client.get(
        "/search", params={"q": "Will the minister regulate widgets?", "k": 2}
    )
    assert response.status_code == 200
    hits = response.json()["hits"]
    assert len(hits) == 2
    top = hits[0]
    assert top["subject"] == "Widget Regulation"
    assert top["speaker"] == "Ms Example"
    assert top["jurisdiction"] == "wa"
    assert top["text_id"] and top["subject_id"]
    # debate context + speaker metadata ride along from the payload
    assert top["proceeding"] == "Questions Without Notice"
    assert top["bills"] == ["Widget Regulation Bill 2026"]
    assert top["party"] == "Example Party"
    assert top["party_abbreviation"] == "EX"
    assert top["electorate"] == "Testville"
    assert top["kind"] == "question"
    assert top["page"] == "7"
    assert top["time"] == "2026-03-04T06:30:00+00:00"
    assert top["parliament_num"] == 41
    assert top["review_stage"] == "uncorrected"
    # structural nulls surface as None (and bills as []), never dropped keys
    assert set(top) == {
        "score", "jurisdiction", "date", "house",
        "subject", "proceeding", "subproceeding", "committee", "bills",
        "speaker", "party", "party_abbreviation", "electorate", "role", "kind",
        "text_kind", "page", "time",
        "parliament_num", "session_num", "review_stage",
        "official_url", "source_url", "text_id", "subject_id",
    }
    # the response contains no prose fields and never echoes text back
    assert "Will the minister regulate widgets?" not in response.text


def test_search_jurisdiction_filter(client):
    response = client.get("/search", params={"q": "widgets", "jurisdiction": "sa"})
    assert response.status_code == 200
    assert response.json()["hits"] == []


def test_search_validation(client):
    assert client.get("/search", params={"q": ""}).status_code == 422
    assert client.get("/search", params={"q": "x", "k": MAX_K + 1}).status_code == 422
    assert client.get("/search", params={"q": "x", "k": 0}).status_code == 422
    assert (
        client.get("/search", params={"q": "x", "jurisdiction": "nz"}).status_code == 422
    )
    assert client.get("/search", params={"q": "   "}).status_code == 422


def test_healthz_and_contract(client, service):
    assert client.get("/healthz").json() == {"status": "ok"}
    contract = client.get("/contract").json()
    assert contract["collection"] == collection_name(MODEL)
    assert contract["embedding_model"] == MODEL
    assert "no Hansard prose" in contract["results"]


def test_home_serves_search_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # the page calls the API with relative URLs so it works behind any proxy prefix
    assert 'fetch("search?' in response.text
    assert 'fetch("healthz")' in response.text


def test_healthz_degrades_without_collection():
    qdrant = FakeQdrant()  # no collection created
    service = SearchService(
        embedder=FakeEmbedder(), model=MODEL,
        index=QdrantIndex("http://qdrant.test", transport=httpx.MockTransport(qdrant.handler)),
        collection=collection_name(MODEL),
    )
    client = TestClient(build_app(service=service))
    assert client.get("/healthz").json() == {"status": "degraded"}

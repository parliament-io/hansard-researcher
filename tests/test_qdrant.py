"""Qdrant index + ANN search backend — against a mocked Qdrant REST server."""

from __future__ import annotations

import json

import httpx
import pytest

from parlhansard.enrich.embed import embed_texts
from parlhansard.enrich.providers import ProviderError
from parlhansard.enrich.qdrant import QdrantIndex, collection_name, index_embeddings
from parlhansard.enrich.search import search_qdrant
from parlhansard.normalize.silver import write_silver
from test_enrich import FakeEmbedder


class FakeQdrant:
    """Minimal in-memory Qdrant REST double."""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.points: dict[str, dict[str, dict]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        name = parts[1]
        if request.method == "GET" and len(parts) == 2:
            if name not in self.collections:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(
                200,
                json={"result": {"config": {"params": {"vectors": self.collections[name]}}}},
            )
        if request.method == "PUT" and len(parts) == 2:
            self.collections[name] = json.loads(request.content)["vectors"]
            self.points[name] = {}
            return httpx.Response(200, json={"result": True})
        if request.method == "PUT" and parts[2] == "points":
            for point in json.loads(request.content)["points"]:
                self.points[name][point["id"]] = point
            return httpx.Response(200, json={"result": {"status": "acknowledged"}})
        if request.method == "POST" and parts[-1] == "search":
            body = json.loads(request.content)
            match = None
            if body.get("filter"):
                match = body["filter"]["must"][0]["match"]["value"]
            candidates = [
                p for p in self.points.get(name, {}).values()
                if match is None or p["payload"]["jurisdiction"] == match
            ]

            def cosine(a, b):
                return sum(x * y for x, y in zip(a, b, strict=True))

            scored = sorted(
                candidates,
                key=lambda p: cosine(p["vector"], body["vector"]),
                reverse=True,
            )[: body["limit"]]
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": p["id"],
                            "score": cosine(p["vector"], body["vector"]),
                            "payload": p["payload"],
                        }
                        for p in scored
                    ]
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")


@pytest.fixture
def data_dir(synthetic_fragment, tmp_path):
    write_silver([synthetic_fragment], tmp_path / "silver")
    embed_texts(
        tmp_path, "wa", FakeEmbedder(), provider="test", model="fake/embed-v1",
        log=lambda *_: None,
    )
    return tmp_path


@pytest.fixture
def qdrant():
    return FakeQdrant()


def _index(qdrant) -> QdrantIndex:
    return QdrantIndex("http://qdrant.test", transport=httpx.MockTransport(qdrant.handler))


def test_index_embeddings_upserts_all_vectors(data_dir, qdrant):
    stats = index_embeddings(
        data_dir, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None
    )
    assert stats == {"points": 2, "created": 1}
    name = collection_name("fake/embed-v1")
    assert qdrant.collections[name] == {"size": FakeEmbedder.dim, "distance": "Cosine"}
    point = next(iter(qdrant.points[name].values()))
    # licensing invariant: join keys only — no Hansard prose enters Qdrant
    assert set(point["payload"]) == {
        "jurisdiction", "date", "house", "subject_id", "talker_id", "model",
    }
    # re-index is an idempotent upsert, not a duplicate
    again = index_embeddings(
        data_dir, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None
    )
    assert again["points"] == 2
    assert len(qdrant.points[name]) == 2


def test_index_without_embeddings_is_a_clear_error(tmp_path, qdrant):
    with pytest.raises(ProviderError, match="enrich embed"):
        index_embeddings(tmp_path, "wa", _index(qdrant), "fake/embed-v1")


def test_dim_mismatch_is_refused(data_dir, qdrant):
    index = _index(qdrant)
    index.ensure_collection(collection_name("fake/embed-v1"), 512)
    with pytest.raises(ProviderError, match="dim"):
        index_embeddings(data_dir, "wa", index, "fake/embed-v1", log=lambda *_: None)


def test_search_qdrant_hydrates_text_from_silver(data_dir, qdrant):
    index_embeddings(data_dir, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None)
    query = "Will the minister regulate widgets?"  # synthetic fixture text
    query_vector = FakeEmbedder().embed([query])[0]
    hits = search_qdrant(
        data_dir, query_vector, "fake/embed-v1", k=2, index=_index(qdrant)
    )
    assert len(hits) == 2
    assert hits[0].text == query
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].subject_name == "Widget Regulation"
    assert hits[0].speaker == "Ms Example"
    # jurisdiction filter is pushed into the Qdrant query
    assert search_qdrant(
        data_dir, query_vector, "fake/embed-v1", jurisdiction="sa", index=_index(qdrant)
    ) == []

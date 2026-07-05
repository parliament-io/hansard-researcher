"""Qdrant index + ANN search backend — against a mocked Qdrant REST server."""

from __future__ import annotations

import json

import httpx
import pytest

from hansard_researcher.enrich.embed import embed_texts
from hansard_researcher.enrich.providers import ProviderError
from hansard_researcher.enrich.qdrant import (
    INDEXING_THRESHOLD_DEFAULT,
    QdrantIndex,
    collection_name,
    index_embeddings,
    prune_index,
)
from hansard_researcher.enrich.search import search_qdrant
from hansard_researcher.model.canonical import Jurisdiction
from hansard_researcher.normalize.silver import write_silver
from test_enrich import FakeEmbedder


class FakeQdrant:
    """Minimal in-memory Qdrant REST double."""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.points: dict[str, dict[str, dict]] = {}
        self.threshold_updates: list[int] = []
        self.fail_upserts = False

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
        if request.method == "PATCH" and len(parts) == 2:
            body = json.loads(request.content)
            self.threshold_updates.append(body["optimizers_config"]["indexing_threshold"])
            return httpx.Response(200, json={"result": True})
        if request.method == "PUT" and parts[2] == "points":
            if self.fail_upserts:
                return httpx.Response(500, json={"status": {"error": "boom"}})
            for point in json.loads(request.content)["points"]:
                self.points[name][point["id"]] = point
            return httpx.Response(200, json={"result": {"status": "acknowledged"}})
        if request.method == "POST" and parts[-1] == "scroll":
            body = json.loads(request.content)
            match = None
            if body.get("filter"):
                match = body["filter"]["must"][0]["match"]["value"]
            ids = [
                p["id"] for p in self.points.get(name, {}).values()
                if match is None or p["payload"]["jurisdiction"] == match
            ]
            return httpx.Response(
                200,
                json={"result": {"points": [{"id": i} for i in ids],
                                 "next_page_offset": None}},
            )
        if request.method == "POST" and parts[-1] == "delete":
            for point_id in json.loads(request.content)["points"]:
                self.points[name].pop(point_id, None)
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
    # bulk-load contract: HNSW building paused for the load, then restored
    assert qdrant.threshold_updates == [0, INDEXING_THRESHOLD_DEFAULT]
    # re-index is an idempotent upsert, not a duplicate
    again = index_embeddings(
        data_dir, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None
    )
    assert again["points"] == 2
    assert len(qdrant.points[name]) == 2


def test_index_prunes_to_requested_jurisdiction(synthetic_fragment, tmp_path, qdrant):
    """The jurisdiction filter is pushed into the parquet scan — a dataset
    holding other jurisdictions' vectors yields only the requested one."""
    import copy

    sa = copy.deepcopy(synthetic_fragment)
    sa.fragment_id = "sa-" + synthetic_fragment.fragment_id
    sa.jurisdiction = Jurisdiction.SA
    write_silver([synthetic_fragment, sa], tmp_path / "silver")
    for code in ("wa", "sa"):
        embed_texts(
            tmp_path, code, FakeEmbedder(), provider="test", model="fake/embed-v1",
            log=lambda *_: None,
        )

    stats = index_embeddings(
        tmp_path, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None
    )
    assert stats["points"] == 2
    points = qdrant.points[collection_name("fake/embed-v1")].values()
    assert {p["payload"]["jurisdiction"] for p in points} == {"wa"}


def test_index_restores_indexing_threshold_on_failure(data_dir, qdrant):
    qdrant.fail_upserts = True
    with pytest.raises(httpx.HTTPStatusError):
        index_embeddings(
            data_dir, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None
        )
    assert qdrant.threshold_updates == [0, INDEXING_THRESHOLD_DEFAULT]


def test_prune_deletes_orphans_from_revised_days(data_dir, synthetic_fragment, qdrant):
    """A revised house-day (draft -> corrected) retires its text_ids; upserts
    never delete, so prune must."""
    import copy

    model = "fake/embed-v1"
    index_embeddings(data_dir, "wa", _index(qdrant), model, log=lambda *_: None)
    name = collection_name(model)
    old_ids = set(qdrant.points[name])

    revised = copy.deepcopy(synthetic_fragment)
    revised.fragment_id = "rev-" + synthetic_fragment.fragment_id
    write_silver([revised], data_dir / "silver")  # same partition, new identity
    embed_texts(
        data_dir, "wa", FakeEmbedder(), provider="test", model=model,
        force=True, log=lambda *_: None,
    )
    index_embeddings(data_dir, "wa", _index(qdrant), model, log=lambda *_: None)
    assert len(qdrant.points[name]) == 4  # old + new: upserts never delete

    stats = prune_index(data_dir, "wa", _index(qdrant), model, log=lambda *_: None)
    assert stats["points_deleted"] == 2
    assert stats["partitions_removed"] == 0
    assert set(qdrant.points[name]).isdisjoint(old_ids)
    assert len(qdrant.points[name]) == 2


def test_prune_drops_embeddings_partitions_without_silver(data_dir, qdrant):
    """A vanished silver partition (identity moved, repair run) kills its
    embeddings slice too — otherwise every future index run re-seeds the
    dead points."""
    import shutil

    model = "fake/embed-v1"
    index_embeddings(data_dir, "wa", _index(qdrant), model, log=lambda *_: None)
    shutil.rmtree(data_dir / "silver" / "texts" / "jurisdiction=wa")

    stats = prune_index(data_dir, "wa", _index(qdrant), model, log=lambda *_: None)
    assert stats["partitions_removed"] == 1
    assert stats["points_deleted"] == 2
    assert qdrant.points[collection_name(model)] == {}
    assert not any((data_dir / "enriched" / "embeddings").rglob("*.parquet"))


def test_prune_without_collection_is_a_noop(data_dir, qdrant):
    stats = prune_index(
        data_dir, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None
    )
    assert stats == {"partitions_removed": 0, "points_checked": 0, "points_deleted": 0}


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

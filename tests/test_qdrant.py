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
        self.payload_indexes: list[str] = []
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
        if request.method == "PUT" and parts[2:] == ["index"]:
            self.payload_indexes.append(json.loads(request.content)["field_name"])
            return httpx.Response(200, json={"result": {"status": "acknowledged"}})
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
    by_speaker = {
        p["payload"]["speaker"]: p["payload"] for p in qdrant.points[name].values()
    }
    question = by_speaker["Ms Example"]
    # licensing invariant: join keys + citation metadata — never body text
    # (nulls are dropped: extract_index/source_url/subproceeding/committee
    # are unset in the fixture)
    assert set(question) == {
        "jurisdiction", "date", "house", "subject_id", "talker_id", "model",
        "subject_uid", "subject_name", "proceeding_name", "bill_names",
        "speaker", "party", "party_abbreviation", "electorate", "role",
        "talker_kind", "text_kind", "page_no", "time_anchor",
        "parliament_num", "session_num", "review_stage",
    }
    assert question["subject_name"] == "Widget Regulation"
    assert question["subject_uid"] == "s1"
    assert question["proceeding_name"] == "Questions Without Notice"
    assert question["bill_names"] == ["Widget Regulation Bill 2026"]
    assert question["party"] == "Example Party"
    assert question["party_abbreviation"] == "EX"
    assert question["electorate"] == "Testville"
    assert question["talker_kind"] == "question"
    assert question["page_no"] == "7"
    assert question["time_anchor"] == "2026-03-04T06:30:00+00:00"
    assert question["parliament_num"] == 41
    assert question["review_stage"] == "uncorrected"
    assert not {"clean_text", "raw_text", "text"} & set(question)
    # structural nulls stay dropped, not stored as 'None'
    answer = by_speaker["Mr Sample"]
    assert answer["talker_kind"] == "answer"
    assert answer["role"] == "minister"
    assert not {"party", "electorate", "page_no", "time_anchor"} & set(answer)
    # bulk-load contract: HNSW building paused for the load, then restored
    assert qdrant.threshold_updates == [0, INDEXING_THRESHOLD_DEFAULT]
    # filterable payload fields get keyword indexes (search + prune scroll)
    assert set(qdrant.payload_indexes) == {"jurisdiction", "house", "date"}
    # re-index is an idempotent upsert, not a duplicate
    again = index_embeddings(
        data_dir, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None
    )
    assert again["points"] == 2
    assert len(qdrant.points[name]) == 2


def test_index_payload_survives_null_embed_time_keys(synthetic_fragment, tmp_path, qdrant):
    """Pre-provenance embeddings slices carry NULL subject/talker/fragment ids —
    citation payloads must come from silver via text_id, never embed-time
    columns."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    write_silver([synthetic_fragment], tmp_path / "silver")
    embed_texts(
        tmp_path, "wa", FakeEmbedder(), provider="test", model="fake/embed-v1",
        log=lambda *_: None,
    )
    emb_file = next((tmp_path / "enriched" / "embeddings").rglob("*.parquet"))
    table = pq.read_table(emb_file)
    for col in ("subject_id", "talker_id", "fragment_id"):
        idx = table.schema.get_field_index(col)
        table = table.set_column(idx, col, pa.nulls(table.num_rows, pa.string()))
    pq.write_table(table, emb_file)

    index_embeddings(tmp_path, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None)
    point = next(iter(qdrant.points[collection_name("fake/embed-v1")].values()))
    assert point["payload"]["subject_name"] == "Widget Regulation"
    assert point["payload"]["subject_id"]  # re-derived from silver, not 'None'
    assert point["payload"]["speaker"] in ("Ms Example", "Mr Sample")


def test_index_joins_subproceeding_names(synthetic_fragment, tmp_path, qdrant):
    """Text inside a bill-stage subproceeding carries the stage name; the
    main fixture (no subproceedings table at all) covers the empty-relation
    fallback in the same suite run."""
    import copy

    from hansard_researcher.model.canonical import Subproceeding, Talker, TextPara

    fragment = copy.deepcopy(synthetic_fragment)
    fragment.proceedings[0].subjects[0].subproceedings = [
        Subproceeding(
            uid="sp1",
            name="Second Reading",
            document_order=7,
            talkers=[
                Talker(
                    uid="t3",
                    document_order=8,
                    name="Ms Example",
                    texts=[
                        TextPara(
                            document_order=9,
                            para_index=0,
                            raw_text="I move that the bill be read a second time.",
                            clean_text="I move that the bill be read a second time.",
                        )
                    ],
                )
            ],
        )
    ]
    write_silver([fragment], tmp_path / "silver")
    embed_texts(
        tmp_path, "wa", FakeEmbedder(), provider="test", model="fake/embed-v1",
        log=lambda *_: None,
    )
    index_embeddings(tmp_path, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None)
    payloads = [p["payload"] for p in qdrant.points[collection_name("fake/embed-v1")].values()]
    stages = {p.get("subproceeding_name") for p in payloads}
    assert stages == {None, "Second Reading"}  # subject-level text has no stage


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


def test_prune_drops_embeddings_and_themes_partitions_without_silver(data_dir, qdrant):
    """A vanished silver partition (identity moved, repair run) kills its
    embeddings AND themes slices — otherwise every future index run re-seeds
    the dead points and stale themes pollute the next aggregate."""
    import shutil

    model = "fake/embed-v1"
    index_embeddings(data_dir, "wa", _index(qdrant), model, log=lambda *_: None)
    # mirror each embeddings partition as a themes partition (all model slugs
    # are swept — themes are not tied to the Qdrant collection)
    embeddings_jur = next(
        (data_dir / "enriched" / "embeddings").glob("model_slug=*")
    ) / "jurisdiction=wa"
    themes_jur = (
        data_dir / "enriched" / "themes" / "model_slug=embedding-other" / "jurisdiction=wa"
    )
    for date_dir in embeddings_jur.glob("date=*"):
        for house_dir in date_dir.glob("house=*"):
            target = themes_jur / date_dir.name / house_dir.name
            target.mkdir(parents=True)
            (target / "part-0.parquet").write_bytes(b"stub")

    shutil.rmtree(data_dir / "silver" / "texts" / "jurisdiction=wa")

    stats = prune_index(data_dir, "wa", _index(qdrant), model, log=lambda *_: None)
    assert stats["partitions_removed"] == 1
    assert stats["theme_partitions_removed"] == 1
    assert stats["points_deleted"] == 2
    assert qdrant.points[collection_name(model)] == {}
    assert not any((data_dir / "enriched" / "embeddings").rglob("*.parquet"))
    assert not any((data_dir / "enriched" / "themes").rglob("*.parquet"))


def test_prune_without_collection_is_a_noop(data_dir, qdrant):
    stats = prune_index(
        data_dir, "wa", _index(qdrant), "fake/embed-v1", log=lambda *_: None
    )
    assert stats == {
        "partitions_removed": 0,
        "theme_partitions_removed": 0,
        "points_checked": 0,
        "points_deleted": 0,
    }


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

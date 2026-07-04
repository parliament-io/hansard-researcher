"""Enrichment provider layer + embed/search stages — no network, no models.

Provider HTTP behaviour is tested against httpx.MockTransport; the embed and
search stages run with a deterministic fake embedder over the synthetic
fixture fragment (no real Hansard text — see LICENSES-DATA.md).
"""

from __future__ import annotations

import hashlib
import math

import httpx
import pyarrow.dataset as ds
import pytest

from parlhansard.enrich.embed import embed_texts, model_slug
from parlhansard.enrich.providers import (
    PRESETS,
    OpenAICompatClient,
    ProviderConfig,
    ProviderError,
    resolve_config,
)
from parlhansard.enrich.search import search
from parlhansard.normalize.silver import write_silver


class FakeEmbedder:
    """Deterministic bag-of-words vectors: identical text -> identical vector."""

    dim = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in text.lower().split():
                digest = int(hashlib.md5(token.encode()).hexdigest(), 16)
                vector[digest % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            out.append([x / norm for x in vector])
        return out


@pytest.fixture(autouse=True)
def _clean_enrich_env(monkeypatch):
    for suffix in ("PROVIDER", "BASE_URL", "API_KEY", "CHAT_MODEL", "EMBED_MODEL"):
        monkeypatch.delenv(f"PARLHANSARD_ENRICH_{suffix}", raising=False)


# --- config resolution -------------------------------------------------------


def test_presets_resolve():
    config = resolve_config("ollama")
    assert config.base_url == "http://localhost:11434/v1"
    assert config.embed_model == "nomic-embed-text"
    assert config.api_key is None  # local server: no key

    assert resolve_config("local").base_url is None
    assert resolve_config("openai").base_url == "https://api.openai.com/v1"


def test_env_overrides_and_custom_provider(monkeypatch):
    monkeypatch.setenv("PARLHANSARD_ENRICH_BASE_URL", "https://vllm.internal/v1")
    monkeypatch.setenv("PARLHANSARD_ENRICH_API_KEY", "sk-byo")
    monkeypatch.setenv("PARLHANSARD_ENRICH_EMBED_MODEL", "my-embedder")
    config = resolve_config()  # no preset: BASE_URL implies 'custom'
    assert (config.provider, config.base_url) == ("custom", "https://vllm.internal/v1")
    assert config.api_key == "sk-byo"
    assert config.embed_model == "my-embedder"

    monkeypatch.setenv("PARLHANSARD_ENRICH_EMBED_MODEL", "mxbai-embed-large")
    assert resolve_config("ollama").embed_model == "mxbai-embed-large"  # env beats preset


def test_unconfigured_provider_is_a_clear_error():
    with pytest.raises(ProviderError, match="optional"):
        resolve_config()
    with pytest.raises(ProviderError, match="options"):
        resolve_config("watson")


# --- OpenAI-compatible client -------------------------------------------------


def _client(handler, **config_overrides) -> OpenAICompatClient:
    config = ProviderConfig(
        provider="custom",
        base_url="https://api.test/v1",
        api_key="sk-test",
        chat_model="chat-1",
        embed_model="embed-1",
    )
    if config_overrides:
        config = ProviderConfig(**{**config.__dict__, **config_overrides})
    return OpenAICompatClient(config, transport=httpx.MockTransport(handler))


def test_embed_sends_auth_and_restores_input_order():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        # deliberately out of order — client must sort by index
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [1.0, 1.0]},
                    {"index": 0, "embedding": [0.0, 0.0]},
                ]
            },
        )

    assert _client(handler).embed(["a", "b"]) == [[0.0, 0.0], [1.0, 1.0]]
    assert seen == {"path": "/v1/embeddings", "auth": "Bearer sk-test"}


def test_client_retries_transient_5xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "themed"}}]}
        )

    assert _client(handler).complete([{"role": "user", "content": "hi"}]) == "themed"
    assert calls["n"] == 2


def test_missing_model_is_a_config_error_not_a_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be sent")

    with pytest.raises(ProviderError, match="EMBED_MODEL"):
        _client(handler, embed_model=None).embed(["a"])


# --- embed + search stages ----------------------------------------------------


@pytest.fixture
def data_dir(synthetic_fragment, tmp_path):
    write_silver([synthetic_fragment], tmp_path / "silver")
    return tmp_path


def _run_embed(data_dir, **kwargs):
    return embed_texts(
        data_dir,
        "wa",
        FakeEmbedder(),
        provider="test",
        model="fake/embed-v1",
        log=lambda *_: None,
        **kwargs,
    )


def test_embed_writes_vectors_without_prose(data_dir):
    stats = _run_embed(data_dir)
    assert stats == {"days": 1, "skipped": 0, "vectors": 2}

    dataset = ds.dataset(
        data_dir / "enriched" / "embeddings", format="parquet", partitioning="hive"
    )
    # licensing invariant: enriched tables carry vectors + keys, never text
    assert not {"raw_text", "clean_text"} & set(dataset.schema.names)
    rows = dataset.to_table().to_pylist()
    assert {r["model"] for r in rows} == {"fake/embed-v1"}
    assert all(r["dim"] == FakeEmbedder.dim and len(r["embedding"]) == r["dim"] for r in rows)
    # model id is partition-safe but preserved verbatim in the model column
    assert {r["model_slug"] for r in rows} == {model_slug("fake/embed-v1")}


def test_embed_is_incremental_until_forced(data_dir):
    _run_embed(data_dir)
    assert _run_embed(data_dir) == {"days": 0, "skipped": 1, "vectors": 0}
    assert _run_embed(data_dir, force=True) == {"days": 1, "skipped": 0, "vectors": 2}


def test_search_ranks_the_matching_paragraph_first(data_dir):
    _run_embed(data_dir)
    query = "Will the minister regulate widgets?"  # synthetic fixture text
    query_vector = FakeEmbedder().embed([query])[0]
    hits = search(data_dir, query_vector, "fake/embed-v1", k=2)
    assert len(hits) == 2
    assert hits[0].text == query
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].subject_name == "Widget Regulation"
    assert hits[0].speaker == "Ms Example"
    # a different model id must find nothing
    assert search(data_dir, query_vector, "other-model") == []


def test_presets_cover_local_and_hosted_options():
    """Decision #3: multiple options — local no-key AND hosted BYO-key."""
    def is_local(preset: dict) -> bool:
        url = preset.get("base_url")
        return url is None or url.startswith("http://localhost")

    local = {name for name, preset in PRESETS.items() if is_local(preset)}
    hosted = set(PRESETS) - local
    assert local and hosted

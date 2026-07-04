"""Pluggable enrichment providers — bring your own processing.

Tier 3 is optional-by-design: nothing in Tiers 1–2 imports this module, and
no provider is ever required or shipped. Two capabilities cover the stage:

- ``embed(texts) -> vectors``      (paragraph embeddings, semantic search)
- ``complete(messages) -> text``   (theme classification)

One HTTP client speaks the **OpenAI-compatible API** (``/v1/embeddings`` +
``/v1/chat/completions``), which covers local servers (Ollama, LM Studio,
vLLM — no key) and hosted BYO-key endpoints (OpenAI, OpenRouter, gateways)
with zero per-vendor code. The ``local`` provider runs sentence-transformers
in-process (optional extra: ``parlhansard[local]``) — embeddings with no
server at all.

Configuration: a ``--provider`` preset and/or ``PARLHANSARD_ENRICH_*`` env
vars (env wins over preset defaults). The API key comes from the environment
only — never from repo config, and never required for local providers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

_ENV = "PARLHANSARD_ENRICH_"

# Preset = a base URL plus (where a safe choice exists) default model names.
# Every field can be overridden by env; hosted endpoints without an obvious
# default (openrouter, custom) require PARLHANSARD_ENRICH_*_MODEL explicitly.
PRESETS: dict[str, dict[str, str | None]] = {
    # local servers — no key
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "embed_model": "nomic-embed-text",
        "chat_model": "llama3.2",
    },
    "lmstudio": {"base_url": "http://localhost:1234/v1"},
    # hosted — BYO key via PARLHANSARD_ENRICH_API_KEY
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "embed_model": "text-embedding-3-small",
        "chat_model": "gpt-4o-mini",
    },
    "openrouter": {"base_url": "https://openrouter.ai/api/v1"},
    # in-process sentence-transformers — no server, no key
    "local": {"base_url": None, "embed_model": "all-MiniLM-L6-v2"},
}


class ProviderError(RuntimeError):
    """Provider misconfiguration or unrecoverable provider response."""


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    base_url: str | None  # None -> in-process local embedder
    api_key: str | None = None
    chat_model: str | None = None
    embed_model: str | None = None


def resolve_config(provider: str | None = None) -> ProviderConfig:
    """Resolve provider config from a preset name + PARLHANSARD_ENRICH_* env."""
    provider = provider or os.environ.get(_ENV + "PROVIDER")
    if provider is None and os.environ.get(_ENV + "BASE_URL"):
        provider = "custom"
    if provider is None:
        raise ProviderError(
            "no enrichment provider configured — pass --provider "
            f"({', '.join(PRESETS)}) or set {_ENV}BASE_URL. Tier 3 is optional: "
            "harvest/normalize/aggregate never need a provider."
        )
    if provider != "custom" and provider not in PRESETS:
        raise ProviderError(
            f"unknown provider {provider!r} — options: {', '.join(PRESETS)}, custom"
        )
    preset = PRESETS.get(provider, {})

    def env(name: str, fallback: str | None = None) -> str | None:
        return os.environ.get(_ENV + name) or fallback

    base_url = env("BASE_URL", preset.get("base_url"))
    if provider == "custom" and not base_url:
        raise ProviderError(f"provider 'custom' needs {_ENV}BASE_URL")
    return ProviderConfig(
        provider=provider,
        base_url=base_url,
        api_key=env("API_KEY"),
        chat_model=env("CHAT_MODEL", preset.get("chat_model")),
        embed_model=env("EMBED_MODEL", preset.get("embed_model")),
    )


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code >= 500 or exc.response.status_code == 429
    )


_RETRY = retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, max=8),
    reraise=True,
)


class OpenAICompatClient:
    """Any OpenAI-compatible endpoint: Ollama/LM Studio/vLLM local, or hosted BYO-key."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not config.base_url:
            raise ProviderError(
                "OpenAICompatClient needs a base_url (use provider 'local' "
                "for in-process embeddings)"
            )
        headers = {}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self.config = config
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @_RETRY
    def _post(self, path: str, payload: dict) -> dict:
        response = self._client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self.config.embed_model
        if not model:
            raise ProviderError(
                f"no embedding model set — set {_ENV}EMBED_MODEL "
                f"(provider {self.config.provider!r} has no default)"
            )
        data = self._post("/embeddings", {"model": model, "input": texts})["data"]
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]

    def complete(self, messages: list[dict], **options) -> str:
        model = self.config.chat_model
        if not model:
            raise ProviderError(
                f"no chat model set — set {_ENV}CHAT_MODEL "
                f"(provider {self.config.provider!r} has no default)"
            )
        data = self._post("/chat/completions", {"model": model, "messages": messages} | options)
        return data["choices"][0]["message"]["content"]


class LocalEmbedder:
    """In-process sentence-transformers embeddings — no server, no key."""

    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ProviderError(
                "provider 'local' needs sentence-transformers — install the "
                "optional extra: uv sync --extra local "
                "(or: pip install 'parlhansard[local]')"
            ) from exc
        self.model_name = model_name
        self._model = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(list(texts), normalize_embeddings=True).tolist()


def get_embedder(config: ProviderConfig) -> LocalEmbedder | OpenAICompatClient:
    """Embedder for the config; ``config.embed_model`` is the id in dedup keys."""
    if config.base_url is None:
        if not config.embed_model:
            raise ProviderError(f"no embedding model set — set {_ENV}EMBED_MODEL")
        return LocalEmbedder(config.embed_model)
    return OpenAICompatClient(config)

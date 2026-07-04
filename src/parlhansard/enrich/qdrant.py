"""Qdrant vector index — optional ANN backend for semantic search.

The default search backend scans the embeddings Parquet with DuckDB, which
is fine for a slice but brute-force over the full archive (6.4M paragraphs).
Qdrant gives indexed approximate search: ``parlhansard enrich index`` loads
the already-computed embeddings into a collection, and
``enrich search --backend qdrant`` queries it.

Plain REST via httpx — no extra client dependency. One collection per
embedding model (``parlhansard__{model_slug}``); the point id is the
deterministic silver ``text_id`` (already a UUID), so re-indexing is an
idempotent upsert. Payloads carry join keys only — **no Hansard prose ever
enters Qdrant**; result text is hydrated from local silver at query time.

Server: ``docker compose --profile enrich up -d`` (or any Qdrant); URL from
``PARLHANSARD_QDRANT_URL`` (default ``http://localhost:6333``).
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pyarrow.dataset as ds

from parlhansard.enrich.embed import model_slug
from parlhansard.enrich.providers import ProviderError

DEFAULT_URL = "http://localhost:6333"

_PAYLOAD_KEYS = ("jurisdiction", "date", "house", "subject_id", "talker_id", "model")


def collection_name(model: str) -> str:
    return f"parlhansard__{model_slug(model)}"


class QdrantIndex:
    def __init__(
        self,
        url: str | None = None,
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = (url or os.environ.get("PARLHANSARD_QDRANT_URL") or DEFAULT_URL).rstrip("/")
        self._client = httpx.Client(base_url=self.url, timeout=timeout, transport=transport)

    def ensure_collection(self, name: str, dim: int) -> bool:
        """Create the collection if missing; returns True when created."""
        response = self._client.get(f"/collections/{name}")
        if response.status_code == 200:
            existing = (
                response.json()["result"]["config"]["params"]["vectors"].get("size")
            )
            if existing != dim:
                raise ProviderError(
                    f"collection {name!r} exists with dim {existing}, embeddings have "
                    f"dim {dim} — different model output? delete the collection first"
                )
            return False
        response = self._client.put(
            f"/collections/{name}",
            json={"vectors": {"size": dim, "distance": "Cosine"}},
        )
        response.raise_for_status()
        return True

    def upsert(self, name: str, points: list[dict]) -> None:
        response = self._client.put(
            f"/collections/{name}/points",
            params={"wait": "true"},
            json={"points": points},
        )
        response.raise_for_status()

    def search(
        self,
        name: str,
        vector: list[float],
        *,
        k: int = 10,
        jurisdiction: str | None = None,
    ) -> list[dict]:
        """Top-k points: ``[{id, score, payload}, ...]``."""
        body: dict = {"vector": vector, "limit": k, "with_payload": True}
        if jurisdiction:
            body["filter"] = {
                "must": [{"key": "jurisdiction", "match": {"value": jurisdiction}}]
            }
        response = self._client.post(f"/collections/{name}/points/search", json=body)
        response.raise_for_status()
        return response.json()["result"]


def index_embeddings(
    data_dir: Path,
    jurisdiction: str,
    index: QdrantIndex,
    model: str,
    *,
    batch_size: int = 512,
    log=print,
) -> dict[str, int]:
    """Load ``data/enriched/embeddings`` for one model+jurisdiction into Qdrant."""
    embeddings_dir = (
        Path(data_dir) / "enriched" / "embeddings" / f"model_slug={model_slug(model)}"
    )
    if not embeddings_dir.is_dir():
        raise ProviderError(
            f"no embeddings for model {model!r} under {embeddings_dir} — "
            "run 'parlhansard enrich embed' first"
        )
    name = collection_name(model)
    created = None
    points_written = 0
    dataset = ds.dataset(embeddings_dir, format="parquet", partitioning="hive")
    for batch in dataset.to_batches(
        columns=["text_id", "embedding", "dim", *(k for k in _PAYLOAD_KEYS if k != "model")],
        batch_size=batch_size,
    ):
        rows = batch.to_pylist()
        rows = [r for r in rows if str(r.get("jurisdiction")) == jurisdiction]
        if not rows:
            continue
        if created is None:
            created = index.ensure_collection(name, rows[0]["dim"])
        index.upsert(
            name,
            [
                {
                    "id": row["text_id"],
                    "vector": row["embedding"],
                    "payload": {
                        **{k: str(row[k]) for k in _PAYLOAD_KEYS if k != "model"},
                        "model": model,
                    },
                }
                for row in rows
            ],
        )
        points_written += len(rows)
        if points_written % (batch_size * 20) < batch_size:
            log(f"  {points_written:,} points indexed")
    return {"points": points_written, "created": int(bool(created))}

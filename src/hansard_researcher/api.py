"""Tier 2 hosted search API — read-only semantic search, metadata-only results.

``hansard-researcher api`` serves a thin FastAPI app (optional extra:
``hansard-researcher[api]``) that embeds the query with the configured
enrichment provider, searches the Qdrant collection, and answers with
scores, citation metadata and official-source links built from point
payloads — **never Hansard prose**. Silver never leaves the archive box;
hydration = run the harvester and join on ``text_id``.

Deep links are constructed here, at response time, from payload keys
(``subject_uid`` / ``extract_index`` / ``source_url``) — never baked into
the index, so a link-format fix is a code change, not a re-index.

Deployment stance (backlog Tier 2): bind localhost behind a TLS +
rate-limit proxy (Caddy/nginx); Qdrant itself is never exposed. Query
embedding follows the same vector-space contract as ``enrich embed``
(same model, no task prefix — query and corpus vectors must share the
space).

The search logic lives on :class:`SearchService` (plain methods,
unit-testable without fastapi); :func:`build_app` wraps it in FastAPI and
serves a single-file search UI (``static/index.html``) at ``/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from hansard_researcher.enrich.providers import ProviderError, get_embedder, resolve_config
from hansard_researcher.enrich.qdrant import QdrantIndex, collection_name

JURISDICTIONS = ("au", "nsw", "sa", "wa")
MAX_K = 50
MAX_QUERY_CHARS = 1_000
STATIC_DIR = Path(__file__).parent / "static"


def official_url(payload: dict) -> str | None:
    """Human-facing official-source link for one hit, best available.

    NSW subject uids are per-subject ``HANSARD-…`` doc ids with a stable
    public permalink. The other jurisdictions fall back to the harvested
    day-level source URL until their human permalink patterns are confirmed
    (AU ParlInfo / SA / WA — backlog Tier 2).
    """
    uid = str(payload.get("subject_uid") or "")
    if payload.get("jurisdiction") == "nsw" and uid.startswith("HANSARD-"):
        return (
            "https://www.parliament.nsw.gov.au/Hansard/Pages/HansardResult.aspx"
            f"#/docid/{uid}"
        )
    return payload.get("source_url")


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class SearchService:
    embedder: Embedder
    model: str
    index: QdrantIndex
    collection: str

    @classmethod
    def from_env(
        cls, *, provider: str | None = None, qdrant_url: str | None = None
    ) -> SearchService:
        config = resolve_config(provider)
        if not config.embed_model:
            raise ProviderError(
                "no embedding model set — set HANSARD_RESEARCHER_ENRICH_EMBED_MODEL"
            )
        return cls(
            embedder=get_embedder(config),
            model=config.embed_model,
            index=QdrantIndex(qdrant_url),
            collection=collection_name(config.embed_model),
        )

    def search(
        self, q: str, *, k: int = 10, jurisdiction: str | None = None
    ) -> list[dict]:
        if not q.strip():
            raise ValueError("empty query")
        if len(q) > MAX_QUERY_CHARS:
            raise ValueError(f"query too long (max {MAX_QUERY_CHARS} chars)")
        if jurisdiction is not None and jurisdiction not in JURISDICTIONS:
            raise ValueError(f"unknown jurisdiction — options: {', '.join(JURISDICTIONS)}")
        vector = self.embedder.embed([q])[0]
        results = self.index.search(
            self.collection, vector, k=max(1, min(k, MAX_K)), jurisdiction=jurisdiction
        )
        return [self._hit(result) for result in results]

    @staticmethod
    def _hit(result: dict) -> dict:
        payload = result.get("payload") or {}
        return {
            "score": result["score"],
            "jurisdiction": payload.get("jurisdiction"),
            "date": payload.get("date"),
            "house": payload.get("house"),
            "subject": payload.get("subject_name"),
            "proceeding": payload.get("proceeding_name"),
            "subproceeding": payload.get("subproceeding_name"),
            "committee": payload.get("committee_name"),
            "bills": payload.get("bill_names") or [],
            "speaker": payload.get("speaker"),
            "party": payload.get("party"),
            "party_abbreviation": payload.get("party_abbreviation"),
            "electorate": payload.get("electorate"),
            "role": payload.get("role"),
            "kind": payload.get("talker_kind"),
            "text_kind": payload.get("text_kind"),
            "page": payload.get("page_no"),
            "time": payload.get("time_anchor"),
            "parliament_num": payload.get("parliament_num"),
            "session_num": payload.get("session_num"),
            "review_stage": payload.get("review_stage"),
            "official_url": official_url(payload),
            "source_url": payload.get("source_url"),
            "text_id": str(result["id"]),
            "subject_id": payload.get("subject_id"),
        }

    def healthy(self) -> bool:
        return self.index.collection_exists(self.collection)

    def contract(self) -> dict:
        return {
            "collection": self.collection,
            "embedding_model": self.model,
            "distance": "cosine",
            "query_contract": "queries are embedded with the same model, no task prefix",
            "results": "metadata + official-source links only — no Hansard prose",
            "hydration": (
                "text_id/subject_id join the open-data pipeline; run the "
                "harvester locally to obtain the text"
            ),
        }


def build_app(
    *,
    provider: str | None = None,
    qdrant_url: str | None = None,
    service: SearchService | None = None,
):
    """FastAPI wrapper over :class:`SearchService` (import needs the api extra)."""
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import FileResponse

    service = service or SearchService.from_env(provider=provider, qdrant_url=qdrant_url)
    app = FastAPI(
        title="hansard-researcher search",
        description="Semantic search over Australian Hansard — metadata and "
        "official-source links only, never prose.",
    )

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok" if service.healthy() else "degraded"}

    @app.get("/contract")
    def contract() -> dict:
        return service.contract()

    @app.get("/search")
    def search(
        q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
        k: int = Query(10, ge=1, le=MAX_K),
        jurisdiction: str | None = None,
    ) -> dict:
        try:
            hits = service.search(q, k=k, jurisdiction=jurisdiction)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (ProviderError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"k": k, "jurisdiction": jurisdiction, "hits": hits}

    return app

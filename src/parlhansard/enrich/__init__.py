"""Optional, pluggable enrichment (Tier 3): embeddings, semantic search,
themes (roadmap), entity links, NER.

Bring your own processing — a local model server (Ollama/LM Studio/vLLM),
in-process sentence-transformers, or any hosted OpenAI-compatible endpoint
with a user-supplied key (see :mod:`parlhansard.enrich.providers`). Tier 1–2
analytics never depend on anything in this package — the structural pipeline
must run with zero API keys and zero models.
"""

"""Theme classification: silver subjects -> ``data/enriched/themes``.

Optional Tier 3 — runs only with a user-configured provider. This is the
"subject baseline" tier of the C# pipeline's 3-tier provenance (paragraph →
subject baseline → bill baseline); paragraph-grain refinement is future work.
Labels come from the open seed taxonomy (:mod:`hansard_researcher.reference.themes`)
for the jurisdiction's locale.

Two engines:

- ``embedding`` (default) — embed each theme as "name — description" once,
  embed each subject as "name + text excerpt", rank by cosine similarity.
  Works with every provider (including in-process ``local``); one embedding
  call per subject.
- ``llm`` — ask a chat model to pick up to ``top_k`` theme ids from the
  catalog. Higher quality, needs a chat-capable endpoint; ids are validated
  against the taxonomy so a chatty response can't invent labels.

Output rows carry theme ids + join keys, **no Hansard prose** (the text is
only ever sent to the provider the user configured). Layout mirrors the
embeddings stage: hive-partitioned (model_slug, jurisdiction, date, house)
with ``delete_matching``; (engine, model, taxonomy version, subject_id) is
the dedup key, so switching providers or bumping the taxonomy re-classifies
cleanly.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds

from hansard_researcher.enrich.embed import Embedder, HouseDay, iter_house_days, model_slug
from hansard_researcher.reference.themes import Theme, load_themes, locale_for

SCHEMA = pa.schema(
    [
        ("subject_id", pa.string()),
        ("fragment_id", pa.string()),
        ("theme_id", pa.string()),
        ("theme_name", pa.string()),
        ("rank", pa.int32()),
        ("score", pa.float32()),  # cosine similarity; null for the llm engine
        ("engine", pa.string()),
        ("model", pa.string()),
        ("provider", pa.string()),
        ("taxonomy_locale", pa.string()),
        ("taxonomy_version", pa.int32()),
        ("model_slug", pa.string()),
        ("jurisdiction", pa.string()),
        ("date", pa.string()),
        ("house", pa.string()),
    ]
)

_PARTITIONING = ds.partitioning(
    pa.schema(
        [
            ("model_slug", pa.string()),
            ("jurisdiction", pa.string()),
            ("date", pa.string()),
            ("house", pa.string()),
        ]
    ),
    flavor="hive",
)


class Completer:
    """Chat capability protocol — satisfied by OpenAICompatClient."""

    def complete(self, messages: list[dict], **options) -> str: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return dot / norm if norm else 0.0


def _subject_docs(day: HouseDay, silver_dir: Path, max_chars: int) -> list[dict]:
    """One classification doc per subject: name + leading text excerpt."""
    relative = day.path.relative_to(silver_dir / "texts")
    subjects_dir = silver_dir / "subjects" / relative
    if not subjects_dir.is_dir():
        return []
    names: dict[str, dict] = {
        row["subject_id"]: row
        for row in ds.dataset(subjects_dir, format="parquet")
        .to_table(columns=["subject_id", "fragment_id", "name"])
        .to_pylist()
    }
    texts: dict[str, list[tuple[int, str]]] = {}
    for row in (
        ds.dataset(day.path, format="parquet")
        .to_table(columns=["subject_id", "clean_text", "document_order"])
        .to_pylist()
    ):
        if row["subject_id"] and row["clean_text"]:
            texts.setdefault(row["subject_id"], []).append(
                (row["document_order"], row["clean_text"])
            )
    docs = []
    for subject_id, subject in names.items():
        body = " ".join(t for _, t in sorted(texts.get(subject_id, [])))
        if not (subject["name"] or body):
            continue
        docs.append(
            {
                "subject_id": subject_id,
                "fragment_id": subject["fragment_id"],
                "doc": f"{subject['name'] or ''}\n{body[:max_chars]}".strip(),
            }
        )
    return docs


def _rank_by_embedding(
    docs: list[dict],
    themes: list[Theme],
    theme_vectors: list[list[float]],
    embedder: Embedder,
    *,
    batch_size: int,
    top_k: int,
    min_score: float,
) -> list[tuple[dict, Theme, int, float]]:
    picks: list[tuple[dict, Theme, int, float]] = []
    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        for doc, vector in zip(
            batch, embedder.embed([d["doc"] for d in batch]), strict=True
        ):
            pairs = zip(themes, theme_vectors, strict=True)
            scored = sorted(
                ((_cosine(vector, tv), theme) for theme, tv in pairs),
                key=lambda pair: pair[0],
                reverse=True,
            )
            for rank, (score, theme) in enumerate(scored[:top_k], start=1):
                if score < min_score:
                    break
                picks.append((doc, theme, rank, score))
    return picks


_LLM_SYSTEM = (
    "You classify parliamentary debate subjects into a fixed theme catalog. "
    "Reply with ONLY a JSON array of up to {top_k} theme ids from the "
    "catalog, most relevant first. Use [] if nothing fits."
)


def _rank_by_llm(
    docs: list[dict],
    themes: list[Theme],
    completer: Completer,
    *,
    top_k: int,
) -> list[tuple[dict, Theme, int, float | None]]:
    catalog = "\n".join(f"{t.theme_id}: {t.name} — {t.description}" for t in themes)
    by_id = {t.theme_id: t for t in themes}
    # match ids longest-first so 'economy-finance' can't be eaten by 'economy'
    id_pattern = re.compile(
        "|".join(re.escape(i) for i in sorted(by_id, key=len, reverse=True))
    )
    picks: list[tuple[dict, Theme, int, float | None]] = []
    for doc in docs:
        response = completer.complete(
            [
                {"role": "system", "content": _LLM_SYSTEM.format(top_k=top_k)},
                {
                    "role": "user",
                    "content": f"Theme catalog:\n{catalog}\n\nDebate subject:\n{doc['doc']}",
                },
            ],
            temperature=0,
        )
        seen: list[str] = []
        for match in id_pattern.finditer(response):
            if match.group(0) not in seen:
                seen.append(match.group(0))
        for rank, theme_id in enumerate(seen[:top_k], start=1):
            picks.append((doc, by_id[theme_id], rank, None))
    return picks


def classify_themes(
    data_dir: Path,
    jurisdiction: str,
    *,
    engine: str,
    model: str,
    provider: str,
    embedder: Embedder | None = None,
    completer: Completer | None = None,
    themes: list[Theme] | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    top_k: int = 3,
    min_score: float = 0.25,
    max_chars: int = 1500,
    batch_size: int = 96,
    include_procedural: bool = False,
    workers: int = 1,
    force: bool = False,
    log=print,
) -> dict[str, int]:
    """Classify every subject; incremental per (engine+model, house-day).

    Procedural themes (question-time, petitions, ...) are excluded from the
    candidate set by default: they label the *kind of business* rather than
    the topic, that fact is already structural (proceeding names, bill_refs,
    petitions tables), and measured on live data they act as attractors —
    with them in the set, half of WA 2026-06-18's subjects ranked a
    procedural theme first. ``include_procedural=True`` restores them.
    """
    silver_dir = Path(data_dir) / "silver"
    out_dir = Path(data_dir) / "enriched" / "themes"
    themes = themes if themes is not None else load_themes(locale_for(jurisdiction))
    if not include_procedural:
        themes = [t for t in themes if not t.procedural]
    slug = model_slug(f"{engine}-{model}")

    theme_vectors: list[list[float]] = []
    if engine == "embedding":
        if embedder is None:
            raise ValueError("embedding engine needs an embedder")
        theme_vectors = embedder.embed([f"{t.name} — {t.description}" for t in themes])
    elif engine == "llm":
        if completer is None:
            raise ValueError("llm engine needs a chat-capable client")
    else:
        raise ValueError(f"unknown engine {engine!r}")

    def classify_day(day: HouseDay) -> int:
        """Classify one house-day; returns labels written (0 = nothing to do)."""
        docs = _subject_docs(day, silver_dir, max_chars)
        if not docs:
            return 0
        if engine == "embedding":
            picks = _rank_by_embedding(
                docs, themes, theme_vectors, embedder,
                batch_size=batch_size, top_k=top_k, min_score=min_score,
            )
        else:
            picks = _rank_by_llm(docs, themes, completer, top_k=top_k)
        rows = [
            {
                "subject_id": doc["subject_id"],
                "fragment_id": doc["fragment_id"],
                "theme_id": theme.theme_id,
                "theme_name": theme.name,
                "rank": rank,
                "score": score,
                "engine": engine,
                "model": model,
                "provider": provider,
                "taxonomy_locale": theme.locale,
                "taxonomy_version": theme.taxonomy_version,
                "model_slug": slug,
                "jurisdiction": day.jurisdiction,
                "date": day.date,
                "house": day.house,
            }
            for doc, theme, rank, score in picks
        ]
        if not rows:
            return 0
        # each house-day writes its own partition dir, so concurrent
        # writes from the worker pool never touch the same files
        ds.write_dataset(
            pa.Table.from_pylist(rows, schema=SCHEMA),
            base_dir=str(out_dir),
            format="parquet",
            partitioning=_PARTITIONING,
            existing_data_behavior="delete_matching",
            basename_template="part-{i}.parquet",
        )
        log(f"  {day.date} {day.house}: {len(docs)} subjects -> {len(rows)} theme labels")
        return len(rows)

    days = skipped = labels = 0
    pending: list[HouseDay] = []
    for day in iter_house_days(silver_dir / "texts", jurisdiction, start, end):
        partition = (
            out_dir / f"model_slug={slug}" / day.path.parent.parent.name
            / day.path.parent.name / day.path.name
        )
        if partition.exists() and not force:
            skipped += 1
        else:
            pending.append(day)

    if workers <= 1:
        for day in pending:
            written = classify_day(day)
            days += 1 if written else 0
            labels += written
    else:
        # embed calls are HTTP-bound: a thread pool keeps `workers` house-days
        # in flight so the provider is never idle between parquet reads/writes
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(classify_day, day) for day in pending]
            for future in as_completed(futures):
                written = future.result()
                days += 1 if written else 0
                labels += written
    return {"days": days, "skipped": skipped, "labels": labels}

"""Theme classification stage — both engines, no network, no models."""

from __future__ import annotations

import pyarrow.dataset as ds
import pytest

from hansard_researcher.enrich.themes import classify_themes
from hansard_researcher.normalize.silver import write_silver
from hansard_researcher.reference.themes import Theme
from test_enrich import FakeEmbedder


def _theme(
    theme_id: str, name: str, description: str, procedural: bool = False
) -> Theme:
    return Theme(
        locale="en-AU",
        taxonomy_version=1,
        theme_id=theme_id,
        name=name,
        description=description,
        procedural=procedural,
    )


# descriptions share vocabulary with the synthetic fixture ("widgets",
# "minister", "regulate") vs none at all — the bag-of-words FakeEmbedder
# makes the overlap deterministic
TAXONOMY = [
    _theme("widget-policy", "Widget Policy", "Will the minister regulate widgets?"),
    _theme("space-affairs", "Space Affairs", "orbital launches satellites astronauts"),
]


@pytest.fixture
def data_dir(synthetic_fragment, tmp_path):
    write_silver([synthetic_fragment], tmp_path / "silver")
    return tmp_path


def _read_labels(data_dir):
    return (
        ds.dataset(data_dir / "enriched" / "themes", format="parquet", partitioning="hive")
        .to_table()
        .to_pylist()
    )


def test_embedding_engine_ranks_overlapping_theme_first(data_dir):
    stats = classify_themes(
        data_dir,
        "wa",
        engine="embedding",
        model="fake/embed-v1",
        provider="test",
        embedder=FakeEmbedder(),
        themes=TAXONOMY,
        min_score=0.05,
        log=lambda *_: None,
    )
    assert stats["days"] == 1 and stats["labels"] >= 1
    labels = _read_labels(data_dir)
    top = [r for r in labels if r["rank"] == 1]
    assert {r["theme_id"] for r in top} == {"widget-policy"}
    assert all(r["engine"] == "embedding" and r["score"] > 0 for r in labels)
    assert all(r["taxonomy_version"] == 1 for r in labels)
    # licensing invariant: theme rows carry ids + keys, never text
    columns = set(
        ds.dataset(
            data_dir / "enriched" / "themes", format="parquet", partitioning="hive"
        ).schema.names
    )
    assert not {"raw_text", "clean_text", "doc"} & columns


def test_procedural_themes_excluded_by_default(data_dir):
    """A procedural theme that would win on vocabulary overlap is not even a
    candidate unless include_procedural=True (kind-of-business labels are
    structural facts, and they attract topical subjects — see themes.py)."""
    taxonomy = [
        *TAXONOMY,
        # deliberately overlaps the fixture better than widget-policy does
        _theme(
            "question-time", "Question Time",
            "Will the minister regulate widgets minister widgets question",
            procedural=True,
        ),
    ]
    kwargs = dict(
        engine="embedding", model="fake/embed-v1", provider="test",
        embedder=FakeEmbedder(), min_score=0.05, log=lambda *_: None,
    )
    classify_themes(data_dir, "wa", themes=taxonomy, **kwargs)
    labels = _read_labels(data_dir)
    assert "question-time" not in {r["theme_id"] for r in labels}
    assert {r["theme_id"] for r in labels if r["rank"] == 1} == {"widget-policy"}

    classify_themes(
        data_dir, "wa", themes=taxonomy, include_procedural=True, force=True,
        **kwargs,
    )
    assert "question-time" in {r["theme_id"] for r in _read_labels(data_dir)}


def test_embedding_engine_is_incremental(data_dir):
    kwargs = dict(
        engine="embedding", model="fake/embed-v1", provider="test",
        embedder=FakeEmbedder(), themes=TAXONOMY, min_score=0.05,
        log=lambda *_: None,
    )
    classify_themes(data_dir, "wa", **kwargs)
    again = classify_themes(data_dir, "wa", **kwargs)
    assert (again["days"], again["skipped"]) == (0, 1)
    forced = classify_themes(data_dir, "wa", force=True, **kwargs)
    assert forced["days"] == 1
    # the worker pool path writes the same result
    pooled = classify_themes(data_dir, "wa", force=True, workers=4, **kwargs)
    assert pooled["days"] == 1 and pooled["labels"] == forced["labels"]


class FakeCompleter:
    """Returns a chatty response — ids must still parse and validate."""

    def __init__(self):
        self.calls = []

    def complete(self, messages, **options):
        self.calls.append(messages)
        assert options.get("temperature") == 0
        return (
            'Sure! The best fits are ["widget-policy", "not-a-real-theme"] '
            "based on the excerpt."
        )


def test_llm_engine_validates_ids_against_taxonomy(data_dir):
    completer = FakeCompleter()
    stats = classify_themes(
        data_dir,
        "wa",
        engine="llm",
        model="chat-1",
        provider="test",
        completer=completer,
        themes=TAXONOMY,
        log=lambda *_: None,
    )
    assert stats["labels"] >= 1
    labels = _read_labels(data_dir)
    assert {r["theme_id"] for r in labels} == {"widget-policy"}  # invented id dropped
    assert all(r["score"] is None and r["engine"] == "llm" for r in labels)
    # the prompt carried the catalog and the subject doc
    prompt = completer.calls[0][1]["content"]
    assert "Widget Policy" in prompt and "Widget Regulation" in prompt


def test_engine_requires_matching_client(data_dir):
    with pytest.raises(ValueError, match="embedder"):
        classify_themes(
            data_dir, "wa", engine="embedding", model="m", provider="p",
            themes=TAXONOMY, log=lambda *_: None,
        )
    with pytest.raises(ValueError, match="chat"):
        classify_themes(
            data_dir, "wa", engine="llm", model="m", provider="p",
            themes=TAXONOMY, log=lambda *_: None,
        )

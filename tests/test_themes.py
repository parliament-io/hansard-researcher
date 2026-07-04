"""Theme taxonomy — YAML validity, locale coverage, loader invariants."""

from __future__ import annotations

import pytest

from parlhansard.model.canonical import Jurisdiction
from parlhansard.reference.themes import (
    JURISDICTION_LOCALES,
    load_themes,
    locale_for,
)


def test_every_jurisdiction_resolves_to_a_seeded_locale():
    seeded = {t.locale for t in load_themes()}
    for jurisdiction in Jurisdiction:
        assert locale_for(jurisdiction.value) in seeded
    assert set(JURISDICTION_LOCALES) == {j.value for j in Jurisdiction}


def test_locale_lists_are_bounded_and_unique():
    for locale in ("en-AU", "en-NZ", "en-GB"):
        themes = load_themes(locale)
        assert 20 <= len(themes) <= 30  # PRD bound: <=30 broad categories per locale
        assert len({t.theme_id for t in themes}) == len(themes)
        assert len({t.name.lower() for t in themes}) == len(themes)
        assert all(t.description for t in themes)
        assert all(t.taxonomy_version == 1 for t in themes)


def test_locale_vocabularies_differ_where_they_should():
    au = {t.theme_id for t in load_themes("en-AU")}
    nz = {t.theme_id for t in load_themes("en-NZ")}
    assert "first-nations" in au and "first-nations" not in nz
    assert "te-tiriti-treaty-settlements" in nz and "maori-affairs" in nz
    assert "climate-net-zero" in {t.theme_id for t in load_themes("en-GB")}


def test_unknown_locale_is_an_error():
    with pytest.raises(ValueError, match="no theme taxonomy"):
        load_themes("fr-CA")  # not seeded: no target jurisdiction yet

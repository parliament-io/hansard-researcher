"""Theme taxonomy loader — open seed catalog, per locale.

Ports the C# pipeline's curated theme catalog: ≤30 broad debate categories
per locale, used as the label space for the Phase 4 theme classifier. The
locale matters — an Australian house and a New Zealand house have materially
different debate vocabularies, and classifying against the wrong list
poisons accuracy — so each jurisdiction resolves to a locale list.

The YAML files ship with the package (``themes/{locale}.yaml``). Names and
descriptions are freshly authored category summaries (never Hansard text).
Each file carries a ``version``; the taxonomy version joins the model id in
every enrichment dedup key, so evolving the catalog re-classifies cleanly.
Extend by PR — a test validates ids, uniqueness, and locale coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

import yaml

#: jurisdiction -> taxonomy locale (all six target jurisdictions covered)
JURISDICTION_LOCALES = {
    "wa": "en-AU",
    "sa": "en-AU",
    "nsw": "en-AU",
    "au": "en-AU",
    "nz": "en-NZ",
    "scot": "en-GB",
}


@dataclass(frozen=True)
class Theme:
    locale: str
    taxonomy_version: int
    theme_id: str
    name: str
    description: str
    #: kind-of-business themes (question-time, petitions, ...) rather than
    #: topics. Structurally derivable (proceeding names, bill_refs, petition
    #: tables), and measured to act as attractors under embedding
    #: classification — the classifier excludes them by default.
    procedural: bool = False


def locale_for(jurisdiction: str) -> str:
    """Taxonomy locale for a jurisdiction code (defaults to en-AU)."""
    return JURISDICTION_LOCALES.get(jurisdiction, "en-AU")


def load_themes(locale: str | None = None) -> list[Theme]:
    """Themes for one locale, or all locales when ``locale`` is None."""
    themes: list[Theme] = []
    theme_dir = resources.files("hansard_researcher.reference") / "themes"
    for entry in sorted(theme_dir.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".yaml"):
            continue
        doc = yaml.safe_load(entry.read_text(encoding="utf-8"))
        if locale is not None and doc["locale"] != locale:
            continue
        seen: set[str] = set()
        for item in doc["themes"]:
            if item["id"] in seen:
                raise ValueError(f"{entry.name}: duplicate theme id {item['id']!r}")
            seen.add(item["id"])
            themes.append(
                Theme(
                    locale=doc["locale"],
                    taxonomy_version=int(doc["version"]),
                    theme_id=item["id"],
                    name=item["name"].strip(),
                    description=item["description"].strip(),
                    procedural=bool(item.get("procedural", False)),
                )
            )
    if locale is not None and not themes:
        raise ValueError(f"no theme taxonomy for locale {locale!r}")
    return themes

"""Stage vocabulary loader — curated YAML per jurisdiction.

Ports the C# pipeline's stage-vocabulary concept: a small, crowd-sourceable
mapping from subproceeding names *as published* to canonical bill stages, so
a bill's journey compares across jurisdictions ("Agreement in Principle" in
the NSW LA and "Second Reading" elsewhere are the same stage). Matching is
case-insensitive exact; unmapped names simply keep a null canonical stage —
the raw label always survives in gold.

The YAML files live next to this module (``stages/{jurisdiction}.yaml``) and
ship with the package. To extend: add the observed name under the right
stage and open a PR; a test validates stage ids and duplicate names.
"""

from __future__ import annotations

from importlib import resources

import yaml

#: canonical stages in progression order (index = sort key in gold)
STAGE_ORDER = (
    "introduction",
    "first_reading",
    "second_reading",
    "referral",
    "committee",
    "recommittal",
    "report",
    "third_reading",
    "passed",
    "returned",
    "messages",
    "conference",
    "final_stages",
    "assent",
)


def load_stage_vocab() -> list[dict]:
    """All jurisdictions' mappings as rows for the gold build.

    Row shape: ``{jurisdiction, name_lower, stage, stage_order}``.
    """
    rows: list[dict] = []
    stage_dir = resources.files("hansard_researcher.reference") / "stages"
    for entry in sorted(stage_dir.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".yaml"):
            continue
        doc = yaml.safe_load(entry.read_text(encoding="utf-8"))
        jurisdiction = doc["jurisdiction"]
        seen: set[str] = set()
        for stage, names in doc["stages"].items():
            if stage not in STAGE_ORDER:
                raise ValueError(f"{entry.name}: unknown stage {stage!r}")
            for name in names:
                key = name.strip().lower()
                if key in seen:
                    raise ValueError(f"{entry.name}: duplicate name {name!r}")
                seen.add(key)
                rows.append(
                    {
                        "jurisdiction": jurisdiction,
                        "name_lower": key,
                        "stage": stage,
                        "stage_order": STAGE_ORDER.index(stage),
                    }
                )
    return rows

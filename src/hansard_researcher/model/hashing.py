"""Content hashing — the pipeline's change-detection signal.

Ports the concept of the C# ``AnalyticsCanonicalHash``: a fragment's hash is
computed over its canonical JSON serialization with volatile fields excluded,
so a re-download that only bumps ``review_stage`` (uncorrected → published
render churn), ``date_modified`` or provenance fields does not register as a
content change, while any change to the actual proceedings does.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hansard_researcher.model.canonical import Fragment

#: Fragment fields that change without the debate content changing.
VOLATILE_FRAGMENT_FIELDS = frozenset(
    {"review_stage", "date_modified", "source_url", "retrieved_at"}
)


def canonical_json(payload: Any) -> str:
    """Serialize to JSON with a canonical byte representation."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fragment_content_hash(fragment: Fragment) -> str:
    """Stable content hash of a fragment, excluding volatile fields."""
    payload = fragment.model_dump(mode="json", exclude=set(VOLATILE_FRAGMENT_FIELDS))
    return sha256_hex(canonical_json(payload))

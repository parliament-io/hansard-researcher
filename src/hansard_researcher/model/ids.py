"""Deterministic id factory.

Ports the concept of the C# ``AnalyticsIdFactory``: every pipeline row id is a
pure function of its natural key, so re-running any stage is idempotent and
ids are stable across machines and runs.
"""

from __future__ import annotations

import uuid

# Fixed project namespace — derived once from a constant name, never changes.
# Keeps the original "parlhansard" seed after the rename to Hansard Researcher:
# changing it would rewrite every deterministic id in the existing archive.
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "parlhansard")

# Unit separator: cannot appear in natural-key parts, so ("a", "bc") can never
# collide with ("ab", "c").
_SEP = "\x1f"


def deterministic_id(*parts: object) -> str:
    """Return a stable UUIDv5 string for the given natural-key parts.

    ``None`` is encoded distinctly from the empty string; all other values are
    stringified verbatim (case-sensitive — source ids may be case-significant).
    """
    encoded = _SEP.join("\x00" if p is None else str(p) for p in parts)
    return str(uuid.uuid5(_NAMESPACE, encoded))

"""Per-jurisdiction harvest adapters.

Importing this package registers all six adapters.
"""

from hansard_researcher.harvest import (  # noqa: F401 — registration side effect
    au,
    nsw,
    nz,
    sa,
    scot,
    wa,
)
from hansard_researcher.harvest.base import (
    HarvestAdapter,
    RawDocument,
    SittingEvent,
    all_adapters,
    get_adapter,
)

__all__ = [
    "HarvestAdapter",
    "RawDocument",
    "SittingEvent",
    "all_adapters",
    "get_adapter",
]

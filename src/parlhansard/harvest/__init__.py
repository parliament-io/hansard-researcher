"""Per-jurisdiction harvest adapters.

Importing this package registers all six adapters.
"""

from parlhansard.harvest import au, nsw, nz, sa, scot, wa  # noqa: F401 — registration side effect
from parlhansard.harvest.base import (
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

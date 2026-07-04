"""Harvest adapter contract.

Each jurisdiction implements three steps:

- ``discover(start, end)`` — enumerate sitting events (one per fetchable
  document) in a date range, from a calendar/API/URL pattern.
- ``fetch(event)`` — download the raw document(s) exactly as published.
  Raw bytes are immutable and stored verbatim (audit + reprocessing).
- ``normalize(docs)`` — map raw documents into canonical
  :class:`~hansard_researcher.model.canonical.Fragment` objects.

WA/SA normalize ≈ validate + load (they publish the canonical schema family);
NSW/Federal are real transforms; NZ/Scotland are placeholders — see
docs/ROADMAP.md.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from hansard_researcher.model.canonical import Fragment, Jurisdiction

#: Some government WAFs (aph.gov.au, parlinfo) reject non-browser user agents
#: with 403. Used only where required; the WA/SA API accepts our own UA.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class SittingEvent:
    """One fetchable Hansard document for one sitting."""

    jurisdiction: Jurisdiction
    date: dt.date
    house: str | None = None
    source_doc_id: str | None = None
    url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RawDocument:
    """A source document exactly as published — never modified."""

    event: SittingEvent
    content: bytes
    media_type: str = "text/xml"
    url: str | None = None
    retrieved_at: dt.datetime | None = None
    #: filename used by the raw store (e.g. ``toc.json``, ``subject_0001.xml``)
    name: str = "document"


class HarvestAdapter(ABC):
    """Base class for per-jurisdiction harvest adapters."""

    jurisdiction: Jurisdiction
    #: "live" or "placeholder" — shown by ``hansard-researcher sources``
    status: str
    #: one-line source description shown by ``hansard-researcher sources``
    source: str

    @abstractmethod
    def discover(self, start: dt.date, end: dt.date) -> Iterator[SittingEvent]:
        """Yield sitting events in ``[start, end]``."""

    @abstractmethod
    def fetch(self, event: SittingEvent) -> Iterator[RawDocument]:
        """Yield the raw document(s) for one sitting event."""

    @abstractmethod
    def normalize(self, docs: Iterable[RawDocument]) -> Iterator[Fragment]:
        """Map raw documents into canonical fragments."""


_REGISTRY: dict[Jurisdiction, type[HarvestAdapter]] = {}


def register(cls: type[HarvestAdapter]) -> type[HarvestAdapter]:
    """Class decorator: register an adapter for its jurisdiction."""
    _REGISTRY[cls.jurisdiction] = cls
    return cls


def get_adapter(jurisdiction: Jurisdiction | str) -> HarvestAdapter:
    """Instantiate the registered adapter for a jurisdiction."""
    key = Jurisdiction(jurisdiction)
    try:
        return _REGISTRY[key]()
    except KeyError:  # pragma: no cover — all six are registered below
        raise LookupError(f"no adapter registered for {key.value!r}") from None


def all_adapters() -> list[type[HarvestAdapter]]:
    """All registered adapter classes, in enum order."""
    return [_REGISTRY[j] for j in Jurisdiction if j in _REGISTRY]

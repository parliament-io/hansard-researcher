"""Filesystem raw store — immutable, as-published source documents.

Layout::

    data/raw/{jurisdiction}/{date}/{house}/
        toc.json
        subject_0001.xml      # extract 001 = ToC subject index 1
        subject_0002.xml
        meta.json             # event info + retrieval provenance

Raw bytes are stored verbatim (audit + reprocessing). Writes are idempotent:
an existing file is only rewritten with ``force=True``.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from parlhansard.model.canonical import Jurisdiction


@dataclass(frozen=True)
class StoredDay:
    jurisdiction: Jurisdiction
    date: dt.date
    house: str
    path: Path

    @property
    def subject_files(self) -> list[Path]:
        return sorted(self.path.glob("subject_*.xml"))

    @property
    def xml_files(self) -> list[Path]:
        """All content XML documents for the day (excludes the ToC)."""
        return sorted(
            p for p in self.path.glob("*.xml") if not p.name.startswith("toc")
        )


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def day_dir(self, jurisdiction: Jurisdiction, date: dt.date, house: str) -> Path:
        return self.root / jurisdiction.value / date.isoformat() / house

    def save(
        self,
        jurisdiction: Jurisdiction,
        date: dt.date,
        house: str,
        name: str,
        content: bytes,
        *,
        force: bool = False,
    ) -> tuple[Path, bool]:
        """Write one raw document; returns (path, written)."""
        path = self.day_dir(jurisdiction, date, house) / name
        if path.exists() and not force:
            return path, False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path, True

    def save_meta(
        self, jurisdiction: Jurisdiction, date: dt.date, house: str, meta: dict
    ) -> Path:
        path = self.day_dir(jurisdiction, date, house) / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        return path

    def iter_days(
        self,
        jurisdiction: Jurisdiction,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> Iterator[StoredDay]:
        base = self.root / jurisdiction.value
        if not base.is_dir():
            return
        for date_dir in sorted(base.iterdir()):
            try:
                date = dt.date.fromisoformat(date_dir.name)
            except ValueError:
                continue
            if (start and date < start) or (end and date > end):
                continue
            for house_dir in sorted(p for p in date_dir.iterdir() if p.is_dir()):
                yield StoredDay(jurisdiction, date, house_dir.name, house_dir)

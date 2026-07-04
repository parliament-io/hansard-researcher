"""Canonical member register — one Parquet table across jurisdictions.

One row per person per jurisdiction (per-parliament/party time-slicing is a
later refinement — SA's source is one-row-per-person). ``source_member_id``
joins directly to silver ``talkers.member_source_id`` / division votes;
``member_id`` is the canonical deterministic id used by gold.

Hive-partitioned by jurisdiction with ``delete_matching`` — rebuilding one
jurisdiction's register replaces exactly that partition.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds

from parlhansard.model.ids import deterministic_id

SCHEMA = pa.schema(
    [
        ("member_id", pa.string()),
        ("source_member_id", pa.string()),  # joins talkers.member_source_id
        ("display_name", pa.string()),
        ("title", pa.string()),
        ("first_name", pa.string()),
        ("other_names", pa.string()),
        ("last_name", pa.string()),
        ("date_of_birth", pa.date32()),
        ("house", pa.string()),  # verbatim silver house name
        ("electorate", pa.string()),
        ("party_name", pa.string()),
        ("is_current", pa.bool_()),
        ("deceased", pa.bool_()),
        ("elected_date", pa.date32()),
        ("archived_date", pa.date32()),
        ("retrieved_at", pa.timestamp("us", tz="UTC")),
        ("jurisdiction", pa.string()),
    ]
)


def member_id(jurisdiction: str, source_member_id: object) -> str:
    return deterministic_id(jurisdiction, "member", source_member_id)


def write_register(rows: list[dict], out_dir: Path) -> int:
    """Write member rows to ``{out_dir}/members`` (replaces the jurisdiction)."""
    normalized = [{name: row.get(name) for name in SCHEMA.names} for row in rows]
    ds.write_dataset(
        pa.Table.from_pylist(normalized, schema=SCHEMA),
        base_dir=str(Path(out_dir) / "members"),
        format="parquet",
        partitioning=ds.partitioning(
            pa.schema([("jurisdiction", pa.string())]), flavor="hive"
        ),
        existing_data_behavior="delete_matching",
        basename_template="part-{i}.parquet",
    )
    return len(rows)

"""Silver layer: flatten canonical fragments into Parquet tables.

Tables (each row carries ``fragment_id``, ``jurisdiction``, ``date``,
``house`` for joining/partition pruning):

fragments, proceedings, subjects, subproceedings, clauses, talkers, texts,
divisions, division_votes, bill_refs, attendees, meeting_time_marks.

Row ids are deterministic (:func:`deterministic_id` over the natural key), so
re-normalizing a day yields identical ids. Datasets are hive-partitioned by
``jurisdiction``/``date``/``house`` and written with ``delete_matching`` —
re-running a house-day atomically replaces exactly that partition
(idempotent). ``house`` MUST be in the partition key: both chambers usually
sit on the same date, and a (jurisdiction, date) partition would let the
second house's write delete the first's rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from hansard_researcher.model.canonical import Clause, Division, Fragment, Subproceeding, Talker
from hansard_researcher.model.hashing import fragment_content_hash
from hansard_researcher.model.ids import deterministic_id

TABLES = (
    "fragments",
    "proceedings",
    "subjects",
    "subproceedings",
    "clauses",
    "talkers",
    "texts",
    "divisions",
    "division_votes",
    "bill_refs",
    "attendees",
    "meeting_time_marks",
)

_TS = pa.timestamp("us", tz="UTC")

_COMMON = [
    ("fragment_id", pa.string()),
    ("jurisdiction", pa.string()),
    ("date", pa.string()),
    ("house", pa.string()),
]

SCHEMAS: dict[str, pa.Schema] = {
    "fragments": pa.schema(
        _COMMON
        + [
            ("committee_name", pa.string()),
            ("source_doc_id", pa.string()),
            ("schema_version", pa.string()),
            ("name", pa.string()),
            ("venue", pa.string()),
            ("parliament_num", pa.int32()),
            ("session_num", pa.int32()),
            ("parliament_name", pa.string()),
            ("session_name", pa.string()),
            ("review_stage", pa.string()),
            ("start_time", _TS),
            ("end_time", _TS),
            ("start_page", pa.string()),
            ("end_page", pa.string()),
            ("lang", pa.string()),
            ("source_url", pa.string()),
            ("retrieved_at", _TS),
            ("extract_count", pa.int32()),
            ("content_hash", pa.string()),
        ]
    ),
    "proceedings": pa.schema(
        _COMMON
        + [
            ("proceeding_id", pa.string()),
            ("uid", pa.string()),
            ("name", pa.string()),
            ("continued", pa.bool_()),
            ("document_order", pa.int32()),
        ]
    ),
    "subjects": pa.schema(
        _COMMON
        + [
            ("subject_id", pa.string()),
            ("proceeding_id", pa.string()),
            ("uid", pa.string()),
            ("name", pa.string()),
            ("names", pa.string()),  # JSON array
            ("committee_name", pa.string()),
            ("extract_index", pa.int32()),
            ("document_order", pa.int32()),
        ]
    ),
    "subproceedings": pa.schema(
        _COMMON
        + [
            ("subproceeding_id", pa.string()),
            ("subject_id", pa.string()),
            ("uid", pa.string()),
            ("name", pa.string()),
            ("document_order", pa.int32()),
        ]
    ),
    "clauses": pa.schema(
        _COMMON
        + [
            ("clause_id", pa.string()),
            ("subject_id", pa.string()),
            ("subproceeding_id", pa.string()),
            ("uid", pa.string()),
            ("name", pa.string()),
            ("document_order", pa.int32()),
        ]
    ),
    "talkers": pa.schema(
        _COMMON
        + [
            ("talker_id", pa.string()),
            ("proceeding_id", pa.string()),
            ("subject_id", pa.string()),
            ("subproceeding_id", pa.string()),
            ("clause_id", pa.string()),
            ("division_id", pa.string()),
            ("uid", pa.string()),
            ("member_source_id", pa.string()),
            ("member_reference_id", pa.string()),
            ("name", pa.string()),
            ("role", pa.string()),
            ("kind", pa.string()),
            ("party", pa.string()),
            ("party_source_id", pa.string()),
            ("party_abbreviation", pa.string()),
            ("electorate", pa.string()),
            ("portfolios", pa.string()),  # JSON array
            ("first_speech", pa.bool_()),
            ("continued", pa.bool_()),
            ("start_time", _TS),
            # provenance for the day-level normalize passes: kind_inferred
            # separates source markup from same-member inference;
            # time_source is 'clock' for span-derived wall-clock readings
            # (document times leave it null); clock_rolled marks turns the
            # midnight rollover moved to the next date
            ("kind_inferred", pa.bool_()),
            ("time_source", pa.string()),
            ("clock_rolled", pa.bool_()),
            ("paragraph_count", pa.int32()),
            ("word_count", pa.int32()),
            ("document_order", pa.int32()),
        ]
    ),
    "texts": pa.schema(
        _COMMON
        + [
            ("text_id", pa.string()),
            ("proceeding_id", pa.string()),
            ("subject_id", pa.string()),
            ("subproceeding_id", pa.string()),
            ("clause_id", pa.string()),
            ("talker_id", pa.string()),
            ("division_id", pa.string()),
            ("source_id", pa.string()),
            ("kind", pa.string()),
            ("raw_text", pa.string()),
            ("clean_text", pa.string()),
            ("para_index", pa.int32()),
            ("page_no", pa.string()),
            ("time_anchor", _TS),
            ("style", pa.string()),
            ("mapped_style", pa.string()),
            ("document_order", pa.int32()),
        ]
    ),
    "divisions": pa.schema(
        _COMMON
        + [
            ("division_id", pa.string()),
            ("subject_id", pa.string()),
            ("subproceeding_id", pa.string()),
            ("clause_id", pa.string()),
            ("uid", pa.string()),
            ("source_id", pa.string()),
            ("result", pa.string()),
            ("ayes_count", pa.int32()),
            ("noes_count", pa.int32()),
            ("pairs_count", pa.int32()),
            ("abstentions_count", pa.int32()),
            ("document_order", pa.int32()),
        ]
    ),
    "division_votes": pa.schema(
        _COMMON
        + [
            ("vote_id", pa.string()),
            ("division_id", pa.string()),
            ("member_source_id", pa.string()),
            ("member_name", pa.string()),
            ("vote", pa.string()),
            ("teller", pa.bool_()),
            ("proxy", pa.bool_()),
            ("proxy_source_id", pa.string()),
            ("proxy_name", pa.string()),
            ("party", pa.string()),
        ]
    ),
    "bill_refs": pa.schema(
        _COMMON
        + [
            ("bill_ref_id", pa.string()),
            ("subject_id", pa.string()),
            ("uid", pa.string()),
            ("source_id", pa.string()),
            ("reference_id", pa.string()),
            ("name", pa.string()),
        ]
    ),
    "attendees": pa.schema(
        _COMMON
        + [
            ("attendee_id", pa.string()),
            ("kind", pa.string()),
            ("name", pa.string()),
            ("reference_id", pa.string()),
        ]
    ),
    "meeting_time_marks": pa.schema(
        _COMMON
        + [
            ("mark_id", pa.string()),
            ("kind", pa.string()),
            ("time", _TS),
            ("label", pa.string()),
        ]
    ),
}


def _word_count(texts) -> int:
    return sum(len(t.clean_text.split()) for t in texts)


class _Flattener:
    def __init__(self, fragment: Fragment) -> None:
        self.f = fragment
        self.rows: dict[str, list[dict]] = {t: [] for t in TABLES}

    def _common(self) -> dict:
        return {
            "fragment_id": self.f.fragment_id,
            "jurisdiction": self.f.jurisdiction.value,
            "date": self.f.date.isoformat(),
            "house": self.f.house,
        }

    def _text_row(self, text, *, para_scope: str, **parents) -> None:
        row = self._common() | {
            "text_id": deterministic_id(
                self.f.fragment_id, "text", text.source_id or "", para_scope, text.document_order
            ),
            "source_id": text.source_id,
            "kind": text.kind.value,
            "raw_text": text.raw_text,
            "clean_text": text.clean_text,
            "para_index": text.para_index,
            "page_no": text.page_no,
            "time_anchor": text.time_anchor,
            "style": text.style,
            "mapped_style": text.mapped_style,
            "document_order": text.document_order,
        }
        row.update(parents)
        self.rows["texts"].append(row)

    def _talker_row(self, talker: Talker, **parents) -> str:
        talker_id = deterministic_id(
            self.f.fragment_id, "talker", talker.uid or "", talker.document_order
        )
        self.rows["talkers"].append(
            self._common()
            | {
                "talker_id": talker_id,
                "uid": talker.uid,
                "member_source_id": talker.member_source_id,
                "member_reference_id": talker.member_reference_id,
                "name": talker.name,
                "role": talker.role.value if talker.role else None,
                "kind": talker.kind.value if talker.kind else None,
                "party": talker.party,
                "party_source_id": talker.party_source_id,
                "party_abbreviation": talker.extensions.get("party_abbreviation"),
                "electorate": talker.electorate,
                "portfolios": json.dumps(talker.portfolios) if talker.portfolios else None,
                "first_speech": talker.first_speech,
                "continued": talker.continued,
                "start_time": talker.start_time,
                "kind_inferred": "kind_inferred" in talker.extensions,
                "time_source": talker.extensions.get("time_source"),
                "clock_rolled": talker.extensions.get("clock_rolled") == "1",
                "paragraph_count": len(talker.texts),
                "word_count": _word_count(talker.texts),
                "document_order": talker.document_order,
            }
            | parents
        )
        for text in talker.texts:
            self._text_row(text, para_scope=talker_id, talker_id=talker_id, **parents)
        return talker_id

    def _division_rows(self, division: Division, **parents) -> None:
        division_id = deterministic_id(
            self.f.fragment_id, "division", division.uid or "", division.document_order
        )
        self.rows["divisions"].append(
            self._common()
            | {
                "division_id": division_id,
                "uid": division.uid,
                "source_id": division.source_id,
                "result": division.result.value if division.result else None,
                "ayes_count": division.ayes_count,
                "noes_count": division.noes_count,
                "pairs_count": division.pairs_count,
                "abstentions_count": division.abstentions_count,
                "document_order": division.document_order,
            }
            | parents
        )
        for text in division.texts:
            self._text_row(text, para_scope=division_id, division_id=division_id, **parents)
        for talker in division.talkers:
            self._talker_row(talker, division_id=division_id, **parents)
        for vote in division.votes:
            self.rows["division_votes"].append(
                self._common()
                | {
                    "vote_id": deterministic_id(
                        division_id, "vote", vote.member_source_id or vote.member_name or "",
                        vote.document_order,
                    ),
                    "division_id": division_id,
                    "member_source_id": vote.member_source_id,
                    "member_name": vote.member_name,
                    "vote": vote.vote.value,
                    "teller": vote.teller,
                    "proxy": vote.proxy,
                    "proxy_source_id": vote.proxy_source_id,
                    "proxy_name": vote.proxy_name,
                    "party": vote.party,
                }
            )

    def _container_rows(
        self, node: Subproceeding | Clause, table: str, id_field: str, **parents
    ) -> str:
        node_id = deterministic_id(
            self.f.fragment_id, table, node.uid or "", node.document_order
        )
        self.rows[table].append(
            self._common()
            | {
                id_field: node_id,
                "uid": node.uid,
                "name": node.name,
                "document_order": node.document_order,
            }
            | parents
        )
        child_parents = dict(parents) | {id_field: node_id}
        for text in node.texts:
            self._text_row(text, para_scope=node_id, **child_parents)
        for talker in node.talkers:
            self._talker_row(talker, **child_parents)
        for division in node.divisions:
            self._division_rows(division, **child_parents)
        return node_id

    def run(self) -> dict[str, list[dict]]:
        f = self.f
        self.rows["fragments"].append(
            self._common()
            | {
                "committee_name": f.committee_name,
                "source_doc_id": f.source_doc_id,
                "schema_version": f.schema_version,
                "name": f.name,
                "venue": f.venue,
                "parliament_num": f.parliament_num,
                "session_num": f.session_num,
                "parliament_name": f.parliament_name,
                "session_name": f.session_name,
                "review_stage": f.review_stage.value if f.review_stage else None,
                "start_time": f.start_time,
                "end_time": f.end_time,
                "start_page": f.start_page,
                "end_page": f.end_page,
                "lang": f.lang,
                "source_url": f.source_url,
                "retrieved_at": f.retrieved_at,
                "extract_count": int(f.extensions.get("extract_count", "0")) or None,
                "content_hash": fragment_content_hash(f),
            }
        )
        for text in f.texts:
            self._text_row(text, para_scope="root")
        for att in f.attendees:
            self.rows["attendees"].append(
                self._common()
                | {
                    "attendee_id": deterministic_id(
                        f.fragment_id, "attendee", att.name or "", att.document_order
                    ),
                    "kind": att.kind,
                    "name": att.name,
                    "reference_id": att.reference_id,
                }
            )
        for mark in f.meeting_time_marks:
            self.rows["meeting_time_marks"].append(
                self._common()
                | {
                    "mark_id": deterministic_id(
                        f.fragment_id, "timemark", mark.kind or "", mark.document_order
                    ),
                    "kind": mark.kind,
                    "time": mark.time,
                    "label": mark.label,
                }
            )
        for proc in f.proceedings:
            proceeding_id = deterministic_id(
                f.fragment_id, "proceeding", proc.uid or "", proc.document_order
            )
            self.rows["proceedings"].append(
                self._common()
                | {
                    "proceeding_id": proceeding_id,
                    "uid": proc.uid,
                    "name": proc.name,
                    "continued": proc.continued,
                    "document_order": proc.document_order,
                }
            )
            for text in proc.texts:
                self._text_row(text, para_scope=proceeding_id, proceeding_id=proceeding_id)
            for talker in proc.talkers:
                self._talker_row(talker, proceeding_id=proceeding_id)
            for subject in proc.subjects:
                subject_id = deterministic_id(
                    f.fragment_id, "subject", subject.uid or "", subject.document_order
                )
                self.rows["subjects"].append(
                    self._common()
                    | {
                        "subject_id": subject_id,
                        "proceeding_id": proceeding_id,
                        "uid": subject.uid,
                        "name": subject.name,
                        "names": json.dumps(subject.names) if subject.names else None,
                        "committee_name": subject.committee_name,
                        "extract_index": int(subject.extensions["extract_index"])
                        if "extract_index" in subject.extensions
                        else None,
                        "document_order": subject.document_order,
                    }
                )
                for bill in subject.bill_refs:
                    self.rows["bill_refs"].append(
                        self._common()
                        | {
                            "bill_ref_id": deterministic_id(
                                subject_id, "bill", bill.uid or bill.source_id or "",
                                bill.document_order,
                            ),
                            "subject_id": subject_id,
                            "uid": bill.uid,
                            "source_id": bill.source_id,
                            "reference_id": bill.reference_id,
                            "name": bill.name,
                        }
                    )
                for text in subject.texts:
                    self._text_row(text, para_scope=subject_id, subject_id=subject_id)
                for talker in subject.talkers:
                    self._talker_row(talker, subject_id=subject_id)
                for division in subject.divisions:
                    self._division_rows(division, subject_id=subject_id)
                for sub in subject.subproceedings:
                    sub_id = self._container_rows(
                        sub, "subproceedings", "subproceeding_id", subject_id=subject_id
                    )
                    for clause in sub.clauses:
                        self._container_rows(
                            clause, "clauses", "clause_id",
                            subject_id=subject_id, subproceeding_id=sub_id,
                        )
                for clause in subject.clauses:
                    self._container_rows(clause, "clauses", "clause_id", subject_id=subject_id)
        return self.rows


def fragment_rows(fragment: Fragment) -> dict[str, list[dict]]:
    """Flatten one canonical fragment into silver table rows."""
    return _Flattener(fragment).run()


# delete_matching removes the day's previous files before rewriting; on
# Windows an AV/indexer scan can briefly hold a freshly-written parquet and
# fail that delete with WinError 32 (observed on real SA re-runs). Such locks
# clear in seconds — retry with backoff; a persistent lock still raises.
@retry(
    retry=retry_if_exception_type(PermissionError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.5, max=8),
    reraise=True,
)
def _write_dataset_with_retry(*args, **kwargs) -> None:
    ds.write_dataset(*args, **kwargs)


def write_silver(fragments: list[Fragment], out_dir: Path) -> dict[str, int]:
    """Write fragments to hive-partitioned Parquet; returns rows per table.

    Partitions on (jurisdiction, date) with ``delete_matching`` — re-running a
    day replaces exactly that day's data.
    """
    combined: dict[str, list[dict]] = {t: [] for t in TABLES}
    for fragment in fragments:
        for table, rows in fragment_rows(fragment).items():
            combined[table].extend(rows)

    counts: dict[str, int] = {}
    for table, rows in combined.items():
        if not rows:
            counts[table] = 0
            continue
        schema = SCHEMAS[table]
        normalized = [{name: row.get(name) for name in schema.names} for row in rows]
        arrow_table = pa.Table.from_pylist(normalized, schema=schema)
        _write_dataset_with_retry(
            arrow_table,
            base_dir=str(Path(out_dir) / table),
            format="parquet",
            partitioning=ds.partitioning(
                pa.schema(
                    [
                        ("jurisdiction", pa.string()),
                        ("date", pa.string()),
                        ("house", pa.string()),
                    ]
                ),
                flavor="hive",
            ),
            existing_data_behavior="delete_matching",
            basename_template="part-{i}.parquet",
        )
        counts[table] = len(rows)
    return counts

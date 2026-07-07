# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.0", "huggingface_hub>=0.26"]
# ///
"""Publish the open-data artifacts to Hugging Face datasets (backlog: Tier 1).

What ships (and what never does — LICENSES-DATA.md):
  embeddings/   house-day parquet consolidated into jurisdiction-year shards
                (hive: model_slug/jurisdiction/year; date + house stay as
                columns). Vectors + join keys only — no prose.
  gold/         the ~90 MB of derived cubes (subject/member/party names,
                counts, Q&A pairings). The join target for subject_id /
                talker_id, and what makes the vectors consumable.
  qdrant/       (optional, --qdrant-snapshot) collection snapshot — the
                batteries-included restore-and-search artifact.
  README.md     dataset card (--card, staged verbatim). Documents the
                vector-space contract, the join contract, and that
                hydrating prose = run the harvester yourself.

Silver never leaves this machine; raw prose never enters staging.

Join-contract guard: aborts if subject_id coverage in the source embeddings
falls below the measured-healthy floor (2026-07-05 census: 98.9% overall;
the nulls are structural — procedural preamble before the first subject).
talker_id is reported but never fails the guard: ~84% is its natural level
(chair/procedural text has no speaker attribution). --allow-null-joins
overrides the subject_id floor.

Usage (uv resolves the inline deps; no pyproject change needed):
  uv run scripts/hf_publish.py --dry-run          # stage + report, no upload
  uv run scripts/hf_publish.py                    # stage + upload
  uv run scripts/hf_publish.py --skip-consolidate # re-upload existing staging
                                                  # (card still restages)

Auth: fine-grained HF token (write on parliament-data repos, SSO-authorized)
via HF_TOKEN env var or `hf auth login`. Upload uses upload_large_folder:
resumable, and re-runs skip files already on the hub — the daily incremental
step is just running this again (only the current-year shards change).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import sys
from pathlib import Path

import duckdb

REPO_DEFAULT = "parliament-data/hansard-embeddings"
# 2026-07-05 census: subject_id 98.9% overall (worst year au/2019 at 92.8%);
# below this floor something upstream regressed. talker_id has no floor —
# ~84% is structural (procedural/chair text carries no speaker).
SUBJECT_COVERAGE_FLOOR = 0.95
# a partitioned COPY buffers one row group per open partition writer;
# 50k rows x 768 floats ~ 150 MB each — keep the product bounded
ROW_GROUP_SIZE = 50_000
DUCKDB_MEMORY_LIMIT = "24GB"


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def check_join_contract(con: duckdb.DuckDBPyConnection, glob: str, allow: bool) -> None:
    total, with_subject, with_talker = con.execute(
        f"""
        select count(*), count(subject_id), count(talker_id)
        from read_parquet('{glob}', hive_partitioning=1)
        """
    ).fetchone()
    if total == 0:
        fail(f"no embeddings found under {glob}")
    subject_cov, talker_cov = with_subject / total, with_talker / total
    print(
        f"join-key coverage over {total:,} vectors: "
        f"subject_id {subject_cov:.1%}, talker_id {talker_cov:.1%} "
        "(talker gaps are structural: procedural/chair text has no speaker)"
    )
    if subject_cov < SUBJECT_COVERAGE_FLOOR and not allow:
        fail(
            f"subject_id coverage below {SUBJECT_COVERAGE_FLOOR:.0%} — "
            "upstream regression? (2026-07-05 census was 98.9%). The join "
            "contract to gold would be broken; fix normalize/embed or pass "
            "--allow-null-joins to publish anyway."
        )


def consolidate_embeddings(data_dir: Path, staging: Path, allow_null_joins: bool) -> None:
    """5,243 house-day files -> one shard per (model_slug, jurisdiction, year).

    date/house were hive directories in the source; here they materialize as
    ordinary columns so a shard is self-describing on its own.
    """
    out = staging / "embeddings"
    if out.exists():
        shutil.rmtree(out)
    glob = (data_dir / "enriched" / "embeddings" / "**" / "*.parquet").as_posix()
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute("SET preserve_insertion_order=false")
    check_join_contract(con, glob, allow=allow_null_joins)
    jurisdictions = [
        r[0]
        for r in con.execute(
            f"select distinct jurisdiction "
            f"from read_parquet('{glob}', hive_partitioning=1) order by 1"
        ).fetchall()
    ]
    # one COPY per jurisdiction keeps open partition writers to ~one per
    # sitting year instead of jurisdictions x years at once (OOM otherwise)
    for j in jurisdictions:
        print(f"consolidating {j}...")
        # jurisdiction partitions are disjoint, so writing into the shared
        # root with overwrite_or_ignore can never clobber another COPY's files
        con.execute(
            f"""
            copy (
                select text_id, fragment_id, talker_id, subject_id,
                       model, provider, dim, embedding,
                       date, house, jurisdiction, model_slug,
                       year(date) as year
                from read_parquet('{glob}', hive_partitioning=1)
                where jurisdiction = '{j}'
            )
            to '{out.as_posix()}'
            (format parquet, compression zstd,
             partition_by (model_slug, jurisdiction, year),
             row_group_size {ROW_GROUP_SIZE},
             overwrite_or_ignore,
             filename_pattern 'part-{{i}}')
            """
        )


def stage_gold(data_dir: Path, staging: Path) -> None:
    out = staging / "gold"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    cubes = sorted((data_dir / "gold").glob("*.parquet"))
    if not cubes:
        fail(f"no gold cubes under {data_dir / 'gold'} — run aggregate first")
    for cube in cubes:
        shutil.copy2(cube, out / cube.name)
    print(f"staged {len(cubes)} gold cubes")


def stage_extras(staging: Path, card: Path, snapshot: Path | None) -> None:
    if card.is_file():
        shutil.copy2(card, staging / "README.md")
        print(f"staged dataset card from {card}")
    else:
        print(
            f"WARNING: no dataset card at {card} — publishing without one; "
            "the hub page will be bare until a README.md is pushed"
        )
    if snapshot:
        if not snapshot.is_file():
            fail(f"qdrant snapshot not found: {snapshot}")
        qdrant_dir = staging / "qdrant"
        qdrant_dir.mkdir(exist_ok=True)
        shutil.copy2(snapshot, qdrant_dir / snapshot.name)
        print(f"staged qdrant snapshot {snapshot.name}")


def refresh_card_stats(staging: Path) -> None:
    """Rewrite the gold-cube row counts (and the as-of date) in the *staged*
    card from the staged parquet itself, so the published numbers can never
    drift from the published data. docs/hf_dataset_card.md keeps the grain
    descriptions; the counts there are informational and refreshed here."""
    card = staging / "README.md"
    gold = staging / "gold"
    if not (card.is_file() and gold.is_dir()):
        return
    text = card.read_text(encoding="utf-8")
    con = duckdb.connect()
    updated = 0
    for cube in sorted(gold.glob("*.parquet")):
        rows = con.execute(
            f"select count(*) from '{cube.as_posix()}'"
        ).fetchone()[0]
        pattern = rf"(\| `{re.escape(cube.stem)}` \| )[\d,]+( \|)"
        text, hits = re.subn(pattern, rf"\g<1>{rows:,}\g<2>", text, count=1)
        updated += hits
    text = re.sub(
        r"These are as of \d{4}-\d{2}-\d{2}",
        f"These are as of {dt.date.today().isoformat()}",
        text,
    )
    card.write_text(text, encoding="utf-8")
    print(f"refreshed {updated} cube row count(s) in the staged card")


def report(staging: Path) -> None:
    print(f"\nstaging report ({staging}):")
    total = 0
    for top in sorted(p for p in staging.iterdir() if p.name != ".cache"):
        files = [f for f in top.rglob("*") if f.is_file()] if top.is_dir() else [top]
        size = sum(f.stat().st_size for f in files)
        total += size
        print(f"  {top.name:<12} {len(files):>5} files  {size / 2**20:>10,.1f} MB")
    print(f"  {'total':<12} {'':>5}        {total / 2**20:>10,.1f} MB")


def upload(staging: Path, repo: str) -> None:
    from huggingface_hub import HfApi

    api = HfApi()  # token: HF_TOKEN env or cached `hf auth login`
    api.create_repo(repo, repo_type="dataset", exist_ok=True)
    print(f"uploading {staging} -> hf.co/datasets/{repo} (resumable; safe to re-run)")
    api.upload_large_folder(repo_id=repo, repo_type="dataset", folder_path=staging)
    print("upload complete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--staging", type=Path, default=Path("build/hf-staging"))
    parser.add_argument("--repo", default=REPO_DEFAULT)
    parser.add_argument("--card", type=Path, default=Path("docs/hf_dataset_card.md"))
    parser.add_argument("--qdrant-snapshot", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="stage + report, no upload")
    parser.add_argument(
        "--skip-consolidate",
        action="store_true",
        help="skip embeddings/gold consolidation; card + snapshot still restage",
    )
    parser.add_argument(
        "--allow-null-joins",
        action="store_true",
        help="publish even if subject_id/talker_id are mostly NULL (pre re-embed)",
    )
    args = parser.parse_args()

    args.staging.mkdir(parents=True, exist_ok=True)
    if not args.skip_consolidate:
        consolidate_embeddings(args.data_dir, args.staging, args.allow_null_joins)
        stage_gold(args.data_dir, args.staging)
    # card (+ optional snapshot) restage even under --skip-consolidate: they
    # are cheap copies, and a card fix must never ship stale
    stage_extras(args.staging, args.card, args.qdrant_snapshot)
    refresh_card_stats(args.staging)
    report(args.staging)

    if args.dry_run:
        print("\n--dry-run: skipping upload")
        return
    upload(args.staging, args.repo)


if __name__ == "__main__":
    main()

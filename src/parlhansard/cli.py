"""parlhansard CLI — pipeline entry point.

Stages: harvest → normalize → aggregate (+ optional enrich later).
See docs/ROADMAP.md for architecture and roadmap.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from parlhansard import __version__
from parlhansard.harvest import all_adapters, get_adapter
from parlhansard.harvest.store import RawStore
from parlhansard.model.canonical import Fragment, Jurisdiction

# PARLHANSARD_DATA_DIR lets a container point every stage at a mounted
# volume without repeating --data-dir (see compose.yaml)
DEFAULT_DATA_DIR = Path(os.environ.get("PARLHANSARD_DATA_DIR", "data"))


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def cmd_sources(_: argparse.Namespace) -> int:
    width = max(len(a.jurisdiction.value) for a in all_adapters())
    for adapter in all_adapters():
        print(f"{adapter.jurisdiction.value:<{width}}  {adapter.status:<12} {adapter.source}")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    schema = Fragment.model_json_schema()
    schema["$id"] = "https://github.com/parlhansard/parlhansard/schemas/canonical.schema.json"
    schema["title"] = "parlhansard canonical Hansard fragment"
    text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text, end="")
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    adapter = get_adapter(args.jurisdiction)
    store = RawStore(args.data_dir / "raw")
    try:
        events = list(adapter.discover(args.start, args.end))
    except NotImplementedError as exc:
        print(f"[{args.jurisdiction}] not available yet: {exc}", file=sys.stderr)
        return 2
    if args.house:
        events = [e for e in events if e.house == args.house]
    print(f"[{args.jurisdiction}] {len(events)} sitting event(s) in range")

    refresh_cutoff = (
        dt.date.today() - dt.timedelta(days=args.refresh_window)
        if args.refresh_window
        else None
    )
    fetched = skipped = 0
    # newest first: recent sittings have the most analytical value, so a
    # long backfill delivers them before working back through history
    for event in sorted(events, key=lambda e: (e.date, e.house or ""), reverse=True):
        day_dir = store.day_dir(event.jurisdiction, event.date, event.house or "unknown")
        # recent days are re-fetched inside the refresh window so uncorrected
        # proofs converge to the corrected record
        force_day = args.force or (refresh_cutoff is not None and event.date >= refresh_cutoff)
        if (day_dir / "meta.json").exists() and not force_day:
            skipped += 1
            continue
        docs = 0
        for doc in adapter.fetch(event):
            store.save(
                event.jurisdiction, event.date, event.house or "unknown",
                doc.name, doc.content, force=force_day,
            )
            docs += 1
        if docs == 0:
            # XML not (yet) available for this sitting — leave unmarked so a
            # future run re-probes (parliaments upload historic conversions)
            print(f"  {event.date} {event.house}: no XML yet, will re-probe")
            continue
        store.save_meta(
            event.jurisdiction, event.date, event.house or "unknown",
            {
                "event": {
                    "date": event.date.isoformat(),
                    "house": event.house,
                    "url": event.url,
                    **event.extra,
                },
                "documents": docs,
                "harvested_at": dt.datetime.now(dt.UTC).isoformat(),
            },
        )
        fetched += 1
        print(f"  {event.date} {event.house}: {docs} document(s)")
    print(f"[{args.jurisdiction}] fetched {fetched} day(s), skipped {skipped} already-harvested")
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    from concurrent.futures import ProcessPoolExecutor, as_completed

    from parlhansard.normalize.runner import normalize_day
    from parlhansard.normalize.silver import TABLES

    store = RawStore(args.data_dir / "raw")
    out_dir = args.data_dir / "silver"

    jobs = [
        (
            args.jurisdiction,
            day.date.isoformat(),
            day.house,
            [str(p) for p in day.xml_files],
            str(out_dir),
        )
        for day in store.iter_days(Jurisdiction(args.jurisdiction), args.start, args.end)
        if day.xml_files
    ]
    if not jobs:
        print(f"[{args.jurisdiction}] nothing to normalize under {store.root}")
        return 0

    # Windows ProcessPoolExecutor caps at 61 workers
    workers = min(61, args.workers or max(1, (os.cpu_count() or 2) - 1))
    totals = dict.fromkeys(TABLES, 0)
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(normalize_day, *job): job for job in jobs}
        for future in as_completed(futures):
            _, date, house, _, _ = futures[future]
            try:
                counts = future.result()
            except NotImplementedError as exc:
                print(f"[{args.jurisdiction}] not available yet: {exc}", file=sys.stderr)
                pool.shutdown(cancel_futures=True)
                return 2
            for table, count in counts.items():
                totals[table] += count
            done += 1
            print(f"  {date} {house}: {counts['texts']} texts, {counts['talkers']} talkers")

    print(
        f"[{args.jurisdiction}] normalized {done} day(s) "
        f"({workers} workers) -> {out_dir}"
    )
    for table in TABLES:
        if totals[table]:
            print(f"  {table:<20} {totals[table]:>8}")
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    from parlhansard.aggregate.cubes import build_gold

    counts = build_gold(args.data_dir / "silver", args.data_dir / "gold")
    print(f"gold cubes -> {args.data_dir / 'gold'}")
    for name, count in counts.items():
        print(f"  {name:<24} {count:>8}")
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    from parlhansard.aggregate.cubes import build_db

    silver = (args.data_dir / "silver") if args.include_silver else None
    tables = build_db(args.data_dir / "gold", args.out, silver_dir=silver)
    print(f"wrote {args.out} ({len(tables)} tables: {', '.join(tables)})")
    if args.include_silver:
        print("NOTE: includes silver full text — local analysis only, do not publish")
    return 0


def _not_yet(stage: str):
    def run(_: argparse.Namespace) -> int:
        print(f"'{stage}' is not implemented yet — see docs/ROADMAP.md", file=sys.stderr)
        return 2

    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parlhansard",
        description="Open-source Parliamentary Hansard analytics extraction.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sources", help="list jurisdictions, adapter status and sources")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("schema", help="emit the canonical fragment JSON Schema")
    p.add_argument("--out", help="write to file instead of stdout")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("harvest", help="discover + fetch raw Hansard documents")
    p.add_argument("jurisdiction", choices=[j.value for j in Jurisdiction])
    p.add_argument("--start", type=_parse_date, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--end", type=_parse_date, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--house", help="restrict to one house code (e.g. lh, uh)")
    p.add_argument("--force", action="store_true", help="re-fetch already-harvested days")
    p.add_argument(
        "--refresh-window",
        type=int,
        default=0,
        metavar="DAYS",
        help="re-fetch days within DAYS of today (captures proof -> corrected revisions)",
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("normalize", help="raw XML -> canonical silver Parquet tables")
    p.add_argument("jurisdiction", choices=[j.value for j in Jurisdiction])
    p.add_argument("--start", type=_parse_date, metavar="YYYY-MM-DD")
    p.add_argument("--end", type=_parse_date, metavar="YYYY-MM-DD")
    p.add_argument(
        "--workers", type=int, default=0,
        help="parallel worker processes (default: CPU count - 1)",
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_normalize)

    p = sub.add_parser("aggregate", help="silver -> gold cubes (derived facts, publishable)")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_aggregate)

    p = sub.add_parser("db", help="build a self-contained hansard.duckdb from gold")
    p.add_argument("--out", type=Path, default=DEFAULT_DATA_DIR / "hansard.duckdb")
    p.add_argument(
        "--include-silver",
        action="store_true",
        help="also materialize full-text silver tables (LOCAL USE ONLY — do not publish)",
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("enrich", help="themes/embeddings/entity links (optional, roadmap)")
    p.set_defaults(func=_not_yet("enrich"))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

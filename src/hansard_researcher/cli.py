"""hansard-researcher CLI — pipeline entry point.

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

from hansard_researcher import __version__
from hansard_researcher.harvest import all_adapters, get_adapter
from hansard_researcher.harvest.store import RawStore
from hansard_researcher.model.canonical import Fragment, Jurisdiction

# HANSARD_RESEARCHER_DATA_DIR lets a container point every stage at a mounted
# volume without repeating --data-dir (see compose.yaml)
DEFAULT_DATA_DIR = Path(os.environ.get("HANSARD_RESEARCHER_DATA_DIR", "data"))


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
    schema["$id"] = "https://github.com/parliament-io/hansard-researcher/schemas/canonical.schema.json"
    schema["title"] = "Hansard Researcher canonical Hansard fragment"
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

    from hansard_researcher.normalize.runner import normalize_day
    from hansard_researcher.normalize.silver import TABLES

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
    from hansard_researcher.aggregate.cubes import build_gold

    counts = build_gold(
        args.data_dir / "silver",
        args.data_dir / "gold",
        reference_dir=args.data_dir / "reference",
        enriched_dir=args.data_dir / "enriched",
        raw_dir=args.data_dir / "raw",
    )
    print(f"gold cubes -> {args.data_dir / 'gold'}")
    for name, count in counts.items():
        print(f"  {name:<24} {count:>8}")
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    from hansard_researcher.aggregate.cubes import build_db

    silver = (args.data_dir / "silver") if args.include_silver else None
    tables = build_db(args.data_dir / "gold", args.out, silver_dir=silver)
    print(f"wrote {args.out} ({len(tables)} tables: {', '.join(tables)})")
    if args.include_silver:
        print("NOTE: includes silver full text — local analysis only, do not publish")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from hansard_researcher.aggregate.coverage import collect_status

    status = collect_status(args.data_dir, counts=args.counts)
    if args.json:
        print(json.dumps(status, indent=2, default=str))
        return 0

    print(f"hansard-researcher status - data dir: {status['data_dir']}")
    jurisdictions = status["jurisdictions"]
    if not jurisdictions:
        print("  no data yet - run 'hansard-researcher harvest' first")
        return 0

    count_cols = (
        f" {'subjects':>9} {'turns':>10} {'texts':>10} {'divisions':>9}"
        if args.counts
        else ""
    )
    print()
    print(
        f"  {'jur':<5} {'raw days':>8} {'silver hd':>9} {'span':<25} "
        f"{'pending':>7}{count_cols}"
    )
    for code, j in jurisdictions.items():
        span = f"{j['first_date']} .. {j['last_date']}" if j["first_date"] else "-"
        counts = (
            f" {j['subjects']:>9,} {j['talker_turns']:>10,} "
            f"{j['texts']:>10,} {j['divisions']:>9,}"
            if args.counts
            else ""
        )
        print(
            f"  {code:<5} {j['raw_days']:>8,} {j['silver_house_days']:>9,} "
            f"{span:<25} {j['pending_normalize_days']:>7,}{counts}"
        )
    print("  (silver hd = house-days; pending = harvested days awaiting normalize)")

    enrich = status["enrichment"]
    total_hd = enrich["silver_house_days"]
    subjects_note = (
        f", {enrich['silver_subjects']:,} subjects" if args.counts else ""
    )
    print()
    print(f"enrichment - silver total: {total_hd:,} house-days{subjects_note}")
    if not enrich["embeddings"] and not enrich["themes"]:
        print("  none yet - run 'hansard-researcher enrich embed' / 'enrich themes'")
    for kind, key, unit in (("embeddings", "vectors", "vectors"),
                            ("themes", "labels", "labels")):
        for slug, e in sorted(enrich[kind].items()):
            pct = 100 * e["house_days"] / total_hd if total_hd else 0
            detail = f"  {e[key]:>10,} {unit}" if key in e else ""
            print(
                f"  {kind:<11} {slug:<32} {e['house_days']:>6,}/{total_hd:,} "
                f"house-days ({pct:.1f}%){detail}"
            )

    print()
    gold = status["gold"]
    if gold["cubes"]:
        print(f"gold - {gold['cubes']} cubes, built {gold['built_at']}")
    else:
        print("gold - not built yet ('hansard-researcher aggregate')")
    members = status["reference"]["members"]
    if members:
        detail = ", ".join(f"{j} {n:,}" for j, n in sorted(members.items()))
        print(f"reference - members: {detail}")
    else:
        print("reference - no member registers yet ('hansard-researcher reference sa|nsw')")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from hansard_researcher.mcp_server import build_server

    try:
        # the mcp package import lives inside build_server
        server = build_server(args.data_dir, include_silver=not args.no_silver)
    except ImportError:
        print(
            "the MCP server needs the 'mcp' extra — install with: "
            "uv sync --extra mcp  (or: pip install 'hansard-researcher[mcp]')",
            file=sys.stderr,
        )
        return 2
    server.run()
    return 0


def cmd_reference(args: argparse.Namespace) -> int:
    builders = {"sa": "hansard_researcher.reference.sa", "nsw": "hansard_researcher.reference.nsw"}
    module_name = builders.get(args.jurisdiction)
    if module_name is None:
        print(
            f"'reference {args.jurisdiction}' is not implemented yet — "
            f"see docs/ROADMAP.md (live: {', '.join(builders)})",
            file=sys.stderr,
        )
        return 2
    import importlib

    build = importlib.import_module(module_name).build
    try:
        count = build(args.data_dir, offline=args.offline)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    mode = "rebuilt offline from stored snapshot" if args.offline else "fetched + built"
    print(f"[{args.jurisdiction}] member register: {count} people ({mode}) -> "
          f"{args.data_dir / 'reference' / 'members'}")
    return 0


def cmd_enrich_embed(args: argparse.Namespace) -> int:
    from hansard_researcher.enrich.embed import embed_texts
    from hansard_researcher.enrich.providers import ProviderError, get_embedder, resolve_config

    try:
        config = resolve_config(args.provider)
        embedder = get_embedder(config)
    except ProviderError as exc:
        print(exc, file=sys.stderr)
        return 2
    stats = embed_texts(
        args.data_dir,
        args.jurisdiction,
        embedder,
        provider=config.provider,
        model=config.embed_model,
        start=args.start,
        end=args.end,
        batch_size=args.batch_size,
        workers=args.workers,
        force=args.force,
    )
    print(
        f"[{args.jurisdiction}] embedded {stats['days']} day(s) "
        f"({stats['vectors']} vectors, model {config.embed_model}), "
        f"skipped {stats['skipped']} already-embedded"
    )
    return 0


def cmd_enrich_themes(args: argparse.Namespace) -> int:
    from hansard_researcher.enrich.providers import (
        OpenAICompatClient,
        ProviderError,
        get_embedder,
        resolve_config,
    )
    from hansard_researcher.enrich.themes import classify_themes

    try:
        config = resolve_config(args.provider)
        embedder = completer = None
        if args.engine == "embedding":
            embedder = get_embedder(config)
            model = config.embed_model
        else:
            if config.base_url is None:
                raise ProviderError(
                    "the llm engine needs a chat-capable endpoint — provider "
                    "'local' only embeds (use --engine embedding, or ollama/"
                    "openai/... for llm)"
                )
            completer = OpenAICompatClient(config)
            model = config.chat_model
            if not model:
                raise ProviderError(
                    "no chat model set — set HANSARD_RESEARCHER_ENRICH_CHAT_MODEL"
                )
    except ProviderError as exc:
        print(exc, file=sys.stderr)
        return 2
    stats = classify_themes(
        args.data_dir,
        args.jurisdiction,
        engine=args.engine,
        model=model,
        provider=config.provider,
        embedder=embedder,
        completer=completer,
        start=args.start,
        end=args.end,
        top_k=args.top_k,
        min_score=args.min_score,
        include_procedural=args.include_procedural,
        workers=args.workers,
        force=args.force,
    )
    print(
        f"[{args.jurisdiction}] classified {stats['days']} day(s) "
        f"({stats['labels']} theme labels, {args.engine}:{model}), "
        f"skipped {stats['skipped']} already-classified"
    )
    return 0


def cmd_enrich_index(args: argparse.Namespace) -> int:
    from hansard_researcher.enrich.providers import ProviderError, resolve_config
    from hansard_researcher.enrich.qdrant import QdrantIndex, collection_name, index_embeddings

    try:
        config = resolve_config(args.provider)
        if not config.embed_model:
            raise ProviderError(
                "no embedding model set — set HANSARD_RESEARCHER_ENRICH_EMBED_MODEL"
            )
        index = QdrantIndex(args.qdrant_url)
        stats = index_embeddings(
            args.data_dir, args.jurisdiction, index, config.embed_model,
            batch_size=args.batch_size,
        )
    except ProviderError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        f"[{args.jurisdiction}] indexed {stats['points']:,} vectors into "
        f"{collection_name(config.embed_model)!r} at {index.url}"
        f"{' (collection created)' if stats['created'] else ''}"
    )
    return 0


def cmd_enrich_search(args: argparse.Namespace) -> int:
    from hansard_researcher.enrich.providers import ProviderError, get_embedder, resolve_config
    from hansard_researcher.enrich.search import search, search_qdrant

    try:
        config = resolve_config(args.provider)
        query_vector = get_embedder(config).embed([args.query])[0]
    except ProviderError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.backend == "qdrant":
        hits = search_qdrant(
            args.data_dir,
            query_vector,
            config.embed_model,
            k=args.k,
            jurisdiction=args.jurisdiction,
        )
    else:
        hits = search(
            args.data_dir,
            query_vector,
            config.embed_model,
            k=args.k,
            jurisdiction=args.jurisdiction,
        )
    if not hits:
        print("no matches — run 'hansard-researcher enrich embed' first?", file=sys.stderr)
        return 1
    for hit in hits:
        context = " · ".join(filter(None, (hit.subject_name, hit.speaker)))
        snippet = hit.text if len(hit.text) <= 240 else hit.text[:237] + "..."
        print(f"{hit.score:.3f}  {hit.jurisdiction} {hit.date} {hit.house}  {context}")
        print(f"       {snippet}")
    return 0


def _not_yet(stage: str):
    def run(_: argparse.Namespace) -> int:
        print(f"'{stage}' is not implemented yet — see docs/ROADMAP.md", file=sys.stderr)
        return 2

    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hansard-researcher",
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

    p = sub.add_parser(
        "mcp",
        help="run the MCP server (stdio) exposing the local archive to AI agents",
    )
    p.add_argument(
        "--no-silver",
        action="store_true",
        help="serve derived gold facts only (default also serves local full-text silver)",
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser(
        "status",
        help="pipeline coverage report: raw -> silver -> enriched -> gold",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--counts",
        action="store_true",
        help="also read row counts from parquet footers (slow on a full archive)",
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "reference",
        help="member registers (Tier 2): fetch + normalize, snapshots stored offline",
    )
    p.add_argument("jurisdiction", choices=[j.value for j in Jurisdiction])
    p.add_argument(
        "--offline",
        action="store_true",
        help="rebuild from the stored raw snapshot (no network)",
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_reference)

    p = sub.add_parser(
        "enrich",
        help="optional Tier 3: embeddings + semantic search (BYO provider, never required)",
    )
    esub = p.add_subparsers(dest="enrich_command", required=True)

    def _provider_arg(sp: argparse.ArgumentParser) -> None:
        from hansard_researcher.enrich.providers import PRESETS

        sp.add_argument(
            "--provider",
            choices=[*PRESETS, "custom"],
            help="preset (local server or BYO-key endpoint); "
            "HANSARD_RESEARCHER_ENRICH_* env vars override/extend",
        )

    pe = esub.add_parser("embed", help="silver paragraphs -> embeddings (data/enriched)")
    pe.add_argument("jurisdiction", choices=[j.value for j in Jurisdiction])
    pe.add_argument("--start", type=_parse_date, metavar="YYYY-MM-DD")
    pe.add_argument("--end", type=_parse_date, metavar="YYYY-MM-DD")
    _provider_arg(pe)
    pe.add_argument("--batch-size", type=int, default=96)
    pe.add_argument(
        "--workers", type=int, default=1,
        help="house-days embedded concurrently (provider must handle "
        "parallel requests; Ollama does)",
    )
    pe.add_argument("--force", action="store_true", help="re-embed already-embedded days")
    pe.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    pe.set_defaults(func=cmd_enrich_embed)

    pi = esub.add_parser(
        "index", help="load computed embeddings into Qdrant for ANN search"
    )
    pi.add_argument("jurisdiction", choices=[j.value for j in Jurisdiction])
    _provider_arg(pi)
    pi.add_argument(
        "--qdrant-url",
        help="Qdrant base URL (default: HANSARD_RESEARCHER_QDRANT_URL or http://localhost:6333)",
    )
    pi.add_argument("--batch-size", type=int, default=512)
    pi.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    pi.set_defaults(func=cmd_enrich_index)

    ps = esub.add_parser("search", help="semantic search over embedded paragraphs")
    ps.add_argument("query")
    ps.add_argument("--jurisdiction", choices=[j.value for j in Jurisdiction])
    ps.add_argument("--k", type=int, default=10, help="number of results")
    ps.add_argument(
        "--backend",
        choices=["duckdb", "qdrant"],
        default="duckdb",
        help="duckdb: exact scan of the parquet (no server); "
        "qdrant: ANN via 'enrich index' (fast at archive scale)",
    )
    _provider_arg(ps)
    ps.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ps.set_defaults(func=cmd_enrich_search)

    pt = esub.add_parser(
        "themes",
        help="classify subjects against the seed taxonomy (embedding or llm engine)",
    )
    pt.add_argument("jurisdiction", choices=[j.value for j in Jurisdiction])
    pt.add_argument(
        "--engine",
        choices=["embedding", "llm"],
        default="embedding",
        help="embedding: cosine vs theme descriptions (cheap, any provider); "
        "llm: chat-model pick (higher quality, needs a chat endpoint)",
    )
    pt.add_argument("--start", type=_parse_date, metavar="YYYY-MM-DD")
    pt.add_argument("--end", type=_parse_date, metavar="YYYY-MM-DD")
    _provider_arg(pt)
    pt.add_argument("--top-k", type=int, default=3, help="max themes per subject")
    pt.add_argument(
        "--min-score", type=float, default=0.25,
        help="minimum cosine similarity (embedding engine)",
    )
    pt.add_argument(
        "--workers", type=int, default=1,
        help="house-days classified concurrently (provider must handle "
        "parallel requests; Ollama does)",
    )
    pt.add_argument(
        "--include-procedural",
        action="store_true",
        help="also offer procedural themes (question-time, petitions, ...) as "
        "candidates — excluded by default: they mark the kind of business, "
        "which is structural, and they attract topical subjects",
    )
    pt.add_argument("--force", action="store_true", help="re-classify already-done days")
    pt.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    pt.set_defaults(func=cmd_enrich_themes)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

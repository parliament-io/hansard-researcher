"""Gold cubes — structural analytics over silver (+ optional theme cubes).

Ports the pure aggregation math of the C# ``HansardAnalyticsAggregator`` and
``ContributionProjector``:

- ``member_activity``          all-time per member: turns by kind, words,
                               subjects, sitting days, division votes
- ``member_activity_by_week``  the C# weekly partition grain (ISO year/week x
                               house), for trend charts
- ``contributions``            subject x member grain (who said how much where)
- ``qa_pairs``                 structural Q->A pairing: each question paired
                               with the next answer in the same subject
- ``subject_occurrence``       one row per subject: participation + volume +
                               the extract index for deep-linking to the API
- ``division_summary``         one row per division with subject context
- ``division_votes_detail``    one row per member vote with full context
- ``bill_journey``             bill x house-day: stages (raw + canonical via
                               the curated stage vocabulary), volume, divisions
- ``bills``                    one row per bill: houses, span, furthest stage
- ``sitting_days``             per sitting: duration, volume, rhythm

Theme cubes (the C# aggregator's enrichment set) build from the optional
``data/enriched/themes`` assignments — empty until ``enrich themes`` runs;
subject-grain here (the C# paragraph grain arrives with the paragraph tier).
Every theme cube carries (engine, model) so runs from different providers
never mix:

- ``theme_by_week``            theme x ISO week x house: occurrences + score
- ``theme_cooccurrence``       theme pairs debated in the same subject
- ``member_theme_rank``        member x theme: turns/words + rank in theme
- ``bill_theme_link``          bill x theme occurrences
- ``member_vote_by_theme``     division votes joined to subject themes
- ``theme_candidates``         curator-workflow port: unclassified or
                               low-confidence subjects (taxonomy gaps)

Everything here is plain SQL over Parquet — reproducible by anyone with
DuckDB. Gold is tiny (MBs), so each run is a full recompute: simple and
always consistent. Gold contains **derived facts only** — no Hansard prose —
so it is publishable under every jurisdiction's terms (see LICENSES-DATA.md).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa

from parlhansard.normalize.silver import SCHEMAS, TABLES
from parlhansard.reference.register import SCHEMA as REGISTER_SCHEMA
from parlhansard.reference.stages import load_stage_vocab

# NOTE on text columns: gold deliberately selects names/labels and *numbers*,
# never clean_text/raw_text. Keep it that way (licensing stance, LICENSES-DATA.md).

# The member register (data/reference/members, optional) joins on
# (jurisdiction, member_source_id) and fills gaps the source XML leaves:
# blank names on NSW division votes, party for NSW/SA (their talkers carry
# none). Source values always win — the register only backfills nulls.

GOLD_QUERIES: dict[str, str] = {
    "member_activity": """
        with turns as (
            select * from talkers where member_source_id is not null
        ),
        speaking as (
            select
                jurisdiction,
                member_source_id,
                arg_max(name, date)                 as member_name,
                arg_max(party_abbreviation, date)   as party_abbreviation,
                arg_max(party, date)                as party,
                arg_max(electorate, date)           as electorate,
                count(*)                                          as turns,
                count(*) filter (kind = 'speech')                 as speeches,
                count(*) filter (kind = 'question')               as questions,
                count(*) filter (kind = 'answer')                 as answers,
                count(*) filter (kind = 'interjection')           as interjections,
                coalesce(sum(word_count), 0)                      as words,
                coalesce(sum(paragraph_count), 0)                 as paragraphs,
                count(distinct subject_id)                        as subjects,
                count(distinct date)                              as sitting_days,
                min(date)                                         as first_sitting,
                max(date)                                         as last_sitting,
                bool_or(first_speech)                             as gave_first_speech
            from turns
            group by 1, 2
        ),
        voting as (
            select
                jurisdiction,
                member_source_id,
                count(*)                              as division_votes,
                count(*) filter (vote = 'AYES')       as votes_ayes,
                count(*) filter (vote = 'NOES')       as votes_noes,
                count(*) filter (teller)              as teller_count
            from division_votes
            where member_source_id is not null
            group by 1, 2
        )
        select
            coalesce(s.jurisdiction, v.jurisdiction)           as jurisdiction,
            coalesce(s.member_source_id, v.member_source_id)   as member_source_id,
            coalesce(s.member_name, m.display_name)            as member_name,
            coalesce(s.party, m.party_name)                    as party,
            s.* exclude (jurisdiction, member_source_id, member_name, party),
            coalesce(v.division_votes, 0)                      as division_votes,
            coalesce(v.votes_ayes, 0)                          as votes_ayes,
            coalesce(v.votes_noes, 0)                          as votes_noes,
            coalesce(v.teller_count, 0)                        as teller_count
        from speaking s
        full join voting v using (jurisdiction, member_source_id)
        left join members m
          on coalesce(s.jurisdiction, v.jurisdiction) = m.jurisdiction
         and coalesce(s.member_source_id, v.member_source_id) = m.source_member_id
    """,
    "member_activity_by_week": """
        select
            jurisdiction,
            house,
            datepart('isoyear', cast(date as date))  as iso_year,
            datepart('week', cast(date as date))     as iso_week,
            min(date)                                as week_start_sitting,
            member_source_id,
            arg_max(name, date)                      as member_name,
            arg_max(party_abbreviation, date)        as party_abbreviation,
            count(*)                                          as turns,
            count(*) filter (kind = 'speech')                 as speeches,
            count(*) filter (kind = 'question')               as questions,
            count(*) filter (kind = 'answer')                 as answers,
            count(*) filter (kind = 'interjection')           as interjections,
            coalesce(sum(word_count), 0)                      as words,
            count(distinct subject_id)                        as subjects,
            count(distinct date)                              as sitting_days
        from talkers
        where member_source_id is not null
        group by 1, 2, 3, 4, 6
    """,
    "contributions": """
        select
            t.jurisdiction,
            t.date,
            t.house,
            t.subject_id,
            any_value(s.name)                        as subject_name,
            any_value(p.name)                        as proceeding_name,
            t.member_source_id,
            arg_max(t.name, t.document_order)        as member_name,
            arg_max(t.party_abbreviation, t.document_order) as party_abbreviation,
            arg_max(t.electorate, t.document_order)  as electorate,
            count(*)                                          as turns,
            count(*) filter (t.kind = 'speech')               as speeches,
            count(*) filter (t.kind = 'question')             as questions,
            count(*) filter (t.kind = 'answer')               as answers,
            count(*) filter (t.kind = 'interjection')         as interjections,
            coalesce(sum(t.word_count), 0)                    as words,
            min(t.document_order)                             as first_turn_order,
            bool_or(t.first_speech)                           as includes_first_speech
        from talkers t
        left join subjects s using (subject_id)
        left join proceedings p on s.proceeding_id = p.proceeding_id
        where t.subject_id is not null and t.member_source_id is not null
        group by t.jurisdiction, t.date, t.house, t.subject_id, t.member_source_id
    """,
    "qa_pairs": """
        with q as (
            select * from talkers
            where kind = 'question' and subject_id is not null
        ),
        a as (
            select * from talkers
            where kind = 'answer' and subject_id is not null
        )
        select
            q.jurisdiction,
            q.date,
            q.house,
            q.subject_id,
            s.name                       as subject_name,
            q.talker_id                  as question_talker_id,
            q.member_source_id           as question_member_id,
            q.name                       as question_member,
            q.party_abbreviation         as question_party,
            q.word_count                 as question_words,
            q.start_time                 as question_time,
            ans.talker_id                as answer_talker_id,
            ans.member_source_id         as answer_member_id,
            ans.name                     as answer_member,
            ans.party_abbreviation       as answer_party,
            ans.portfolios               as answer_portfolios,
            ans.word_count               as answer_words,
            ans.talker_id is not null    as answered
        from q
        left join subjects s using (subject_id)
        left join lateral (
            select * from a
            where a.subject_id = q.subject_id
              and a.document_order > q.document_order
            order by a.document_order
            limit 1
        ) ans on true
    """,
    "subject_occurrence": """
        with talker_stats as (
            select
                subject_id,
                count(*)                                  as talker_turns,
                count(distinct member_source_id)          as distinct_speakers,
                count(*) filter (kind = 'speech')         as speeches,
                count(*) filter (kind = 'question')       as questions,
                count(*) filter (kind = 'answer')         as answers,
                count(*) filter (kind = 'interjection')   as interjections,
                coalesce(sum(word_count), 0)              as words,
                min(start_time)                           as first_spoken_at
            from talkers
            where subject_id is not null
            group by 1
        ),
        division_stats as (
            select subject_id, count(*) as divisions
            from divisions where subject_id is not null group by 1
        ),
        bills as (
            select subject_id, string_agg(name, '; ' order by name) as bill_names
            from bill_refs where subject_id is not null group by 1
        )
        select
            s.jurisdiction,
            s.date,
            s.house,
            s.subject_id,
            s.proceeding_id,
            p.name                                  as proceeding_name,
            s.name                                  as subject_name,
            s.extract_index,
            coalesce(t.talker_turns, 0)             as talker_turns,
            coalesce(t.distinct_speakers, 0)        as distinct_speakers,
            coalesce(t.speeches, 0)                 as speeches,
            coalesce(t.questions, 0)                as questions,
            coalesce(t.answers, 0)                  as answers,
            coalesce(t.interjections, 0)            as interjections,
            coalesce(t.words, 0)                    as words,
            t.first_spoken_at,
            coalesce(d.divisions, 0)                as divisions,
            b.bill_names,
            s.document_order
        from subjects s
        left join proceedings p using (proceeding_id)
        left join talker_stats t using (subject_id)
        left join division_stats d using (subject_id)
        left join bills b using (subject_id)
    """,
    "division_summary": """
        with vote_stats as (
            select
                division_id,
                count(*)                        as recorded_votes,
                count(*) filter (teller)        as tellers,
                count(*) filter (proxy)         as proxies
            from division_votes
            group by 1
        )
        select
            d.jurisdiction,
            d.date,
            d.house,
            d.division_id,
            d.subject_id,
            s.name                              as subject_name,
            p.name                              as proceeding_name,
            s.extract_index,
            d.result,
            d.ayes_count,
            d.noes_count,
            d.pairs_count,
            d.abstentions_count,
            abs(coalesce(d.ayes_count, 0) - coalesce(d.noes_count, 0)) as margin,
            coalesce(v.recorded_votes, 0)       as recorded_votes,
            coalesce(v.tellers, 0)              as tellers,
            coalesce(v.proxies, 0)              as proxies,
            d.document_order
        from divisions d
        left join subjects s using (subject_id)
        left join proceedings p on s.proceeding_id = p.proceeding_id
        left join vote_stats v using (division_id)
    """,
    "division_votes_detail": """
        select
            v.jurisdiction,
            v.date,
            v.house,
            v.division_id,
            d.subject_id,
            s.name                   as subject_name,
            d.result                 as division_result,
            v.member_source_id,
            coalesce(v.member_name, m.display_name) as member_name,
            v.vote,
            v.vote = upper(coalesce(d.result, '')) as voted_with_result,
            v.teller,
            v.proxy,
            coalesce(v.party, m.party_name)         as party
        from division_votes v
        left join divisions d using (division_id)
        left join subjects s on d.subject_id = s.subject_id
        left join members m
          on v.jurisdiction = m.jurisdiction
         and v.member_source_id = m.source_member_id
    """,
    # bill identity is the normalized bill NAME + jurisdiction: WA/SA carry
    # explicit bill elements (bill_refs) whose uids do NOT track across
    # houses, while NSW/AU publish the bill name as the subject name — the
    # name string is what parliaments themselves keep stable between chambers
    "bill_journey": """
        with bill_subjects as (
            select
                s.jurisdiction, s.date, s.house, s.subject_id,
                coalesce(b.name, s.name) as bill_name
            from subjects s
            left join (
                select subject_id, any_value(name) as name
                from bill_refs where name is not null group by 1
            ) b using (subject_id)
            where b.name is not null
               or regexp_matches(coalesce(s.name, ''), '\\bBill\\b')
        ),
        keyed as (
            select *,
                lower(trim(regexp_replace(bill_name, '\\s+', ' ', 'g'))) as bill_key
            from bill_subjects
        ),
        stage_stats as (
            select
                sp.subject_id,
                string_agg(sp.name, ' · ' order by sp.document_order) as stage_labels,
                arg_max(v.stage, v.stage_order)                       as furthest_stage,
                max(v.stage_order)                                    as furthest_stage_order
            from subproceedings sp
            left join stage_vocab v
              on v.jurisdiction = sp.jurisdiction and lower(sp.name) = v.name_lower
            group by 1
        ),
        talker_stats as (
            select subject_id,
                   count(*)                          as talker_turns,
                   count(distinct member_source_id)  as distinct_speakers,
                   coalesce(sum(word_count), 0)      as words
            from talkers where subject_id is not null group by 1
        ),
        division_stats as (
            select subject_id,
                   count(*)                                   as divisions,
                   string_agg(result, ', ' order by document_order) as division_results
            from divisions where subject_id is not null group by 1
        )
        select
            k.jurisdiction,
            k.bill_key,
            arg_max(k.bill_name, k.date)              as bill_name,
            k.date,
            k.house,
            string_agg(ss.stage_labels, ' · ')        as stage_labels,
            arg_max(ss.furthest_stage, ss.furthest_stage_order) as furthest_stage,
            max(ss.furthest_stage_order)              as furthest_stage_order,
            coalesce(sum(t.talker_turns), 0)          as talker_turns,
            coalesce(sum(t.distinct_speakers), 0)     as distinct_speakers,
            coalesce(sum(t.words), 0)                 as words,
            coalesce(sum(d.divisions), 0)             as divisions,
            string_agg(d.division_results, ', ')      as division_results
        from keyed k
        left join stage_stats ss using (subject_id)
        left join talker_stats t using (subject_id)
        left join division_stats d using (subject_id)
        group by k.jurisdiction, k.bill_key, k.date, k.house
    """,
    # one row per bill: the Explorer's bills-list shape (houses, span,
    # furthest stage reached, volume) — reads the bill_journey cube above
    "bills": """
        select
            jurisdiction,
            bill_key,
            arg_max(bill_name, date)                  as bill_name,
            string_agg(distinct house, ', ')          as house_names,
            count(distinct house)                     as houses,
            min(date)                                 as first_sitting,
            max(date)                                 as last_sitting,
            count(*)                                  as house_days,
            arg_max(furthest_stage, furthest_stage_order) as latest_stage,
            max(furthest_stage_order)                 as latest_stage_order,
            sum(talker_turns)                         as talker_turns,
            sum(words)                                as words,
            sum(divisions)                            as divisions
        from bill_journey
        group by 1, 2
    """,
    "theme_by_week": """
        select
            jurisdiction,
            house,
            engine,
            model,
            datepart('isoyear', cast(date as date))  as iso_year,
            datepart('week', cast(date as date))     as iso_week,
            min(date)                                as week_start_sitting,
            theme_id,
            any_value(theme_name)                    as theme_name,
            count(*)                                 as subject_occurrences,
            count(*) filter (rank = 1)               as top_rank_occurrences,
            round(avg(score), 4)                     as avg_score
        from subject_themes
        group by 1, 2, 3, 4, 5, 6, 8
    """,
    "theme_cooccurrence": """
        select
            a.jurisdiction,
            a.engine,
            a.model,
            a.theme_id              as theme_id_a,
            any_value(a.theme_name) as theme_name_a,
            b.theme_id              as theme_id_b,
            any_value(b.theme_name) as theme_name_b,
            count(*)                as cooccurrences
        from subject_themes a
        join subject_themes b
          on a.subject_id = b.subject_id
         and a.engine = b.engine and a.model = b.model
         and a.theme_id < b.theme_id
        group by 1, 2, 3, 4, 6
    """,
    "member_theme_rank": """
        with themed_turns as (
            select
                st.engine, st.model, st.theme_id, st.theme_name,
                t.jurisdiction, t.member_source_id, t.name, t.word_count, t.date
            from talkers t
            join subject_themes st on t.subject_id = st.subject_id
            where t.member_source_id is not null
        )
        select
            jurisdiction,
            engine,
            model,
            theme_id,
            any_value(theme_name)         as theme_name,
            member_source_id,
            arg_max(name, date)           as member_name,
            count(*)                      as turns,
            coalesce(sum(word_count), 0)  as words,
            dense_rank() over (
                partition by jurisdiction, engine, model, theme_id
                order by count(*) desc
            )                             as theme_rank
        from themed_turns
        group by jurisdiction, engine, model, theme_id, member_source_id
    """,
    "bill_theme_link": """
        with bill_subjects as (
            select
                s.subject_id, s.jurisdiction, s.date,
                coalesce(b.name, s.name) as bill_name,
                lower(trim(regexp_replace(coalesce(b.name, s.name), '\\s+', ' ', 'g')))
                    as bill_key
            from subjects s
            left join (
                select subject_id, any_value(name) as name
                from bill_refs where name is not null group by 1
            ) b using (subject_id)
            where b.name is not null
               or regexp_matches(coalesce(s.name, ''), '\\bBill\\b')
        )
        select
            bs.jurisdiction,
            st.engine,
            st.model,
            bs.bill_key,
            arg_max(bs.bill_name, bs.date) as bill_name,
            st.theme_id,
            any_value(st.theme_name)       as theme_name,
            count(*)                       as subject_occurrences
        from bill_subjects bs
        join subject_themes st using (subject_id)
        group by 1, 2, 3, 4, 6
    """,
    "member_vote_by_theme": """
        select
            v.jurisdiction,
            st.engine,
            st.model,
            st.theme_id,
            any_value(st.theme_name) as theme_name,
            v.member_source_id,
            coalesce(arg_max(v.member_name, v.date), any_value(m.display_name))
                as member_name,
            v.vote,
            count(*)                 as votes
        from division_votes v
        join divisions d using (division_id)
        join subject_themes st on d.subject_id = st.subject_id
        left join members m
          on v.jurisdiction = m.jurisdiction
         and v.member_source_id = m.source_member_id
        where v.member_source_id is not null
        group by v.jurisdiction, st.engine, st.model, st.theme_id,
                 v.member_source_id, v.vote
    """,
    # curator-workflow port: subjects the classifier could not place (or
    # placed weakly) on house-days that WERE classified — the queue for
    # expanding the taxonomy. Empty until 'enrich themes' runs; days not yet
    # classified are not gaps.
    "theme_candidates": """
        with best as (
            select subject_id, max(score) as best_score
            from subject_themes where rank = 1 group by 1
        ),
        themed_days as (
            select distinct jurisdiction, date, house from subject_themes
        )
        select
            s.jurisdiction,
            s.date,
            s.house,
            s.subject_id,
            s.name as subject_name,
            b.best_score,
            case when b.subject_id is null then 'unclassified'
                 else 'low_confidence' end as reason
        from subjects s
        join themed_days using (jurisdiction, date, house)
        left join best b using (subject_id)
        where b.subject_id is null or b.best_score < 0.30
    """,
    "sitting_days": """
        with subject_stats as (
            select fragment_id, count(*) as subjects
            from subjects group by 1
        ),
        talker_stats as (
            select
                fragment_id,
                count(*)                          as talker_turns,
                count(distinct member_source_id)  as distinct_speakers,
                coalesce(sum(word_count), 0)      as words
            from talkers group by 1
        ),
        division_stats as (
            select fragment_id, count(*) as divisions
            from divisions group by 1
        )
        select
            f.jurisdiction,
            f.date,
            f.house,
            f.committee_name,
            f.parliament_num,
            f.session_num,
            f.review_stage,
            f.start_time,
            f.end_time,
            date_diff('minute', f.start_time, f.end_time)  as duration_minutes,
            f.start_page,
            f.end_page,
            coalesce(s.subjects, 0)             as subjects,
            coalesce(t.talker_turns, 0)         as talker_turns,
            coalesce(t.distinct_speakers, 0)    as distinct_speakers,
            coalesce(t.words, 0)                as words,
            coalesce(d.divisions, 0)            as divisions,
            f.extract_count,
            f.fragment_id
        from fragments f
        left join subject_stats s using (fragment_id)
        left join talker_stats t using (fragment_id)
        left join division_stats d using (fragment_id)
    """,
}


def _attach_silver(con: duckdb.DuckDBPyConnection, silver_dir: Path) -> None:
    """Expose each silver table as a view (empty-but-typed when no data)."""
    for table in TABLES:
        table_dir = Path(silver_dir) / table
        if table_dir.is_dir() and any(table_dir.rglob("*.parquet")):
            con.execute(
                f"create or replace view {table} as select * from read_parquet("
                f"'{table_dir.as_posix()}/**/*.parquet', hive_partitioning=1)"
            )
        else:
            empty = SCHEMAS[table].empty_table()
            con.register(f"_empty_{table}", empty)
            con.execute(f"create or replace view {table} as select * from _empty_{table}")


def _attach_reference(
    con: duckdb.DuckDBPyConnection,
    reference_dir: Path | None,
    enriched_dir: Path | None = None,
) -> None:
    """Expose the member register, theme assignments and stage vocabulary as
    views (empty-but-typed when absent)."""
    members_dir = Path(reference_dir) / "members" if reference_dir else None
    if members_dir and members_dir.is_dir() and any(members_dir.rglob("*.parquet")):
        con.execute(
            f"create or replace view members as select * from read_parquet("
            f"'{members_dir.as_posix()}/**/*.parquet', hive_partitioning=1)"
        )
    else:
        con.register("_empty_members", REGISTER_SCHEMA.empty_table())
        con.execute("create or replace view members as select * from _empty_members")
    # optional theme assignments from 'enrich themes' (empty-but-typed when absent)
    themes_dir = Path(enriched_dir) / "themes" if enriched_dir else None
    if themes_dir and themes_dir.is_dir() and any(themes_dir.rglob("*.parquet")):
        con.execute(
            f"create or replace view subject_themes as select * from read_parquet("
            f"'{themes_dir.as_posix()}/**/*.parquet', hive_partitioning=1)"
        )
    else:
        from parlhansard.enrich.themes import SCHEMA as SUBJECT_THEMES_SCHEMA

        con.register("_empty_subject_themes", SUBJECT_THEMES_SCHEMA.empty_table())
        con.execute(
            "create or replace view subject_themes as select * from _empty_subject_themes"
        )
    # curated stage vocabulary ships with the package (reference/stages/*.yaml)
    con.register(
        "_stage_vocab",
        pa.Table.from_pylist(
            load_stage_vocab(),
            schema=pa.schema(
                [
                    ("jurisdiction", pa.string()),
                    ("name_lower", pa.string()),
                    ("stage", pa.string()),
                    ("stage_order", pa.int32()),
                ]
            ),
        ),
    )
    con.execute("create or replace view stage_vocab as select * from _stage_vocab")


def build_gold(
    silver_dir: Path,
    gold_dir: Path,
    reference_dir: Path | None = None,
    enriched_dir: Path | None = None,
) -> dict[str, int]:
    """Recompute all gold cubes from silver (+ optional register/themes)."""
    gold_dir = Path(gold_dir)
    gold_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    _attach_silver(con, silver_dir)
    _attach_reference(con, reference_dir, enriched_dir)

    counts: dict[str, int] = {}
    for name, query in GOLD_QUERIES.items():
        out = (gold_dir / f"{name}.parquet").as_posix()
        con.execute(f"copy ({query}) to '{out}' (format parquet)")
        (counts[name],) = con.execute(f"select count(*) from '{out}'").fetchone()
        # written cubes are queryable by later cubes (bills reads bill_journey)
        con.execute(f"create or replace view {name} as select * from '{out}'")
    return counts


def build_db(
    gold_dir: Path, out_path: Path, *, silver_dir: Path | None = None
) -> list[str]:
    """Build a self-contained hansard.duckdb from gold (and optionally silver).

    The default artifact is gold-only — derived facts, publishable everywhere.
    ``silver_dir`` additionally materializes the full-text silver tables for
    LOCAL analysis only (do not publish; see LICENSES-DATA.md).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    con = duckdb.connect(str(out_path))
    tables: list[str] = []
    for parquet in sorted(Path(gold_dir).glob("*.parquet")):
        name = parquet.stem
        con.execute(f"create table {name} as select * from '{parquet.as_posix()}'")
        tables.append(name)
    if silver_dir is not None:
        for table in TABLES:
            table_dir = Path(silver_dir) / table
            if table_dir.is_dir() and any(table_dir.rglob("*.parquet")):
                con.execute(
                    f"create table silver_{table} as select * from read_parquet("
                    f"'{table_dir.as_posix()}/**/*.parquet', hive_partitioning=1)"
                )
                tables.append(f"silver_{table}")
    con.close()
    return tables

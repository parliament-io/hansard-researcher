"""Reference data (Tier 2): member registers per jurisdiction.

Registers hold facts — who sat, for which house/electorate, for which party,
over which dates — so unlike Hansard prose they are publishable. Each
jurisdiction gets a builder that fetches official sources into
``data/reference/raw/{jur}/`` (stored offline, like the raw Hansard store)
and normalizes them into one Parquet register that joins to silver on
``source_member_id`` = ``talkers.member_source_id``.

Builders: sa (live — membersapp + contact-details APIs). WA/NSW/AU/Scotland
follow (see docs/ROADMAP.md); the WA/SA Hansard API's sessional member index
is a speech index (name → referenceid + counts), NOT a register — verified
2026-07-04, it carries no party/electorate/dates.
"""

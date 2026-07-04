---
title: hansard-researcher — Parliamentary Hansard Analytics
---

Open analytics over Parliamentary Hansard, rebuilt from official sources every
sitting day. All figures on this site are **derived statistics** — counts,
votes, timings — computed from the official record; full text remains with
each parliament (see the
[data licensing notes](https://github.com/parliament-io/hansard-researcher/blob/main/LICENSES-DATA.md)).

```sql overview
select
    jurisdiction,
    count(distinct date)            as sitting_days,
    sum(subjects)                   as subjects,
    sum(talker_turns)               as speaking_turns,
    sum(words)                      as words_spoken,
    sum(divisions)                  as divisions
from hansard.sitting_days
group by 1
order by 1
```

<DataTable data={overview} title="Coverage by jurisdiction" />

```sql turns_by_week
select
    jurisdiction,
    iso_year || '-W' || lpad(iso_week::varchar, 2, '0') as week,
    sum(turns) as turns
from hansard.member_activity_by_week
group by 1, 2
order by 2
```

<BarChart
    data={turns_by_week}
    x=week
    y=turns
    series=jurisdiction
    title="Speaking turns per sitting week"
/>

## Explore

- [Members](/members) — activity league tables, words spoken, first speeches
- [Question Time](/question-time) — who asks, who answers, response patterns
- [Divisions](/divisions) — recorded votes with per-member breakdowns
- [Bills](/bills) — each bill's journey through the houses, stage by stage
- [Themes](/themes) — what parliament debates (BYO-provider classification)
- [Themes Across Parliaments](/themes-compare) — common ground, local
  obsessions, and how themes travel between jurisdictions
- [Sitting Days](/sittings) — chamber rhythm and volume

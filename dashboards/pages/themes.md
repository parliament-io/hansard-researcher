---
title: Themes
---

What parliament debates, classified against the open
[seed taxonomy](https://github.com/parliament-io/hansard-researcher/tree/main/src/hansard_researcher/reference/themes)
(≤30 broad categories per locale). Classification is **bring-your-own
provider** (`hansard-researcher enrich themes`) — this page is empty until it has
been run; each chart is scoped to one engine+model so different providers
never mix. For cross-jurisdiction comparison — common ground, local
obsessions, theme travel — see [Themes Across Parliaments](/themes-compare).

```sql models
select distinct engine || ':' || model as model_key, engine, model
from hansard.theme_by_week
order by 1
```

<Dropdown data={models} name=model value=model_key title="Classifier (engine:model)" />

```sql themes_over_time
select
    iso_year || '-W' || lpad(iso_week::varchar, 2, '0') as week,
    theme_name,
    sum(subject_occurrences) as subjects
from hansard.theme_by_week
where engine || ':' || model = '${inputs.model.value}'
group by 1, 2
order by 1
```

<BarChart
    data={themes_over_time}
    x=week
    y=subjects
    series=theme_name
    title="Themes debated per sitting week (subject occurrences)"
/>

```sql top_themes
select
    theme_name,
    jurisdiction,
    sum(subject_occurrences) as subjects,
    round(avg(avg_score), 3) as avg_score
from hansard.theme_by_week
where engine || ':' || model = '${inputs.model.value}'
group by 1, 2
order by subjects desc
limit 30
```

<DataTable data={top_themes} title="Most-debated themes, by jurisdiction" />

```sql theme_picker
select distinct theme_id, theme_name from hansard.theme_by_week
where engine || ':' || model = '${inputs.model.value}'
order by theme_name
```

<Dropdown data={theme_picker} name=theme value=theme_id label=theme_name title="Theme detail" />

```sql theme_members
select
    member_name,
    jurisdiction,
    turns,
    words,
    theme_rank
from hansard.member_theme_rank
where engine || ':' || model = '${inputs.model.value}'
  and theme_id = '${inputs.theme.value}'
order by theme_rank, words desc
limit 25
```

<DataTable data={theme_members} title="Who speaks on this theme" />

```sql theme_votes
select
    member_name,
    jurisdiction,
    vote,
    votes
from hansard.member_vote_by_theme
where engine || ':' || model = '${inputs.model.value}'
  and theme_id = '${inputs.theme.value}'
order by votes desc
limit 25
```

<DataTable data={theme_votes} title="Division votes on this theme" />

```sql theme_bills
select
    bill_name,
    jurisdiction,
    subject_occurrences
from hansard.bill_theme_link
where engine || ':' || model = '${inputs.model.value}'
  and theme_id = '${inputs.theme.value}'
order by subject_occurrences desc
limit 25
```

<DataTable data={theme_bills} title="Bills linked to this theme" />

```sql candidates
select jurisdiction, count(*) as subjects,
       count(*) filter (reason = 'unclassified') as unclassified,
       count(*) filter (reason = 'low_confidence') as low_confidence
from hansard.theme_candidates
group by 1 order by 1
```

<DataTable data={candidates} title="Theme candidates — subjects the taxonomy could not place (curation queue)" />

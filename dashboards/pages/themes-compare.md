---
title: Themes Across Parliaments
---

What every parliament debates — compared. All figures are each theme's **share
of that jurisdiction's classified debate**, not raw counts, so chambers of
different sizes and sitting patterns can sit on the same axis. Shares count
each subject once, by its primary (top-ranked) theme. Scoped to one
classifier (engine:model) so providers never mix.

```sql models
select distinct engine || ':' || model as model_key, engine, model
from hansard.theme_by_week
order by 1
```

<Dropdown data={models} name=model value=model_key title="Classifier (engine:model)" />

## Comparability check

Before reading the comparisons: how much of each jurisdiction's debate has
this classifier actually placed? Low or uneven coverage skews shares.

```sql coverage
select jurisdiction, total_subjects, classified_subjects, classified_pct
from hansard.theme_coverage
where engine || ':' || model = '${inputs.model.value}'
order by jurisdiction
```

<DataTable data={coverage} title="Classification coverage by jurisdiction">
    <Column id=jurisdiction />
    <Column id=total_subjects fmt=num0 />
    <Column id=classified_subjects fmt=num0 />
    <Column id=classified_pct title="Classified %" contentType=colorscale scaleColor=green />
</DataTable>

## The map: theme share by jurisdiction

Each cell is the theme's share of that jurisdiction's classified subjects.
Read down a column for one parliament's agenda; read across a row to see how
a theme travels.

```sql theme_share
select theme_name, jurisdiction, subjects, share_pct
from hansard.theme_share_by_jurisdiction
where engine || ':' || model = '${inputs.model.value}'
```

<Heatmap
    data={theme_share}
    x=jurisdiction
    y=theme_name
    value=share_pct
    valueFmt='0.0"%"'
    title="Share of debate by theme and jurisdiction"
/>

## Common ground vs local obsessions

**Common ground** — themes debated at a meaningful share (≥1%) in *every*
jurisdiction, ranked by how uniform that attention is (low spread = truly
shared agenda).

```sql common_themes
with n_juris as (
    select count(distinct jurisdiction) as n
    from hansard.theme_share_by_jurisdiction
    where engine || ':' || model = '${inputs.model.value}'
)
select
    theme_name,
    count(*)                 as jurisdictions,
    round(avg(share_pct), 2) as avg_share_pct,
    round(min(share_pct), 2) as min_share_pct,
    round(max(share_pct), 2) as max_share_pct,
    round(stddev_pop(share_pct) / nullif(avg(share_pct), 0), 2)
        as spread  -- coefficient of variation; lower = more uniform
from hansard.theme_share_by_jurisdiction
where engine || ':' || model = '${inputs.model.value}'
group by 1
having count(*) = (select n from n_juris)   -- present everywhere
   and min(share_pct) >= 1.0                -- and not trivially
order by spread asc, avg_share_pct desc
limit 15
```

<DataTable data={common_themes} title="Themes every parliament debates (most uniform first)">
    <Column id=theme_name />
    <Column id=jurisdictions />
    <Column id=avg_share_pct title="Avg share %" />
    <Column id=min_share_pct title="Min %" />
    <Column id=max_share_pct title="Max %" />
    <Column id=spread title="Spread (CV)" contentType=colorscale scaleColor=blue />
</DataTable>

**Local obsessions** — for each jurisdiction, the themes it debates far more
than the pooled average. Lift = jurisdiction share ÷ all-parliaments share;
2.0 means twice the attention it gets across parliaments overall.

```sql distinctive
with pooled as (
    select
        theme_id,
        100.0 * sum(subjects) / sum(sum(subjects)) over () as overall_pct
    from hansard.theme_share_by_jurisdiction
    where engine || ':' || model = '${inputs.model.value}'
    group by 1
)
select
    s.jurisdiction,
    s.theme_name,
    round(s.share_pct, 2)   as share_pct,
    round(p.overall_pct, 2) as overall_pct,
    round(s.share_pct / nullif(p.overall_pct, 0), 1) as lift
from hansard.theme_share_by_jurisdiction s
join pooled p using (theme_id)
where s.engine || ':' || s.model = '${inputs.model.value}'
  and s.share_pct >= 1.0
qualify row_number() over (
    partition by s.jurisdiction
    order by s.share_pct / nullif(p.overall_pct, 0) desc) <= 5
order by s.jurisdiction, lift desc
```

<DataTable data={distinctive} title="Most distinctive themes per jurisdiction (top 5 by lift)" groupBy=jurisdiction>
    <Column id=theme_name />
    <Column id=share_pct title="Local share %" />
    <Column id=overall_pct title="All parliaments %" />
    <Column id=lift contentType=colorscale scaleColor=red />
</DataTable>

## One theme, every parliament

Pick a theme to see how attention moves across jurisdictions over time —
who leads, who follows, who never picks it up.

```sql theme_picker
select distinct theme_id, theme_name
from hansard.theme_by_week
where engine || ':' || model = '${inputs.model.value}'
order by theme_name
```

<Dropdown data={theme_picker} name=theme value=theme_id label=theme_name title="Theme" />

```sql theme_trend
with weekly_totals as (
    select
        jurisdiction, iso_year, iso_week,
        sum(top_rank_occurrences) as week_total
    from hansard.theme_by_week
    where engine || ':' || model = '${inputs.model.value}'
    group by 1, 2, 3
)
select
    t.jurisdiction,
    t.iso_year || '-W' || lpad(t.iso_week::varchar, 2, '0') as week,
    round(100.0 * sum(t.top_rank_occurrences) / any_value(w.week_total), 1)
        as share_pct
from hansard.theme_by_week t
join weekly_totals w using (jurisdiction, iso_year, iso_week)
where t.engine || ':' || t.model = '${inputs.model.value}'
  and t.theme_id = '${inputs.theme.value}'
group by 1, 2
order by 2
```

<LineChart
    data={theme_trend}
    x=week
    y=share_pct
    series=jurisdiction
    yFmt='0.0"%"'
    title="Share of each week's debate, by jurisdiction"
/>

```sql theme_peaks
with weekly as (
    select
        jurisdiction,
        iso_year || '-W' || lpad(iso_week::varchar, 2, '0') as week,
        sum(top_rank_occurrences) as subjects
    from hansard.theme_by_week
    where engine || ':' || model = '${inputs.model.value}'
      and theme_id = '${inputs.theme.value}'
    group by 1, 2
)
select
    jurisdiction,
    arg_max(week, subjects) as peak_week,
    max(subjects)           as peak_subjects,
    sum(subjects)           as total_subjects
from weekly
group by 1
order by peak_week
```

<DataTable data={theme_peaks} title="When attention peaked — lead and lag across parliaments" />

**Same theme, different framing** — the subject headings each parliament
files under this theme. Official-record titles only; near-identical rows are
shared national conversations, divergent ones are local framings.

```sql theme_framing
select jurisdiction, subject_name, occurrences
from hansard.theme_subject_names
where engine || ':' || model = '${inputs.model.value}'
  and theme_id = '${inputs.theme.value}'
order by jurisdiction, occurrences desc
```

<DataTable data={theme_framing} title="Top subject headings per jurisdiction" groupBy=jurisdiction rows=40 />

**The same theme in legislation** — bills each parliament attached to this
theme. Near-identical names across rows are your uniform/mirror-legislation
candidates.

```sql theme_bills_compare
select
    jurisdiction,
    bill_name,
    subject_occurrences
from hansard.bill_theme_link
where engine || ':' || model = '${inputs.model.value}'
  and theme_id = '${inputs.theme.value}'
order by jurisdiction, subject_occurrences desc
```

<DataTable data={theme_bills_compare} title="Bills linked to this theme, by jurisdiction" groupBy=jurisdiction search=true rows=30 />

**Who owns the theme in each chamber** — top speakers per jurisdiction.

```sql theme_speakers_compare
select
    jurisdiction,
    member_name,
    turns,
    words
from hansard.member_theme_rank
where engine || ':' || model = '${inputs.model.value}'
  and theme_id = '${inputs.theme.value}'
qualify row_number() over (partition by jurisdiction order by words desc) <= 5
order by jurisdiction, words desc
```

<DataTable data={theme_speakers_compare} title="Leading voices per jurisdiction (top 5 by words)" groupBy=jurisdiction>
    <Column id=member_name />
    <Column id=turns />
    <Column id=words fmt=num0 />
</DataTable>

---
title: Divisions
---

Recorded votes with per-member breakdowns. Results marked *derived* are
computed from the ayes/noes counts where the source omits an explicit result.
NSW member names on votes are resolved from the open member register (the
source XML publishes ids only).

```sql by_year
select
    jurisdiction,
    year(cast(date as date)) as year,
    count(*)                 as divisions
from hansard.division_summary
group by 1, 2
order by 2
```

<BarChart
    data={by_year}
    x=year
    y=divisions
    series=jurisdiction
    title="Divisions per year"
/>

Pick a jurisdiction and year to browse divisions (the archive holds too many
to list at once):

```sql jurisdictions
select distinct jurisdiction from hansard.division_summary order by 1
```

<Dropdown data={jurisdictions} name=jurisdiction value=jurisdiction title="Jurisdiction" />

```sql years
select distinct year(cast(date as date)) as year
from hansard.division_summary
where jurisdiction = '${inputs.jurisdiction.value}'
order by 1 desc
```

<Dropdown data={years} name=year value=year title="Year" />

```sql divisions
select
    date,
    house,
    subject_name,
    result,
    ayes_count,
    noes_count,
    margin,
    recorded_votes
from hansard.division_summary
where jurisdiction = '${inputs.jurisdiction.value}'
  and year(cast(date as date)) = ${inputs.year.value}
order by date desc
```

<DataTable data={divisions} title="Divisions" search=true rows=25 />

```sql division_picker
select
    division_id,
    date || ' — ' || coalesce(subject_name, 'unknown subject')
        || ' (' || house || ')' as label
from hansard.division_summary
where jurisdiction = '${inputs.jurisdiction.value}'
  and year(cast(date as date)) = ${inputs.year.value}
order by date desc, division_id
```

<Dropdown data={division_picker} name=division value=division_id label=label title="Division detail" />

```sql votes
select
    member_name,
    party,
    vote,
    case when teller then 'teller' else '' end as teller,
    voted_with_result
from hansard.division_votes_detail
where division_id = '${inputs.division.value}'
order by vote, member_name
```

<DataTable data={votes} title="Member votes" rows=60>
    <Column id=member_name />
    <Column id=party />
    <Column id=vote />
    <Column id=teller />
    <Column id=voted_with_result contentType=boolean />
</DataTable>

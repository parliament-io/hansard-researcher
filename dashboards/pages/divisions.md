---
title: Divisions
---

Recorded votes with per-member breakdowns. Results marked *derived* are
computed from the ayes/noes counts where the source omits an explicit result.

```sql divisions
select
    date,
    jurisdiction,
    house,
    subject_name,
    result,
    ayes_count,
    noes_count,
    margin,
    recorded_votes
from hansard.division_summary
order by date desc
```

<DataTable data={divisions} title="Divisions" search=true />

```sql division_picker
select
    division_id,
    date || ' — ' || coalesce(subject_name, 'unknown subject')
        || ' (' || upper(jurisdiction) || ')' as label
from hansard.division_summary
order by date desc
```

<Dropdown data={division_picker} name=division value=division_id label=label title="Division detail" />

```sql votes
select
    member_name,
    vote,
    case when teller then 'teller' else '' end as teller,
    voted_with_result
from hansard.division_votes_detail
where division_id = '${inputs.division.value}'
order by vote, member_name
```

<DataTable data={votes} title="Member votes" rows=60>
    <Column id=member_name />
    <Column id=vote />
    <Column id=teller />
    <Column id=voted_with_result contentType=boolean />
</DataTable>

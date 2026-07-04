---
title: Bills
---

Every bill's journey through parliament — sitting by sitting, across both
houses. Stages are shown as published; where a stage maps into the curated
[stage vocabulary](https://github.com/parlhansard/parlhansard/tree/main/src/parlhansard/reference/stages)
it also gets a canonical stage (so the NSW Assembly's "Agreement in
Principle" lines up with "Second Reading" elsewhere).

```sql bills_by_year
select
    jurisdiction,
    year(cast(last_sitting as date)) as year,
    count(*)                         as bills
from hansard.bills
group by 1, 2
order by 2
```

<BarChart
    data={bills_by_year}
    x=year
    y=bills
    series=jurisdiction
    title="Bills debated, by year of last sitting"
/>

Pick a jurisdiction and year to browse bills (the archive holds too many to
list at once):

```sql jurisdictions
select distinct jurisdiction from hansard.bills order by 1
```

<Dropdown data={jurisdictions} name=jurisdiction value=jurisdiction title="Jurisdiction" />

```sql years
select distinct year(cast(date as date)) as year
from hansard.bill_journey
where jurisdiction = '${inputs.jurisdiction.value}'
order by 1 desc
```

<Dropdown data={years} name=year value=year title="Year" />

```sql bills_active
select
    b.bill_name,
    b.house_names,
    b.first_sitting,
    b.last_sitting,
    b.house_days,
    b.latest_stage,
    b.words,
    b.divisions
from hansard.bills b
where b.jurisdiction = '${inputs.jurisdiction.value}'
  and year(cast(b.first_sitting as date)) <= ${inputs.year.value}
  and year(cast(b.last_sitting as date)) >= ${inputs.year.value}
order by b.last_sitting desc
```

<DataTable data={bills_active} title="Bills active in the selected year" search=true rows=25>
    <Column id=bill_name />
    <Column id=house_names title="Houses" />
    <Column id=first_sitting />
    <Column id=last_sitting />
    <Column id=house_days title="Sittings" />
    <Column id=latest_stage title="Furthest stage" />
    <Column id=words fmt=num0 />
    <Column id=divisions />
</DataTable>

```sql bill_picker
select
    bill_key,
    bill_name || ' (' || house_names || ')' as label
from hansard.bills
where jurisdiction = '${inputs.jurisdiction.value}'
  and year(cast(first_sitting as date)) <= ${inputs.year.value}
  and year(cast(last_sitting as date)) >= ${inputs.year.value}
order by last_sitting desc
```

<Dropdown data={bill_picker} name=bill value=bill_key label=label title="Bill journey" />

```sql journey
select
    date,
    house,
    stage_labels,
    furthest_stage as stage,
    talker_turns,
    distinct_speakers,
    words,
    divisions,
    division_results
from hansard.bill_journey
where jurisdiction = '${inputs.jurisdiction.value}'
  and bill_key = '${inputs.bill.value}'
order by date, house
```

<DataTable data={journey} title="Journey — every sitting the bill was before a house" rows=40>
    <Column id=date />
    <Column id=house />
    <Column id=stage_labels title="Stages (as published)" />
    <Column id=stage title="Canonical stage" />
    <Column id=distinct_speakers title="Speakers" />
    <Column id=words fmt=num0 />
    <Column id=divisions />
    <Column id=division_results title="Division results" />
</DataTable>

<BarChart
    data={journey}
    x=date
    y=words
    series=house
    title="Debate volume across the journey, by house"
/>

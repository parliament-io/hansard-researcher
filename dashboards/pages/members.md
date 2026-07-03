---
title: Members
---

```sql jurisdictions
select distinct jurisdiction from hansard.member_activity order by 1
```

<Dropdown data={jurisdictions} name=jurisdiction value=jurisdiction title="Jurisdiction">
    <DropdownOption value="%" valueLabel="All"/>
</Dropdown>

```sql members
select
    member_name,
    jurisdiction,
    party_abbreviation as party,
    electorate,
    turns,
    speeches,
    questions,
    answers,
    interjections,
    words,
    subjects,
    sitting_days,
    division_votes
from hansard.member_activity
where jurisdiction like '${inputs.jurisdiction.value}'
order by words desc
```

<DataTable data={members} title="Member activity (all sittings harvested)" search=true rows=25>
    <Column id=member_name />
    <Column id=jurisdiction />
    <Column id=party />
    <Column id=electorate />
    <Column id=turns />
    <Column id=speeches />
    <Column id=questions />
    <Column id=answers />
    <Column id=words fmt=num0 />
    <Column id=subjects />
    <Column id=division_votes />
</DataTable>

```sql top_talkers
select member_name, words
from hansard.member_activity
where jurisdiction like '${inputs.jurisdiction.value}'
order by words desc
limit 15
```

<BarChart
    data={top_talkers}
    x=member_name
    y=words
    swapXY=true
    title="Most words spoken"
/>

```sql first_speeches
select member_name, jurisdiction, party_abbreviation as party, electorate, first_sitting
from hansard.member_activity
where gave_first_speech
order by first_sitting desc
```

<DataTable data={first_speeches} title="First speeches" />

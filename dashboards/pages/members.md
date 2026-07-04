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

Speech/question/answer columns only count turns the source (or the
same-member inference pass) typed — read them against this coverage, not
against `turns`: WA and SA mark up only the lead turn of each exchange, so
their untyped remainder is a markup gap, not silence.

```sql kind_coverage
select
    jurisdiction,
    sum(turns) as turns,
    sum(speeches + questions + answers + interjections) as typed_turns,
    round(100.0 * sum(speeches + questions + answers + interjections)
        / sum(turns), 1) as typed_pct
from hansard.member_activity
group by 1
order by 1
```

<DataTable data={kind_coverage} title="Contribution-kind coverage by jurisdiction">
    <Column id=jurisdiction />
    <Column id=turns fmt=num0 />
    <Column id=typed_turns fmt=num0 />
    <Column id=typed_pct title="Typed %" fmt='0.0"%"' contentType=colorscale scaleColor=green />
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

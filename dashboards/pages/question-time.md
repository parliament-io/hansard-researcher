---
title: Question Time
---

Structural question → answer pairing: each question is matched with the next
answer in the same subject.

```sql qa_by_party
select
    jurisdiction,
    coalesce(question_party, 'unknown') as party,
    count(*)                            as questions,
    count(*) filter (answered)          as answered,
    round(avg(answer_words), 0)         as avg_answer_words
from hansard.qa_pairs
group by 1, 2
order by questions desc
```

<BarChart
    data={qa_by_party}
    x=party
    y=questions
    series=jurisdiction
    title="Questions asked, by party"
/>

```sql answerers
select
    answer_member,
    jurisdiction,
    count(*)                    as answers,
    sum(answer_words)           as words,
    round(avg(answer_words), 0) as avg_words
from hansard.qa_pairs
where answer_member is not null
group by 1, 2
order by answers desc
limit 15
```

<DataTable data={answerers} title="Who answers — ministers by volume" />

Pick a jurisdiction and year to browse individual question/answer pairs (the
archive holds too many to list at once):

```sql qt_jurisdictions
select distinct jurisdiction from hansard.qa_pairs order by 1
```

<Dropdown data={qt_jurisdictions} name=qt_jurisdiction value=jurisdiction title="Jurisdiction" />

```sql qt_years
select distinct year(cast(date as date)) as year
from hansard.qa_pairs
where jurisdiction = '${inputs.qt_jurisdiction.value}'
order by 1 desc
```

<Dropdown data={qt_years} name=qt_year value=year title="Year" />

```sql qa_detail
select
    date,
    house,
    subject_name,
    question_member,
    question_party,
    answer_member,
    answer_words
from hansard.qa_pairs
where jurisdiction = '${inputs.qt_jurisdiction.value}'
  and year(cast(date as date)) = ${inputs.qt_year.value}
order by date desc, subject_name
```

<DataTable data={qa_detail} title="Question/answer pairs" search=true rows=25 />

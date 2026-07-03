---
title: Sitting Days
---

```sql sittings
select
    date,
    jurisdiction,
    house,
    round(duration_minutes / 60.0, 1) as hours,
    subjects,
    talker_turns,
    distinct_speakers,
    words,
    divisions,
    review_stage
from hansard.sitting_days
order by date desc
```

<DataTable data={sittings} title="Sitting days harvested" />

<BarChart
    data={sittings}
    x=date
    y=hours
    series=house
    title="Sitting hours per day"
/>

```sql volume
select date, jurisdiction || '/' || house as chamber, words
from hansard.sitting_days
order by date
```

<BarChart
    data={volume}
    x=date
    y=words
    series=chamber
    title="Words spoken per sitting day"
/>

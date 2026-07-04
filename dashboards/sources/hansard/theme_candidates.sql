-- Evidence writes an invalid 0-byte parquet for empty results, which breaks
-- the build (theme cubes ARE empty until 'enrich themes' runs — e.g. in CI).
-- The sentinel left-join yields one all-null row instead; pages filter it out.
with sentinel as (select 1 as one)
select t.* from sentinel left join theme_candidates t on true

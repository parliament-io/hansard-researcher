import datetime as dt

from hansard_researcher.model.canonical import ReviewStage
from hansard_researcher.model.hashing import fragment_content_hash


def test_hash_is_stable(synthetic_fragment):
    assert fragment_content_hash(synthetic_fragment) == fragment_content_hash(synthetic_fragment)


def test_volatile_fields_do_not_change_hash(synthetic_fragment):
    before = fragment_content_hash(synthetic_fragment)
    bumped = synthetic_fragment.model_copy(
        update={
            "review_stage": ReviewStage.PUBLISHED,
            "date_modified": dt.datetime(2026, 3, 5, 9, 0),
            "retrieved_at": dt.datetime(2026, 3, 6, 12, 0),
            "source_url": "https://example.invalid/redownload",
        }
    )
    assert fragment_content_hash(bumped) == before


def test_content_change_changes_hash(synthetic_fragment):
    before = fragment_content_hash(synthetic_fragment)
    changed = synthetic_fragment.model_copy(deep=True)
    changed.proceedings[0].subjects[0].talkers[1].texts[0].clean_text = "No."
    assert fragment_content_hash(changed) != before

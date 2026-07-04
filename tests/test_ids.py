from hansard_researcher.model.ids import deterministic_id


def test_same_parts_same_id():
    assert deterministic_id("wa", "2026-03-04", "lh") == deterministic_id("wa", "2026-03-04", "lh")


def test_different_parts_different_id():
    assert deterministic_id("wa", "2026-03-04") != deterministic_id("sa", "2026-03-04")


def test_order_matters():
    assert deterministic_id("a", "b") != deterministic_id("b", "a")


def test_boundary_shift_does_not_collide():
    assert deterministic_id("ab", "c") != deterministic_id("a", "bc")


def test_none_distinct_from_empty_string():
    assert deterministic_id("x", None) != deterministic_id("x", "")


def test_is_valid_uuid_string():
    import uuid

    uuid.UUID(deterministic_id("anything"))

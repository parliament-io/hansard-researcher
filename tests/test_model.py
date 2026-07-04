import json

from hansard_researcher.model.canonical import Fragment


def test_json_round_trip(synthetic_fragment):
    dumped = synthetic_fragment.model_dump_json()
    restored = Fragment.model_validate_json(dumped)
    assert restored == synthetic_fragment


def test_json_schema_generates():
    schema = Fragment.model_json_schema()
    assert schema["title"] == "Fragment"
    # the debate hierarchy is reachable from the schema
    for name in ("Proceeding", "Subject", "Talker", "TextPara", "Division", "DivisionVote"):
        assert name in schema["$defs"], f"missing {name} in $defs"
    json.dumps(schema)  # serializable


def test_document_order_reconstructs_interleaving(synthetic_fragment):
    subject = synthetic_fragment.proceedings[0].subjects[0]
    items = sorted(
        [*subject.talkers, *subject.texts, *subject.divisions],
        key=lambda n: n.document_order,
    )
    orders = [n.document_order for n in items]
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)

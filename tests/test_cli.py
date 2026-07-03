import json

from parlhansard.cli import main


def test_sources_lists_all_jurisdictions(capsys):
    assert main(["sources"]) == 0
    out = capsys.readouterr().out
    for code in ("wa", "sa", "nsw", "au", "nz", "scot"):
        assert code in out


def test_schema_emits_valid_json(capsys):
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "parlhansard canonical Hansard fragment"


def test_harvest_stub_exits_2(capsys):
    assert main(["harvest", "nz", "--start", "2026-01-01", "--end", "2026-01-31"]) == 2
    assert "not available yet" in capsys.readouterr().err

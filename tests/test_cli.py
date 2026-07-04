import json

from hansard_researcher.cli import main


def test_sources_lists_all_jurisdictions(capsys):
    assert main(["sources"]) == 0
    out = capsys.readouterr().out
    for code in ("wa", "sa", "nsw", "au", "nz", "scot"):
        assert code in out


def test_schema_emits_valid_json(capsys):
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "Hansard Researcher canonical Hansard fragment"


def test_harvest_stub_exits_2(capsys):
    assert main(["harvest", "nz", "--start", "2026-01-01", "--end", "2026-01-31"]) == 2
    assert "not available yet" in capsys.readouterr().err


def test_status_empty_data_dir(tmp_path, capsys):
    assert main(["status", "--data-dir", str(tmp_path)]) == 0
    assert "no data yet" in capsys.readouterr().out


def test_status_json(tmp_path, capsys):
    assert main(["status", "--json", "--data-dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["jurisdictions"] == {}
    assert payload["gold"] == {"cubes": 0, "built_at": None}

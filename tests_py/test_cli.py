import json
from pathlib import Path

import pytest

from todox.cli import run
from todox.projects import load_config


def test_init_stats_and_registry(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TODOX_CONFIG", str(tmp_path / "config.json"))
    root = tmp_path / "project"
    root.mkdir()
    assert run(["init"], root) == 0
    assert (root / "TODO.md").read_text() == "# TODO\n"
    assert load_config()[0].path == root / "TODO.md"
    capsys.readouterr()
    (root / "TODO.md").write_text("# TODO\n- [ ] A\n- [/] B\n- [x] C (completed: 2026-10-12)\n")
    assert run(["stats", "--json"], root) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["notStarted"], data["inProgress"], data["completed"]) == (1, 1, 1)
    assert run(["remove-project", str(root)], root) == 0
    assert load_config() == []
    assert (root / "TODO.md").exists()


def test_bad_command_and_non_tty(tmp_path: Path):
    with pytest.raises(ValueError, match="Unknown arguments"):
        run(["--bogus"], tmp_path)
    with pytest.raises(ValueError, match="interactive terminal"):
        run(["--global"], tmp_path)

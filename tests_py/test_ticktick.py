import json
import stat
from pathlib import Path

import pytest

from todox.ticktick import Auth, TickTick, TickTickError


def test_auth_round_trip_and_permissions(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TODOX_TICKTICK_TOKEN", raising=False)
    path = tmp_path / "ticktick.json"
    auth = Auth("secret", "list1", "TODO.md")
    auth.save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Auth.load(path) == auth
    monkeypatch.setenv("TODOX_TICKTICK_TOKEN", "from-env")
    assert Auth.load(path).token == "from-env"
    with pytest.raises(TickTickError, match="No TickTick token"):
        Auth().require_token()


def test_call_maps_http_failures():
    def respond(status, body):
        return lambda method, url, headers, payload: (status, body)

    client = TickTick("token")
    client.transport = respond(401, b"")
    with pytest.raises(TickTickError, match="rejected the token"):
        client.projects()
    client.transport = respond(404, b"")
    assert client.projects() == []
    assert client._call("GET", "/open/v1/task/missing") is None
    client.transport = respond(500, b"boom")
    with pytest.raises(TickTickError, match="failed \\(500\\)"):
        client.projects()


def test_ensure_list_reuses_or_creates(fake):
    fake.projects_list = [{"id": "other", "name": "Inbox", "kind": "TASK"}]
    assert fake.ensure_list() == ("list2", "TODO.md")
    assert fake.ensure_list("list2") == ("list2", "TODO.md")
    assert len([p for p in fake.projects_list if p["name"] == "TODO.md"]) == 1


def test_ensure_tag_lowercases_and_caches(fake):
    assert fake.ensure_tag("Web App") == "web app"
    assert fake.ensure_tag("web app") == "web app"
    assert len([t for t in fake.tags_list if t["name"] == "web app"]) == 1


def test_cli_sync_dry_run_json(tmp_path: Path, monkeypatch, capsys):
    from todox.cli import run
    from tests_py.conftest import FakeTickTick

    file = tmp_path / "web-app" / "TODO.md"
    file.parent.mkdir()
    file.write_text("# TODO\n- [ ] Alpha\n")
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "web-app", "path": str(file)}]}))
    monkeypatch.setenv("TODOX_CONFIG", str(config))
    monkeypatch.setenv("TODOX_SYNC_STATE", str(tmp_path / "sync.json"))
    monkeypatch.setenv("TODOX_TICKTICK_TOKEN", "test-token")
    fake = FakeTickTick()
    monkeypatch.setattr(TickTick, "_call", fake._call)
    assert run(["sync", "--dry-run", "--json"], tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dryRun"] is True and payload["changes"][0]["action"] == "create"
    assert "<!-- todox:id=" not in file.read_text()
    assert run(["sync", "--json"], tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["changes"][0]["action"] == "create"
    assert "<!-- todox:id=" in file.read_text()
    assert run(["sync", "--json"], tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["changes"] == []
    with pytest.raises(ValueError, match="keep-local"):
        run(["sync", "--keep-local"], tmp_path)

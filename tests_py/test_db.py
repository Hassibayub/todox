"""SQLite migration, privacy, transaction and interrupted-sync regressions."""
import json
import os
import sqlite3

import pytest

from todox.cli import run
from todox.db import Database, database_path
from todox.projects import Project, load_config, load_projects, save_config
from todox.store import read
from todox.sync import Entry, State, Sync
from todox.ticktick import TickTickError


def legacy(tmp_path, monkeypatch):
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n- [ ] Existing <!-- todox:id=abcdef123456 -->\n")
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "web-app", "path": str(file)}]}))
    state_file = tmp_path / "sync.json"
    data = State("list1", {"abcdef123456": Entry("existing", "inbox", "local-hash", "remote-hash", "Main", str(file),
                                              "2026-10-13T10:00:00+00:00", True)}, True).payload()
    state_file.write_text(json.dumps(data))
    monkeypatch.setenv("TODOX_CONFIG", str(config))
    monkeypatch.setenv("TODOX_SYNC_STATE", str(state_file))
    return file, config, state_file, data


def test_legacy_reads_are_read_only(tmp_path, monkeypatch):
    file, config, state_file, data = legacy(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in (file, config, state_file)}
    assert load_config()[0].name == "web-app"
    assert State.load().payload() == data
    assert not database_path().exists()
    assert all(p.read_bytes() == contents for p, contents in before.items())


def test_migration_preserves_ids_baselines_retirement_and_files(tmp_path, monkeypatch, fake):
    file, config, state_file, data = legacy(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in (file, config, state_file)}
    engine = Sync(fake)
    status = engine.migrate()
    assert status["migrated"] and status["linkedTasks"] == 1 and status["projects"] == 1
    assert State.load().payload() == data
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert not fake.calls
    assert len(list((tmp_path / "backups").glob("*/manifest.json"))) == 1
    engine.migrate()
    assert len(list((tmp_path / "backups").glob("*/manifest.json"))) == 1


def test_registry_switches_to_db_without_dual_writing(tmp_path, monkeypatch):
    file, config, state_file, _ = legacy(tmp_path, monkeypatch)
    original = config.read_bytes()
    save_config([Project("renamed", file)])
    assert load_config()[0].name == "renamed"
    assert config.read_bytes() == original


def test_registry_profiles_are_isolated(tmp_path):
    path = tmp_path / "shared.sqlite3"
    a = Database(path, tmp_path / "a.json")
    b = Database(path, tmp_path / "b.json")
    a.save_projects([{"name": "a", "path": str(tmp_path / "a.md")}])
    b.save_projects([{"name": "b", "path": str(tmp_path / "b.md")}])
    assert [p["name"] for p in a.load_projects()] == ["a"]
    assert [p["name"] for p in b.load_projects()] == ["b"]
    a.save_state(State("a-list").payload())
    assert b.load_state() is None


def test_duplicate_remote_mapping_rolls_back_entire_migration(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    state = State("list1", {uid: Entry("same-remote", "inbox", "a", "b", "Main", str(tmp_path / "TODO.md"))
                             for uid in ("uid-one", "uid-two")})
    with pytest.raises(ValueError, match="UNIQUE"):
        database.migrate_data([{"name": "web-app", "path": str(tmp_path / "TODO.md")}], state.payload())
    assert database.load_state() is None
    assert database.load_projects() is None


def test_colliding_tags_roll_back_registry_update(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.save_projects([{"name": "old", "path": str(tmp_path / "old.md")}])
    with pytest.raises(ValueError, match="UNIQUE"):
        database.save_projects([{"name": "SAME", "path": str(tmp_path / "a.md")},
                                {"name": "same", "path": str(tmp_path / "b.md")}])
    assert [p["name"] for p in database.load_projects()] == ["old"]


def test_database_permissions_and_backup(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.save_state(State("list1").payload())
    assert os.stat(database.path).st_mode & 0o777 == 0o600
    destination = tmp_path / "backup.sqlite3"
    database.backup(destination)
    assert Database(destination).load_state()["listId"] == "list1"
    assert os.stat(destination).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        database.backup(destination)


def test_read_connection_cannot_write(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.save_state(State().payload())
    with pytest.raises(ValueError, match="readonly"):
        with database.connection() as con:
            con.execute("DELETE FROM profiles")


def test_unsupported_schema_rejected_without_modification(tmp_path):
    path = tmp_path / "future.sqlite3"
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version=999")
    con.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported"):
        Database(path).save_state(State().payload())
    assert path.read_bytes() == before


def test_successful_sync_journals_operations_and_snapshots(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Task\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    operations = engine.database.operations()
    assert len(operations) == 1 and operations[0]["status"] == "succeeded"
    assert operations[0]["remote_id"] == "r1"
    entry = next(iter(State.load(engine.path).tasks.values()))
    assert entry.local_snapshot["text"] == "Task"
    assert entry.remote_snapshot["title"] == "Task"
    assert engine.database.status()["runs"][0]["status"] == "succeeded"


def test_lost_upload_response_blocks_duplicate_retry(tmp_path, fake, monkeypatch):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Task\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    original = fake.create_task

    def lost(payload):
        original(payload)
        raise TickTickError("Cannot reach TickTick: response lost")

    monkeypatch.setattr(fake, "create_task", lost)
    plan = engine.plan(load_projects([Project("web-app", file)]))
    engine.apply(plan)
    assert plan.changes[0].error
    assert engine.database.operations(pending=True)[0]["status"] == "uncertain"
    assert len(fake.tasks) == 1
    retry = engine.plan(load_projects([Project("web-app", file)]))
    assert retry.summary() == {"conflict": 1}
    engine.apply(retry)
    assert len(fake.tasks) == 1
    assert engine.recover()[0]["status"] == "needs-review"


def test_uncertain_upload_without_visible_remote_is_not_retried(tmp_path, fake, monkeypatch):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Task\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")

    def lost(payload):
        raise TickTickError("Cannot reach TickTick: timed out")

    monkeypatch.setattr(fake, "create_task", lost)
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    retry = engine.plan(load_projects([Project("web-app", file)]))
    assert retry.summary() == {"conflict": 1}
    assert "uncertain" in retry.changes[0].reason


def test_import_recovery_links_existing_uid_without_appending(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n")
    fake.tasks.append({"id": "remote", "title": "Inbox task", "tags": ["web-app"], "projectId": "inbox", "status": 0})
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    plan = engine.plan(load_projects([Project("web-app", file)]))
    from dataclasses import asdict
    engine.state.save(engine.path)
    run_id = engine.database.begin_run([])
    operation = engine.database.begin_operation(run_id, asdict(plan.changes[0]))
    engine._append(plan, plan.changes[0], "Inbox task", " ", None, plan.changes[0].uid)
    recovery = Sync(fake, path=engine.path)
    assert recovery.recover()[0]["status"] == "recovered"
    assert len(read(file).tasks) == 1
    assert recovery.database.operations()[0]["id"] == operation
    assert recovery.database.operations()[0]["status"] == "succeeded"
    assert recovery.plan(load_projects([Project("web-app", file)])).summary() == {}


def test_local_edit_after_preview_is_not_overwritten(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Local\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    def ps():
        return load_projects([Project("web-app", file)])
    engine.apply(engine.plan(ps()))
    fake.tasks[0]["title"] = "Remote edit"
    plan = engine.plan(ps())
    file.write_text(file.read_text().replace("Local", "New local edit"))
    engine.apply(plan)
    assert plan.changes[0].error
    assert read(file).tasks[0].text == "New local edit"


def test_local_only_projects_do_not_upload_or_delete(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Private\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    plan = engine.plan(load_projects([Project("web-app", file, sync_enabled=False)]), allow_deletes=True)
    assert not plan.pending
    engine.apply(plan)
    assert not fake.tasks and not fake.deleted


def test_status_and_doctor_do_not_initialize_database(tmp_path, capsys):
    assert run(["sync", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["migrated"] is False
    assert run(["doctor"]) == 0
    assert not database_path().exists()


def test_migrate_export_and_history_commands(tmp_path, monkeypatch, capsys):
    _, _, _, data = legacy(tmp_path, monkeypatch)
    assert run(["db", "migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["linkedTasks"] == 1
    assert run(["db", "export"]) == 0
    assert json.loads(capsys.readouterr().out)["sync"] == data
    assert run(["sync", "history"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_independent_local_text_and_remote_status_edits_merge(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Original\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    ps = load_projects([Project("web-app", file)])
    engine.apply(engine.plan(ps))
    file.write_text(file.read_text().replace("Original", "Local title"))
    fake.tasks[0]["status"] = 2
    fake.tasks[0]["completedTime"] = "2026-10-13T10:00:00+0000"
    plan = engine.plan(load_projects([Project("web-app", file)]))
    assert plan.summary() == {"merge": 1}
    engine.apply(plan)
    assert not plan.changes[0].error
    assert read(file).tasks[0].text == "Local title" and read(file).tasks[0].status == "x"
    assert fake.tasks[0]["title"] == "Local title" and fake.tasks[0]["status"] == 2


def test_conflicting_field_edits_stay_conflicts(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Original\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    file.write_text(file.read_text().replace("Original", "Local title"))
    fake.tasks[0]["title"] = "Remote title"
    plan = engine.plan(load_projects([Project("web-app", file)]))
    assert plan.summary() == {"conflict": 1}
    engine.apply(plan)
    assert engine.database.status()["conflicts"] == 1
    assert engine.database.status()["runs"][0]["status"] == "conflicted"


def test_project_controls_persist_without_remote_changes(tmp_path, monkeypatch, capsys):
    _, _, _, _ = legacy(tmp_path, monkeypatch)
    assert run(["project", "web-app", "--local-only"]) == 0
    assert not load_config()[0].sync_enabled
    assert run(["project", "web-app", "--sync"]) == 0
    assert load_config()[0].sync_enabled


def test_database_preview_leaves_files_and_database_unchanged(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Original\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = Sync(fake, path=engine.path)
    preview.plan(load_projects([Project("web-app", file)]))
    after = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after


def test_custom_profile_does_not_import_personal_legacy_state(tmp_path, monkeypatch):
    _, _, _, _ = legacy(tmp_path, monkeypatch)
    custom = tmp_path / "demo-projects.json"
    assert not State.load(registry=custom).tasks


def test_custom_database_does_not_import_default_account_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TODOX_SYNC_STATE")
    monkeypatch.delenv("TODOX_CONFIG")
    source = tmp_path / "todox" / "sync.json"
    source.parent.mkdir()
    source.write_text(json.dumps(State("real-list", {"real-uid": Entry("real-task", "real-list", "", "", "Main", "/real/TODO.md")}).payload()))
    monkeypatch.setenv("TODOX_DB", str(tmp_path / "demo.sqlite3"))
    assert not State.load().tasks

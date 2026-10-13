"""Race-condition and installed-app integration regressions found during audit."""
import json

import pytest

from todox.db import Database
from todox.projects import Project, load_projects
from todox.store import read
from todox.sync import State, Sync
from todox.ticktick import TickTickError
from todox.ui import TodoApp
from todox.cli import run


def linked(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Original\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    return file, engine


def test_edit_during_upload_remains_pending(tmp_path, fake, monkeypatch):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Original\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    original = fake.create_task

    def concurrent_edit(payload):
        result = original(payload)
        file.write_text(file.read_text().replace("Original", "New edit during upload"))
        return result

    monkeypatch.setattr(fake, "create_task", concurrent_edit)
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    assert fake.tasks[0]["title"] == "Original"
    assert read(file).tasks[0].text == "New edit during upload"
    next_plan = engine.plan(load_projects([Project("web-app", file)]))
    assert next_plan.summary() == {"update": 1}
    engine.apply(next_plan)
    assert fake.tasks[0]["title"] == "New edit during upload"


def test_deleted_task_is_not_resurrected_by_stale_pull(tmp_path, fake):
    file, engine = linked(tmp_path, fake)
    fake.tasks[0]["title"] = "Remote edit"
    plan = engine.plan(load_projects([Project("web-app", file)]))
    file.write_text("# TODO\n")
    engine.apply(plan)
    assert plan.changes[0].error
    assert not read(file).tasks


def test_mixed_case_tag_does_not_duplicate_unlinked_local_task(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Original\n")
    fake.tasks.append({"id": "existing", "projectId": "inbox", "title": "Original", "status": 0, "tags": ["WEB-APP"]})
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    assert engine.plan(load_projects([Project("web-app", file)])).summary() == {"conflict": 1}


def test_remote_change_after_preview_is_not_overwritten(tmp_path, fake):
    file, engine = linked(tmp_path, fake)
    file.write_text(file.read_text().replace("Original", "Local edit"))
    plan = engine.plan(load_projects([Project("web-app", file)]))
    fake.tasks[0]["title"] = "Later remote edit"
    engine.apply(plan)
    assert plan.changes[0].error
    assert fake.tasks[0]["title"] == "Later remote edit"


def test_later_personal_tag_is_not_removed_by_update(tmp_path, fake):
    file, engine = linked(tmp_path, fake)
    file.write_text(file.read_text().replace("Original", "Local edit"))
    plan = engine.plan(load_projects([Project("web-app", file)]))
    fake.tasks[0]["tags"].append("new-personal-tag")
    engine.apply(plan)
    assert plan.changes[0].error
    assert "new-personal-tag" in fake.tasks[0]["tags"]


def test_reopened_remote_task_is_not_deleted_by_stale_retirement(tmp_path, fake):
    file, engine = linked(tmp_path, fake)
    file.write_text(file.read_text().replace("- [ ]", "- [x]"))
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    plan = engine.plan(load_projects([Project("web-app", file)]), allow_deletes=True)
    fake.tasks[0]["status"] = 0
    fake.tasks[0]["completedTime"] = ""
    engine.apply(plan)
    assert plan.changes[0].error and not fake.deleted


def test_server_error_after_upload_is_not_blindly_retried(tmp_path, fake, monkeypatch):
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Original\n")
    engine = Sync(fake, State(), path=tmp_path / "sync.json")

    def server_error(payload):
        raise TickTickError("TickTick POST /open/v1/task failed (500): internal error")

    monkeypatch.setattr(fake, "create_task", server_error)
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    assert engine.database.operations(pending=True)[0]["status"] == "uncertain"
    assert engine.plan(load_projects([Project("web-app", file)])).summary() == {"conflict": 1}


def test_empty_uid_conflict_cannot_import_an_unlinked_duplicate(tmp_path, fake):
    file, engine = linked(tmp_path, fake)
    fake.tasks.append({"id": "duplicate", "projectId": "inbox", "title": "Original", "status": 0, "tags": ["web-app"]})
    with pytest.raises(TickTickError, match="identity"):
        engine.resolve(load_projects([Project("web-app", file)]), "", "remote")
    assert len(read(file).tasks) == 1
    assert "" not in engine.state.tasks


def test_stale_explicit_delete_cannot_overwrite_new_state(tmp_path, fake):
    file, engine = linked(tmp_path, fake)
    stale = Sync(fake, path=engine.path)
    plan = stale.plan(load_projects([Project("web-app", file)]))
    engine.state.save(engine.path)
    with pytest.raises(TickTickError, match="state changed"):
        stale.delete_remote(read(file).tasks[0].uid, plan)
    assert not fake.deleted


def test_read_transaction_keeps_consistent_snapshot(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.save_state(State("before").payload())
    with database.connection() as con:
        assert database.profile(con)["list_id"] == "before"
        database.save_state(State("after").payload())
        assert database.profile(con)["list_id"] == "before"
    assert database.load_state()["listId"] == "after"


@pytest.mark.asyncio
async def test_sqlite_tui_imports_inbox_and_syncs_local_edit(tmp_path, fake, monkeypatch):
    import todox.ui as ui
    from todox.projects import save_config
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n")
    config = tmp_path / "projects.json"
    save_config([Project("web-app", file)], config)
    fake.tasks.append({"id": "inbox-task", "projectId": "inbox", "title": "From Inbox", "status": 0, "tags": ["web-app"]})
    monkeypatch.setenv("TODOX_TICKTICK_TOKEN", "fake")
    monkeypatch.setattr(ui, "TickTick", lambda token: fake)
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("Y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "received" in app.message
        assert [r.task.text for r in app.rows if r.task] == ["From Inbox"]
        assert read(file).tasks[0].uid
        file.write_text(file.read_text().replace("From Inbox", "Edited locally"))
        await pilot.press("r", "Y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.tasks[0]["title"] == "Edited locally"
        assert fake.tasks[0]["projectId"] == "inbox"
        assert "sent" in app.message


@pytest.mark.asyncio
async def test_failed_tui_conflict_resolution_remains_visible(tmp_path, fake, monkeypatch):
    import todox.ui as ui
    file = tmp_path / "TODO.md"
    file.write_text("- [ ] Original\n")
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "web-app", "path": str(file)}]}))
    monkeypatch.setenv("TODOX_TICKTICK_TOKEN", "fake")
    monkeypatch.setattr(ui, "TickTick", lambda token: fake)
    app = TodoApp(local=file, registry=config)
    async with app.run_test() as pilot:
        await pilot.press("Y")
        await app.workers.wait_for_complete()
        file.write_text(file.read_text().replace("Original", "Local edit"))
        fake.tasks[0]["title"] = "Remote edit"
        await pilot.press("Y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.page == "conflicts" and len(app.conflicts) == 1
        fake.fail_paths.add('/open/v1/task/r1')
        await pilot.press("1")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.page == "conflicts" and len(app.conflicts) == 1
        assert "failure" in app.message


def test_dry_run_cannot_apply_conflict_resolution(tmp_path):
    with pytest.raises(ValueError, match="dry-run"):
        run(["sync", "--dry-run", "--keep-local", "some-uid"])
    assert not Database().path.exists()


def test_mutually_exclusive_resolution_flags_are_rejected():
    with pytest.raises(ValueError, match="exactly one"):
        run(["sync", "--keep-local", "uid-one", "--keep-remote", "uid-two"])


def test_updates_send_remote_only_metadata_unchanged(tmp_path, fake):
    file, engine = linked(tmp_path, fake)
    metadata = {"priority": 3, "content": "Keep notes", "reminders": ["TRIGGER:PT0S"],
                "dueDate": "2026-11-01T10:00:00+0000", "items": [{"id": "item1", "title": "Keep subtask"}]}
    fake.tasks[0].update(metadata)
    file.write_text(file.read_text().replace("Original", "Local edit"))
    engine.apply(engine.plan(load_projects([Project("web-app", file)])))
    payload = next(payload for method, path, payload in reversed(fake.calls) if method == "POST" and path == "/open/v1/task/r1")
    assert all(payload[key] == value for key, value in metadata.items())

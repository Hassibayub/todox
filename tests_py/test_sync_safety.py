"""Regression coverage for active-only migration and real-account safety."""
import pytest

from todox.projects import Project, load_projects
from todox.sync import Entry, State, Sync
from todox.ticktick import TickTickError
from todox.ui import TodoApp


def projects(file):
    return load_projects([Project("web-app", file)])


def setup(workspace, fake):
    file, _ = workspace
    engine = Sync(fake, State(), path=file.parent / "sync.json")
    engine.apply(engine.plan(projects(file)))
    return file, engine


def test_preview_does_not_create_list_or_stamp_files(workspace, fake):
    file, _ = workspace
    fake.projects_list = []
    before = file.read_bytes()
    state = file.parent / "sync.json"
    engine = Sync(fake, State(), path=state)
    plan = engine.plan(projects(file))
    assert not plan.list_id and plan.summary() == {"create": 2}
    assert fake.projects_list == [] and fake.tags_list == [{"name": "web-app", "label": "web-app"}]
    assert file.read_bytes() == before and not state.exists()
    engine.apply(plan)
    assert len(fake.projects_list) == 1 and len(fake.tasks) == 2


def test_retirement_keeps_local_history_and_reopen_restores_remote(workspace, fake):
    file, engine = setup(workspace, fake)
    file.write_text(file.read_text().replace("- [ ] Alpha", "- [x] Alpha"))
    engine.apply(engine.plan(projects(file)))
    before = file.read_bytes()
    assert engine.plan(projects(file)).summary() == {"skip": 1}
    engine.apply(engine.plan(projects(file), allow_deletes=True))
    assert file.read_bytes() == before
    assert fake.deleted == ["r1"]
    assert engine.plan(projects(file), allow_deletes=True).summary() == {}
    assert any(e.retired for e in State.load(engine.path).tasks.values())
    file.write_text(file.read_text().replace("- [x] Alpha", "- [ ] Alpha"))
    plan = engine.plan(projects(file))
    assert plan.summary() == {"create": 1}
    engine.apply(plan)
    assert not any(c.error for c in plan.changes)
    assert engine.plan(projects(file)).summary() == {}


def test_missing_file_never_deletes_remote(workspace, fake):
    file, engine = setup(workspace, fake)
    file.unlink()
    plan = engine.plan(projects(file), allow_deletes=True)
    assert plan.summary() == {"skip": 2}
    engine.apply(plan)
    assert not fake.deleted and len(engine.state.tasks) == 2


def test_updates_preserve_personal_tags(workspace, fake):
    file, engine = setup(workspace, fake)
    fake.tasks[0]["tags"].append("personal")
    file.write_text(file.read_text().replace("Alpha", "Alpha edited"))
    engine.apply(engine.plan(projects(file)))
    assert fake.tasks[0]["tags"] == ["web-app", "personal"]


def test_matching_unlinked_task_pauses_instead_of_duplicate_upload(workspace, fake):
    file, _ = workspace
    fake.tasks.append({"id": "existing", "title": "Alpha", "tags": ["web-app"], "status": 0})
    engine = Sync(fake, State(), path=file.parent / "sync.json")
    plan = engine.plan(projects(file))
    assert plan.summary() == {"conflict": 1, "create": 1}
    uid = next(c.uid for c in plan.changes if c.action == "conflict")
    engine.resolve(projects(file), uid, "local")
    assert len(fake.tasks) == 1


def test_simultaneous_matching_edits_update_baseline(workspace, fake):
    file, engine = setup(workspace, fake)
    file.write_text(file.read_text().replace("Alpha", "Same edit"))
    fake.tasks[0]["title"] = "Same edit"
    assert engine.plan(projects(file)).summary() == {"remember": 1}
    engine.apply(engine.plan(projects(file)))
    assert len(engine.state.tasks) == 2
    assert engine.plan(projects(file)).summary() == {}


def test_stale_writer_cannot_apply_old_plan(workspace, fake):
    file, engine = setup(workspace, fake)
    stale = Sync(fake, State.load(engine.path), path=engine.path)
    plan = stale.plan(projects(file))
    engine.state.tasks[next(iter(engine.state.tasks))].section = "changed"
    engine.state.save(engine.path)
    with pytest.raises(TickTickError, match="state changed"):
        stale.apply(plan)


def test_custom_registry_requires_explicit_auto_sync(monkeypatch, workspace):
    file, config = workspace
    monkeypatch.delenv("TODOX_AUTO_SYNC")
    assert not TodoApp(registry=config).auto_sync_enabled
    monkeypatch.setenv("TODOX_AUTO_SYNC", "1")
    assert TodoApp(registry=config).auto_sync_enabled


@pytest.mark.asyncio
async def test_first_launch_only_previews(workspace, fake, monkeypatch):
    file, config = workspace
    monkeypatch.setenv("TODOX_AUTO_SYNC", "1")
    monkeypatch.setenv("TODOX_TICKTICK_TOKEN", "fake")
    monkeypatch.setattr("todox.ui.TickTick", lambda token: fake)
    app = TodoApp(local=file, registry=config)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert "preview required" in app.message
        assert not fake.tasks
        assert "todox:id=" not in file.read_text()


def test_cleanup_only_targets_unregistered_temporary_entries(workspace, fake, tmp_path):
    file, _ = workspace
    temp_file = tmp_path / "tmp-demo" / "TODO.md"
    state = State("list1", {
        "temp-id": Entry("temp-task", "list1", "", "", "Main", str(temp_file)),
        "real-id": Entry("real-task", "list1", "", "", "Main", "/Users/haseeb/real/TODO.md"),
    })
    fake.tasks.extend([{"id": "temp-task", "title": "Demo", "tags": ["many"], "status": 0},
                       {"id": "real-task", "title": "Real", "tags": ["real"], "status": 0}])
    engine = Sync(fake, state, path=tmp_path / "sync.json")
    plan = engine.plan(projects(file), allow_deletes=True, cleanup_temp=True)
    assert [c.remote_id for c in plan.changes if c.action == "delete"] == ["temp-task"]
    engine.apply(plan)
    assert fake.deleted == ["temp-task"]


def test_duplicate_uid_blocks_both_copies_and_deletion(workspace, fake):
    file, engine = setup(workspace, fake)
    alpha = next(line for line in file.read_text().splitlines() if "Alpha" in line)
    file.write_text(file.read_text() + alpha + "\n")
    plan = engine.plan(projects(file), allow_deletes=True)
    assert plan.summary() == {"conflict": 2}
    engine.apply(plan)
    assert len(fake.tasks) == 2 and not fake.deleted


def test_untracked_remote_duplicate_is_not_imported(workspace, fake):
    file, engine = setup(workspace, fake)
    fake.tasks.append({"id": "duplicate", "title": "Alpha", "tags": ["web-app"], "status": 0})
    plan = engine.plan(projects(file))
    assert plan.summary() == {"conflict": 1}
    assert "duplicates a local title" in plan.changes[0].reason


def test_first_manual_sync_backs_up_local_files(workspace, fake):
    import json
    file, _ = workspace
    before = file.read_bytes()
    engine = Sync(fake, State(), path=file.parent / "sync.json")
    engine.apply(engine.plan(projects(file)))
    manifests = list((file.parent / "backups").glob("*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    name = next(name for name, source in manifest.items() if source == str(file))
    assert (manifests[0].parent / name).read_bytes() == before
    assert State.load(engine.path).active_only


def test_successful_upload_survives_later_failure(workspace, fake):
    file, _ = workspace
    call = fake._call

    def fail_second(method, path, payload=None):
        if path == "/open/v1/task" and len(fake.tasks) == 1:
            raise TickTickError("second upload failed", rejected=True)
        return call(method, path, payload)

    fake._call = fail_second
    engine = Sync(fake, State(), path=file.parent / "sync.json")
    plan = engine.plan(projects(file))
    engine.apply(plan)
    assert len(State.load(engine.path).tasks) == 1
    assert [bool(c.error) for c in plan.changes] == [False, True]
    fake._call = call
    retry = engine.plan(projects(file))
    assert retry.summary() == {"create": 1}
    engine.apply(retry)
    assert len(fake.tasks) == 2 and engine.plan(projects(file)).summary() == {}


def test_colliding_project_tags_pause_instead_of_guessing(workspace, fake):
    file, _ = workspace
    other = file.parent / "other.md"
    other.write_text("- [ ] Other\n")
    ps = load_projects([Project("WEB-APP", file), Project("web-app", other)])
    plan = Sync(fake, State(), path=file.parent / "sync.json").plan(ps)
    assert plan.summary() == {"conflict": 2}
    assert not fake.tasks

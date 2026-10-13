from pathlib import Path

import pytest

from todox.projects import Project, load_projects
from todox.store import parse, read
from todox.sync import Change, Plan, Sync, State, new_uid
from todox.ticktick import IN_PROGRESS_TAG


def reload(file: Path):
    return load_projects([Project("web-app", file)])


def test_completed_remote_tasks_do_not_look_deleted(workspace, fake):
    """TickTick hides completed tasks from the project endpoint; sync must still see them."""
    file, _ = workspace
    state_path = file.parent / "sync.json"
    Sync(fake, State(), path=state_path).apply(Sync(fake, State(), path=state_path).plan(reload(file)))
    assert [t["status"] for t in fake.tasks] == [0, 0]
    assert [t["id"] for t in fake.project_tasks("list1")] == ["r1", "r2"]
    plan = Sync(fake, State.load(state_path), path=state_path).plan(reload(file))
    assert plan.summary() == {}
    # completing a task in the file still reaches TickTick, and stays quiet afterwards
    file.write_text(file.read_text().replace("- [ ] Alpha", "- [x] Alpha"))
    engine = Sync(fake, State.load(state_path), path=state_path)
    plan = engine.plan(reload(file))
    assert plan.summary() == {"update": 1}
    engine.apply(plan)
    assert next(t for t in fake.tasks if t["id"] == "r1")["status"] == 2
    assert Sync(fake, State.load(state_path), path=state_path).plan(reload(file)).summary() == {"skip": 1}


def test_push_create_stamps_ids_tags_and_progress(workspace, fake):
    file, _ = workspace
    engine = Sync(fake, State(), path=file.parent / "sync.json")
    plan = engine.plan(reload(file))
    assert plan.summary() == {"create": 2}
    engine.apply(plan)
    text = file.read_text()
    assert text.count("<!-- todox:id=") == 2
    assert [t["title"] for t in fake.tasks] == ["Alpha", "Beta"]
    assert fake.tasks[1]["tags"] == ["web-app", IN_PROGRESS_TAG]
    assert "- [x] Done (completed: 2026-10-10)" in text
    assert len(engine.state.tasks) == 2
    # a second run is a no-op
    assert Sync(fake, State.load(file.parent / "sync.json"), path=file.parent / "sync.json").plan(reload(file)).changes == []


def test_push_update_and_pull_round_trip(workspace, fake):
    file, _ = workspace
    state_path = file.parent / "sync.json"
    Sync(fake, State(), path=state_path).apply(Sync(fake, State(), path=state_path).plan(reload(file)))
    file.write_text(file.read_text().replace("Alpha", "Alpha v2"))
    plan = Sync(fake, State.load(state_path), path=state_path).plan(reload(file))
    assert [c.action for c in plan.changes] == ["update"]
    Sync(fake, State.load(state_path), path=state_path).apply(plan)
    assert fake.tasks[0]["title"] == "Alpha v2"
    fake.tasks[0]["title"] = "Remote rename"
    plan = Sync(fake, State.load(state_path), path=state_path).plan(reload(file))
    assert [c.action for c in plan.changes] == ["pull"]
    Sync(fake, State.load(state_path), path=state_path).apply(plan)
    assert "Remote rename" in file.read_text()


def test_conflict_pauses_and_keeps_honour_the_choice(workspace, fake):
    file, _ = workspace
    state_path = file.parent / "sync.json"
    Sync(fake, State(), path=state_path).apply(Sync(fake, State(), path=state_path).plan(reload(file)))
    uid = next(iter(State.load(state_path).tasks))
    fake.tasks[0]["title"] = "Remote"
    file.write_text(file.read_text().replace("Alpha", "Local"))
    engine = Sync(fake, State.load(state_path), path=state_path)
    plan = engine.plan(reload(file))
    assert plan.summary() == {"conflict": 1}
    Sync(fake, State.load(state_path), path=state_path).apply(plan)  # conflicts never write
    assert fake.tasks[0]["title"] == "Remote" and "Local" in file.read_text()
    engine = Sync(fake, State.load(state_path), path=state_path)
    engine.resolve(reload(file), uid, "local")
    assert fake.tasks[0]["title"] == "Local"
    fake.tasks[0]["title"] = "Remote again"
    engine = Sync(fake, State.load(state_path), path=state_path)
    engine.resolve(reload(file), uid, "remote")
    assert "Remote again" in file.read_text()


def test_imports_tagged_remote_tasks_and_skips_unknown_tags(workspace, fake):
    file, _ = workspace
    state_path = file.parent / "sync.json"
    Sync(fake, State(), path=state_path).apply(Sync(fake, State(), path=state_path).plan(reload(file)))
    fake.tasks.append({"id": "new1", "title": "From the app", "tags": ["Web-App"], "status": 0, "completedTime": ""})
    fake.tasks.append({"id": "new2", "title": "Orphan", "tags": ["nope"], "status": 0, "completedTime": ""})
    engine = Sync(fake, State.load(state_path), path=state_path)
    plan = engine.plan(reload(file))
    assert plan.summary() == {"import": 1, "skip": 1}
    engine.apply(plan)
    assert "From the app" in file.read_text() and "Orphan" not in file.read_text()


def test_import_appends_to_main_when_section_is_gone(workspace, fake):
    file, _ = workspace
    state_path = file.parent / "sync.json"
    Sync(fake, State(), path=state_path).apply(Sync(fake, State(), path=state_path).plan(reload(file)))
    engine = Sync(fake, State.load(state_path), path=state_path)
    change = Change("import", new_uid(), "Added later", "web-app", str(file), "Later", " ", None, "remote-9",
                    {"id": "remote-9", "title": "Added later", "tags": ["web-app"], "status": 0, "completedTime": ""})
    engine._import(Plan("list1", "TODO.md"), change)
    text = file.read_text()
    assert "## Later" not in text
    main = text.split("## Work")[0]
    assert "Added later" in main
    assert engine.state.tasks[change.uid].section == "Main"


def test_local_deletion_only_touches_ticktick_with_permission(workspace, fake, tmp_path):
    file, _ = workspace
    state_path = file.parent / "sync.json"
    Sync(fake, State(), path=state_path).apply(Sync(fake, State(), path=state_path).plan(reload(file)))
    remaining = [line for line in file.read_text().splitlines(keepends=True) if not line.startswith("- [ ] Alpha")]
    file.write_text("".join(remaining))
    engine = Sync(fake, State.load(state_path), path=state_path)
    plan = engine.plan(reload(file))
    assert plan.summary() == {"skip": 1} and not fake.deleted
    engine = Sync(fake, State.load(state_path), path=state_path)
    plan = engine.plan(reload(file), allow_deletes=True)
    assert plan.summary() == {"delete": 1}
    engine.apply(plan)
    assert fake.deleted == ["r1"]


def test_failures_do_not_corrupt_state(workspace, fake):
    file, _ = workspace
    state_path = file.parent / "sync.json"
    fake.fail_paths.add("/open/v1/task")
    engine = Sync(fake, State(), path=state_path)
    plan = engine.plan(reload(file))
    engine.apply(plan)
    assert all(change.error for change in plan.changes if change.action == "create")
    assert State.load(state_path).tasks == {}


def test_state_round_trip(tmp_path: Path):
    state = State("list1", {})
    state.save(tmp_path / "s.json")
    assert State.load(tmp_path / "s.json").list_id == "list1"
    with pytest.raises(ValueError):
        (tmp_path / "bad.json").write_text("{}")
        State.load(tmp_path / "bad.json")


def test_uid_survives_edits_and_status_changes(tmp_path: Path):
    uid = new_uid()
    file = tmp_path / "TODO.md"
    file.write_text(f"# TODO\n- [ ] Task <!-- todox:id={uid} -->\n")
    doc = read(file)
    assert doc.tasks[0].uid == uid
    edited = parse(doc.content, file)
    assert edited.tasks[0].uid == uid

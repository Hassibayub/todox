"""Tag-based sync must work independently of the remote task's list."""
import pytest

from todox.projects import Project, load_projects
from todox.store import read
from todox.sync import State, Sync
from todox.ticktick import TickTickError


def local(file):
    return load_projects([Project("web-app", file)])


def imported(tmp_path, fake, source="inbox"):
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n")
    fake.tasks.append({"id": "external", "projectId": source, "title": "From TickTick",
                       "tags": ["web-app", "personal"], "status": 0})
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    plan = engine.plan(local(file))
    assert plan.summary() == {"import": 1}
    engine.apply(plan)
    assert not any(c.error for c in plan.changes)
    return file, engine


@pytest.mark.parametrize("source", ["inbox", "other-list"])
def test_import_edit_complete_retire_and_reopen_in_original_list(tmp_path, fake, source):
    file, engine = imported(tmp_path, fake, source)
    uid = read(file).tasks[0].uid
    assert engine.state.tasks[uid].list_id == source
    assert engine.plan(local(file)).summary() == {}

    file.write_text(file.read_text().replace("From TickTick", "Local edit"))
    engine.apply(engine.plan(local(file)))
    assert fake.tasks[0]["title"] == "Local edit"
    assert fake.tasks[0]["projectId"] == source
    assert fake.tasks[0]["tags"] == ["web-app", "personal"]

    file.write_text(file.read_text().replace("- [ ]", "- [x]"))
    engine.apply(engine.plan(local(file)))
    assert fake.tasks[0]["status"] == 2
    before = file.read_bytes()
    engine.apply(engine.plan(local(file), allow_deletes=True))
    assert not fake.tasks and fake.deleted == ["external"]
    assert file.read_bytes() == before
    assert engine.plan(local(file)).summary() == {}

    file.write_text(file.read_text().replace("- [x]", "- [ ]"))
    engine.apply(engine.plan(local(file)))
    assert len(fake.tasks) == 1 and fake.tasks[0]["projectId"] == source
    assert engine.state.tasks[uid].list_id == source
    assert engine.plan(local(file)).summary() == {}


def test_remote_edit_and_completion_pull_from_inbox(tmp_path, fake):
    file, engine = imported(tmp_path, fake)
    fake.tasks[0]["title"] = "Remote edit"
    fake.tasks[0]["status"] = 2
    fake.tasks[0]["completedTime"] = "2026-10-13T10:00:00+0000"
    plan = engine.plan(local(file))
    assert plan.summary() == {"pull": 1}
    engine.apply(plan)
    task = read(file).tasks[0]
    assert (task.text, task.status, task.completed) == ("Remote edit", "x", "2026-10-13")
    assert engine.state.tasks[task.uid].list_id == "inbox"


def test_local_deletion_uses_original_list(tmp_path, fake):
    file, engine = imported(tmp_path, fake)
    file.write_text("# TODO\n")
    engine.apply(engine.plan(local(file), allow_deletes=True))
    assert fake.deleted == ["external"]


def test_explicit_delete_uses_original_list(tmp_path, fake):
    file, engine = imported(tmp_path, fake)
    engine.delete_remote(read(file).tasks[0].uid, engine.plan(local(file)))
    assert fake.deleted == ["external"]


def test_remote_move_is_remembered_and_not_undone(tmp_path, fake):
    file, engine = imported(tmp_path, fake)
    fake.tasks[0]["projectId"] = "moved-list"
    plan = engine.plan(local(file))
    assert plan.summary() == {"remember": 1}
    engine.apply(plan)
    assert next(iter(engine.state.tasks.values())).list_id == "moved-list"
    file.write_text(file.read_text().replace("From TickTick", "After move"))
    engine.apply(engine.plan(local(file)))
    assert fake.tasks[0]["projectId"] == "moved-list"


def test_missing_project_tag_pauses_linked_inbox_task(tmp_path, fake):
    file, engine = imported(tmp_path, fake)
    fake.tasks[0]["tags"] = ["personal"]
    fake.tasks[0]["title"] = "Still linked"
    plan = engine.plan(local(file))
    assert plan.summary() == {"skip": 1}
    engine.apply(plan)
    assert read(file).tasks[0].text == "From TickTick"
    assert "project tag was removed" in plan.changes[0].reason


def test_import_does_not_create_default_list(tmp_path, fake):
    fake.projects_list = []
    file, engine = imported(tmp_path, fake)
    assert not fake.projects_list
    assert engine.state.list_id == ""
    assert engine.plan(local(file)).summary() == {}


def test_ambiguous_project_tags_pause_instead_of_importing(tmp_path, fake):
    files = [tmp_path / "one.md", tmp_path / "two.md"]
    for file in files:
        file.write_text("# TODO\n")
    ps = load_projects([Project("one", files[0]), Project("two", files[1])])
    fake.tasks.append({"id": "ambiguous", "projectId": "inbox", "title": "Do not guess",
                       "tags": ["one", "two"], "status": 0})
    engine = Sync(fake, State(), path=tmp_path / "sync.json")
    plan = engine.plan(ps)
    assert plan.summary() == {"conflict": 1}
    assert "multiple local projects" in plan.changes[0].reason
    engine.apply(plan)
    assert all(not read(file).tasks for file in files)
    assert not fake.deleted


def test_linked_task_with_ambiguous_tags_also_pauses(tmp_path, fake):
    file, engine = imported(tmp_path, fake)
    other = tmp_path / "other.md"
    other.write_text("# TODO\n")
    fake.tasks[0]["tags"].append("other")
    ps = load_projects([Project("web-app", file), Project("other", other)])
    assert engine.plan(ps).summary() == {"conflict": 1}


def test_filter_limit_stops_sync_before_any_writes(tmp_path, fake):
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n")
    fake.tasks = [{"id": str(i), "projectId": "inbox", "title": f"Task {i}",
                   "tags": ["web-app"], "status": 0} for i in range(200)]
    path = tmp_path / "sync.json"
    with pytest.raises(TickTickError, match="200-task"):
        Sync(fake, State(), path=path).plan(local(file))
    assert not path.exists() and not read(file).tasks


def test_unrelated_tasks_not_imported(tmp_path, fake):
    file, engine = imported(tmp_path, fake)
    fake.tasks.append({"id": "personal", "projectId": "inbox", "title": "Private task",
                       "tags": ["personal"], "status": 0})
    engine.apply(engine.plan(local(file)))
    assert [t.text for t in read(file).tasks] == ["From TickTick"]
    assert not fake.deleted


def test_completion_endpoint_uses_original_list(tmp_path, fake, monkeypatch):
    file, engine = imported(tmp_path, fake)
    file.write_text(file.read_text().replace("- [ ]", "- [x]"))
    update = fake.update_task

    def needs_complete(task_id, payload):
        result = update(task_id, payload)
        return dict(result, status=0, completedTime="")

    monkeypatch.setattr(fake, "update_task", needs_complete)
    engine.apply(engine.plan(local(file)))
    assert any(path == "/open/v1/project/inbox/task/external/complete" for _, path, _ in fake.calls)

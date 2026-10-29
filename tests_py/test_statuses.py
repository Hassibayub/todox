"""Four-state workflow contracts across Markdown, UI, CLI, and sync."""
import json
from datetime import date

import pytest

from todox.cli import run
from todox.projects import Project, load_projects
from todox.store import STATUS, counts, mutate, parse, read
from todox.sync import State, Sync, remote_status
from todox.ui import TodoApp


def test_review_round_trip_preserves_markdown():
    source = "# Tasks\r\nnotes\r\n  - [?] Review <!-- todox:id=abcdef123456 -->\r\n"
    doc = parse(source)
    task = doc.tasks[0]
    assert (task.status, task.text, task.uid) == ("?", "Review", "abcdef123456")
    assert counts(doc.tasks) == {" ": 0, "/": 0, "?": 1, "x": 0}
    done = parse(mutate(doc, "status", line=2, status="x", today=date(2026, 10, 12)))
    assert done.tasks[0].completed == "2026-10-12"
    assert mutate(done, "status", line=2, status="?") == source
    assert parse(mutate(parse(""), "add", value="Review", status="?")).tasks[0].status == "?"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (60, 18), (40, 12), (32, 7)])
async def test_status_shortcuts_and_cycle_at_small_sizes(workspace, size):
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=size) as pilot:
        for key, status in zip("1234", STATUS):
            await pilot.press(key)
            assert read(file).tasks[0].status == status
        assert read(file).tasks[0].completed
        for status in STATUS:
            await pilot.press("space")
            assert read(file).tasks[0].status == status
        await pilot.press("3", "u")
        assert read(file).tasks[0].completed is None
        assert any(row.task and row.task.status == "?" for row in app.rows)
        await pilot.resize_terminal(40, 12)
        await pilot.press("2")
        assert read(file).tasks[0].status == "/"


def test_review_stats(workspace, monkeypatch, capsys):
    file, config = workspace
    monkeypatch.setenv("TODOX_CONFIG", str(config))
    file.write_text("- [ ] A\n- [/] B\n- [?] C\n- [x] D\n")
    assert run(["stats", "--json"], file.parent) == 0
    result = json.loads(capsys.readouterr().out)
    assert [result[key] for key in ("notStarted", "inProgress", "inReview", "completed")] == [1, 1, 1, 1]


def test_review_sync_round_trip(workspace, fake):
    file, config = workspace
    file.write_text("- [?] Review\n")
    path = file.parent / "sync.json"

    def engine():
        return Sync(fake, State.load(path, registry=config), path=path, registry=config)

    def sync():
        current = engine()
        plan = current.plan(load_projects([Project("web-app", file)]))
        current.apply(plan)
        assert not any(change.error for change in plan.changes)
        return plan

    sync()
    remote = fake.tasks[0]
    assert remote["status"] == 0
    assert remote["tags"] == ["web-app", "inreview"]
    assert sync().changes == []
    remote["tags"].append("personal")
    for status, tag in [("/", "inprogress"), ("?", "inreview"), ("x", None), ("?", "inreview"), (" ", None)]:
        doc = read(file)
        file.write_text(mutate(doc, "status", line=doc.tasks[0].line, status=status))
        sync()
        assert remote_status(remote) == status
        assert set(remote["tags"]) == {"web-app", "personal"} | ({tag} if tag else set())
        assert all(change.action == "skip" for change in sync().changes)
    remote["tags"].append("inreview")
    sync()
    assert read(file).tasks[0].status == "?"
    assert sync().changes == []


def test_remote_review_precedence():
    assert remote_status({"tags": ["inreview", "inprogress"]}) == "?"
    assert remote_status({"status": 2, "tags": ["inreview"]}) == "x"


def test_review_tag_is_reserved(workspace):
    from todox.db import Database

    file, config = workspace
    with pytest.raises(ValueError, match="inreview"):
        Database(registry=config).save_projects([{"name": "inreview", "path": str(file)}])

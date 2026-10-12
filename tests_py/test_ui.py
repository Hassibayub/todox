import json
from pathlib import Path

import pytest

from todox.ui import TodoApp


@pytest.fixture
def workspace(tmp_path: Path):
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n- [ ] Main task\n## Work\n- [/] Active\n- [x] Old (completed: 2026-10-12)\n")
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "sample", "path": str(file)}]}))
    return file, config


@pytest.mark.asyncio
async def test_global_opens_projects_and_navigates(workspace):
    file, config = workspace
    app = TodoApp(registry=config)
    async with app.run_test(size=(70, 18)) as pilot:
        assert app.page == "projects"
        assert app.rows[0].project.name == "sample"
        await pilot.press("t")
        assert app.page == "tasks"
        assert [r.task.text for r in app.rows] == ["Main task", "Active"]
        await pilot.press("p", "enter")
        assert app.page == "detail"
        await pilot.press("p")
        assert app.page == "projects"


@pytest.mark.asyncio
async def test_global_project_selection_survives_return(tmp_path: Path):
    projects = []
    for name in ("first", "second"):
        folder = tmp_path / name
        folder.mkdir()
        file = folder / "TODO.md"
        file.write_text("- [ ] Task\n")
        projects.append({"name": name, "path": str(file)})
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": projects}))
    app = TodoApp(registry=config)
    async with app.run_test(size=(32, 7)) as pilot:
        await pilot.press("down", "enter", "p")
        assert app.page == "projects"
        assert app.rows[app.query_one("#items").index].project.name == "second"


@pytest.mark.asyncio
async def test_local_cannot_navigate_global_and_adds_main_or_section(workspace):
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(55, 14)) as pilot:
        assert [r.kind for r in app.rows] == ["task", "section", "task", "task"]
        await pilot.press("p", "t", "escape")
        assert app.page == "detail"
        await pilot.press("n")
        assert app.prompt_kind == "add"
        await pilot.press(*"New main", "enter")
        assert "- [ ] New main\n## Work" in file.read_text()
        assert app.page == "detail"
        index = next(i for i, r in enumerate(app.rows) if r.kind == "section")
        app.query_one("#items").index = index
        await pilot.press("n")
        await pilot.press(*"In work", "enter")
        assert "- [ ] In work\n" in file.read_text().split("## Work\n")[1]


@pytest.mark.asyncio
async def test_local_status_dates_visibility_and_search(workspace):
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(38, 7)) as pilot:
        await pilot.press("u")
        assert all(not row.task or row.task.status != "x" for row in app.rows)
        await pilot.press("u", "d")
        assert app.show_dates
        app.query_one("#items").index = 0
        await pilot.press("3")
        assert "completed:" in file.read_text()
        await pilot.press("/")
        assert app.prompt_kind == "search"
        await pilot.press(*"Active", "enter")
        assert [r.task.text for r in app.rows if r.task] == ["Active"]
        await pilot.press("escape")
        assert not app.search_query


@pytest.mark.asyncio
async def test_sync_from_the_tui_reports_and_resolves_conflicts(workspace, monkeypatch, tmp_path):
    file, config = workspace
    monkeypatch.setenv("TODOX_SYNC_STATE", str(tmp_path / "sync.json"))
    import todox.ui as ui
    from todox.ticktick import Auth
    from tests_py.conftest import FakeTickTick

    fake = FakeTickTick()
    monkeypatch.setattr(ui, "TickTick", lambda token: fake)
    monkeypatch.setattr(ui.Auth, "load", classmethod(lambda cls, path=None: Auth("token", "list1")))
    app = TodoApp(local=file, registry=config)
    monkeypatch.setattr(TodoApp, "_copy", lambda self, text: True)
    async with app.run_test(size=(60, 14)) as pilot:
        await pilot.press("Y")
        await pilot.pause(0.5)
        assert "create" in app.message and "<!-- todox:id=" in file.read_text()
        fake.tasks[0]["title"] = "Remote rename"
        file.write_text(file.read_text().replace("Main task", "Local rename"))
        await pilot.press("Y")
        await pilot.pause(0.5)
        assert app.page == "conflicts" and len(app.rows) == 1
        assert "1 keep local" in str(app.query_one("#footer").render())
        await pilot.press("2")
        await pilot.pause(0.5)
        assert app.page == "detail" and "Remote rename" in file.read_text()


@pytest.mark.asyncio
async def test_y_copies_the_selected_task_and_section(workspace, monkeypatch):
    file, config = workspace
    copied: list[str] = []
    monkeypatch.setattr(TodoApp, "_copy", lambda self, text: bool(copied.append(text)) or True)
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 14)) as pilot:
        await pilot.press("y")
        assert copied[-1] == "Main task" and app.message == "Task copied"
        index = next(i for i, row in enumerate(app.rows) if row.kind == "section")
        app.query_one("#items").index = index
        await pilot.pause(0.1)
        await pilot.press("y")
        assert copied[-1] == "Work" and app.message == "Section name copied"
        # a task on the global task list copies its own text
        await pilot.press("escape")
        assert copied


@pytest.mark.asyncio
async def test_help_section_delete_and_tiny_layout(workspace):
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(22, 4)) as pilot:
        await pilot.press("?")
        assert app.page == "help"
        await pilot.press("escape", "s")
        assert app.page == "detail" and app.prompt_kind == "section"
        await pilot.press(*"More", "enter")
        assert "## More" in file.read_text()
        assert app.rows[-1].section.title == "More"
        app.query_one("#items").index = 0
        await pilot.press("x")
        assert app.prompt_kind == "delete"
        await pilot.press(*"DELETE", "enter")
        assert "Main task" not in file.read_text()
        assert app.page == "detail"

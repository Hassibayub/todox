import json
from pathlib import Path

import pytest

from rich.cells import cell_len

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
async def test_sync_on_launch_when_configured(workspace, monkeypatch, tmp_path):
    file, config = workspace
    monkeypatch.setenv("TODOX_SYNC_STATE", str(tmp_path / "sync.json"))
    monkeypatch.setenv("TODOX_AUTO_SYNC", "1")
    from todox.sync import State
    State(active_only=True).save(tmp_path / "sync.json", registry=config)
    import todox.ui as ui
    from todox.ticktick import Auth
    from tests_py.conftest import FakeTickTick

    fake = FakeTickTick()
    monkeypatch.setattr(ui, "TickTick", lambda token: fake)
    monkeypatch.setattr(ui.Auth, "load", classmethod(lambda cls, path=None: Auth("token", "list1")))
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 12)) as pilot:
        await pilot.pause(0.6)
        assert "create" in app.message
        assert [t["title"] for t in fake.tasks] == ["Main task", "Active"]
        assert "<!-- todox:id=" in file.read_text()


@pytest.mark.asyncio
async def test_launch_sync_stays_quiet_without_a_token(workspace, monkeypatch):
    file, config = workspace
    monkeypatch.setenv("TODOX_AUTO_SYNC", "1")
    monkeypatch.delenv("TODOX_TICKTICK_TOKEN", raising=False)
    monkeypatch.setenv("TODOX_TICKTICK_CONFIG", str(Path(file).parent / "missing.json"))
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 12)) as pilot:
        await pilot.pause(0.4)
        assert app.message == "" and app.page == "detail"


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


@pytest.mark.asyncio
async def test_escape_dismisses_the_status_line_and_restores_hints(workspace):
    """A local project cannot navigate away, so Esc must still release the footer."""
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 14)) as pilot:
        await pilot.press("d")
        assert app.message == "Dates visible" and "Dates visible" in str(app.query_one("#footer").render())
        await pilot.press("escape")
        assert app.page == "detail" and app.message == ""
        assert "d dates" in str(app.query_one("#footer").render())
        # A later action still reports, and Esc clears it again.
        await pilot.press("u")
        assert app.message == "Completed hidden"
        await pilot.press("escape")
        assert app.message == ""


@pytest.fixture
def long_task(tmp_path: Path):
    """A project whose first task is far longer than a narrow terminal row."""
    text = ("stop fake voicemail verdicts — agent declares voicemail with zero words captured "
            "and the false-positive correction never fires")
    folder = tmp_path / "long"
    folder.mkdir()
    file = folder / "TODO.md"
    file.write_text(f"- [ ] {text}\n## Work\n- [ ] short\n")
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "long", "path": str(file)}]}))
    return file, config, text


def _row_parts(app, index: int = 0):
    """The marker and wrapping body widgets of a rendered task row."""
    line = app.query_one("#items").children[index].children[0]
    assert type(line).__name__ == "Horizontal", "task rows render a marker beside a body"
    marker, body = line.children
    return marker, body


@pytest.mark.asyncio
async def test_rows_use_the_whole_terminal_width(long_task):
    file, config, text = long_task
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(200, 20)) as pilot:
        await pilot.pause(0.1)
        assert app.query_one("#shell").size.width == 198  # no 100-cell cap
        assert app.query_one("#items").size.width == 198
        marker, body = _row_parts(app)
        assert marker.render_line(0).text == "> [ ]  "  # the highlighted first row keeps its marker
        assert body.size.width == 189  # list and item padding plus the marker
        assert body.size.height == 1 and body.render_line(0).text == text


@pytest.mark.asyncio
async def test_wrapped_rows_hang_under_the_task_text(long_task):
    file, config, text = long_task
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.pause(0.1)
        _, body = _row_parts(app)
        lines = [body.render_line(y).text for y in range(body.size.height)]
        assert len(lines) > 1
        # The marker column supplies the indent, so no continuation line starts with a space.
        assert lines[0].startswith(text[:12]) and all(not line.startswith(" ") for line in lines)
        assert " ".join(lines) == text  # word wrap keeps every word, in order
        assert max(cell_len(line) for line in lines) <= body.size.width


@pytest.mark.asyncio
async def test_w_switches_between_wrapping_and_clipping(long_task):
    file, config, text = long_task
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("w")
        _, body = _row_parts(app)
        assert app.wrap is False and app.message == "Clipping long rows"
        assert body.size.height == 1
        clipped = body.render_line(0).text
        assert clipped.endswith("…") and clipped[:-1] in text and text not in clipped
        await pilot.press("w")
        _, body = _row_parts(app)
        assert app.wrap is True and app.message == "Wrapping long rows" and body.size.height > 1


@pytest.mark.asyncio
async def test_wrap_can_start_clipped_from_the_environment(long_task, monkeypatch):
    file, config, _ = long_task
    monkeypatch.setenv("TODOX_WRAP", "0")
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.pause(0.1)
        _, body = _row_parts(app)
        assert app.wrap is False and body.size.height == 1 and body.render_line(0).text.endswith("…")

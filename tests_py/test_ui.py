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
async def test_status_grouping_and_completed_toggle(workspace):
    file, config = workspace
    original = file.read_text()
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(70, 18)) as pilot:
        await pilot.press("g")
        assert [(r.kind, r.status if r.kind == "status" else r.task.text) for r in app.rows] == [
            ("status", " "), ("task", "Main task"), ("status", "/"), ("task", "Active"),
            ("status", "?"), ("status", "x"), ("task", "Old"),
        ]
        await pilot.press("u")
        assert not any(r.status == "x" or (r.task and r.task.status == "x") for r in app.rows)
        await pilot.press("u")
        assert any(r.task and r.task.text == "Old" for r in app.rows)
        await pilot.press("g")
        assert [r.kind for r in app.rows] == ["task", "section", "task", "task"]
        assert file.read_text() == original


@pytest.mark.asyncio
async def test_global_opens_projects_and_navigates(workspace):
    file, config = workspace
    app = TodoApp(registry=config)
    async with app.run_test(size=(70, 18)) as pilot:
        assert app.page == "projects"
        assert app.rows[0].project.name == "sample"
        await pilot.press("t")
        assert app.page == "tasks"
        assert [r.kind for r in app.rows] == ["group", "task", "task"]
        assert [r.task.text for r in app.rows if r.task] == ["Main task", "Active"]
        await pilot.press("p", "enter")
        assert app.page == "detail"
        await pilot.press("p")
        assert app.page == "projects"


@pytest.mark.asyncio
async def test_global_task_list_adds_edits_and_updates_status(tmp_path: Path):
    files = {}
    for name, body in (("first", "- [ ] One\n"), ("second", "- [ ] Two\n## Later\n- [/] Three\n")):
        folder = tmp_path / name
        folder.mkdir()
        files[name] = folder / "TODO.md"
        files[name].write_text(body)
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [
        {"name": name, "path": str(file)} for name, file in files.items()]}))
    app = TodoApp(registry=config)
    async with app.run_test(size=(90, 18)) as pilot:
        await pilot.press("t")
        # Tasks sit under one heading per project instead of repeating its name.
        assert [(r.kind, r.task.text if r.task else r.project.name) for r in app.rows] == [
            ("group", "first"), ("task", "One"), ("group", "second"), ("task", "Two"), ("task", "Three")]
        # Edit in place: the row itself becomes the editor, starting at the end of the text.
        app.query_one("#items").index = 3
        await pilot.pause()
        await pilot.press("e")
        assert app.prompt_kind == "edit"
        assert "editing in second" in str(app.query_one("#footer").render())
        await pilot.press(*" again", "enter")
        assert files["second"].read_text() == "- [ ] Two again\n## Later\n- [/] Three\n"
        assert app.page == "tasks"
        # Adding on a task lands in that task's project and section, typed in place, and selects it.
        await pilot.press("down", "n")
        assert app.prompt_row.section.title == "Later"
        draft = next(i for i, r in enumerate(app.rows) if r.kind == "draft")
        assert app.rows[draft - 1].task.text == "Three" and app.query_one("#items").index == draft
        assert "adding to second › Later" in str(app.query_one("#footer").render())
        await pilot.press(*"Four", "enter")
        assert files["second"].read_text().endswith("- [/] Three\n- [ ] Four\n")
        assert app.rows[app.query_one("#items").index].task.text == "Four"
        assert files["first"].read_text() == "- [ ] One\n"
        # Status keys work; completed tasks leave the open list.
        await pilot.press("2")
        assert "- [/] Four" in files["second"].read_text()
        await pilot.press("4")
        assert "- [x] Four (completed:" in files["second"].read_text()
        assert "Four" not in [r.task.text for r in app.rows if r.task]
        assert app.message == "Completed · Four"
        # From Projects, n adds to the selected project's Main section.
        await pilot.press("p")
        app.query_one("#items").index = 0
        await pilot.press("n")
        assert [r.kind for r in app.rows[:2]] == ["project", "draft"]
        assert "adding to first" in str(app.query_one("#footer").render())
        await pilot.press(*"Zero", "enter")
        assert files["first"].read_text() == "- [ ] One\n- [ ] Zero\n"
        assert app.page == "projects" and app.rows[app.query_one("#items").index].project.name == "first"
        # Esc abandons a draft without touching the file.
        await pilot.press("n", *"Never", "escape")
        assert app.draft is None and "Never" not in files["first"].read_text()


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
        # The newly created task is selected; adding again should stay in Work.
        await pilot.press("n")
        assert app.prompt_row.section.title == "Work"
        await pilot.press(*"Another work task", "enter")
        assert "- [ ] Another work task\n" in file.read_text().split("## Work\n")[1]
        await pilot.press("g", "n")
        await pilot.press(*"Grouped work task", "enter")
        assert "- [ ] Grouped work task\n" in file.read_text().split("## Work\n")[1]


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
        await pilot.press("4")
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
        assert "sent" in app.message
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
        assert "sent" in app.message and "<!-- todox:id=" in file.read_text()
        fake.tasks[0]["title"] = "Remote rename"
        file.write_text(file.read_text().replace("Main task", "Local rename"))
        await pilot.press("Y")
        await pilot.pause(0.5)
        assert app.page == "conflicts" and len(app.rows) == 1
        assert "1 keep TODO.md" in str(app.query_one("#footer").render())
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
        assert "y confirm" in app.message
        await pilot.press("y")
        await pilot.pause()
        assert "Main task" not in file.read_text()
        assert app.page == "detail"


@pytest.mark.asyncio
async def test_x_then_y_deletes_and_anything_else_cancels(workspace):
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 14)) as pilot:
        app.query_one("#items").index = 0
        # Any key other than y cancels the delete and keeps its own meaning.
        await pilot.press("x")
        assert app.pending_delete is not None and "y confirm" in app.message
        await pilot.press("j")
        assert app.pending_delete is None and "Main task" in file.read_text()
        # Esc and n cancel without deleting.
        app.query_one("#items").index = 0
        await pilot.pause()
        await pilot.press("x", "escape")
        assert app.pending_delete is None and "Main task" in file.read_text()
        assert app.message == "Deletion cancelled"
        app.query_one("#items").index = 0
        await pilot.pause()
        await pilot.press("x", "n")
        assert app.pending_delete is None and "Main task" in file.read_text()
        # Moving the highlight cancels a pending delete too.
        app.query_one("#items").index = 0
        await pilot.pause()
        await pilot.press("x", "down")
        assert app.pending_delete is None and "Main task" in file.read_text()
        # y confirms.
        app.query_one("#items").index = 0
        await pilot.pause()
        await pilot.press("x", "y")
        await pilot.pause()
        assert app.pending_delete is None
        assert "Main task" not in file.read_text()
        assert "Deleted" in app.message


@pytest.mark.asyncio
async def test_backspace_then_y_removes_an_empty_section(workspace):
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 14)) as pilot:
        await pilot.press("s")
        await pilot.press(*"Empty", "enter")
        assert "## Empty" in file.read_text()
        index = next(i for i, row in enumerate(app.rows) if row.section and row.section.title == "Empty")
        app.query_one("#items").index = index
        await pilot.pause()
        await pilot.press("backspace")
        assert app.pending_delete is not None and "Remove section" in app.message
        await pilot.press("Y")
        await pilot.pause()
        assert "## Empty" not in file.read_text()
        assert "Removed section" in app.message


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
        assert "e edit" in str(app.query_one("#footer").render())
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


@pytest.mark.asyncio
async def test_moving_the_selection_repaints_only_the_changed_rows(workspace):
    """Repainting every row on each keypress backs up on large files."""
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 14)) as pilot:
        calls: list[set[int]] = []
        original = app._paint_rows
        app._paint_rows = lambda indices: (calls.append(set(indices)), original(indices))[1]
        await pilot.press("j")
        assert calls and calls[-1] == {0, 1}, "only the old and new selection need a new marker"


@pytest.mark.asyncio
async def test_refresh_and_dates_reuse_rows_instead_of_rebuilding(workspace):
    """A full ListItem rebuild blocks the event loop; unchanged rows must be kept."""
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 14)) as pilot:
        items = lambda: [id(item) for item in app.query_one("#items").children]
        before = items()
        await pilot.press("d")
        assert items() == before and app.show_dates
        await pilot.press("r")
        assert items() == before
        # A change to the set of rows still rebuilds them.
        await pilot.press("n")
        await pilot.press(*"Fresh", "enter")
        assert items() != before and any(r.task and r.task.text == "Fresh" for r in app.rows)


@pytest.mark.asyncio
async def test_arrows_move_exactly_one_row_even_without_list_focus(workspace):
    """Up/Down must take the same always-live path as j/k.

    A focused ``ListView`` moves its own cursor when it holds focus, and the app
    moves it from ``on_key`` otherwise; binding both would step twice, and
    relying only on the list would strand the arrows when focus is elsewhere.
    """
    file, config = workspace
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(60, 14)) as pilot:
        listing = app.query_one("#items")
        assert listing.index == 0
        await pilot.press("down")
        assert listing.index == 1, "a focused list must not step twice"
        await pilot.press("up")
        assert listing.index == 0
        # Some terminals/actions leave nothing focused; arrows must still work.
        app.screen.set_focus(None)
        await pilot.pause()
        await pilot.press("down", "down")
        assert listing.index == 2
        await pilot.press("up")
        assert listing.index == 1
        # j/k share the path.
        await pilot.press("j")
        assert listing.index == 2
        await pilot.press("k")
        assert listing.index == 1


@pytest.mark.asyncio
async def test_plain_q_does_not_quit(workspace):
    _, config = workspace
    app = TodoApp(registry=config)
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running


@pytest.fixture
def portfolio(tmp_path: Path):
    """Two projects with every status, for layout and split-view tests."""
    entries = []
    for name, body in (("web-app", "## Frontend\n- [ ] Login\n- [/] Layout\n- [x] Shortcuts (completed: 2026-10-30)\n"),
                       ("api-service", "- [ ] Rotate keys\n## Backend\n- [?] Database\n")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "TODO.md").write_text(body)
        entries.append({"name": name, "path": str(folder / "TODO.md")})
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": entries}))
    return config


def _screen(app) -> str:
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


@pytest.mark.asyncio
@pytest.mark.parametrize("width, expected", [
    (34, "1/3"),                 # tiny: done / total
    (56, "[ ] 1 · [/] 1"),       # narrow: counts under the name
    (80, "todo  doing  review"),  # one line per project, with column labels
    (120, "33%"),                # wide enough for the progress meter
])
async def test_project_rows_adapt_to_the_terminal_width(portfolio, width, expected):
    app = TodoApp(registry=portfolio)
    async with app.run_test(size=(width, 20)) as pilot:
        await pilot.pause(0.1)
        assert expected in _screen(app)
        two_line = "narrow" in app._row_widgets[0].item.classes
        assert two_line == (width == 56)


@pytest.mark.asyncio
async def test_split_view_on_wide_terminals(portfolio):
    app = TodoApp(registry=portfolio)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause(0.1)
        side = app.query_one("#side")
        # Projects become a pane beside the selected project's tasks.
        assert side.display and app.side_focus and app.page == "detail"
        assert app._project().name == "web-app"
        await pilot.press("j")
        assert app._project().name == "api-service" and [r.task.text for r in app.rows if r.task] == ["Rotate keys", "Database"]
        # Task keys wait while the pane holds the keyboard; tab hands it to the list.
        await pilot.press("space")
        assert "- [ ] Rotate keys" in (Path(app._project().path)).read_text()
        await pilot.press("tab", "space")
        assert not app.side_focus and "- [/] Rotate keys" in Path(app._project().path).read_text()
        # n from the pane adds to that project's Main, typed in place.
        await pilot.press("escape")
        assert app.side_focus
        await pilot.press("n", *"Pane task", "enter")
        assert "- [ ] Pane task\n## Backend" in Path(app._project().path).read_text()
        # "All open tasks" heads the pane.
        await pilot.press("t")
        assert app.page == "tasks" and app.side_index == 0 and not app.side_focus
        # Narrowing the terminal folds the pane back into separate screens.
        await pilot.press("escape")
        await pilot.resize_terminal(90, 24)
        await pilot.pause(0.1)
        assert not side.display and app.page == "projects" and not app.side_focus


@pytest.mark.asyncio
async def test_styles_are_named_ansi_colors_only(portfolio):
    """The terminal's theme must decide every color: no hex, 256-color or truecolor."""
    allowed = {"", "bold", "dim", "reverse", "red", "green", "yellow", "cyan", "bold yellow", "bold cyan"}
    app = TodoApp(registry=portfolio)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause(0.1)
        assert app.current_theme.ansi and app.native_ansi_color
        texts = [app._header(156), app._footer(156)]
        for page in ("tasks", "detail"):
            await pilot.press("t" if page == "tasks" else "p")
            await pilot.pause(0.05)
            width = app._content_width()
            for index, row in enumerate(app.rows):
                rendered = app._render_row(row, width, index, index == app.selected_index)
                texts += [rendered.body] + ([rendered.marker] if rendered.marker else [])
            texts += [app._render_side_row(row, True) for row in app.side_rows]
        styles = {str(span.style) for text in texts for span in text.spans} | {str(text.style) for text in texts}
        assert styles <= allowed, styles - allowed


@pytest.mark.asyncio
async def test_footer_keeps_help_and_shows_messages_beside_hints(portfolio):
    app = TodoApp(registry=portfolio)
    async with app.run_test(size=(44, 20)) as pilot:
        await pilot.press("enter")
        footer = lambda: str(app.query_one("#footer").render())
        assert footer().rstrip().endswith("? keys") and "n new" in footer()
        await pilot.press("d")
        assert "Dates visible" in footer() and "? keys" in footer()
        # A message lasts until the next key.
        await pilot.press("j")
        assert app.message == "" and "Dates visible" not in footer()
        # The header shows view state that would otherwise be invisible.
        await pilot.press("u")
        assert "done hidden · dates" in str(app.query_one("#title").render())


@pytest.mark.asyncio
async def test_drafts_support_line_editing_keys(portfolio):
    app = TodoApp(registry=portfolio)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.press("enter", "n", *"Shpi", "backspace", "backspace", *"ip", "home", "delete", *"S", "end", *" it")
        assert app.draft.text == "Ship it"
        await pilot.press("ctrl+w")
        assert app.draft.text == "Ship "
        await pilot.press("ctrl+u", *"Ship it", "enter")
        assert "- [ ] Ship it\n" in Path(app._project().path).read_text()
        assert app.message == "Saved to web-app/TODO.md"


@pytest.mark.asyncio
async def test_tiny_panes_clip_rows_and_count_what_is_below(long_task):
    file, config, text = long_task
    app = TodoApp(local=file, registry=config)
    async with app.run_test(size=(34, 5)) as pilot:
        await pilot.pause(0.2)
        assert not app.query_one("#title").display
        _, body = _row_parts(app)
        assert body.size.height == 1 and body.render_line(0).text.endswith("…")

"""Responsive, keyboard-first Textual interface.

The look is deliberately terminal-native: the ANSI theme leaves the terminal
background and foreground untouched, secondary text is dim, and the selected
row is marked with ``>`` instead of a filled bar.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from asyncio import to_thread
import os
import subprocess
import sys

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.widgets import Input, ListItem, ListView, Static

from .projects import Project, load_config, load_projects
from .store import Document, Section, Task, counts, main_section, save
from .sync import Change, Sync
from .ticktick import Auth, TickTick, TickTickError


@dataclass(frozen=True)
class Row:
    kind: str
    project: Project | None = None
    task: Task | None = None
    section: Section | None = None
    change: Change | None = None


@dataclass(frozen=True)
class RowRender:
    """A row's body, plus the optional fixed marker that indents it when it wraps."""
    body: Text
    classes: str
    marker: Text | None = None


@dataclass
class RowWidgets:
    """Live widgets behind a row, so selection repaints without rebuilding the list."""
    item: ListItem
    body: Static
    marker: Static | None = None


class TodoApp(App[None]):
    CSS_PATH = "ui.tcss"
    TITLE = "todo"
    BINDINGS = [
        Binding("n", "new_task", "New task", show=False),
        Binding("e", "edit_task", "Edit", show=False),
        Binding("u", "toggle_completed", "Completed", show=False),
        Binding("d", "toggle_dates", "Dates", show=False),
        Binding("w", "toggle_wrap", "Wrap", show=False),
        Binding("p", "projects", "Projects", show=False),
        Binding("t", "tasks", "Tasks", show=False),
        Binding("c", "copy_path", "Copy path", show=False),
        Binding("s", "new_section", "Section", show=False),
        Binding("space", "cycle_status", "Status", show=False),
        Binding("1", "todo_status", "Todo", show=False),
        Binding("2", "progress_status", "In progress", show=False),
        Binding("3", "done_status", "Completed", show=False),
        Binding("x", "delete_task", "Delete", show=False),
        Binding("backspace", "remove_section", "Remove section", show=False),
        Binding("o", "open_editor", "Neovim", show=False),
        Binding("slash", "search", "Search", show=False),
        Binding("y", "copy_task", "Copy task", show=False),
        Binding("Y", "sync", "Sync", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, local: Path | None = None, registry: Path | None = None):
        super().__init__()
        # Any Textual theme name works; the default keeps the terminal's own colors.
        self.theme = os.environ.get("TODOX_THEME", "ansi-dark")
        self.local = local.resolve() if local else None
        self.registry = registry
        self.projects_data: list[Project] = []
        self.page = "detail" if self.local else "projects"
        self.selected_project: Path | None = self.local
        self.last_project: Path | None = None
        self.rows: list[Row] = []
        self._row_widgets: list[RowWidgets] = []
        self.selected_index = 0
        self.show_completed = True
        self.show_dates = False
        # TODOX_WRAP=0 opens with long rows clipped to one line instead of wrapped.
        self.wrap = os.environ.get("TODOX_WRAP", "1").strip().lower() not in {"0", "false", "off", "no"}
        self.search_query = ""
        self.prompt_kind: str | None = None
        self.prompt_row: Row | None = None
        self.conflicts: list[Change] = []
        self.sync_running = False
        self._engine: Sync | None = None
        self.message = ""
        self._return_page = self.page
        self._reload_data()

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Static(id="title")
            yield ListView(id="items")
            yield Static(id="empty")
            yield Input(id="prompt")
            yield Static(id="footer")

    async def on_mount(self) -> None:
        self.query_one("#prompt", Input).display = False
        await self.draw()
        self.query_one("#items", ListView).focus()
        if self.auto_sync_enabled:
            self.run_worker(self.auto_sync(), exclusive=True)

    def _reload_data(self) -> None:
        try:
            self.projects_data = load_projects(load_config(self.registry))
            if self.local and not any(p.path == self.local for p in self.projects_data):
                self.projects_data.append(load_projects([Project(self.local.parent.name, self.local)])[0])
            self.message = ""
        except (OSError, ValueError) as exc:
            self.projects_data = load_projects([Project(self.local.parent.name, self.local)]) if self.local else []
            self.message = f"Cannot read projects: {exc}"

    def _project(self) -> Project | None:
        return next((p for p in self.projects_data if p.path == self.selected_project), None)

    def _selected(self) -> Row | None:
        index = self.query_one("#items", ListView).index
        return self.rows[index] if index is not None and 0 <= index < len(self.rows) else None

    @property
    def auto_sync_enabled(self) -> bool:
        # A custom registry is commonly a test/demo workspace, not permission
        # to use the user's saved credentials and shared sync state.
        if (self.registry is not None or os.environ.get("TODOX_CONFIG")) and "TODOX_AUTO_SYNC" not in os.environ:
            return False
        return os.environ.get("TODOX_AUTO_SYNC", "1").strip().lower() not in {"0", "false", "off", "no"}

    async def auto_sync(self) -> None:
        """Quietly catch up with TickTick once at startup; silence if unconfigured."""
        try:
            client = TickTick(Auth.load().require_token())
        except (OSError, ValueError, TickTickError):
            return
        await self._sync(client, announce=False)

    def _task_rows(self, project: Project) -> list[Row]:
        if not project.doc:
            return []
        doc = project.doc
        matching = [t for t in doc.tasks if (self.show_completed or t.status != "x") and self.search_query.casefold() in t.text.casefold()]
        result: list[Row] = []
        for section in doc.sections:
            section_tasks = [t for t in matching if t.section == section.line]
            # A search shows only matching groups; otherwise empty sections remain actionable.
            if not section.main and (not self.search_query or section_tasks):
                result.append(Row("section", project, section=section))
            result.extend(Row("task", project, task=task) for task in section_tasks)
        return result

    def _rows(self) -> list[Row]:
        if self.page == "projects":
            return [Row("project", p) for p in self.projects_data if self.search_query.casefold() in p.name.casefold()]
        if self.page == "tasks":
            return [Row("task", p, task=t) for p in self.projects_data if p.doc for t in p.doc.tasks
                    if t.status != "x" and self.search_query.casefold() in (p.name + " " + t.text).casefold()]
        if self.page == "detail":
            project = self._project()
            return self._task_rows(project) if project else []
        if self.page == "conflicts":
            return [Row("conflict", change=change) for change in self.conflicts]
        return [Row("help") for _ in self._help_lines()]

    def _help_lines(self) -> list[str]:
        if self.size.width < 42:
            common = ["j/k, ↑↓  Move", "Enter  Open / edit", "n  New task", "e  Edit task",
                      "Space, 1–3  Status", "u  Toggle completed", "d  Toggle dates", "w  Wrap text",
                      "s  New section", "x  Delete task", "Backspace  Remove section",
                      "/  Search", "o  Neovim", "y  Copy task", "Y  Sync TickTick", "r  Refresh", "Esc  Back · clear status", "q  Quit"]
            if not self.local:
                common[2:2] = ["p/t  Projects / tasks", "c  Copy path"]
            return common
        common = ["j/k, ↑↓       Move", "Enter         Open project / edit task",
                  "n             New task (selected section)", "e             Edit task",
                  "Space, 1–3    Change status", "u             Show / hide completed",
                  "d             Show / hide dates", "w             Wrap / clip long rows", "s             Add ## section",
                  "x             Delete task (confirm)", "Backspace     Remove empty section",
                  "o             Open task in Neovim", "/             Search; Esc clears",
                  "y             Copy selected task", "Y             Sync with TickTick",
                  "r             Refresh TODO.md", "? / Esc       Close help · clear status", "q             Quit"]
        if not self.local:
            common[2:2] = ["p / t         Projects / open tasks", "c             Copy project path"]
        return common

    def _render_row(self, row: Row, width: int, index: int = 0, selected: bool = False) -> RowRender:
        marker = "> " if selected else "  "
        if row.kind == "help":
            return RowRender(Text(marker + self._help_lines()[index], style="" if selected else "dim"), "help-row")
        if row.kind == "conflict":
            change = row.change
            assert change
            label = change.name or change.project or change.uid
            body = Text.assemble((marker, "bold" if selected else ""), (label + " · ", "dim"),
                                 (change.text or change.uid, "bold"), (f"\n  {change.reason}", "dim"))
            return RowRender(body, "conflict-row")
        if row.kind == "project":
            p = row.project
            assert p
            if p.error:
                unavailable = Text.assemble((marker, "bold" if selected else ""), (p.name, "bold"), ("  unavailable", "red"))
                return RowRender(unavailable, "project-row")
            tally = counts(list(p.doc.tasks)) if p.doc else {" ": 0, "/": 0, "x": 0}
            summary = f'{tally["x"]} completed  ·  {tally["/"]} in progress  ·  {tally[" "]} todo'
            if width < 78:
                compact = f'{tally["x"]} done · {tally["/"]} doing · {tally[" "]} todo'
                narrow = Text.assemble((marker, "bold" if selected else ""), (p.name + "\n", "bold"), ("  " + compact, "dim"))
                return RowRender(narrow, "project-row narrow")
            label = Text(marker, style="bold" if selected else "")
            label.append(p.name, style="bold")
            # Marker, shell padding and list padding all cost cells before the summary.
            gap = max(2, width - cell_len(p.name) - cell_len(summary) - 5 - 10)
            label.append(" " * gap)
            label.append(summary, style="dim")
            return RowRender(label, "project-row")
        if row.kind == "section":
            section = row.section
            assert section
            return RowRender(Text(section.title, style="bold"), "section-row", Text(marker, style="bold" if selected else ""))
        assert row.task and row.project
        t = row.task
        box = {" ": "[ ]", "/": "[/]", "x": "[x]"}[t.status]
        prefix = Text(marker, style="bold" if selected else "")
        prefix.append(box + "  ", style="dim" if t.status == "x" else "bold")
        body = Text()
        if self.page == "tasks":
            body.append(f"{row.project.name} / ", style="dim")
        body.append(t.text, style="dim" if t.status == "x" else "")
        if self.show_dates and t.completed:
            body.append(f" · {t.completed}", style="dim")
        return RowRender(body, "task-row" + (" done" if t.status == "x" else ""), prefix)

    def _paint_selection(self) -> None:
        """Move the ``>`` marker without rebuilding the list on every keypress."""
        width = self.size.width
        for index, (row, widgets) in enumerate(zip(self.rows, self._row_widgets)):
            rendered = self._render_row(row, width, index, index == self.selected_index)
            widgets.body.update(rendered.body)
            if widgets.marker is not None:
                widgets.marker.update(rendered.marker or Text())

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "items":
            return
        self.selected_index = event.list_view.index or 0
        self._paint_selection()

    async def draw(self, preserve: bool = False) -> None:
        if not self.is_mounted:
            return
        listing = self.query_one("#items", ListView)
        old = self._selected() if preserve else None
        old_index = listing.index or 0
        self.rows = self._rows()
        self._row_widgets = []
        items: list[ListItem] = []
        width = self.size.width
        for index, row in enumerate(self.rows):
            rendered = self._render_row(row, width, index)
            # ``unwrap`` clips long rows to one line; wrapping is the default.
            classes = rendered.classes if self.wrap else rendered.classes + " unwrap"
            if rendered.marker is None:
                body = Static(rendered.body, markup=False)
                item = ListItem(body, classes=classes)
                self._row_widgets.append(RowWidgets(item, body))
            else:
                marker = Static(rendered.marker, markup=False, classes="row-marker")
                body = Static(rendered.body, markup=False, classes="row-body")
                item = ListItem(Horizontal(marker, body, classes="row-line"), classes=classes)
                self._row_widgets.append(RowWidgets(item, body, marker))
            items.append(item)
        await listing.clear()
        if items:
            await listing.extend(items)
            index = self.rows.index(old) if old in self.rows else min(old_index, len(self.rows) - 1) if preserve else 0
            listing.index = index
            self.selected_index = index
            self._paint_selection()
        self._update_chrome()

    def _update_chrome(self) -> None:
        project = self._project()
        title = {"projects": "Projects", "tasks": "Open tasks", "help": "Keys", "conflicts": "Sync conflicts"}.get(self.page, project.name if project else "Project")
        self.query_one("#title", Static).update(Text(title, style="bold"))
        empty = self.query_one("#empty", Static)
        if self.page == "projects":
            message = "No projects yet · run todo add-project /path" if not self.projects_data else "No matching projects"
        elif self.page == "tasks":
            message = "No open tasks" if not self.search_query else "No matching tasks"
        elif self.page == "detail":
            message = project.error if project and project.error else "No tasks yet · n to add one"
        elif self.page == "conflicts":
            message = "Both TODO.md and TickTick changed these tasks · pick a side"
        else:
            message = ""
        empty.update(message)
        empty.display = not self.rows
        footer = self.query_one("#footer", Static)
        if self.page == "conflicts":
            hint = "1 keep local   2 keep remote   Esc back"
        elif self.message:
            hint = self.message
        elif self.page == "projects":
            hint = "c copy path   Enter open   p projects   t tasks   ? keys"
        elif self.page == "tasks":
            hint = "Enter open project   p projects   w wrap   ? keys"
        elif self.page == "detail":
            hint = "n new   e edit   y copy   u completed   d dates   w wrap   ? keys" + ("   p projects" if not self.local else "")
        elif self.page == "conflicts":
            hint = "1 keep local   2 keep remote   Esc back"
        else:
            hint = "Esc close   q quit"
        if self.size.width < 43 and not self.message:
            hint = "? keys   q quit"
        footer.update(hint)
        footer.display = self.size.height >= 5 and (not self.prompt_kind or bool(self.message))
        self.query_one("#title", Static).display = self.size.height >= 3

    async def on_resize(self, event: Resize) -> None:
        if self.is_mounted:
            await self.draw(preserve=True)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self.prompt_kind:
            return
        row = self._selected()
        if not row:
            return
        if self.page == "projects" and row.project:
            if row.project.error:
                self.message = row.project.error
                self._update_chrome()
            else:
                self.selected_project = row.project.path
                self.last_project = row.project.path
                self.page = "detail"
                self.search_query = ""
                await self.draw()
        elif self.page == "tasks" and row.project:
            self.selected_project = row.project.path
            self.last_project = row.project.path
            self.page = "detail"
            self.search_query = ""
            await self.draw()
            for index, candidate in enumerate(self.rows):
                if candidate.task and row.task and candidate.task.line == row.task.line:
                    self.query_one("#items", ListView).index = index
                    break
        elif row.kind == "task":
            self.action_edit_task()
        elif row.kind == "section":
            self.action_new_task()

    async def _navigate(self, page: str) -> None:
        if self.local or self.prompt_kind:
            if self.local:
                self.message = "Local TODO.md only · use todo --global for Projects"
                self._update_chrome()
            return
        self.page = page
        self.selected_project = None
        self.search_query = ""
        self.message = ""
        await self.draw()
        if page == "projects":
            for i, row in enumerate(self.rows):
                if row.project and row.project.path == self.last_project:
                    self.query_one("#items", ListView).index = i
                    break
        self.query_one("#items", ListView).focus()

    async def action_projects(self) -> None:
        await self._navigate("projects")

    async def action_tasks(self) -> None:
        await self._navigate("tasks")

    async def action_back(self) -> None:
        if self.prompt_kind:
            self._close_prompt()
        elif self.search_query:
            self.search_query = ""
            self.message = ""
            await self.draw()
        elif self.page == "help":
            self.page = self._return_page
            await self.draw()
        elif self.page == "conflicts":
            self.page = self._return_page
            await self.draw()
        elif not self.local and self.page != "projects":
            await self._navigate("projects")
        elif self.message:
            # Dismiss the status line so the key hints come back; nothing else is pending.
            self.message = ""
            self._update_chrome()

    async def action_help(self) -> None:
        if self.prompt_kind:
            return
        if self.page == "help":
            await self.action_back()
        else:
            self._return_page = self.page
            self.page = "help"
            self.message = ""
            await self.draw()

    def _copy(self, text: str) -> bool:
        try:
            if sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=2)
            else:
                self.copy_to_clipboard(text)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def action_copy_task(self) -> None:
        """Copy the selected task's text, a section name, or a project path."""
        if self.prompt_kind or self.page == "conflicts":
            return
        row = self._selected()
        if not row:
            return
        if row.kind == "task" and row.task:
            text, label = row.task.text, "Task copied"
        elif row.kind == "section" and row.section:
            text, label = row.section.title, "Section name copied"
        elif row.project:
            text, label = str(row.project.path.parent), "Project path copied"
        else:
            return
        self.message = label if self._copy(text) else f"Clipboard unavailable · {text}"
        self._update_chrome()

    def action_copy_path(self) -> None:
        row = self._selected()
        project = row.project if row and row.kind == "project" else self._project()
        if not project:
            return
        path = str(project.path.parent)
        self.message = "Project path copied" if self._copy(path) else "Clipboard unavailable · project path: " + path
        self._update_chrome()

    def action_toggle_completed(self) -> None:
        if self.page != "detail" or self.prompt_kind:
            return
        self.show_completed = not self.show_completed
        self.message = "Showing completed" if self.show_completed else "Completed hidden"
        self.run_worker(self.draw(preserve=True), exclusive=True)

    def action_toggle_dates(self) -> None:
        if self.page != "detail" or self.prompt_kind:
            return
        self.show_dates = not self.show_dates
        self.message = "Dates visible" if self.show_dates else "Dates hidden"
        self.run_worker(self.draw(preserve=True), exclusive=True)

    def action_toggle_wrap(self) -> None:
        if self.page not in {"detail", "tasks"} or self.prompt_kind:
            return
        self.wrap = not self.wrap
        self.message = "Wrapping long rows" if self.wrap else "Clipping long rows"
        self.run_worker(self.draw(preserve=True), exclusive=True)

    def _open_prompt(self, kind: str, row: Row | None = None, value: str = "") -> None:
        self.message = ""
        self.prompt_kind = kind
        self.prompt_row = row
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = {"add": "New task · Enter saves", "edit": "Edit task · Enter saves",
                              "section": "New section · Enter saves", "search": "Search tasks",
                              "delete": "Type DELETE to confirm", "remove-section": "Type DELETE to remove empty section"}[kind]
        prompt.value = value
        prompt.display = True
        self.query_one("#footer", Static).display = False
        prompt.focus()

    def _close_prompt(self) -> None:
        self.prompt_kind = None
        self.prompt_row = None
        self.query_one("#prompt", Input).display = False
        self.query_one("#items", ListView).focus()
        self._update_chrome()

    def action_new_task(self) -> None:
        if self.page != "detail" or not self._project() or self.prompt_kind:
            return
        row = self._selected()
        self._open_prompt("add", row if row and row.kind == "section" else None)

    def action_edit_task(self) -> None:
        row = self._selected()
        if self.page == "detail" and row and row.task and not self.prompt_kind:
            self._open_prompt("edit", row, row.task.text)

    def action_new_section(self) -> None:
        if self.page == "detail" and self._project() and not self.prompt_kind:
            self._open_prompt("section")

    def action_delete_task(self) -> None:
        row = self._selected()
        if self.page == "detail" and row and row.task and not self.prompt_kind:
            self._open_prompt("delete", row)

    def action_remove_section(self) -> None:
        row = self._selected()
        if self.page == "detail" and row and row.section and not self.prompt_kind:
            self._open_prompt("remove-section", row)

    async def action_open_editor(self) -> None:
        row = self._selected()
        if self.page != "detail" or not row or not row.task or not row.project or self.prompt_kind:
            return
        try:
            with self.suspend():
                subprocess.run(["nvim", f"+{row.task.line + 1}", "--", str(row.project.path)], check=True)
            await self.action_reload()
        except (OSError, subprocess.SubprocessError) as exc:
            self.message = f"Could not open Neovim: {exc}"
            self._update_chrome()

    def action_search(self) -> None:
        if self.page != "help" and not self.prompt_kind:
            self._open_prompt("search", value=self.search_query)

    @on(Input.Submitted, "#prompt")
    async def submit_prompt(self, event: Input.Submitted) -> None:
        kind = self.prompt_kind
        row = self.prompt_row
        value = event.value.strip()
        if kind in {"delete", "remove-section"} and value != "DELETE":
            self.message = "Deletion cancelled"
            self._close_prompt()
            return
        if kind == "search":
            self.search_query = value
            self.message = ""
            self._close_prompt()
            await self.draw()
            return
        project = self._project()
        if not kind or not project or not project.doc:
            self._close_prompt()
            return
        if kind not in {"delete", "remove-section"} and not value:
            self.message = "Enter a non-empty name"
            self._update_chrome()
            return
        try:
            if kind == "add":
                section = row.section.line if row and row.section else main_section(project.doc).line
                updated = save(project.doc, "add", section=section, value=value)
            elif kind == "edit":
                updated = save(project.doc, "edit", line=row.task.line, value=value)
            elif kind == "section":
                updated = save(project.doc, "add-section", value=value)
            elif kind == "remove-section":
                updated = save(project.doc, "remove-section", section=row.section.line)
            else:
                updated = save(project.doc, "delete", line=row.task.line)
        except (OSError, ValueError) as exc:
            self.message = str(exc)
            self._update_chrome()
            return
        self._replace_doc(project.path, updated)
        self.message = "Saved to TODO.md"
        self._close_prompt()
        await self.draw(preserve=True)
        if kind == "add":
            self._select_task(value, section)
        elif kind == "section":
            self._select_section(value)

    def _replace_doc(self, path: Path, doc: Document) -> None:
        self.projects_data = [Project(p.name, p.path, doc, None) if p.path == path else p for p in self.projects_data]

    def _select_task(self, text: str, section: int) -> None:
        for i in range(len(self.rows) - 1, -1, -1):
            row = self.rows[i]
            if row.task and row.task.text == text and row.task.section == section:
                self.query_one("#items", ListView).index = i
                break

    def _select_section(self, title: str) -> None:
        for i, row in enumerate(self.rows):
            if row.section and row.section.title == title:
                self.query_one("#items", ListView).index = i

    async def _set_status(self, status: str | None = None) -> None:
        row = self._selected()
        if self.page != "detail" or not row or not row.task or not row.project or not row.project.doc or self.prompt_kind:
            return
        task = row.task
        target = status or {" ": "/", "/": "x", "x": " "}[task.status]
        try:
            updated = save(row.project.doc, "status", line=task.line, status=target)
        except (OSError, ValueError) as exc:
            self.message = str(exc)
            self._update_chrome()
            return
        self._replace_doc(row.project.path, updated)
        self.message = {" ": "Todo", "/": "In progress", "x": "Completed"}[target]
        await self.draw(preserve=True)

    def action_sync(self) -> None:
        if self.prompt_kind or self.sync_running:
            return
        try:
            client = TickTick(Auth.load().require_token())
        except (OSError, ValueError, TickTickError) as exc:
            self.message = str(exc)
            self._update_chrome()
            return
        self.run_worker(self._sync(client, announce=True), exclusive=True)

    def _sync_blocking(self, client: TickTick, announce: bool = True) -> tuple[Sync, object]:
        engine = Sync(client, registry=self.registry)
        projects = load_projects(load_config(self.registry))
        plan = engine.plan(projects)
        if announce or (engine.state.active_only and engine.database.load_state() is not None):
            engine.apply(plan)
        return engine, plan

    async def _sync(self, client: TickTick, announce: bool) -> None:
        if self.sync_running:
            return
        self.sync_running = True
        if announce:
            self.message = "Syncing with TickTick…"
            self._update_chrome()
        try:
            engine, plan = await to_thread(self._sync_blocking, client, announce)
        except (OSError, ValueError, TickTickError) as exc:
            self.sync_running = False
            self.message = f"Sync failed · {exc}"
            self._update_chrome()
            return
        self.sync_running = False
        self._engine = engine
        self._reload_data()
        if not announce and not engine.applied:
            self.message = "Sync preview required · run todo sync --dry-run before the first active-only sync"
            await self.draw()
            return
        tally = plan.summary()
        failures = [change for change in plan.changes if change.error]
        self.conflicts = [change for change in plan.changes if change.action == "conflict"]
        if announce or tally or failures or self.conflicts:
            self.message = "Synced · " + (", ".join(f"{count} {name}" for name, count in sorted(tally.items())) if tally else "no changes")
            if failures:
                self.message += f" · {len(failures)} failed"
            if self.conflicts:
                self.message += f" · {len(self.conflicts)} conflict" + ("s" if len(self.conflicts) > 1 else "")
        if self.conflicts and announce:
            self._return_page = self.page if self.page != "conflicts" else self._return_page
            self.page = "conflicts"
        await self.draw()

    async def _resolve_conflict(self, keep: str) -> None:
        row = self._selected()
        if self.page != "conflicts" or not row or not row.change or self.sync_running or not self._engine:
            return
        uid = row.change.uid
        try:
            change = await to_thread(self._engine.resolve, load_projects(load_config(self.registry)), uid, keep)
        except (OSError, ValueError, TickTickError) as exc:
            self.message = f"Could not resolve · {exc}"
            self._update_chrome()
            return
        self.conflicts = [change_ for change_ in self.conflicts if change_.uid != uid]
        self._reload_data()
        self.message = f"Kept {keep} · {change.text or uid}"
        if not self.conflicts:
            self.page = self._return_page
        await self.draw()

    async def action_cycle_status(self) -> None:
        await self._set_status()

    async def action_todo_status(self) -> None:
        if self.page == "conflicts":
            await self._resolve_conflict("local")
            return
        await self._set_status(" ")

    async def action_progress_status(self) -> None:
        if self.page == "conflicts":
            await self._resolve_conflict("remote")
            return
        await self._set_status("/")

    async def action_done_status(self) -> None:
        await self._set_status("x")

    async def action_reload(self) -> None:
        self._reload_data()
        await self.draw(preserve=True)
        self.message = self.message or "Refreshed from TODO.md"
        self._update_chrome()

    async def on_key(self, event) -> None:
        if self.prompt_kind:
            return
        listing = self.query_one("#items", ListView)
        if event.key == "j":
            listing.action_cursor_down()
            event.stop()
        elif event.key == "k":
            listing.action_cursor_up()
            event.stop()

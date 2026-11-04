"""Responsive, keyboard-first Textual interface.

Color comes from the terminal. The default ``ansi-dark`` theme passes ANSI
colors through and paints no background, and every style in this module is a
named ANSI color, ``dim``, ``bold`` or ``reverse`` — never a hex, 256-color or
truecolor value — so the terminal's own theme decides how everything looks.
The selected row is marked with ``>`` and bold rather than a filled bar.

The layout adapts to the terminal size in tiers (see the ``*_WIDTH`` and
``*_HEIGHT`` constants). Tasks never disappear; the header, footer and extras
shrink first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
import os
import subprocess
import sys

from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.widgets import Input, ListItem, ListView, Static

from .projects import Project, load_config, load_projects
from .store import STATUS, STATUS_CYCLE, Document, Section, Task, counts, main_section, save
from .sync import Change, Plan, Sync, friendly_error, review_reason
from .background import BackgroundSync
from .ticktick import Auth, TickTick, TickTickError

# Width tiers, in terminal columns.
TINY_WIDTH = 40     # below: rows clip with "…", projects show done/total
TABLE_WIDTH = 70    # from here: one line per project with count columns; section labels on open tasks
METER_WIDTH = 110   # from here: a progress meter on each project
SPLIT_WIDTH = 150   # from here (global only): projects pane beside the task list
SIDE_WIDTH = 40     # projects pane, border included
# Height tiers, in rows.
BARE_HEIGHT = 8     # below: no header; the footer shows only when it has something to say
TALL_HEIGHT = 16    # from here: header and footer get a blank row of breathing room

# Status marks take a terminal ANSI color; the task text keeps the default.
MARK = {" ": "bold", "/": "bold yellow", "?": "bold cyan", "x": "green"}
COUNT = {" ": "", "/": "yellow", "?": "cyan", "x": "dim"}
COLUMNS = ((" ", "todo", 6), ("/", "doing", 7), ("?", "review", 8), ("x", "done", 6))
COLUMNS_WIDTH = sum(width for *_, width in COLUMNS)
METER_CELLS = 15  # ten bar cells plus " 100%"


@dataclass(frozen=True)
class Row:
    kind: str
    project: Project | None = None
    task: Task | None = None
    section: Section | None = None
    change: Change | None = None
    status: str | None = None


@dataclass
class Draft:
    """A task being typed in place: a new row, or an existing task being edited."""
    kind: str  # "add" or "edit"
    project: Path
    section: Section
    task: Task | None = None
    text: str = ""
    cursor: int = 0


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


def _fit(left: Text, right: Text, width: int) -> Text:
    """``left`` with ``right`` flush against the right edge, or ``left`` alone when both don't fit."""
    if not right.plain:
        return left
    gap = width - cell_len(left.plain) - cell_len(right.plain)
    if gap < 2:
        return left
    return Text.assemble(left, " " * gap, right)


def _clip(text: str, width: int) -> str:
    if cell_len(text) <= width:
        return text
    out = ""
    for char in text:
        if cell_len(out + char) > width - 1:
            break
        out += char
    return out + "…"


def _meter(done: int, total: int) -> Text:
    filled = round(10 * done / total) if total else 0
    percent = round(100 * done / total) if total else 0
    return Text.assemble(("━" * filled, "green"), ("─" * (10 - filled), "dim"), (f"{percent:>4}%", "dim"))


def _ago(when: datetime) -> str:
    seconds = (datetime.now() - when).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"


def _tilde(path: Path) -> str:
    home, text = str(Path.home()), str(path)
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


class TaskList(ListView):
    """The task/project list, without its own cursor-key bindings.

    ``ListView`` normally binds ``up``/``down`` to its cursor actions, but those
    bindings only exist while the list itself holds focus. The app moves the
    cursor from ``on_key`` instead (the same path as ``j``/``k``), so navigation
    keeps working even when focus sits on the screen rather than the list. Those
    bindings are dropped here to avoid moving twice when the list is focused.
    """

    BINDINGS = [binding for binding in ListView.BINDINGS if binding.key not in {"up", "down"}]


class Shell(Vertical):
    """The outer container; it learns about a terminal resize after the app's size is current.

    ``App.on_resize`` runs before Textual records the new size, so laying out
    from there would use the previous width.
    """

    def on_resize(self, event: Resize) -> None:
        app = self.app
        if isinstance(app, TodoApp):
            app.call_later(app.relayout)


class TodoApp(App[None]):
    CSS_PATH = "ui.tcss"
    TITLE = "todo"
    BINDINGS = [
        Binding("n", "new_task", "New task", show=False),
        Binding("e", "edit_task", "Edit", show=False),
        Binding("u", "toggle_completed", "Completed", show=False),
        Binding("g", "toggle_grouping", "Group by status", show=False),
        Binding("d", "toggle_dates", "Dates", show=False),
        Binding("w", "toggle_wrap", "Wrap", show=False),
        Binding("p", "projects", "Projects", show=False),
        Binding("t", "tasks", "Tasks", show=False),
        Binding("c", "copy_path", "Copy path", show=False),
        Binding("s", "new_section", "Section", show=False),
        Binding("S", "sort_dates", "Sort by date", show=False),
        Binding("space", "cycle_status", "Status", show=False),
        Binding("1", "todo_status", "Not started", show=False),
        Binding("2", "progress_status", "In progress", show=False),
        Binding("3", "review_status", "In review", show=False),
        Binding("4", "done_status", "Completed", show=False),
        Binding("x", "delete_task", "Delete", show=False),
        Binding("backspace", "remove_section", "Remove section", show=False),
        Binding("o", "open_editor", "Neovim", show=False),
        Binding("slash", "search", "Search", show=False),
        Binding("y", "copy_task", "Copy task", show=False),
        Binding("Y", "sync", "Sync", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("escape", "back", "Back", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
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
        # The projects pane shown beside the list on wide terminals.
        self.side_rows: list[Row] = []
        self._side_widgets: list[Static] = []
        self.side_index = 0
        self.side_focus = False
        self._split_active = False
        self.show_completed = True
        self.group_by_status = False
        self.show_dates = False
        # Date sort while dates are visible: None keeps file order, else "newest" / "oldest".
        self.date_sort: str | None = None
        # TODOX_WRAP=0 opens with long rows clipped to one line instead of wrapped.
        self.wrap = os.environ.get("TODOX_WRAP", "1").strip().lower() not in {"0", "false", "off", "no"}
        self._built_wrap = self.wrap
        self.search_query = ""
        self.prompt_kind: str | None = None
        self.prompt_row: Row | None = None
        self.draft: Draft | None = None
        self._draft_return = 0
        self.pending_delete: Row | None = None
        self.conflicts: list[Change] = []
        self.sync_running = False
        self.synced_at: datetime | None = None
        self.sync_failed = False
        self._engine: Sync | None = None
        self._background = BackgroundSync()
        self._quitting = False
        self.message = ""
        self._help_cache: list[Text] = []
        self._return_page = self.page
        self._reload_data()

    # A status message; errors show in red until the next message replaces them.
    @property
    def message(self) -> str:
        return self._message

    @message.setter
    def message(self, value: str) -> None:
        self._message = value
        self._message_error = False

    def _fail(self, text: str) -> None:
        self.message = text
        self._message_error = True
        self._update_chrome()

    def compose(self) -> ComposeResult:
        with Shell(id="shell"):
            yield Static(id="title")
            with Horizontal(id="body"):
                yield TaskList(id="side")
                with Vertical(id="main"):
                    yield Static(id="columns")
                    yield TaskList(id="items")
                    yield Static(id="empty")
            yield Input(id="prompt")
            yield Static(id="footer")

    async def on_mount(self) -> None:
        self.query_one("#prompt", Input).display = False
        await self.draw()
        self._apply_focus()
        self.watch(self.query_one("#items", ListView), "scroll_y", self._on_scroll, init=False)
        # Keeps "synced 2m ago" honest.
        self.set_interval(30, self._update_chrome)
        if self.auto_sync_enabled:
            self.run_worker(self.auto_sync(), exclusive=True, group="ticktick")

    def _reload_data(self) -> None:
        try:
            self.projects_data = load_projects(load_config(self.registry))
            if self.local and not any(p.path == self.local for p in self.projects_data):
                self.projects_data.append(load_projects([Project(self.local.parent.name, self.local)])[0])
            self.message = ""
        except (OSError, ValueError) as exc:
            self.projects_data = load_projects([Project(self.local.parent.name, self.local)]) if self.local else []
            self.message = f"Cannot read projects: {exc}"
            self._message_error = True

    def _project(self) -> Project | None:
        return self._fresh(self.selected_project)

    def _fresh(self, path: Path | None) -> Project | None:
        """The current copy of a project; rows can outlive a save or a reload."""
        return next((p for p in self.projects_data if p.path == path), None)

    def _selected(self) -> Row | None:
        index = self.query_one("#items", ListView).index
        return self.rows[index] if index is not None and 0 <= index < len(self.rows) else None

    def _side_selected(self) -> Row | None:
        return self.side_rows[self.side_index] if 0 <= self.side_index < len(self.side_rows) else None

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

    # ----- layout tiers -------------------------------------------------

    def _wide(self) -> bool:
        return not self.local and self.size.width >= SPLIT_WIDTH

    def _side_visible(self) -> bool:
        return self._wide() and self.page not in {"help", "conflicts"}

    def _side_active(self) -> bool:
        """The projects pane holds the keyboard; task keys wait for the list."""
        return self.side_focus and self._side_visible()

    def _wrap_now(self) -> bool:
        # Below TINY_WIDTH wrapping would spend a row per word, so rows clip.
        return self.wrap and self.size.width >= TINY_WIDTH

    def _content_width(self) -> int:
        """Cells available to a main-list row: shell, item padding and a scrollbar."""
        width = self.size.width - 2 - (SIDE_WIDTH if self._side_visible() else 0)
        return max(10, width - 3)

    def _sync_split(self) -> None:
        """Map pages onto the split view when the terminal crosses SPLIT_WIDTH."""
        wide = self._wide()
        if wide and self.page == "projects":
            row = self._selected() if self.is_mounted else None
            path = row.project.path if row and row.kind == "project" and row.project else self.last_project
            project = self._fresh(path) or next(iter(self.projects_data), None)
            self.side_focus = True
            if project:
                self.page, self.selected_project, self.last_project = "detail", project.path, project.path
            else:
                self.page, self.selected_project = "tasks", None
        elif not wide and self._split_active and self.side_focus:
            self.side_focus = False
            if self.page in {"detail", "tasks"}:
                self.last_project = self.selected_project or self.last_project
                self.page, self.selected_project = "projects", None
        self._split_active = wide

    def _apply_focus(self) -> None:
        if self.prompt_kind in {"search", "section"}:
            return
        target = "#side" if self._side_active() else "#items"
        self.query_one(target, ListView).focus()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id in {"side", "items"} and (widget_id == "side") != self.side_focus:
            self.side_focus = widget_id == "side"
            self._paint_selection()
            self._paint_side(range(len(self.side_rows)))
            self._update_chrome()

    def _toggle_pane(self) -> None:
        self.side_focus = not self.side_focus
        self._apply_focus()
        self._paint_selection()
        self._paint_side(range(len(self.side_rows)))
        self._update_chrome()

    # ----- rows -----------------------------------------------------------

    def _task_rows(self, project: Project) -> list[Row]:
        if not project.doc:
            return []
        doc = project.doc
        matching = [t for t in doc.tasks if (self.show_completed or t.status != "x") and self.search_query.casefold() in t.text.casefold()]
        result: list[Row] = []
        if self.group_by_status:
            for status in STATUS:
                if status == "x" and not self.show_completed:
                    continue
                tasks = [task for task in matching if task.status == status]
                if self.search_query and not tasks:
                    continue
                if self.show_dates and self.date_sort:
                    dated = sorted((t for t in tasks if t.completed), key=lambda t: t.completed or "",
                                   reverse=self.date_sort == "newest")
                    tasks = dated + [t for t in tasks if not t.completed]
                result.append(Row("status", project, status=status))
                result.extend(Row("task", project, task=task) for task in tasks)
            return result
        for section in doc.sections:
            section_tasks = [t for t in matching if t.section == section.line]
            if self.show_dates and self.date_sort:
                # Undated tasks keep file order after the dated ones.
                dated = sorted((t for t in section_tasks if t.completed), key=lambda t: t.completed or "",
                               reverse=self.date_sort == "newest")
                section_tasks = dated + [t for t in section_tasks if not t.completed]
            # A search shows only matching groups; otherwise empty sections remain actionable.
            if not section.main and (not self.search_query or section_tasks):
                result.append(Row("section", project, section=section))
            result.extend(Row("task", project, task=task) for task in section_tasks)
        return result

    def _open_task_rows(self) -> list[Row]:
        """Unfinished tasks from every project, under one heading per project."""
        query = self.search_query.casefold()
        result: list[Row] = []
        for p in self.projects_data:
            if not p.doc:
                continue
            tasks = [t for t in p.doc.tasks if t.status != "x" and query in (p.name + " " + t.text).casefold()]
            if tasks:
                result.append(Row("group", p))
                result.extend(Row("task", p, task=t) for t in tasks)
        return result

    def _rows(self) -> list[Row]:
        self._help_cache = []
        if self.page == "projects":
            rows = [Row("project", p) for p in self.projects_data if self.search_query.casefold() in p.name.casefold()]
        elif self.page == "tasks":
            rows = self._open_task_rows()
        elif self.page == "detail":
            project = self._project()
            rows = self._task_rows(project) if project else []
        elif self.page == "conflicts":
            return [Row("conflict", change=change) for change in self.conflicts]
        else:
            self._help_cache = self._help_lines()
            return [Row("help") for _ in self._help_cache]
        return self._with_draft(rows)

    def _with_draft(self, rows: list[Row]) -> list[Row]:
        """Place a task being added where it will land once saved."""
        draft = self.draft
        project = self._fresh(draft.project) if draft else None
        if not draft or draft.kind != "add" or not project:
            return rows
        line = draft.section.line
        mine = lambda row: row.project is not None and row.project.path == draft.project
        if self.page == "projects":
            at = next((i + 1 for i, r in enumerate(rows) if r.kind == "project" and mine(r)), len(rows))
        elif self.page == "tasks":
            group = next((i for i, r in enumerate(rows) if r.kind == "group" and mine(r)), None)
            if group is None:
                rows, group = rows + [Row("group", project)], len(rows)
            at = group + 1
            for i in range(group + 1, len(rows)):
                if rows[i].kind != "task" or not mine(rows[i]):
                    break
                if rows[i].task.section <= line:
                    at = i + 1
        elif self.group_by_status:
            at = next((i + 1 for i in reversed(range(len(rows))) if rows[i].kind == "task" and rows[i].task.status == " "), None)
            if at is None:
                at = next((i + 1 for i, r in enumerate(rows) if r.kind == "status" and r.status == " "), 0)
        else:
            inside = [i for i, r in enumerate(rows) if r.kind == "task" and r.task.section == line]
            at = inside[-1] + 1 if inside else next(
                (i + 1 for i, r in enumerate(rows) if r.kind == "section" and r.section.line == line), 0)
        return rows[:at] + [Row("draft", project, section=draft.section)] + rows[at:]

    def _side_row_list(self) -> list[Row]:
        return [Row("all")] + [Row("project", p) for p in self.projects_data]

    def _side_index_for_view(self) -> int:
        if self.page == "detail":
            return next((i for i, r in enumerate(self.side_rows) if r.project and r.project.path == self.selected_project),
                        self.side_index)
        return 0 if self.page == "tasks" else self.side_index

    # ----- help -----------------------------------------------------------

    def _help_groups(self) -> list[tuple[str, list[tuple[str, str, bool]]]]:
        page = self._return_page
        inside, editable = page == "detail", page in {"detail", "tasks"}
        move = [("j/k", "move", True), ("enter", "open · edit", True)]
        if self._wide():
            move.append(("tab", "switch pane", True))
        move += [("/", "search", True), ("esc", "back · clear", True)]
        if not self.local:
            move += [("p", "projects", True), ("t", "open tasks", True)]
        tasks = [("n", "new task", True), ("e", "edit", editable), ("space", "next status", editable),
                 ("1–4", "set status", editable), ("x", "delete, then y", inside),
                 ("o", "open in Neovim", inside), ("y", "copy text", True)]
        view = [("u", "show completed", inside), ("g", "group by", inside), ("d", "dates", inside),
                ("S", "sort by date", inside and self.show_dates), ("w", "wrap · clip", editable)]
        project = [("s", "new section", inside), ("bksp", "remove section", inside)]
        if not self.local:
            project.append(("c", "copy path", True))
        project += [("Y", "sync TickTick", True), ("r", "refresh", True), ("ctrl+q", "quit", True)]
        return [("Move", move), ("Tasks", tasks), ("View", view), ("Project & sync", project)]

    def _help_lines(self) -> list[Text]:
        """Key groups side by side when they fit: four columns, two, or one."""
        groups = self._help_groups()
        cell = 24
        usable = self.size.width - 4
        # The last column only needs room for its own longest entry.
        longest = max(7 + cell_len(label) for _, items in groups for _, label, _ in items)
        columns = next(n for n in (4, 2, 1) if n == 1 or (n - 1) * cell + longest <= usable)

        def entry(key: str, label: str, available: bool) -> Text:
            return Text.assemble((key.ljust(7), "bold" if available else "dim"), (label, "" if available else "dim"))

        lines: list[Text] = []
        for start in range(0, len(groups), columns):
            band = groups[start:start + columns]
            if lines:
                lines.append(Text())
            heading = Text()
            for title, _ in band:
                heading.append(title.ljust(cell), style="bold")
            lines += [heading, Text()]
            for entries in zip_longest(*(items for _, items in band)):
                line = Text()
                for position, item in enumerate(entries):
                    piece = entry(*item) if item else Text()
                    if position < len(entries) - 1:
                        piece.append(" " * max(0, cell - cell_len(piece.plain)))
                    line.append_text(piece)
                lines.append(line)
        if any(not available for _, items in groups for *_, available in items):
            note = "Faded keys need dates on: press d" if self._return_page == "detail" else "Faded keys work inside a project"
            lines += [Text(), Text(note, style="dim")]
        return lines

    # ----- rendering ------------------------------------------------------

    def _main_marked(self) -> bool:
        """The list shows its ``>`` unless the projects pane holds the keyboard."""
        return self.draft is not None or not self._side_active()

    def _draft_text(self) -> Text:
        draft = self.draft
        assert draft
        text = Text(draft.text[:draft.cursor])
        text.append(draft.text[draft.cursor:draft.cursor + 1] or " ", style="reverse")
        text.append(draft.text[draft.cursor + 1:])
        return text

    def _is_draft_task(self, row: Row) -> bool:
        draft = self.draft
        return (draft is not None and draft.kind == "edit" and row.kind == "task" and draft.task is not None
                and row.project is not None and row.project.path == draft.project
                and row.task.line == draft.task.line and row.task.text == draft.task.text)

    def _name_field(self, width: int, meter: bool) -> int | None:
        """Width of the project-name column, or None when one-line rows don't fit."""
        room = width - 2 - COLUMNS_WIDTH - (METER_CELLS if meter else 0) - 2
        if room < 8:
            return None
        longest = max((cell_len(p.name) for p in self.projects_data), default=0)
        return min(room, longest + 2)

    def _project_row(self, p: Project, width: int, marker: str, marked: bool) -> RowRender:
        head = Text(marker, style="bold" if marked else "")
        name_style = "bold" if marked else ""
        if p.error:
            state = "missing" if not p.path.exists() else "unavailable"
            line = Text.assemble(head, (p.name, name_style), ("  " + state, "red"))
            where = "  " + _tilde(p.path)
            if cell_len(line.plain + where) <= width:
                line.append(where, style="dim")
            return RowRender(line, "project-row")
        tally = counts(p.doc.tasks if p.doc else [])
        total, done = sum(tally.values()), tally["x"]
        screen = self.size.width
        if screen < TINY_WIDTH:
            line = _fit(Text.assemble(head, (p.name, name_style)), Text(f"{done}/{total}", style="dim"), width)
            return RowRender(line, "project-row")
        meter = screen >= METER_WIDTH
        field = self._name_field(width, meter)
        if screen < TABLE_WIDTH or field is None:
            compact = Text("  ")
            for position, status in enumerate(STATUS):
                if position:
                    compact.append(" · ", style="dim")
                compact.append(f"[{status}]", style=MARK[status] if tally[status] else "dim")
                compact.append(f" {tally[status]}", style="" if tally[status] else "dim")
            return RowRender(Text.assemble(head, (p.name, name_style), "\n", compact), "project-row narrow")
        name = _clip(p.name, field - 2)
        line = Text.assemble(head, (name, name_style), " " * (field - cell_len(name)))
        if meter:
            line.append_text(_meter(done, total))
        line.append(" " * (width - 2 - field - (METER_CELLS if meter else 0) - COLUMNS_WIDTH))
        for status, _, cells in COLUMNS:
            count = tally[status]
            line.append((str(count) if count else "–").rjust(cells), style=COUNT[status] if count else "dim")
        return RowRender(line, "project-row")

    def _columns_text(self) -> Text | None:
        """Labels over the project count columns, aligned with the rows."""
        width = self._content_width()
        if (self.page != "projects" or self.size.width < TABLE_WIDTH or not self.rows
                or self._name_field(width, self.size.width >= METER_WIDTH) is None):
            return None
        line = Text(" " * (width - COLUMNS_WIDTH))
        for _, label, cells in COLUMNS:
            line.append(label.rjust(cells), style="dim")
        return line

    def _render_row(self, row: Row, width: int, index: int = 0, selected: bool = False) -> RowRender:
        marked = selected and self._main_marked()
        marker = "> " if marked else "  "
        marker_text = Text(marker, style="bold" if marked else "")
        if row.kind == "help":
            return RowRender(self._help_cache[index] if index < len(self._help_cache) else Text(), "help-row")
        if row.kind == "conflict":
            change = row.change
            assert change
            label = change.name or (Path(change.project).parent.name if change.project else "TickTick")
            reason = review_reason(change)
            reason = reason[0].upper() + reason[1:]
            body = Text.assemble(marker_text, (label + " · ", "dim"),
                                 (change.text or "Task needs review", "bold"), (f"\n  {reason}", "dim"))
            if change.remote and change.remote.get("title") != change.text:
                body.append(f"\n  TickTick: {change.remote.get('title', '')}", "dim")
            return RowRender(body, "conflict-row")
        if row.kind == "project":
            assert row.project
            return self._project_row(row.project, width, marker, marked)
        if row.kind == "group":
            p = row.project
            assert p and p.doc
            open_count = sum(t.status != "x" for t in p.doc.tasks)
            body = _fit(Text(p.name, style="bold"), Text(f"{open_count} open", style="dim"), width - 2)
            return RowRender(body, "section-row group-row", marker_text)
        if row.kind == "status":
            assert row.status is not None
            shown = sum(1 for r in self.rows if r.kind == "task" and r.task.status == row.status)
            body = _fit(Text(STATUS[row.status].capitalize(), style="bold"), Text(str(shown), style="dim"), width - 2)
            return RowRender(body, "section-row", marker_text)
        if row.kind == "section":
            section, project = row.section, row.project
            assert section and project and project.doc
            tasks = [t for t in project.doc.tasks if t.section == section.line]
            done = sum(t.status == "x" for t in tasks)
            summary = (f"{len(tasks) - done} open" + (f" · {done} done" if done else "")) if tasks else "empty · n adds here"
            body = _fit(Text(section.title, style="bold"), Text(summary, style="dim"), width - 2)
            return RowRender(body, "section-row", marker_text)
        if row.kind == "draft":
            prefix = Text.assemble(marker_text, ("[ ]", "dim"), "  ")
            section = row.section
            label = "new" if section is None or section.main else f"new · {section.title}"
            return RowRender(_fit(self._draft_text(), Text(label, style="dim"), width - 7), "task-row draft-row", prefix)
        assert row.task and row.project
        t = row.task
        prefix = Text.assemble(marker_text, (f"[{t.status}]", MARK[t.status]), "  ")
        if self._is_draft_task(row):
            return RowRender(self._draft_text(), "task-row draft-row", prefix)
        body = Text(t.text, style="dim" if t.status == "x" else "")
        if self.show_dates and t.completed:
            fitted = _fit(body, Text(t.completed, style="dim"), width - 7)
            if fitted is body:
                body.append(f" · {t.completed}", style="dim")
            body = fitted
        elif self.page == "tasks" and self.size.width >= TABLE_WIDTH and row.project.doc:
            section = next((s for s in row.project.doc.sections if s.line == t.section), None)
            if section and not section.main:
                body = _fit(body, Text(section.title, style="dim"), width - 7)
        return RowRender(body, "task-row" + (" done" if t.status == "x" else ""), prefix)

    def _render_side_row(self, row: Row, selected: bool) -> Text:
        width = SIDE_WIDTH - 4
        if selected and self.side_focus:
            marker = Text("> ", style="bold")
        elif selected:
            marker = Text("› ", style="dim")
        else:
            marker = Text("  ")
        name_style = "bold" if selected else ""
        if row.kind == "all":
            total = sum(t.status != "x" for p in self.projects_data if p.doc for t in p.doc.tasks)
            label = Text.assemble(marker, ("All open tasks", "bold" if selected else "dim"))
            return _fit(label, Text(str(total), style="dim"), width)
        p = row.project
        assert p
        if p.error:
            state = "missing" if not p.path.exists() else "unavailable"
            return Text.assemble(marker, (_clip(p.name, 16), name_style), ("  " + state, "red"))
        tally = counts(p.doc.tasks if p.doc else [])
        name = _clip(p.name, 14)
        return Text.assemble(marker, (name, name_style), " " * (16 - cell_len(name)),
                             _meter(tally["x"], sum(tally.values())))

    def _row_classes(self, rendered: RowRender) -> str:
        # ``unwrap`` clips long rows to one line; wrapping is the default.
        return rendered.classes if self._wrap_now() else rendered.classes + " unwrap"

    def _paint_rows(self, indices) -> None:
        """Re-render only the given rows; a keypress must not repaint the whole list."""
        width = self._content_width()
        for index in indices:
            if not 0 <= index < len(self.rows) or index >= len(self._row_widgets):
                continue
            row = self.rows[index]
            widgets = self._row_widgets[index]
            rendered = self._render_row(row, width, index, index == self.selected_index)
            classes = self._row_classes(rendered)
            if set(widgets.item.classes) != set(classes.split()):
                widgets.item.set_classes(classes)
            widgets.body.update(rendered.body)
            if widgets.marker is not None:
                widgets.marker.update(rendered.marker or Text())

    def _paint_selection(self) -> None:
        """Repaint every row; used after a rebuild or a width change, not per key."""
        self._paint_rows(range(len(self.rows)))

    def _paint_side(self, indices) -> None:
        for index in indices:
            if 0 <= index < len(self.side_rows) and index < len(self._side_widgets):
                self._side_widgets[index].update(self._render_side_row(self.side_rows[index], index == self.side_index))

    def _row_identity(self, row: Row) -> tuple:
        """What must change for the list to need rebuilding instead of repainting."""
        if row.kind == "task":
            return ("task", row.project.path if row.project else None, row.task.line, row.task.uid or row.task.text)
        if row.kind == "section":
            return ("section", row.project.path if row.project else None, row.section.line)
        if row.kind == "status":
            return ("status", row.status)
        if row.kind in {"project", "group"}:
            return (row.kind, row.project.path)
        if row.kind == "conflict":
            return ("conflict", row.change.uid)
        return (row.kind,)

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "side":
            index = event.list_view.index
            if index is None or index == self.side_index or not 0 <= index < len(self.side_rows):
                return
            previous, self.side_index = self.side_index, index
            self._paint_side({previous, index})
            row = self.side_rows[index]
            if row.kind == "all":
                self.page, self.selected_project = "tasks", None
            elif row.project:
                self.page, self.selected_project, self.last_project = "detail", row.project.path, row.project.path
            self.search_query = ""
            await self.draw()
            return
        if event.list_view.id != "items":
            return
        previous = self.selected_index
        self.selected_index = event.list_view.index or 0
        if self.pending_delete is not None:
            self.pending_delete = None
            self.message = ""
            self._update_chrome()
        # Only the row that lost the ``>`` marker and the one that gained it changed.
        self._paint_rows({previous, self.selected_index})

    async def draw(self, preserve: bool = False) -> None:
        if not self.is_mounted:
            return
        self._sync_split()
        listing = self.query_one("#items", ListView)
        old = self._selected() if preserve else None
        old_index = listing.index or 0
        rows = self._rows()
        wrap = self._wrap_now()
        # The same rows with the same wrapping only need fresh text: rebuilding
        # hundreds of ListItems on resize or refresh blocks the event loop.
        if preserve and self._built_wrap == wrap and [self._row_identity(r) for r in rows] == [self._row_identity(r) for r in self.rows]:
            self.rows = rows
            if rows:
                old_key = self._row_identity(old) if old else None
                index = next((i for i, r in enumerate(rows) if old_key is not None and self._row_identity(r) == old_key),
                             min(old_index, len(rows) - 1))
                if listing.index != index:
                    listing.index = index
                self.selected_index = index
            self._paint_selection()
        else:
            self.rows = rows
            self._row_widgets = []
            items: list[ListItem] = []
            width = self._content_width()
            for index, row in enumerate(self.rows):
                rendered = self._render_row(row, width, index)
                classes = self._row_classes(rendered)
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
            self._built_wrap = wrap
            if items:
                await listing.extend(items)
                # A fresh Open tasks list starts on its first task, not a project heading.
                first = 1 if self.page == "tasks" and len(self.rows) > 1 and self.rows[0].kind == "group" else 0
                index = self.rows.index(old) if old in self.rows else min(old_index, len(self.rows) - 1) if preserve else first
                listing.index = index
                self.selected_index = index
                self._paint_selection()
        await self._draw_side()
        self._update_chrome()
        # "N more ↓" needs the rows laid out first.
        self.call_after_refresh(self._update_chrome)

    async def _draw_side(self) -> None:
        side = self.query_one("#side", ListView)
        visible = self._side_visible()
        side.display = visible
        if not visible:
            return
        rows = self._side_row_list()
        same = [self._row_identity(r) for r in rows] == [self._row_identity(r) for r in self.side_rows]
        self.side_rows = rows
        self.side_index = min(self._side_index_for_view(), len(rows) - 1)
        if not same or len(self._side_widgets) != len(rows):
            self._side_widgets = [Static(self._render_side_row(r, i == self.side_index), markup=False)
                                  for i, r in enumerate(rows)]
            await side.clear()
            await side.extend([ListItem(widget, classes="side-all" if r.kind == "all" else "side-row")
                               for widget, r in zip(self._side_widgets, rows)])
        else:
            self._paint_side(range(len(rows)))
        if side.index != self.side_index:
            side.index = self.side_index

    def _scroll_to_selection(self) -> None:
        if 0 <= self.selected_index < len(self._row_widgets):
            self.query_one("#items", ListView).scroll_to_widget(self._row_widgets[self.selected_index].item, animate=False)

    # ----- header and footer ------------------------------------------------

    def _page_label(self, page: str) -> str:
        project = self._project()
        return {"projects": "on Projects", "tasks": "on Open tasks", "conflicts": "on Tasks needing review"}.get(
            page, f"in {project.name}" if project else "in a project")

    def _sync_badge(self) -> tuple[Text, Text]:
        """The sync state at full length and as a single glyph."""
        if self.sync_running:
            return Text("↻ syncing", style="dim"), Text("↻", style="dim")
        if self.sync_failed:
            return Text.assemble(("●", "red"), (" sync failed", "dim")), Text("●", style="red")
        if self.synced_at:
            return Text.assemble(("●", "green"), (f" synced {_ago(self.synced_at)}", "dim")), Text("●", style="green")
        return Text(), Text()

    def _header(self, width: int) -> Text:
        left = Text()
        open_tasks = [(p, t) for p in self.projects_data if p.doc for t in p.doc.tasks if t.status != "x"]
        if self.page == "projects":
            left.append("Projects", style="bold")
            plural = "s" if len(self.projects_data) != 1 else ""
            left.append(f"  {len(self.projects_data)} project{plural} · {len(open_tasks)} open", style="dim")
        elif self.page == "tasks":
            spread = len({p.path for p, _ in open_tasks})
            left.append("Open tasks", style="bold")
            left.append(f"  {len(open_tasks)} in {spread} project{'s' if spread != 1 else ''}", style="dim")
        elif self.page == "detail":
            project = self._project()
            if not self.local:
                left.append("Projects › ", style="dim")
            left.append(project.name if project else "Project", style="bold")
        elif self.page == "help":
            left.append("Keys", style="bold")
            left.append(f"  · {self._page_label(self._return_page)}", style="dim")
        else:
            left.append("Tasks needing review", style="bold")
        state: list[str] = []
        if self.page == "detail":
            if self.group_by_status:
                state.append("by status")
            if not self.show_completed:
                state.append("done hidden")
            if self.show_dates:
                state.append({"newest": "dates, newest first", "oldest": "dates, oldest first"}.get(self.date_sort or "", "dates"))
        if self.search_query and self.page in {"projects", "tasks", "detail"}:
            state.append(f"/{self.search_query}")
        status = Text(" · ".join(state), style="dim")
        full, short = self._sync_badge()
        options = []
        for badge in (full, short):
            options.append(Text.assemble(status, "   ", badge) if status.plain and badge.plain else status if status.plain else badge)
        options += [full, short]
        if cell_len(left.plain) > width:
            left.truncate(width, overflow="ellipsis")
            return left
        for right in options:
            if not right.plain or cell_len(left.plain) + 2 + cell_len(right.plain) <= width:
                return _fit(left, right, width)
        return left

    def _hints(self) -> list[tuple[str, str]]:
        """Key hints in priority order; ``? keys`` comes last and is never dropped."""
        if self.draft:
            return [("enter", "save"), ("esc", "cancel")]
        if self.prompt_kind:
            return [("enter", "search" if self.prompt_kind == "search" else "add"), ("esc", "cancel")]
        if self.page == "help":
            return [("esc", "close"), ("ctrl+q", "quit")]
        if self.page == "conflicts":
            selected = self._selected()
            if selected and selected.change and selected.change.uid and selected.change.remote:
                return [("1", "keep TODO.md"), ("2", "keep TickTick"), ("esc", "back")]
            return [("esc", "back")]
        if self._side_active():
            hints = [("enter", "open"), ("n", "new task"), ("tab", "tasks"), ("c", "copy path"), ("/", "search"), ("Y", "sync")]
        elif self.page == "projects":
            hints = [("enter", "open"), ("n", "new task"), ("t", "open tasks"), ("c", "copy path"), ("/", "search"), ("Y", "sync")]
        elif self.page == "tasks":
            hints = [("n", "new"), ("e", "edit"), ("space", "status"), ("enter", "open project"),
                     ("p", "projects"), ("/", "search"), ("w", "wrap")]
        else:
            hints = [("n", "new"), ("e", "edit"), ("space", "status"), ("u", "done"), ("g", "group"), ("d", "dates"),
                     ("x", "delete"), ("s", "section"), ("/", "search")]
            if not self.local:
                hints.append(("p", "projects"))
        if self._side_visible() and not self._side_active():
            hints.insert(3, ("tab", "projects"))
        return hints + [("?", "keys")]

    def _below_count(self) -> int:
        """Task rows scrolled out of view below the list."""
        listing = self.query_one("#items", ListView)
        bottom = listing.scroll_offset.y + listing.scrollable_content_region.height
        return sum(1 for row, widgets in zip(self.rows, self._row_widgets)
                   if row.kind == "task" and widgets.item.virtual_region.y >= bottom)

    def _footer(self, width: int) -> Text:
        right = Text()
        if self.message:
            right = Text(self.message, style="red" if self.pending_delete is not None or self._message_error else "")
        elif self.draft:
            project = self._fresh(self.draft.project)
            name = project.name if project else "project"
            if self.draft.kind == "add":
                where = name if self.draft.section.main else f"{name} › {self.draft.section.title}"
                right = Text.assemble(("adding to ", "dim"), where)
            else:
                right = Text.assemble(("editing in ", "dim"), name)
        elif self.size.width < TINY_WIDTH or self.size.height < TALL_HEIGHT:
            hidden = self._below_count()
            if hidden:
                right = Text(f"{hidden} more ↓", style="dim")
        hints = self._hints()
        keep = [hints.pop()] if hints and hints[-1][0] == "?" else []

        def chunk(key: str, label: str) -> Text:
            return Text.assemble((key, "bold"), (" " + label, "dim"))

        gap = "   "
        room = width - (cell_len(right.plain) + len(gap) if right.plain else 0)
        reserve = sum(len(gap) + cell_len(chunk(*hint).plain) for hint in keep)
        if room < reserve:
            right.truncate(width, overflow="ellipsis")
            return right
        left = Text()
        for hint in hints:
            piece = chunk(*hint)
            extra = (len(gap) if left.plain else 0) + cell_len(piece.plain)
            if cell_len(left.plain) + extra + reserve > room:
                break
            if left.plain:
                left.append(gap)
            left.append_text(piece)
        for hint in keep:
            if left.plain:
                left.append(gap)
            left.append_text(chunk(*hint))
        return _fit(left, right, width) if right.plain else left

    def _update_chrome(self) -> None:
        if not self.is_mounted:
            return
        width, height = self.size
        self.query_one("#shell").set_class(height >= TALL_HEIGHT, "-tall")
        title = self.query_one("#title", Static)
        title.display = height >= BARE_HEIGHT
        title.update(self._header(width - 4))
        columns = self.query_one("#columns", Static)
        labels = self._columns_text()
        columns.display = labels is not None and height >= BARE_HEIGHT
        columns.update(labels or Text())
        project = self._project()
        empty = self.query_one("#empty", Static)
        if self.page == "projects":
            message = "No projects yet · run todo add-project /path" if not self.projects_data else "No matching projects"
        elif self.page == "tasks":
            message = "No open tasks" if not self.search_query else "No matching tasks"
        elif self.page == "detail":
            message = project.error if project and project.error else "No tasks yet · n to add one"
        elif self.page == "conflicts":
            message = "These tasks need review before they can sync."
        else:
            message = ""
        empty.update(message)
        empty.display = not self.rows
        footer = self.query_one("#footer", Static)
        news = bool(self.message or self.draft or self.prompt_kind or self.pending_delete)
        footer.display = height >= BARE_HEIGHT or news
        footer.update(self._footer(width - 4))

    def _on_scroll(self) -> None:
        self._update_chrome()

    async def relayout(self) -> None:
        """Re-render for a new terminal size, keeping the selected row in view."""
        if self.is_mounted:
            await self.draw(preserve=True)
            self._apply_focus()
            self.call_after_refresh(self._scroll_to_selection)

    # ----- navigation -------------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self.prompt_kind:
            return
        if event.list_view.id == "side":
            row = self._side_selected()
            if row and row.project and row.project.error:
                self._fail(row.project.error)
            elif self.side_focus:
                self._toggle_pane()
            return
        row = self._selected()
        if not row:
            return
        if self.page == "projects" and row.project:
            if row.project.error:
                self._fail(row.project.error)
            else:
                await self._open_project(row.project.path)
        elif self.page == "tasks" and row.project:
            await self._open_project(row.project.path, row.task)
        elif row.kind == "task":
            await self.action_edit_task()
        elif row.kind == "section":
            await self.action_new_task()

    async def _open_project(self, path: Path, task: Task | None = None) -> None:
        self.selected_project = path
        self.last_project = path
        self.page = "detail"
        self.search_query = ""
        self.side_focus = False
        await self.draw()
        if task:
            for index, candidate in enumerate(self.rows):
                if candidate.task and candidate.task.line == task.line:
                    self.query_one("#items", ListView).index = index
                    break
        self._apply_focus()

    async def _navigate(self, page: str) -> None:
        if self.local or self.prompt_kind:
            if self.local:
                self._fail("Local TODO.md only · use todo --global for Projects")
            return
        self.page = page
        self.selected_project = None
        self.search_query = ""
        self.message = ""
        if page == "tasks":
            self.side_focus = False
        await self.draw()
        if self.page == "projects":
            for i, row in enumerate(self.rows):
                if row.project and row.project.path == self.last_project:
                    self.query_one("#items", ListView).index = i
                    break
        self._apply_focus()
        self._paint_selection()
        self._update_chrome()

    async def action_projects(self) -> None:
        await self._navigate("projects")

    async def action_tasks(self) -> None:
        await self._navigate("tasks")

    async def action_back(self) -> None:
        if self.draft:
            await self._end_draft(saved=False)
        elif self.prompt_kind:
            self._close_prompt()
        elif self.search_query:
            self.search_query = ""
            self.message = ""
            await self.draw()
        elif self.page in {"help", "conflicts"}:
            self.page = self._return_page
            await self.draw()
            self._apply_focus()
        elif not self.local and self.page != "projects" and not self._side_active():
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
        row = self._side_selected() if self._side_active() else self._selected()
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
        if self._copy(text):
            self.message = label
            self._update_chrome()
        else:
            self._fail(f"Clipboard unavailable · {text}")

    def action_copy_path(self) -> None:
        row = self._side_selected() if self._side_active() else self._selected()
        project = row.project if row and row.project else self._project()
        if not project:
            return
        path = str(project.path.parent)
        if self._copy(path):
            self.message = "Project path copied"
            self._update_chrome()
        else:
            self._fail("Clipboard unavailable · project path: " + path)

    def action_toggle_completed(self) -> None:
        if self.page != "detail" or self.prompt_kind:
            return
        self.show_completed = not self.show_completed
        self.message = "Showing completed" if self.show_completed else "Completed hidden"
        self.run_worker(self.draw(preserve=True), exclusive=True)

    def action_toggle_grouping(self) -> None:
        if self.page != "detail" or self.prompt_kind:
            return
        self.group_by_status = not self.group_by_status
        self.message = "Grouped by status" if self.group_by_status else "Grouped by section"
        self.run_worker(self.draw(preserve=True), exclusive=True)

    def action_toggle_dates(self) -> None:
        if self.page != "detail" or self.prompt_kind:
            return
        self.show_dates = not self.show_dates
        self.message = "Dates visible" if self.show_dates else "Dates hidden"
        self.run_worker(self.draw(preserve=True), exclusive=True)

    def action_sort_dates(self) -> None:
        if self.page != "detail" or self.prompt_kind or not self.show_dates:
            return
        self.date_sort = {None: "newest", "newest": "oldest", "oldest": None}[self.date_sort]
        self.message = {"newest": "Sorted newest first", "oldest": "Sorted oldest first", None: "File order"}[self.date_sort]
        self.run_worker(self.draw(preserve=True), exclusive=True)

    def action_toggle_wrap(self) -> None:
        if self.page not in {"detail", "tasks"} or self.prompt_kind:
            return
        self.wrap = not self.wrap
        self.message = "Wrapping long rows" if self.wrap else "Clipping long rows"
        self.run_worker(self.draw(preserve=True), exclusive=True)

    # ----- prompts: search and section names use the input line -------------

    def _open_prompt(self, kind: str, value: str = "") -> None:
        self.message = ""
        self.prompt_kind = kind
        self.prompt_row = None
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = {"section": "New section name", "search": "Search tasks"}[kind]
        prompt.value = value
        prompt.display = True
        prompt.focus()
        self._update_chrome()

    def _close_prompt(self) -> None:
        self.prompt_kind = None
        self.prompt_row = None
        self.query_one("#prompt", Input).display = False
        self._apply_focus()
        self._update_chrome()

    def action_new_section(self) -> None:
        if self.page == "detail" and self._project() and not self.prompt_kind:
            self._open_prompt("section")

    def action_search(self) -> None:
        if self.page != "help" and not self.prompt_kind:
            self._open_prompt("search", value=self.search_query)

    @on(Input.Submitted, "#prompt")
    async def submit_prompt(self, event: Input.Submitted) -> None:
        kind = self.prompt_kind
        value = event.value.strip()
        if kind == "search":
            self.search_query = value
            self.message = ""
            self._close_prompt()
            await self.draw()
            return
        project = self._project()
        if kind != "section" or not project or not project.doc:
            self._close_prompt()
            return
        if not value:
            self._fail("Enter a non-empty name")
            return
        try:
            updated = save(project.doc, "add-section", value=value)
        except (OSError, ValueError) as exc:
            self._fail(str(exc))
            return
        self._replace_doc(project.path, updated)
        self.message = f"Saved to {project.name}/TODO.md"
        self._close_prompt()
        await self.draw(preserve=True)
        self._select_section(value)

    # ----- drafts: new and edited tasks are typed in their own row ------------

    async def action_new_task(self) -> None:
        """Add to the selected section, the selected task's section, or Main.

        On Projects, the projects pane and the global task list the task goes to
        the selected row's project, so tasks can be added without opening it.
        """
        if self.page not in {"detail", "tasks", "projects"} or self.prompt_kind:
            return
        section: Section | None = None
        if self._side_active():
            side = self._side_selected()
            project = self._fresh(side.project.path) if side and side.project else None
        else:
            row = self._selected()
            project = self._project() if self.page == "detail" else self._fresh(row.project.path) if row and row.project else None
            if project and project.doc and row:
                if row.task:
                    section = next((s for s in project.doc.sections if s.line == row.task.section), None)
                elif row.kind == "section":
                    section = row.section
        if not project or not project.doc:
            if self.page != "detail":
                self._fail(project.error if project and project.error else "Select a project to add a task")
            return
        await self._start_draft("add", project, section or main_section(project.doc))

    async def action_edit_task(self) -> None:
        row = self._selected()
        if (self.page not in {"detail", "tasks"} or not row or not row.task or not row.project or not row.project.doc
                or self.prompt_kind or self._side_active()):
            return
        section = next((s for s in row.project.doc.sections if s.line == row.task.section), main_section(row.project.doc))
        await self._start_draft("edit", row.project, section, row.task)

    async def _start_draft(self, kind: str, project: Project, section: Section, task: Task | None = None) -> None:
        listing = self.query_one("#items", ListView)
        self._draft_return = listing.index or 0
        text = task.text if task else ""
        self.draft = Draft(kind, project.path, section, task, text, len(text))
        self.prompt_kind = kind
        self.prompt_row = Row("task", project, task=task, section=section) if task else Row("section", project, section=section)
        self.pending_delete = None
        self.message = ""
        await self.draw(preserve=True)
        index = self._draft_index()
        if index is not None:
            listing.index = index
            self.selected_index = index
            self._paint_selection()
            self.call_after_refresh(self._scroll_to_selection)
        self._update_chrome()

    def _draft_index(self) -> int | None:
        for index, row in enumerate(self.rows):
            if row.kind == "draft" or self._is_draft_task(row):
                return index
        return None

    async def _draft_key(self, event: events.Key) -> None:
        draft = self.draft
        assert draft
        event.prevent_default()
        event.stop()
        key, text, cursor = event.key, draft.text, draft.cursor
        if key == "escape":
            await self._end_draft(saved=False)
            return
        if key == "enter":
            await self._save_draft()
            return
        if key == "backspace" and cursor:
            text, cursor = text[:cursor - 1] + text[cursor:], cursor - 1
        elif key == "delete":
            text = text[:cursor] + text[cursor + 1:]
        elif key == "left":
            cursor = max(0, cursor - 1)
        elif key == "right":
            cursor = min(len(text), cursor + 1)
        elif key in {"home", "ctrl+a"}:
            cursor = 0
        elif key in {"end", "ctrl+e"}:
            cursor = len(text)
        elif key == "ctrl+u":
            text, cursor = text[cursor:], 0
        elif key == "ctrl+k":
            text = text[:cursor]
        elif key == "ctrl+w":
            start = text[:cursor].rstrip().rfind(" ") + 1
            text, cursor = text[:start] + text[cursor:], start
        elif event.is_printable and event.character:
            text, cursor = text[:cursor] + event.character + text[cursor:], cursor + len(event.character)
        else:
            return
        self._edit_draft(text, cursor)

    def _edit_draft(self, text: str, cursor: int) -> None:
        assert self.draft
        self.draft.text, self.draft.cursor = text, cursor
        if self.message:
            self.message = ""
            self._update_chrome()
        index = self._draft_index()
        if index is not None:
            self._paint_rows({index})

    async def on_paste(self, event: events.Paste) -> None:
        if self.draft is None:
            return
        event.stop()
        pasted = " ".join(event.text.split())
        draft = self.draft
        self._edit_draft(draft.text[:draft.cursor] + pasted + draft.text[draft.cursor:], draft.cursor + len(pasted))

    async def _save_draft(self) -> None:
        draft = self.draft
        assert draft
        value = draft.text.strip()
        project = self._fresh(draft.project)
        if not value:
            self._fail("Enter a non-empty task")
            return
        if not project or not project.doc:
            self._fail("Project changed · nothing saved")
            return
        try:
            if draft.kind == "add":
                updated = save(project.doc, "add", section=draft.section.line, value=value)
            else:
                task = self._locate(project.doc, draft.task) if draft.task else None
                if task is None:
                    raise ValueError("Task changed · refresh before editing")
                updated = save(project.doc, "edit", line=task.line, value=value)
        except (OSError, ValueError) as exc:
            self._fail(str(exc))
            return
        self._replace_doc(project.path, updated)
        await self._end_draft(saved=True)
        self.message = f"Saved to {project.name}/TODO.md"
        if draft.kind == "add" and self._select_task(value, draft.section.line, project.path):
            self.call_after_refresh(self._scroll_to_selection)
        self._update_chrome()

    async def _end_draft(self, saved: bool) -> None:
        kind = self.draft.kind if self.draft else None
        self.draft = None
        self.prompt_kind = None
        self.prompt_row = None
        await self.draw(preserve=True)
        if kind == "add" and self.rows:
            # The draft row is gone; go back to where the add started.
            index = min(self._draft_return, len(self.rows) - 1)
            self.query_one("#items", ListView).index = index
            self.selected_index = index
            self._paint_selection()
        if not saved:
            self._update_chrome()

    # ----- task actions -----------------------------------------------------

    def action_delete_task(self) -> None:
        row = self._selected()
        if self.page == "detail" and row and row.task and not self.prompt_kind and not self._side_active():
            self._arm_delete(row, f'Delete "{row.task.text}"?')

    def action_remove_section(self) -> None:
        row = self._selected()
        if self.page == "detail" and row and row.section and not self.prompt_kind and not self._side_active():
            self._arm_delete(row, f'Remove section "{row.section.title}"?')

    def _arm_delete(self, row: Row, question: str) -> None:
        self.pending_delete = row
        self.message = f"{question}  y confirm · n cancel"
        self._update_chrome()

    @staticmethod
    def _locate(doc: Document, task: Task) -> Task | None:
        """Find the same task again after any refresh; never trust a bare line number."""
        if task.uid:
            match = next((t for t in doc.tasks if t.uid == task.uid), None)
            if match:
                return match
        matches = [t for t in doc.tasks if t.text == task.text and t.status == task.status]
        if len(matches) == 1:
            return matches[0]
        return next((t for t in doc.tasks if t.line == task.line and t.text == task.text), None)

    async def _confirm_pending_delete(self) -> None:
        row = self.pending_delete
        self.pending_delete = None
        if row is None:
            return
        project = self._project()
        if not project or not project.doc:
            self._fail("Project changed · nothing deleted")
            return
        try:
            if row.task:
                task = self._locate(project.doc, row.task)
                if task is None:
                    raise ValueError("Task changed · refresh before deleting")
                updated = save(project.doc, "delete", line=task.line)
                done = f'Deleted "{task.text}"'
            elif row.section:
                section = next((s for s in project.doc.sections if s.line >= 0 and s.title == row.section.title), None)
                if section is None:
                    raise ValueError("Section changed · refresh before removing")
                updated = save(project.doc, "remove-section", section=section.line)
                done = f'Removed section "{section.title}"'
            else:
                return
        except (OSError, ValueError) as exc:
            self._fail(str(exc))
            return
        self._replace_doc(project.path, updated)
        self.message = f"{done} · Saved to TODO.md"
        await self.draw(preserve=True)

    async def action_open_editor(self) -> None:
        row = self._selected()
        if self.page != "detail" or not row or not row.task or not row.project or self.prompt_kind or self._side_active():
            return
        try:
            with self.suspend():
                subprocess.run(["nvim", f"+{row.task.line + 1}", "--", str(row.project.path)], check=True)
            await self.action_reload()
        except (OSError, subprocess.SubprocessError) as exc:
            self._fail(f"Could not open Neovim: {exc}")

    def _replace_doc(self, path: Path, doc: Document) -> None:
        self.projects_data = [Project(p.name, p.path, doc, None) if p.path == path else p for p in self.projects_data]

    def _select_task(self, text: str, section: int, path: Path) -> bool:
        for i in range(len(self.rows) - 1, -1, -1):
            row = self.rows[i]
            if (row.task and row.project and row.project.path == path
                    and row.task.text == text and row.task.section == section):
                self.query_one("#items", ListView).index = i
                return True
        return False

    def _select_section(self, title: str) -> None:
        for i, row in enumerate(self.rows):
            if row.section and row.section.title == title:
                self.query_one("#items", ListView).index = i

    async def _set_status(self, status: str | None = None) -> None:
        row = self._selected()
        if (self.page not in {"detail", "tasks"} or not row or not row.task or not row.project or not row.project.doc
                or self.prompt_kind or self._side_active()):
            return
        task = row.task
        target = status or STATUS_CYCLE[(STATUS_CYCLE.index(task.status) + 1) % len(STATUS_CYCLE)]
        try:
            updated = save(row.project.doc, "status", line=task.line, status=target)
        except (OSError, ValueError) as exc:
            self._fail(str(exc))
            return
        self._replace_doc(row.project.path, updated)
        # The global list hides completed tasks, so say which one just left it.
        self.message = STATUS[target].capitalize() + (f" · {task.text}" if self.page == "tasks" else "")
        await self.draw(preserve=True)

    # ----- sync ---------------------------------------------------------------

    def action_sync(self) -> None:
        if self.prompt_kind or self.sync_running:
            return
        try:
            client = TickTick(Auth.load().require_token())
        except (OSError, ValueError, TickTickError) as exc:
            self._fail(str(exc))
            return
        self.run_worker(self._sync(client, announce=True), exclusive=True, group="ticktick")

    def action_quit(self) -> None:
        self._quitting = True
        self._background.stop()
        self.workers.cancel_all()
        self.exit()

    def on_unmount(self) -> None:
        self._quitting = True
        self._background.stop()

    async def _run_background(self, client: TickTick, announce: bool, uid: str = "", keep: str = "") -> dict:
        return await self._background.run({"token": client.token, "announce": announce,
                                           "registry": str(self.registry) if self.registry else None,
                                           "uid": uid, "keep": keep,
                                           "files": [str(p.path) for p in self.projects_data]})

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
        self._update_chrome()
        try:
            result = await self._run_background(client, announce)
            plan = Plan(result["plan"]["list_id"], result["plan"]["list_name"],
                        [Change(**change) for change in result["plan"]["changes"]])
            engine = Sync(client, registry=self.registry)
            engine.applied = result["applied"]
        except (OSError, ValueError, TickTickError) as exc:
            self.sync_running = False
            if self._quitting:
                return
            self.sync_failed = True
            self._fail(f"Sync failed · {friendly_error(str(exc))}")
            return
        finally:
            self.sync_running = False
        if self._quitting:
            return
        self._engine = engine
        self._reload_data()
        if not announce and not engine.applied:
            self.message = "Sync preview required · run todo sync --dry-run before the first active-only sync"
            await self.draw()
            return
        self.synced_at, self.sync_failed = datetime.now(), False
        self.conflicts = [change for change in plan.changes if change.action == "conflict"]
        self.message = plan.status_message()
        if self.conflicts and announce:
            if not any(change.error for change in plan.changes):
                self.message = ""
            self._return_page = self.page if self.page != "conflicts" else self._return_page
            self.page = "conflicts"
        await self.draw()

    async def _resolve_conflict(self, keep: str) -> None:
        row = self._selected()
        if self.page != "conflicts" or not row or not row.change or self.sync_running or not self._engine:
            return
        uid = row.change.uid
        self.run_worker(self._resolve_in_background(uid, keep), exclusive=True, group="ticktick")

    async def _resolve_in_background(self, uid: str, keep: str) -> None:
        self.sync_running = True
        self.message = "Resolving task in background…"
        self._update_chrome()
        try:
            client = TickTick(Auth.load().require_token())
            result = await self._run_background(client, True, uid, keep)
            change = Change(**result["change"])
            if change.error:
                raise TickTickError(change.error)
        except (OSError, ValueError, TickTickError) as exc:
            if self._quitting:
                return
            self.sync_running = False
            self._fail(f"Could not resolve · {friendly_error(str(exc))}")
            return
        finally:
            self.sync_running = False
        if self._quitting:
            return
        self.conflicts = [change_ for change_ in self.conflicts if change_.uid != uid]
        self._reload_data()
        self.message = f"Used {'TODO.md' if keep == 'local' else 'TickTick'} · {change.text or uid}"
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

    async def action_review_status(self) -> None:
        await self._set_status("?")

    async def action_done_status(self) -> None:
        await self._set_status("x")

    async def action_reload(self) -> None:
        self._reload_data()
        await self.draw(preserve=True)
        self.message = self.message or "Refreshed from TODO.md"
        self._update_chrome()

    async def on_key(self, event: events.Key) -> None:
        if self.draft is not None:
            await self._draft_key(event)
            return
        if self.prompt_kind:
            return
        if self.pending_delete is not None:
            key = event.key.lower()
            if key == "y":
                event.prevent_default()
                event.stop()
                await self._confirm_pending_delete()
                return
            self.pending_delete = None
            if key in {"n", "escape"}:
                event.prevent_default()
                event.stop()
                self.message = "Deletion cancelled"
                self._update_chrome()
                return
            self.message = ""
            self._update_chrome()
        elif self.message and not self.sync_running:
            # A status message lasts until the next key; the hints come back.
            self.message = ""
            self._update_chrome()
        if event.key == "tab" and self._side_visible():
            event.prevent_default()
            event.stop()
            self._toggle_pane()
            return
        # Arrows and j/k share one code path so a keypress moves the cursor no
        # matter which widget currently has focus.
        listing = self.query_one("#side" if self._side_active() else "#items", ListView)
        if event.key in {"j", "down"}:
            listing.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif event.key in {"k", "up"}:
            listing.action_cursor_up()
            event.prevent_default()
            event.stop()

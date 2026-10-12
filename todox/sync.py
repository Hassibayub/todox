"""Two-way reconciliation between TODO.md files and the dedicated TickTick list.

Rules:
* Only tasks this tool has linked are updated on either side.
* A change on exactly one side is propagated; a change on both sides pauses as a
  conflict until the user picks a side.
* Local deletions never delete remote tasks unless ``allow_deletes`` is set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import tempfile
import uuid

from .projects import Project
from .store import Document, Task, main_section, read, save
from .ticktick import IN_PROGRESS_TAG, LIST_NAME, TickTick, TickTickError


def state_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "todox"
    return Path(os.environ.get("TODOX_SYNC_STATE", root / "sync.json"))


def new_uid() -> str:
    return uuid.uuid4().hex[:12]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def local_signature(task: Task) -> str:
    """Completion dates stay local; only status and text define a change."""
    return _digest(task.status, task.text)


def remote_status(task: dict[str, Any]) -> str:
    if task.get("status") == 2 or task.get("completedTime"):
        return "x"
    return "/" if IN_PROGRESS_TAG in (task.get("tags") or []) else " "


def remote_completed(task: dict[str, Any]) -> str | None:
    value = str(task.get("completedTime") or "")
    return value[:10] if len(value) >= 10 and value[4] == "-" else None


def remote_signature(task: dict[str, Any]) -> str:
    """TickTick's own completion timestamp never drives a local rewrite."""
    return _digest(remote_status(task), str(task.get("title", "")))


def _same_title(task: Task, remote: dict[str, Any]) -> bool:
    return task.text.strip().casefold() == str(remote.get("title", "")).strip().casefold()


@dataclass
class Entry:
    ticktick_id: str
    list_id: str
    local: str
    remote: str
    section: str
    project: str
    synced_at: str = ""


@dataclass
class State:
    list_id: str = ""
    tasks: dict[str, Entry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "State":
        path = path or state_path()
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise TickTickError(f"Invalid sync state: {path}")
        tasks = {}
        for uid, item in (data.get("tasks") or {}).items():
            tasks[str(uid)] = Entry(str(item["ticktickId"]), str(item.get("listId", "")), str(item.get("local", "")),
                                    str(item.get("remote", "")), str(item.get("section", "")), str(item.get("project", "")),
                                    str(item.get("syncedAt", "")))
        return cls(str(data.get("listId", "")), tasks)

    def save(self, path: Path | None = None) -> None:
        path = path or state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".sync.", suffix=".tmp")
        try:
            os.fchmod(handle, 0o600)
            payload = {"version": 1, "listId": self.list_id,
                       "tasks": {uid: {"ticktickId": e.ticktick_id, "listId": e.list_id, "local": e.local,
                                       "remote": e.remote, "section": e.section, "project": e.project,
                                       "syncedAt": e.synced_at} for uid, e in self.tasks.items()}}
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
            os.replace(temp, path)
            temp = ""
        finally:
            if temp:
                os.unlink(temp)


@dataclass
class Change:
    action: str  # create | update | pull | import | delete | forget | conflict | skip
    uid: str
    text: str
    name: str = ""
    project: str = ""
    section: str = ""
    status: str = " "
    completed: str | None = None
    remote_id: str = ""
    remote: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    error: str = ""

    @property
    def writes(self) -> bool:
        return self.action in {"create", "update", "pull", "import", "delete", "forget"}


@dataclass
class Plan:
    list_id: str = ""
    list_name: str = LIST_NAME
    changes: list[Change] = field(default_factory=list)

    @property
    def pending(self) -> list[Change]:
        return [c for c in self.changes if c.action not in {"skip", "conflict"}]

    def summary(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for change in self.changes:
            key = f"{change.action}:error" if change.error else change.action
            tally[key] = tally.get(key, 0) + 1
        return tally

    def describe(self) -> list[str]:
        lines = []
        for change in self.changes:
            label = change.action if change.action not in {"skip", "conflict"} else "paused"
            detail = change.reason or change.error
            lines.append(f"{label:<7} {change.name or change.project} · {change.text}{f' — {detail}' if detail else ''}")
        return lines or ["Already in sync."]


def _section_title(doc: Document, line: int) -> str:
    return next((s.title for s in doc.sections if s.line == line), "Main")


def _section_line(doc: Document, title: str) -> int:
    match = next((s for s in doc.sections if s.title == title), None)
    return match.line if match else main_section(doc).line


def _find_uid(doc: Document, uid: str) -> Task | None:
    return next((t for t in doc.tasks if t.uid == uid), None)


def _find_unlinked(doc: Document, change: Change) -> Task | None:
    return next((t for t in doc.tasks
                 if not t.uid and t.text == change.text and t.status == change.status
                 and _section_title(doc, t.section) == change.section), None)


def _project_name(project: Project) -> str:
    return project.name


class Sync:
    """Plans and applies two-way changes for a set of registered projects."""

    def __init__(self, client: TickTick, state: State | None = None, path: Path | None = None):
        self.client = client
        self.path = path
        self.state = state if state is not None else State.load(path)

    # ----- planning -------------------------------------------------------
    def plan(self, projects: list[Project], allow_deletes: bool = False) -> Plan:
        list_id, list_name = self.client.ensure_list(self.state.list_id)
        remote = [t for t in self.client.project_tasks(list_id) if isinstance(t, dict) and t.get("id")]
        remote_by_id = {str(t["id"]): t for t in remote}
        claimed = {str(e.ticktick_id) for e in self.state.tasks.values()}
        plan = Plan(list_id, list_name)
        local: dict[str, tuple[Project, Task]] = {}
        for project in projects:
            if not project.doc:
                continue
            for task in project.doc.tasks:
                section = _section_title(project.doc, task.section)
                if not task.uid:
                    plan.changes.append(Change("create", new_uid(), task.text, _project_name(project), str(project.path),
                                               section, task.status, task.completed))
                    continue
                if task.uid in local:
                    plan.changes.append(Change("conflict", task.uid, task.text, _project_name(project), str(project.path),
                                               section, reason="the same task id appears in more than one file"))
                    continue
                local[task.uid] = (project, task)
        for uid, (project, task) in local.items():
            entry = self.state.tasks.get(uid)
            section = _section_title(project.doc, task.section)
            if entry is None:
                matches = [r for r in remote if _same_title(task, r) and str(r["id"]) not in claimed]
                if len(matches) == 1:
                    claimed.add(str(matches[0]["id"]))
                    plan.changes.append(Change("conflict", uid, task.text, _project_name(project), str(project.path),
                                               section, task.status, task.completed, str(matches[0]["id"]), matches[0],
                                               reason="id not tracked yet and TickTick has this title; choose a side"))
                elif not matches:
                    plan.changes.append(Change("create", uid, task.text, _project_name(project), str(project.path),
                                               section, task.status, task.completed))
                else:
                    plan.changes.append(Change("conflict", uid, task.text, _project_name(project), str(project.path),
                                               section, task.status, task.completed,
                                               reason="several TickTick tasks share this title"))
                continue
            claimed.add(entry.ticktick_id)
            change = Change("skip", uid, task.text, _project_name(project), str(project.path), section,
                            task.status, task.completed, entry.ticktick_id)
            remote_task = remote_by_id.get(entry.ticktick_id)
            if remote_task is None:
                change.action = "conflict"
                change.reason = "not in the TODO.md list any more (deleted or abandoned in TickTick)"
                plan.changes.append(change)
                continue
            change.remote = remote_task
            local_now, remote_now = local_signature(task), remote_signature(remote_task)
            if local_now == entry.local and remote_now == entry.remote:
                entry.section = section
                continue
            if local_now != entry.local and remote_now == entry.remote:
                change.action = "update"
                change.status, change.text = task.status, task.text
            elif local_now == entry.local and remote_now != entry.remote:
                change.action = "pull"
                change.status, change.text = remote_status(remote_task), str(remote_task.get("title", ""))
                change.completed = remote_completed(remote_task)
            elif local_now == remote_now:
                change.action = "forget"
                change.reason = "both sides already match"
            else:
                change.action = "conflict"
                change.reason = "changed in TODO.md and TickTick"
            plan.changes.append(change)
        for uid, entry in self.state.tasks.items():
            if uid in local:
                continue
            remote_task = remote_by_id.get(entry.ticktick_id)
            if remote_task is None:
                plan.changes.append(Change("forget", uid, "", "", entry.project, entry.section,
                                           reason="no longer present in TODO.md or TickTick"))
            elif allow_deletes:
                plan.changes.append(Change("delete", uid, str(remote_task.get("title", "")), "", entry.project,
                                           entry.section, remote_id=entry.ticktick_id, remote=remote_task))
            else:
                plan.changes.append(Change("skip", uid, str(remote_task.get("title", "")), "", entry.project,
                                           entry.section, remote_id=entry.ticktick_id, remote=remote_task,
                                           reason="deleted locally; TickTick copy kept (use --allow-deletes)"))
        for remote_task in remote:
            remote_id = str(remote_task["id"])
            if remote_id in claimed:
                continue
            tags = [str(tag) for tag in (remote_task.get("tags") or [])]
            project = next((p for p in projects if p.doc and p.name.strip().casefold() in {t.casefold() for t in tags}), None)
            if project is None:
                plan.changes.append(Change("skip", "", str(remote_task.get("title", "")), "",
                                           reason="no local project matches tags " + (", ".join(tags) or "(none)")))
                continue
            plan.changes.append(Change("import", new_uid(), str(remote_task.get("title", "")), _project_name(project),
                                       str(project.path), "Main", remote_status(remote_task), remote_completed(remote_task),
                                       remote_id, remote_task))
        return plan

    # ----- applying -------------------------------------------------------
    def apply(self, plan: Plan) -> Plan:
        self.state.list_id = plan.list_id
        for change in plan.changes:
            if change.action in {"skip", "conflict"}:
                continue
            try:
                if change.action == "create":
                    self._push_create(plan, change)
                elif change.action == "update":
                    self._push_update(plan, change)
                elif change.action == "pull":
                    self._pull(plan, change)
                elif change.action == "import":
                    self._import(plan, change)
                elif change.action == "delete":
                    self.client.delete_task(plan.list_id, change.remote_id)
                    self.state.tasks.pop(change.uid, None)
                elif change.action == "forget":
                    self.state.tasks.pop(change.uid, None)
            except (OSError, ValueError, TickTickError) as exc:
                change.error = str(exc)
        self.state.save(self.path)
        return plan

    def resolve(self, projects: list[Project], uid: str, keep: str) -> Change:
        """Force one conflicted task to the chosen side, then re-plan in the caller."""
        plan = self.plan(projects)
        change = next((c for c in plan.changes if c.uid == uid), None)
        if change is None:
            raise TickTickError(f"No pending change for id {uid}.")
        if change.action not in {"conflict", "skip", "update", "pull"}:
            raise TickTickError(f"Task {uid} has nothing to resolve ({change.action}).")
        if keep == "local":
            change.action = "update"
            if not change.remote_id:
                entry = self.state.tasks.get(uid)
                change.remote_id = entry.ticktick_id if entry else ""
            change.reason = ""
        elif keep == "remote":
            change.action = "pull" if _find_uid(read(Path(change.project)), uid) else "import"
            change.reason = ""
        else:
            raise TickTickError("Choose --keep-local or --keep-remote.")
        self.apply(Plan(plan.list_id, plan.list_name, [change]))
        return change

    # ----- single-task operations ----------------------------------------
    def _stamp(self, change: Change) -> tuple[Document, Task]:
        path = Path(change.project)
        doc = read(path)
        task = _find_unlinked(doc, change)
        if task is None:
            raise ValueError("That task changed or moved while syncing. Run sync again.")
        return save(doc, "uid", line=task.line, uid=change.uid), task

    def _push_create(self, plan: Plan, change: Change) -> None:
        doc, task = self._stamp(change)
        tag = self.client.ensure_tag(change.name)
        tags = [tag] + ([self.client.ensure_tag(IN_PROGRESS_TAG)] if task.status == "/" else [])
        created = self.client.create_task({"title": task.text, "projectId": plan.list_id, "tags": tags, "status": 0})
        remote_id = str(created.get("id", ""))
        if not remote_id:
            raise TickTickError("TickTick did not return an id for the new task.")
        if task.status == "x":
            self.client.complete_task(plan.list_id, remote_id)
        stamped = read(doc.path).tasks
        fresh = next((t for t in stamped if t.uid == change.uid), task)
        remote_now = {"id": remote_id, "title": fresh.text, "status": 2 if fresh.status == "x" else 0, "tags": tags,
                      "completedTime": f"{(fresh.completed or _today())}T00:00:00+0000" if fresh.status == "x" else ""}
        self._remember(change.uid, remote_id, plan, fresh, remote_now, _section_title(doc, task.section), change.project)

    def _push_update(self, plan: Plan, change: Change) -> None:
        doc = read(Path(change.project))
        task = _find_uid(doc, change.uid)
        if task is None:
            raise ValueError("That task changed or moved while syncing. Run sync again.")
        tag = self.client.ensure_tag(change.name)
        tags = [tag] + ([self.client.ensure_tag(IN_PROGRESS_TAG)] if task.status == "/" else [])
        payload = {"id": change.remote_id, "projectId": plan.list_id, "title": task.text,
                   "tags": tags, "status": 2 if task.status == "x" else 0}
        updated = self.client.update_task(change.remote_id, payload) or {}
        if task.status == "x" and remote_status(updated) != "x":
            self.client.complete_task(plan.list_id, change.remote_id)
        remote_now = {"id": change.remote_id, "title": task.text, "status": 2 if task.status == "x" else 0, "tags": tags,
                      "completedTime": remote_completed(updated) or (f"{(task.completed or _today())}T00:00:00+0000" if task.status == "x" else "")}
        self._remember(change.uid, change.remote_id, plan, task, remote_now, _section_title(doc, task.section), change.project)

    def _pull(self, plan: Plan, change: Change) -> None:
        path = Path(change.project)
        remote = change.remote or {}
        title = str(remote.get("title", change.text))
        want_status, want_completed = remote_status(remote), remote_completed(remote)
        doc = read(path)
        task = _find_uid(doc, change.uid)
        if task is None:
            doc, task = self._append(plan, change, title, want_status, want_completed, change.uid)
        else:
            if task.text != title:
                doc = save(doc, "edit", line=task.line, value=title)
                task = _find_uid(doc, change.uid) or task
            if task.status != want_status:
                doc = save(doc, "status", line=task.line, status=want_status, completed=want_completed)
        fresh = _find_uid(doc, change.uid)
        assert fresh is not None
        self._remember(change.uid, change.remote_id, plan, fresh, remote or {"id": change.remote_id, "title": title,
                      "status": 2 if want_status == "x" else 0, "tags": [], "completedTime": ""},
                      _section_title(doc, fresh.section), change.project)

    def _import(self, plan: Plan, change: Change) -> None:
        remote = change.remote
        doc, task = self._append(plan, change, change.text, remote_status(remote), remote_completed(remote), change.uid)
        self._remember(change.uid, change.remote_id, plan, task, remote, _section_title(doc, task.section), change.project)

    def _append(self, plan: Plan, change: Change, title: str, status: str,
                completed: str | None, uid: str) -> tuple[Document, Task]:
        path = Path(change.project)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# TODO\n", encoding="utf-8")
        doc = read(path)
        section = _section_line(doc, change.section) if change.section else main_section(doc).line
        doc = save(doc, "add", section=section, value=title, status=status, completed=completed, uid=uid)
        task = _find_uid(doc, uid)
        if task is None:
            raise ValueError("The new task could not be written to TODO.md.")
        return doc, task

    def _remember(self, uid: str, remote_id: str, plan: Plan, task: Task, remote: dict[str, Any], section: str, project: str) -> None:
        self.state.list_id = plan.list_id
        self.state.tasks[uid] = Entry(remote_id, plan.list_id, local_signature(task), remote_signature(remote),
                                      section, project, datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def delete_remote(self, uid: str, plan: Plan) -> None:
        entry = self.state.tasks.get(uid)
        if entry is None:
            raise TickTickError(f"No synced task with id {uid}.")
        self.client.delete_task(plan.list_id or self.state.list_id, entry.ticktick_id)
        self.state.tasks.pop(uid, None)
        self.state.save(self.path)

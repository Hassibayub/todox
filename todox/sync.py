"""Two-way reconciliation using project tags across TickTick lists and Inbox.

Rules:
* Only tasks this tool has linked are updated on either side.
* A change on exactly one side is propagated; a change on both sides pauses as a
  conflict until the user picks a side.
* Local deletions never delete remote tasks unless ``allow_deletes`` is set.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import hashlib
import fcntl
import json
import os
import tempfile
import uuid

from .db import Database, config_root, database_path, legacy_state_path, registry_key
from .projects import Project, config_path, load_config
from .store import Document, Task, main_section, read, save
from .ticktick import IN_PROGRESS_TAG, LIST_NAME, TickTick, TickTickError


def state_path() -> Path:
    return database_path()


def _legacy_source(path: Path | None, registry: Path | None) -> Path | None:
    if path is None:
        if registry is not None and registry.resolve() != config_path():
            return None
        if "TODOX_SYNC_STATE" not in os.environ and (
                registry_key(registry) != str((config_root() / "projects.json").resolve()) or os.environ.get("TODOX_DB")):
            return None
    return legacy_state_path(path)


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
    retired: bool = False
    local_snapshot: dict[str, Any] = field(default_factory=dict)
    remote_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class State:
    list_id: str = ""
    tasks: dict[str, Entry] = field(default_factory=dict)
    active_only: bool = False

    @classmethod
    def load(cls, path: Path | None = None, registry: Path | None = None) -> "State":
        data = Database(path, registry).load_state()
        if data is None:
            legacy = _legacy_source(path, registry)
            if legacy is None or not legacy.exists():
                return cls()
            data = json.loads(legacy.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise TickTickError(f"Invalid sync state: {path}")
        tasks = {}
        for uid, item in (data.get("tasks") or {}).items():
            tasks[str(uid)] = Entry(str(item["ticktickId"]), str(item.get("listId", "")), str(item.get("local", "")),
                                    str(item.get("remote", "")), str(item.get("section", "")), str(item.get("project", "")),
                                    str(item.get("syncedAt", "")), bool(item.get("retired", False)),
                                    item.get("localSnapshot", {}), item.get("remoteSnapshot", {}))
        return cls(str(data.get("listId", "")), tasks, bool(data.get("activeOnly", False)))

    def payload(self) -> dict[str, Any]:
        return {"version": 1, "listId": self.list_id, "activeOnly": self.active_only,
                "tasks": {uid: {"ticktickId": e.ticktick_id, "listId": e.list_id, "local": e.local,
                                "remote": e.remote, "section": e.section, "project": e.project,
                                "syncedAt": e.synced_at, "retired": e.retired,
                                "localSnapshot": e.local_snapshot, "remoteSnapshot": e.remote_snapshot}
                          for uid, e in self.tasks.items()}}

    def save(self, path: Path | None = None, registry: Path | None = None, operation: str = "", remote_id: str = "",
             complete: bool = True) -> None:
        Database(path, registry).save_state(self.payload(), operation, remote_id, complete)


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
    local_before: dict[str, str] = field(default_factory=dict)

    @property
    def writes(self) -> bool:
        return self.action in {"create", "update", "pull", "import", "delete", "forget", "retire", "remember", "merge"}


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

    def project_summary(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for change in self.changes:
            tally = result.setdefault(change.name or change.project or "(unmatched)", {})
            key = f"{change.action}:error" if change.error else change.action
            tally[key] = tally.get(key, 0) + 1
        return result


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

    def __init__(self, client: TickTick, state: State | None = None, path: Path | None = None, registry: Path | None = None):
        self.client = client
        self.path = path
        self.registry = registry
        self.database = Database(path, registry)
        self.state = state if state is not None else State.load(path, registry)
        self._operation = ""
        self._create_sent = False
        self.applied = False
        self._revision = self._disk_revision()

    def _disk_revision(self) -> str:
        legacy = _legacy_source(self.path, self.registry)
        digest = hashlib.sha256(legacy.read_bytes()).hexdigest() if legacy and legacy.exists() else ""
        return f"{self.database.revision()}:{digest}"

    # ----- planning -------------------------------------------------------
    def plan(self, projects: list[Project], allow_deletes: bool = False, cleanup_temp: bool = False) -> Plan:
        list_id, list_name = self.client.find_list(self.state.list_id)
        # Search tags globally, but also read linked lists so removing a tag
        # cannot make an already-linked task appear deleted.
        remote_by_id = {str(t["id"]): t for t in self.client.tagged_tasks(
            [p.name.strip().lower()[:64] for p in projects if p.name.strip() and p.sync_enabled])}
        linked_lists = {e.list_id for e in self.state.tasks.values() if e.list_id and not e.retired}
        if list_id:
            linked_lists.add(list_id)
        for source in sorted(linked_lists):
            for task in self.client.project_tasks(source):
                remote_by_id.setdefault(str(task["id"]), task)
        remote = list(remote_by_id.values())
        claimed = {str(e.ticktick_id) for e in self.state.tasks.values()}
        plan = Plan(list_id, list_name)
        local: dict[str, tuple[Project, Task]] = {}
        unavailable = {str(p.path) for p in projects if not p.doc or not p.sync_enabled}
        registered = {str(p.path) for p in projects}
        tag_counts: dict[str, int] = {}
        for p in projects:
            name = p.name.strip().lower()[:64]
            tag_counts[name] = tag_counts.get(name, 0) + 1
        blocked = {str(p.path) for p in projects if not p.name.strip() or
                   p.name.strip().lower()[:64] == IN_PROGRESS_TAG or tag_counts[p.name.strip().lower()[:64]] > 1}
        unavailable.update(blocked)
        ambiguous_remote = {str(t["id"]) for t in remote if sum(
            p.name.strip().lower()[:64] in {str(tag).lower() for tag in t.get("tags", [])}
            for p in projects if str(p.path) not in blocked) > 1}
        uid_counts: dict[str, int] = {}
        for p in projects:
            for t in p.doc.tasks if p.doc else []:
                if t.uid:
                    uid_counts[t.uid] = uid_counts.get(t.uid, 0) + 1
        duplicate_uids = {uid for uid, count in uid_counts.items() if count > 1}
        for project in projects:
            if not project.sync_enabled:
                continue
            if str(project.path) in blocked:
                plan.changes.append(Change("conflict", "", "", project.name, str(project.path),
                                           reason="project tag is empty, reserved, or shared by multiple projects; rename it first"))
                continue
            if not project.doc:
                continue
            for task in project.doc.tasks:
                section = _section_title(project.doc, task.section)
                if task.uid in duplicate_uids:
                    plan.changes.append(Change("conflict", task.uid, task.text, project.name, str(project.path), section,
                                               reason="the same task id appears more than once; neither copy will be synced"))
                    continue
                if not task.uid:
                    if task.status == "x":
                        continue  # Historical completed tasks are local-only.
                    matches = [r for r in remote if _same_title(task, r) and str(r["id"]) not in claimed
                               and project.name.strip().lower()[:64] in (r.get("tags") or [])]
                    if matches:
                        claimed.update(str(r["id"]) for r in matches)
                        match = matches[0] if len(matches) == 1 else {}
                        plan.changes.append(Change("conflict", _digest(str(project.path), str(task.line), task.text),
                                                   task.text, project.name, str(project.path), section,
                                                   task.status, task.completed, str(match.get("id", "")), match,
                                                   reason="unlinked task matches TickTick; choose a side"))
                        continue
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
                if task.status == "x":
                    continue
                matches = [r for r in remote if _same_title(task, r) and str(r["id"]) not in claimed
                           and project.name.strip().lower()[:64] in (r.get("tags") or [])]
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
            change.local_before = {"text": task.text, "status": task.status}
            remote_task = remote_by_id.get(entry.ticktick_id)
            if remote_task and project.name.strip().lower()[:64] not in {str(t).lower() for t in remote_task.get("tags", [])}:
                change.reason = "project tag was removed; linking paused without deleting either copy"
                plan.changes.append(change)
                continue
            if entry.ticktick_id in ambiguous_remote:
                change.action = "conflict"
                change.reason = "task tags match multiple local projects; keep exactly one project tag"
                plan.changes.append(change)
                continue
            if entry.retired:
                if task.status != "x":
                    change.action = "create"
                    plan.changes.append(change)
                continue
            if task.status == "x" and (remote_task is None or
                                      (remote_status(remote_task) == "x" and remote_task.get("title") == task.text)):
                change.action = "retire" if remote_task is None or allow_deletes else "skip"
                change.remote = remote_task or {}
                change.reason = ("delete completed TickTick copy; local history retained" if change.action == "retire" else
                                 "completed locally; approve remote cleanup with --allow-deletes")
                plan.changes.append(change)
                continue
            if remote_task is None:
                change.action = "conflict"
                change.reason = "linked task not found in TickTick (deleted or no longer accessible)"
                plan.changes.append(change)
                continue
            change.remote = remote_task
            local_now, remote_now = local_signature(task), remote_signature(remote_task)
            if local_now == entry.local and remote_now == entry.remote:
                if str(remote_task.get("projectId", entry.list_id)) != entry.list_id:
                    change.action = "remember"
                    change.reason = "task moved to another TickTick list; keep its new location"
                    plan.changes.append(change)
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
                change.action = "remember"
                change.reason = "both sides already match"
            elif entry.local_snapshot and entry.remote_snapshot:
                local_values = {"text": task.text, "status": task.status}
                remote_values = {"text": str(remote_task.get("title", "")), "status": remote_status(remote_task)}
                base_local = entry.local_snapshot
                base_remote = {"text": entry.remote_snapshot.get("title"), "status": entry.remote_snapshot.get("status")}
                local_fields = {key for key in local_values if local_values[key] != base_local.get(key)}
                remote_fields = {key for key in remote_values if remote_values[key] != base_remote.get(key)}
                if any(local_values[key] != remote_values[key] for key in local_fields & remote_fields):
                    change.action = "conflict"
                    change.reason = "same field changed differently in TODO.md and TickTick"
                else:
                    merged = dict(local_values)
                    merged.update({key: remote_values[key] for key in remote_fields})
                    change.action = "merge"
                    change.text, change.status = merged["text"], merged["status"]
                    if "status" in remote_fields:
                        change.completed = remote_completed(remote_task)
                    change.reason = "independent field edits merged"
            else:
                change.action = "conflict"
                change.reason = "changed in TODO.md and TickTick"
            plan.changes.append(change)
        for uid, entry in self.state.tasks.items():
            if uid in local:
                continue
            if uid in duplicate_uids:
                continue
            if entry.project in unavailable:
                plan.changes.append(Change("skip", uid, "", project=entry.project,
                                           reason="project file could not be read; no deletion planned"))
                continue
            if entry.project not in registered and not cleanup_temp:
                plan.changes.append(Change("skip", uid, "", project=entry.project,
                                           reason="project is unregistered; remote task left untouched"))
                continue
            remote_task = remote_by_id.get(entry.ticktick_id)
            if cleanup_temp and entry.project not in registered:
                candidate = Path(entry.project)
                temp_root = Path(tempfile.gettempdir()).resolve()
                if candidate.resolve().is_relative_to(temp_root) and any(part.startswith("tmp") for part in candidate.parts):
                    action = "delete" if remote_task and allow_deletes else "forget" if not remote_task else "skip"
                    plan.changes.append(Change(action, uid, str((remote_task or {}).get("title", "")),
                                               project=entry.project, section=entry.section,
                                               remote_id=entry.ticktick_id, remote=remote_task or {},
                                               reason="temporary workspace pollution; local file is never deleted"))
                    continue
            if cleanup_temp:
                plan.changes.append(Change("skip", uid, "", project=entry.project,
                                           reason="not confirmed temporary pollution; left untouched"))
                continue
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
            matches = [p for p in projects if p.doc and p.sync_enabled and str(p.path) not in blocked
                       and p.name.strip().lower()[:64] in {t.lower() for t in tags}]
            if len(matches) > 1:
                plan.changes.append(Change("conflict", "", str(remote_task.get("title", "")),
                                           remote_id=remote_id, remote=remote_task,
                                           reason="task tags match multiple local projects; keep exactly one project tag"))
                continue
            project = matches[0] if matches else None
            if project is None:
                plan.changes.append(Change("skip", "", str(remote_task.get("title", "")), "",
                                           reason="no local project matches tags " + (", ".join(tags) or "(none)")))
                continue
            if any(_same_title(t, remote_task) for t in project.doc.tasks):
                plan.changes.append(Change("conflict", "", str(remote_task.get("title", "")), project.name,
                                           str(project.path), remote_id=remote_id, remote=remote_task,
                                           reason="untracked remote task duplicates a local title; inspect before importing"))
                continue
            plan.changes.append(Change("import", new_uid(), str(remote_task.get("title", "")), _project_name(project),
                                       str(project.path), "Main", remote_status(remote_task), remote_completed(remote_task),
                                       remote_id, remote_task))
        # An upload with an unknown outcome must not be sent again automatically.
        pending = {op["uid"]: op for op in self.database.operations(pending=True) if op["action"] == "create"}
        for change in plan.changes:
            op = pending.get(change.uid)
            if op and change.action == "create":
                change.action = "conflict"
                change.reason = "previous upload outcome is uncertain; run todo sync recover before retrying"
        return plan

    # ----- applying -------------------------------------------------------
    def _remote_list(self, plan: Plan, change: Change) -> str:
        entry = self.state.tasks.get(change.uid)
        list_id = change.remote.get("projectId") or (entry.list_id if entry else "") or plan.list_id
        if not list_id:
            raise TickTickError("Cannot update a remote task without its TickTick list id.")
        return str(list_id)

    def apply(self, plan: Plan) -> Plan:
        """Serialize writers and reject plans based on state changed by another process."""
        path = self.database.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with Path(str(path) + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TickTickError("Another sync is running. Try again after it finishes.") from exc
            if self._disk_revision() != self._revision:
                raise TickTickError("Sync state changed since preview. Refresh and preview again.")
            try:
                result = self._apply(plan)
                self.applied = True
                return result
            finally:
                self._revision = self._disk_revision()

    def _apply(self, plan: Plan) -> Plan:
        if not self.state.active_only or self.database.load_state() is None:
            self._backup_migration(plan)
        if self.database.load_projects() is None or self.database.load_state() is None:
            self.database.migrate_data([{"name": p.name, "path": str(p.path), "sync_enabled": p.sync_enabled}
                                        for p in load_config(self.registry)], self.state.payload())
        self.state.save(self.path, self.registry)
        run_id = self.database.begin_run([asdict(c) for c in plan.changes if c.action == "conflict"])
        if not plan.list_id and any(c.action == "create" and not (
                (entry := self.state.tasks.get(c.uid)) and entry.retired and entry.list_id) for c in plan.changes):
            plan.list_id, plan.list_name = self.client.ensure_list()
        self.state.list_id = plan.list_id
        self.state.active_only = True
        for change in plan.changes:
            if change.action in {"skip", "conflict"}:
                continue
            self._operation = self.database.begin_operation(run_id, asdict(change))
            self._create_sent = False
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
                    self.client.delete_task(self._remote_list(plan, change), change.remote_id)
                    self.state.tasks.pop(change.uid, None)
                elif change.action == "forget":
                    self.state.tasks.pop(change.uid, None)
                elif change.action == "remember":
                    self._pull(plan, change)
                elif change.action == "merge":
                    doc = read(Path(change.project))
                    task = _find_uid(doc, change.uid)
                    if task is None or change.local_before != {"text": task.text, "status": task.status}:
                        raise TickTickError("Local task changed since preview; merge paused.")
                    if task.text != change.text:
                        doc = save(doc, "edit", line=task.line, value=change.text)
                        task = _find_uid(doc, change.uid)
                    if task.status != change.status:
                        doc = save(doc, "status", line=task.line, status=change.status, completed=change.completed)
                    self._push_update(plan, replace(change, local_before={"text": change.text, "status": change.status}))
                elif change.action == "retire":
                    task = _find_uid(read(Path(change.project)), change.uid)
                    if task is None or task.status != "x" or task.text != change.text:
                        raise TickTickError("Completed task changed since preview. Refresh before cleanup.")
                    if change.remote:
                        self.client.delete_task(self._remote_list(plan, change), change.remote_id)
                    self.state.tasks[change.uid].retired = True
                entry = self.state.tasks.get(change.uid)
                self.state.save(self.path, self.registry, self._operation,
                                entry.ticktick_id if entry else change.remote_id)
            except (OSError, ValueError, TickTickError) as exc:
                change.error = str(exc)
                linked = self.state.tasks.get(change.uid)
                uncertain = self._create_sent and (linked is None or linked.retired) and (
                    "Cannot reach TickTick" in str(exc) or "return an id" in str(exc)
                    or isinstance(exc, (OSError, json.JSONDecodeError)))
                self.database.fail_operation(self._operation, str(exc), uncertain)
            finally:
                self._operation = ""
        self.state.save(self.path, self.registry)
        self.database.finish_run(run_id, plan.summary(), any(c.error for c in plan.changes))
        return plan

    def _backup_migration(self, plan: Plan) -> None:
        """Keep a private local snapshot before the first active-only write."""
        state_file = self.path or state_path()
        root = state_file.parent / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + new_uid())
        root.mkdir(parents=True, mode=0o700)
        sources = {str(state_file), str(self.database.path), str(self.registry or config_path())}
        legacy = _legacy_source(self.path, self.registry)
        if legacy:
            sources.add(str(legacy))
        sources.update(e.project for e in self.state.tasks.values())
        sources.update(c.project for c in plan.changes if c.project)
        manifest = {}
        for source in sorted(sources):
            path = Path(source)
            if not path.is_file():
                continue
            name = _digest(str(path)) + "-" + path.name
            target = root / name
            if path == self.database.path:
                self.database.backup(target)
            else:
                target.write_bytes(path.read_bytes())
            target.chmod(0o600)
            manifest[name] = str(path)
        index = root / "manifest.json"
        index.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        index.chmod(0o600)

    def migrate(self) -> dict[str, Any]:
        """Migrate bookkeeping only; never contact TickTick or rewrite Markdown."""
        path = self.database.path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with Path(str(path) + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self._disk_revision() != self._revision:
                raise TickTickError("State changed; retry migration.")
            if self.database.load_state() is None or self.database.load_projects() is None:
                projects = load_config(self.registry)
                self._backup_migration(Plan(changes=[Change("skip", "", "", project=str(p.path)) for p in projects]))
                self.database.migrate_data([{"name": p.name, "path": str(p.path), "sync_enabled": p.sync_enabled}
                                            for p in projects], self.state.payload())
            self._revision = self._disk_revision()
        return self.database.status()

    def recover(self) -> list[dict[str, str]]:
        """Recover provable outcomes without issuing remote writes or guessing identities."""
        path = self.database.path
        if not path.exists():
            return []
        result = []
        with Path(str(path) + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self._disk_revision() != self._revision:
                raise TickTickError("State changed; refresh before recovery.")
            for operation in self.database.operations(pending=True):
                payload = operation["payload"]
                uid = operation["uid"]
                entry = self.state.tasks.get(uid)
                remote_id = operation["remote_id"] or payload.get("remote_id", "")
                if entry and not entry.retired:
                    # A saved link makes retrying reconciliation safe: creation
                    # will not be blindly repeated even if completion failed.
                    self.database.recover_operation(operation["id"], "failed")
                    result.append({"id": operation["id"], "status": "retryable", "reason": "remote link already checkpointed"})
                elif operation["action"] == "import" and remote_id:
                    doc = read(Path(payload["project"]))
                    task = _find_uid(doc, uid)
                    if task is not None:
                        remote = payload["remote"]
                        baseline = Task(task.line, payload["text"], payload["status"], task.section, payload.get("completed"), uid)
                        self._remember(uid, remote_id, Plan(), baseline, remote, payload["section"], payload["project"])
                        self.state.save(self.path, self.registry, operation["id"], remote_id)
                        result.append({"id": operation["id"], "status": "recovered", "reason": "imported uid exists; link restored"})
                        continue
                    self.database.recover_operation(operation["id"], "failed")
                    result.append({"id": operation["id"], "status": "retryable", "reason": "no imported local uid; import may be retried"})
                elif operation["action"] != "create":
                    self.database.recover_operation(operation["id"], "failed")
                    result.append({"id": operation["id"], "status": "retryable", "reason": "replan before replaying; no remote write made"})
                else:
                    result.append({"id": operation["id"], "status": "needs-review",
                                   "reason": "upload outcome unknown; verify remote identity before any retry"})
            self._revision = self._disk_revision()
        return result

    def resolve(self, projects: list[Project], uid: str, keep: str) -> Change:
        """Force one conflicted task to the chosen side, then re-plan in the caller."""
        plan = self.plan(projects)
        change = next((c for c in plan.changes if c.uid == uid), None)
        if change is None:
            raise TickTickError(f"No pending change for id {uid}.")
        if change.action not in {"conflict", "skip", "update", "pull"}:
            raise TickTickError(f"Task {uid} has nothing to resolve ({change.action}).")
        if not change.remote_id or not change.remote:
            raise TickTickError("No unambiguous remote task to resolve. Review the task ids first.")
        existing = _find_uid(read(Path(change.project)), uid)
        if existing:
            change.local_before = {"text": existing.text, "status": existing.status}
        if keep == "local":
            change.action = "update"
            if not change.remote_id:
                entry = self.state.tasks.get(uid)
                change.remote_id = entry.ticktick_id if entry else ""
            change.reason = ""
        elif keep == "remote":
            doc = read(Path(change.project))
            change.action = "pull" if _find_uid(doc, uid) or _find_unlinked(doc, change) else "import"
            change.reason = ""
        else:
            raise TickTickError("Choose --keep-local or --keep-remote.")
        self.apply(Plan(plan.list_id, plan.list_name, [change]))
        return change

    # ----- single-task operations ----------------------------------------
    def _stamp(self, change: Change) -> tuple[Document, Task]:
        path = Path(change.project)
        doc = read(path)
        existing = _find_uid(doc, change.uid)
        if existing is not None:
            return doc, existing
        task = _find_unlinked(doc, change)
        if task is None:
            raise ValueError("That task changed or moved while syncing. Run sync again.")
        return save(doc, "uid", line=task.line, uid=change.uid), task

    def _push_create(self, plan: Plan, change: Change) -> None:
        doc, task = self._stamp(change)
        entry = self.state.tasks.get(change.uid)
        target_list = entry.list_id if entry and entry.retired and entry.list_id else plan.list_id
        tag = self.client.ensure_tag(change.name)
        tags = [tag] + ([self.client.ensure_tag(IN_PROGRESS_TAG)] if task.status == "/" else [])
        self._create_sent = True
        created = self.client.create_task({"title": task.text, "projectId": target_list, "tags": tags, "status": 0})
        remote_id = str(created.get("id", ""))
        if not remote_id:
            raise TickTickError("TickTick did not return an id for the new task.")
        # Persist the returned id before another network request can fail.
        self._remember(change.uid, remote_id, plan, task,
                       {"title": task.text, "projectId": target_list, "status": 0, "tags": tags},
                       _section_title(doc, task.section), change.project)
        if task.status == "x":
            self.state.tasks[change.uid].local = ""  # Retry completion if it fails.
        self.state.save(self.path, self.registry, self._operation, remote_id, complete=False)
        if task.status == "x":
            self.client.complete_task(target_list, remote_id)
        stamped = read(doc.path).tasks
        fresh = next((t for t in stamped if t.uid == change.uid), task)
        remote_now = {"id": remote_id, "projectId": target_list, "title": fresh.text,
                       "status": 2 if fresh.status == "x" else 0, "tags": tags,
                      "completedTime": f"{(fresh.completed or _today())}T00:00:00+0000" if fresh.status == "x" else ""}
        self._remember(change.uid, remote_id, plan, fresh, remote_now, _section_title(doc, task.section), change.project)

    def _push_update(self, plan: Plan, change: Change) -> None:
        doc = read(Path(change.project))
        task = _find_uid(doc, change.uid)
        if task is None:
            doc, task = self._stamp(change)
        if change.local_before and change.local_before != {"text": task.text, "status": task.status}:
            raise TickTickError("Local task changed since preview; refresh before syncing.")
        tag = self.client.ensure_tag(change.name)
        tags = list(dict.fromkeys([str(t) for t in change.remote.get("tags", []) if t != IN_PROGRESS_TAG] + [tag]))
        if task.status == "/":
            tags.append(self.client.ensure_tag(IN_PROGRESS_TAG))
        remote_list = self._remote_list(plan, change)
        payload = {"id": change.remote_id, "projectId": remote_list, "title": task.text,
                   "tags": tags, "status": 2 if task.status == "x" else 0}
        updated = self.client.update_task(change.remote_id, payload) or {}
        if task.status == "x" and remote_status(updated) != "x":
            self.client.complete_task(remote_list, change.remote_id)
        remote_now = {"id": change.remote_id, "projectId": remote_list, "title": task.text,
                       "status": 2 if task.status == "x" else 0, "tags": tags,
                      "completedTime": remote_completed(updated) or (f"{(task.completed or _today())}T00:00:00+0000" if task.status == "x" else "")}
        self._remember(change.uid, change.remote_id, plan, task, remote_now, _section_title(doc, task.section), change.project)

    def _pull(self, plan: Plan, change: Change) -> None:
        path = Path(change.project)
        remote = change.remote or {}
        title = str(remote.get("title", change.text))
        want_status, want_completed = remote_status(remote), remote_completed(remote)
        doc = read(path)
        task = _find_uid(doc, change.uid)
        if task is None and _find_unlinked(doc, change):
            doc, task = self._stamp(change)
        if task and change.local_before and change.local_before != {"text": task.text, "status": task.status}:
            raise TickTickError("Local task changed since preview; remote edit was not applied.")
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
        doc = read(Path(change.project))
        task = _find_uid(doc, change.uid)
        if task is None:
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
        self.state.tasks[uid] = Entry(remote_id, str(remote.get("projectId") or plan.list_id),
                                      local_signature(task), remote_signature(remote),
                                      section, project, datetime.now(timezone.utc).isoformat(timespec="seconds"), False,
                                      {"text": task.text, "status": task.status, "completed": task.completed},
                                      {"title": remote.get("title", ""), "status": remote_status(remote),
                                       "tags": remote.get("tags", []), "projectId": remote.get("projectId", plan.list_id)})

    def delete_remote(self, uid: str, plan: Plan) -> None:
        entry = self.state.tasks.get(uid)
        if entry is None:
            raise TickTickError(f"No synced task with id {uid}.")
        self.client.delete_task(entry.list_id or plan.list_id or self.state.list_id, entry.ticktick_id)
        self.state.tasks.pop(uid, None)
        self.state.save(self.path, self.registry)

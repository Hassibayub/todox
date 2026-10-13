"""TickTick Open API client with an injectable transport.

Auth is a personal API token (TickTick web app: avatar > Settings > Account >
API Token) or an OAuth access token. Tokens live in
``~/.config/todox/ticktick.json`` with owner-only permissions; set
``TODOX_TICKTICK_TOKEN`` for ephemeral use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
import json
import os
import tempfile
import urllib.error
import urllib.request

API = "https://api.ticktick.com"
LIST_NAME = "TODO.md"
IN_PROGRESS_TAG = "inprogress"
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class TickTickError(ValueError):
    """A non-retryable API or credential failure."""


def token_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "todox"
    return Path(os.environ.get("TODOX_TICKTICK_CONFIG", root / "ticktick.json"))


@dataclass
class Auth:
    token: str = ""
    list_id: str = ""
    list_name: str = LIST_NAME

    @classmethod
    def load(cls, path: Path | None = None) -> "Auth":
        path = path or token_path()
        env = os.environ.get("TODOX_TICKTICK_TOKEN", "").strip()
        if not path.exists():
            return cls(token=env)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("token"), str):
            raise TickTickError(f"Invalid TickTick credentials: {path}")
        return cls(token=env or data["token"], list_id=str(data.get("listId", "")),
                   list_name=str(data.get("listName", LIST_NAME)))

    def save(self, path: Path | None = None) -> None:
        path = path or token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".ticktick.", suffix=".tmp")
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"token": self.token, "listId": self.list_id, "listName": self.list_name}, stream, indent=2)
                stream.write("\n")
            os.replace(temp, path)
            temp = ""
        finally:
            if temp:
                os.unlink(temp)

    def require_token(self) -> str:
        if not self.token:
            raise TickTickError("No TickTick token. Run `todo ticktick login` or set TODOX_TICKTICK_TOKEN.")
        return self.token


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise TickTickError(f"Cannot reach TickTick: {exc.reason}") from exc


@dataclass
class TickTick:
    """Minimal, explicit wrapper around the documented endpoints."""

    token: str
    transport: Transport = urllib_transport
    tag_cache: dict[str, str] = field(default_factory=dict)

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, raw = self.transport(method, API + path, headers, body)
        text = raw.decode("utf-8", "replace")
        if status in {401, 403}:
            raise TickTickError("TickTick rejected the token. Create a new one in Settings > Account > API Token.")
        if status == 404:
            return None
        if status >= 400:
            raise TickTickError(f"TickTick {method} {path} failed ({status}): {text.strip()[:200]}")
        return json.loads(text) if text.strip() else None

    def projects(self) -> list[dict[str, Any]]:
        return self._call("GET", "/open/v1/project") or []

    def tags(self) -> list[dict[str, Any]]:
        return self._call("GET", "/open/v1/tag") or []

    def create_project(self, name: str) -> dict[str, Any]:
        return self._call("POST", "/open/v1/project", {"name": name}) or {}

    def create_tag(self, name: str, label: str) -> dict[str, Any]:
        return self._call("POST", "/open/v1/tag", {"name": name, "label": label}) or {}

    def completed_tasks(self, project_id: str) -> list[dict[str, Any]]:
        """Completed tasks leave the project-data endpoint, so ask for them explicitly."""
        today = date.today()
        window = {"projectIds": [project_id], "startDate": "2000-01-01T00:00:00+0000",
                  "endDate": f"{today + timedelta(days=1)}T00:00:00+0000"}
        tasks = [t for t in (self._call("POST", "/open/v1/task/completed", window) or []) if isinstance(t, dict) and t.get("id")]
        if len(tasks) < 200:
            return tasks
        # The endpoint caps a single window at 200 tasks; walk it year by year instead.
        found = {str(t["id"]): t for t in tasks}
        for year in range(2000, today.year + 2):
            chunk = self._call("POST", "/open/v1/task/completed", {
                "projectIds": [project_id], "startDate": f"{year}-01-01T00:00:00+0000",
                "endDate": f"{year}-12-31T23:59:59+0000"}) or []
            if len(chunk) >= 200:
                raise TickTickError("Completed-task discovery reached the yearly result limit; sync stopped to avoid incomplete data.")
            for task in chunk:
                if isinstance(task, dict) and task.get("id"):
                    found[str(task["id"])] = task
        return list(found.values())

    def project_tasks(self, project_id: str) -> list[dict[str, Any]]:
        """Every task in a list: open ones plus the completed ones kept elsewhere."""
        data = self._call("GET", f"/open/v1/project/{project_id}/data")
        if not isinstance(data, dict):
            raise TickTickError(f"Cannot read linked TickTick list {project_id}; sync stopped without inferring deletions.")
        found = {str(t["id"]): t for t in (data.get("tasks") or []) if isinstance(t, dict) and t.get("id")}
        for task in self.completed_tasks(project_id):
            found.setdefault(str(task["id"]), task)
        return [dict(task, projectId=task.get("projectId") or project_id) for task in found.values()]

    def tagged_tasks(self, tags: list[str]) -> list[dict[str, Any]]:
        """Search each project tag account-wide, including Inbox and completed tasks.

        TickTick combines multiple tags with AND, so query them separately.
        The filter endpoint caps results at 200: fail closed rather than use
        an incomplete snapshot to reconcile or delete tasks.
        """
        found: dict[str, dict[str, Any]] = {}
        for tag in sorted(set(tags)):
            tasks = self._call("POST", "/open/v1/task/filter", {"tag": [tag]})
            if not isinstance(tasks, list):
                raise TickTickError("TickTick tag discovery failed; sync stopped without applying changes.")
            if len(tasks) >= 200:
                raise TickTickError(f"Tag {tag!r} reached TickTick's 200-task search limit; sync stopped to avoid incomplete data.")
            for task in tasks:
                if not isinstance(task, dict) or not task.get("id"):
                    continue
                if not task.get("projectId"):
                    raise TickTickError("TickTick returned a tagged task without its list id; sync stopped.")
                found[str(task["id"])] = task
        return list(found.values())

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/open/v1/task", payload) or {}

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"/open/v1/task/{task_id}", payload) or {}

    def complete_task(self, project_id: str, task_id: str) -> None:
        self._call("POST", f"/open/v1/project/{project_id}/task/{task_id}/complete")

    def delete_task(self, project_id: str, task_id: str) -> None:
        self._call("DELETE", f"/open/v1/project/{project_id}/task/{task_id}")

    def find_list(self, list_id: str = "") -> tuple[str, str]:
        """Find the dedicated list without creating anything (safe for previews)."""
        projects = self.projects()
        match = next((p for p in projects if list_id and p.get("id") == list_id), None)
        match = match or next((p for p in projects if p.get("name") == LIST_NAME and p.get("kind") == "TASK"), None)
        return (str(match["id"]), str(match.get("name", LIST_NAME))) if match else ("", LIST_NAME)

    def ensure_list(self, list_id: str = "") -> tuple[str, str]:
        """Return (list id, list name), creating the dedicated list when absent."""
        found = self.find_list(list_id)
        if found[0]:
            return found
        created = self.create_project(LIST_NAME)
        if not created.get("id"):
            raise TickTickError("TickTick did not return an id for the new TODO.md list.")
        return str(created["id"]), str(created.get("name", LIST_NAME))

    def ensure_tag(self, label: str) -> str:
        name = label.strip().lower()[:64]
        if not name:
            raise TickTickError("Cannot sync an empty project name as a TickTick tag.")
        if name in self.tag_cache:
            return name
        existing = {str(t.get("name", "")).lower() for t in self.tags()}
        if name not in existing:
            self.create_tag(name, label.strip()[:64])
        self.tag_cache[name] = name
        return name

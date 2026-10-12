"""TickTick Open API client with an injectable transport.

Auth is a personal API token (TickTick web app: avatar > Settings > Account >
API Token) or an OAuth access token. Tokens live in
``~/.config/todox/ticktick.json`` with owner-only permissions; set
``TODOX_TICKTICK_TOKEN`` for ephemeral use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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

    def project_tasks(self, project_id: str) -> list[dict[str, Any]]:
        data = self._call("GET", f"/open/v1/project/{project_id}/data") or {}
        return list(data.get("tasks") or [])

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/open/v1/task", payload) or {}

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"/open/v1/task/{task_id}", payload) or {}

    def complete_task(self, project_id: str, task_id: str) -> None:
        self._call("POST", f"/open/v1/project/{project_id}/task/{task_id}/complete")

    def delete_task(self, project_id: str, task_id: str) -> None:
        self._call("DELETE", f"/open/v1/project/{project_id}/task/{task_id}")

    def ensure_list(self, list_id: str = "") -> tuple[str, str]:
        """Return (list id, list name), creating the dedicated list when absent."""
        projects = self.projects()
        if list_id:
            match = next((p for p in projects if p.get("id") == list_id), None)
            if match:
                return match["id"], str(match.get("name", LIST_NAME))
        match = next((p for p in projects if p.get("name") == LIST_NAME and p.get("kind") == "TASK"), None)
        if match:
            return match["id"], str(match.get("name", LIST_NAME))
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

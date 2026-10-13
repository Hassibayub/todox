"""Shared fixtures: a fake TickTick backend and a registered project."""
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from todox.projects import Project, load_projects
from todox.ticktick import TickTick, TickTickError

TODO = "# TODO\n- [ ] Alpha\n## Work\n- [/] Beta\n- [x] Done (completed: 2026-10-10)\n"


@pytest.fixture(autouse=True)
def no_auto_sync(monkeypatch, tmp_path):
    """Tests drive sync explicitly; launch sync would double-run and hit the network."""
    monkeypatch.setenv("TODOX_AUTO_SYNC", "0")
    monkeypatch.delenv("TODOX_DB", raising=False)
    monkeypatch.delenv("TODOX_TICKTICK_TOKEN", raising=False)
    monkeypatch.setenv("TODOX_TICKTICK_CONFIG", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("TODOX_SYNC_STATE", str(tmp_path / "isolated-sync.json"))
    monkeypatch.setenv("TODOX_CONFIG", str(tmp_path / "isolated-projects.json"))

    def blocked(*args, **kwargs):
        raise AssertionError("Tests must use FakeTickTick, never the real network")

    monkeypatch.setattr("urllib.request.urlopen", blocked)
    from todox.ui import TodoApp
    from todox.sync import Sync
    from todox.projects import load_config
    original = TodoApp._run_background

    async def fake_aware(self, client, announce, uid="", keep=""):
        # In-memory transport doubles stay in the test process. Production
        # clients always exercise the real owned subprocess implementation.
        if getattr(client, "_in_memory", False):
            if keep:
                engine = Sync(client, registry=self.registry)
                change = engine.resolve(load_projects(load_config(self.registry)), uid, keep)
                return {"change": asdict(change)}
            engine, plan = self._sync_blocking(client, announce)
            return {"plan": asdict(plan), "applied": engine.applied}
        return await original(self, client, announce, uid, keep)

    monkeypatch.setattr(TodoApp, "_run_background", fake_aware)


class FakeTickTick(TickTick):
    """In-memory stand-in for the documented endpoints."""

    _in_memory = True

    def __init__(self) -> None:
        super().__init__(token="test-token")
        self.projects_list: list[dict] = [{"id": "list1", "name": "TODO.md", "kind": "TASK"}]
        self.tags_list: list[dict] = [{"name": "web-app", "label": "web-app"}]
        self.tasks: list[dict] = []
        self.deleted: list[str] = []
        self.fail_paths: set[str] = set()
        self.next_id = 1
        self.calls: list[tuple] = []

    def _call(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path in self.fail_paths:
            raise TickTickError(f"simulated failure for {path}", rejected=True)
        if path == "/open/v1/project" and method == "GET":
            return self.projects_list
        if path == "/open/v1/project" and method == "POST":
            created = {"id": f"list{len(self.projects_list) + 1}", "name": payload["name"], "kind": "TASK"}
            self.projects_list.append(created)
            return created
        if path == "/open/v1/tag" and method == "GET":
            return self.tags_list
        if path == "/open/v1/tag" and method == "POST":
            self.tags_list.append({"name": payload["name"], "label": payload["label"]})
            return payload
        if path == "/open/v1/task/completed":
            return deepcopy([t for t in self.tasks if t.get("status") == 2
                             and t.get("projectId", "list1") in payload["projectIds"]])
        if path == "/open/v1/task/filter":
            return deepcopy([dict(t, projectId=t.get("projectId", "list1")) for t in self.tasks
                             if set(payload["tag"]).issubset({str(tag).lower() for tag in t.get("tags", [])})][:200])
        if path.endswith("/data"):
            # TickTick's project endpoint only reports open tasks.
            project_id = path.split("/")[-2]
            return {"tasks": deepcopy([t for t in self.tasks if t.get("status") != 2
                                       and t.get("projectId", "list1") == project_id])}
        if path == "/open/v1/task" and method == "POST":
            task = {"id": f"r{self.next_id}", "title": payload["title"], "projectId": payload["projectId"],
                    "status": int(payload.get("status", 0)), "tags": list(payload.get("tags") or []), "completedTime": ""}
            self.tasks.append(task)
            self.next_id += 1
            return task
        if path.startswith("/open/v1/task/") and method == "POST":
            task = next(t for t in self.tasks if t["id"] == payload["id"])
            assert payload["projectId"] == task.get("projectId", "list1"), "sync must not move tasks between lists"
            task["title"] = payload.get("title", task["title"])
            task["tags"] = list(payload.get("tags") or [])
            if payload.get("status") is not None:
                task["status"] = int(payload["status"])
                if task["status"] != 2:
                    task["completedTime"] = ""
            return task
        if path.endswith("/complete"):
            task_id = path.split("/")[-2]
            task = next(t for t in self.tasks if t["id"] == task_id)
            assert path.split("/")[-4] == task.get("projectId", "list1")
            task["status"] = 2
            task["completedTime"] = "2026-10-12T09:00:00+0000"
            return None
        if method == "GET" and "/task/" in path:
            project_id, task_id = path.split("/")[-3], path.split("/")[-1]
            return next((dict(t) for t in self.tasks if t["id"] == task_id and t.get("projectId", "list1") == project_id), None)
        if method == "DELETE":
            task_id = path.split("/")[-1]
            task = next(t for t in self.tasks if t["id"] == task_id)
            assert path.split("/")[-3] == task.get("projectId", "list1")
            self.tasks = [t for t in self.tasks if t["id"] != task_id]
            self.deleted.append(task_id)
            return None
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def fake() -> FakeTickTick:
    return FakeTickTick()


@pytest.fixture
def workspace(tmp_path: Path):
    file = tmp_path / "web-app" / "TODO.md"
    file.parent.mkdir(parents=True)
    file.write_text(TODO)
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "web-app", "path": str(file)}]}))
    return file, config


@pytest.fixture
def projects(workspace):
    file, _ = workspace
    return load_projects([Project("web-app", file)])

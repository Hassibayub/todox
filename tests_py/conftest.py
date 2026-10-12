"""Shared fixtures: a fake TickTick backend and a registered project."""
import json
from pathlib import Path

import pytest

from todox.projects import Project, load_projects
from todox.ticktick import TickTick, TickTickError

TODO = "# TODO\n- [ ] Alpha\n## Work\n- [/] Beta\n- [x] Done (completed: 2026-10-10)\n"


class FakeTickTick(TickTick):
    """In-memory stand-in for the documented endpoints."""

    def __init__(self) -> None:
        super().__init__(token="test-token")
        self.projects_list: list[dict] = [{"id": "list1", "name": "TODO.md", "kind": "TASK"}]
        self.tags_list: list[dict] = [{"name": "web-app", "label": "web-app"}]
        self.tasks: list[dict] = []
        self.deleted: list[str] = []
        self.fail_paths: set[str] = set()

    def _call(self, method, path, payload=None):
        if path in self.fail_paths:
            raise TickTickError(f"simulated failure for {path}")
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
        if path.endswith("/data"):
            return {"tasks": list(self.tasks)}
        if path == "/open/v1/task" and method == "POST":
            task = {"id": f"r{len(self.tasks) + 1}", "title": payload["title"], "projectId": payload["projectId"],
                    "status": int(payload.get("status", 0)), "tags": list(payload.get("tags") or []), "completedTime": ""}
            self.tasks.append(task)
            return task
        if path.startswith("/open/v1/task/") and method == "POST":
            task = next(t for t in self.tasks if t["id"] == payload["id"])
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
            task["status"] = 2
            task["completedTime"] = "2026-10-12T09:00:00+0000"
            return None
        if method == "DELETE":
            task_id = path.split("/")[-1]
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

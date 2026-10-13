"""Project registry and local TODO discovery."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import fcntl
from .db import Database

from .store import Document, read


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    doc: Document | None = None
    error: str | None = None
    sync_enabled: bool = True


def config_path() -> Path:
    return Path(os.environ.get("TODOX_CONFIG", str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "todox" / "projects.json"))).expanduser().resolve()


def load_config(path: Path | None = None) -> list[Project]:
    path = path or config_path()
    stored = Database(registry=path).load_projects()
    if stored is not None:
        return [Project(p["name"], Path(p["path"]), sync_enabled=bool(p["sync_enabled"])) for p in stored]
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("projects"), list):
        raise ValueError(f"Invalid project registry: {path}")
    result = []
    for item in data["projects"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("path"), str):
            raise ValueError(f"Invalid project registry: {path}")
        candidate = Path(item["path"]).expanduser()
        result.append(Project(item["name"], (candidate if candidate.is_absolute() else path.parent / candidate).resolve()))
    return result


def save_config(projects: list[Project], path: Path | None = None) -> None:
    path = path or config_path()
    database = Database(registry=path)
    database.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Path(str(database.path) + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("A sync or registry update is running; retry after it finishes.") from exc
        database.save_projects([
            {"name": p.name, "path": str(p.path), "sync_enabled": p.sync_enabled} for p in projects])


def resolve_todo(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=True)
    if candidate.is_dir():
        candidate = candidate / "TODO.md"
    if not candidate.is_file():
        raise ValueError(f"Not a TODO file: {candidate}")
    return candidate.resolve()


def register(path: Path, name: str | None = None) -> Project:
    todo = resolve_todo(path)
    project = Project(name or todo.parent.name, todo)
    save_config([p for p in load_config() if p.path != todo] + [project])
    return project


def unregister(path: Path) -> None:
    path = path.expanduser().resolve()
    target = path if path.suffix.lower() == ".md" else path / "TODO.md"
    save_config([p for p in load_config() if p.path != target])


def find_local(cwd: Path) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / "TODO.md"
        if candidate.is_file():
            return candidate.resolve()
        if (current / ".git").exists() or current.parent == current:
            return None
        current = current.parent


def load_projects(projects: list[Project]) -> list[Project]:
    result = []
    for project in projects:
        try:
            result.append(Project(project.name, project.path, read(project.path), sync_enabled=project.sync_enabled))
        except (OSError, ValueError, UnicodeError) as exc:
            result.append(Project(project.name, project.path, error=str(exc), sync_enabled=project.sync_enabled))
    return result

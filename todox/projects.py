"""Project registry and local TODO discovery."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import tempfile

from .store import Document, read


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    doc: Document | None = None
    error: str | None = None


def config_path() -> Path:
    return Path(os.environ.get("TODOX_CONFIG", str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "todox" / "projects.json"))).expanduser().resolve()


def load_config(path: Path | None = None) -> list[Project]:
    path = path or config_path()
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
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(path) + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temp: str | None = None
    try:
        out, temp = tempfile.mkstemp(dir=path.parent, prefix=".projects.", suffix=".tmp")
        with os.fdopen(out, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "projects": [{"name": p.name, "path": str(p.path)} for p in projects]}, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp:
            os.unlink(temp)
        os.close(fd)
        lock.unlink()


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
            result.append(Project(project.name, project.path, read(project.path)))
        except (OSError, ValueError, UnicodeError) as exc:
            result.append(Project(project.name, project.path, error=str(exc)))
    return result

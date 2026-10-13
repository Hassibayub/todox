"""Private SQLite bookkeeping. Markdown remains the editable task source.

Readers never initialize or migrate a database. Every connection is short-lived,
and transactions never span a TickTick request or a Markdown write.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
import json
import os
import sqlite3
import uuid


VERSION = 1


def config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "todox"


def database_path(path: Path | None = None) -> Path:
    candidate = path or Path(os.environ.get("TODOX_DB", os.environ.get("TODOX_SYNC_STATE", config_root() / "todox.sqlite3")))
    if candidate.suffix == ".json":
        candidate = candidate.with_suffix(".sqlite3")
    return candidate.expanduser().resolve()


def legacy_state_path(path: Path | None = None) -> Path:
    if path is not None:
        return path if path.suffix == ".json" else path.parent / "sync.json"
    return Path(os.environ.get("TODOX_SYNC_STATE", config_root() / "sync.json")).expanduser().resolve()


def registry_key(path: Path | None = None) -> str:
    source = path or Path(os.environ.get("TODOX_CONFIG", config_root() / "projects.json"))
    return str(source.expanduser().resolve())


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY, registry_key TEXT NOT NULL UNIQUE,
    registry_migrated INTEGER NOT NULL DEFAULT 0, sync_migrated INTEGER NOT NULL DEFAULT 0,
    list_id TEXT NOT NULL DEFAULT '', active_only INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id),
    path TEXT NOT NULL, name TEXT NOT NULL, tag TEXT NOT NULL,
    registered INTEGER NOT NULL DEFAULT 0, sync_enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0, UNIQUE(profile_id, path)
);
CREATE UNIQUE INDEX IF NOT EXISTS project_tags ON projects(profile_id, tag) WHERE registered = 1;
CREATE TABLE IF NOT EXISTS tasks (
    profile_id INTEGER NOT NULL REFERENCES profiles(id), uid TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id), remote_id TEXT NOT NULL,
    list_id TEXT NOT NULL, local_signature TEXT NOT NULL, remote_signature TEXT NOT NULL,
    section TEXT NOT NULL, synced_at TEXT NOT NULL, retired INTEGER NOT NULL DEFAULT 0,
    local_snapshot TEXT NOT NULL DEFAULT '{}', remote_snapshot TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(profile_id, uid), UNIQUE(profile_id, remote_id)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id),
    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES sync_runs(id),
    profile_id INTEGER NOT NULL REFERENCES profiles(id), uid TEXT NOT NULL,
    action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
    remote_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_operations ON operations(profile_id, status);
CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id),
    uid TEXT NOT NULL, remote_id TEXT NOT NULL, reason TEXT NOT NULL,
    snapshot TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: Path | None = None, registry: Path | None = None):
        self.path = database_path(path)
        self.key = registry_key(registry)

    @contextmanager
    def connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        try:
            con = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=5)
        except sqlite3.Error as exc:
            raise ValueError(f"Cannot open database {self.path}: {exc}") from exc
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, VERSION}:
                raise ValueError(f"Unsupported database schema {version}; upgrade todox before opening {self.path}")
            if write:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=FULL")
                if version == 0:
                    con.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + f"\nPRAGMA user_version={VERSION};\nCOMMIT;")
                    con.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)", (VERSION, now()))
                    con.commit()
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT OR IGNORE INTO profiles(registry_key) VALUES (?)", (self.key,))
            elif version != VERSION:
                raise ValueError(f"Database has no supported schema: {self.path}")
            else:
                con.execute("BEGIN")  # All queries in this reader see one consistent snapshot.
            yield con
            if write:
                con.commit()
        except sqlite3.Error as exc:
            con.rollback()
            raise ValueError(f"Database operation failed: {exc}") from exc
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def profile(self, con: sqlite3.Connection) -> sqlite3.Row | None:
        return con.execute("SELECT * FROM profiles WHERE registry_key=?", (self.key,)).fetchone()

    @contextmanager
    def writer(self, connection: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
        else:
            with self.connection(True) as con:
                yield con

    def revision(self) -> int:
        if not self.path.exists():
            return 0
        with self.connection() as con:
            profile = self.profile(con)
            return int(profile["revision"]) if profile else 0

    def load_projects(self) -> list[dict[str, Any]] | None:
        if not self.path.exists():
            return None
        with self.connection() as con:
            profile = self.profile(con)
            if not profile or not profile["registry_migrated"]:
                return None
            return [dict(row) for row in con.execute(
                "SELECT name, path, sync_enabled FROM projects WHERE profile_id=? AND registered=1 ORDER BY position, id",
                (profile["id"],))]

    def save_projects(self, projects: list[dict[str, Any]], connection: sqlite3.Connection | None = None) -> None:
        with self.writer(connection) as con:
            profile = self.profile(con)
            pid = profile["id"]
            con.execute("UPDATE projects SET registered=0 WHERE profile_id=?", (pid,))
            for position, project in enumerate(projects):
                name = project["name"]
                tag = name.strip().lower()[:64]
                if not tag or tag == "inprogress":
                    raise ValueError("Project tags must be non-empty and cannot be 'inprogress'.")
                con.execute("""INSERT INTO projects(profile_id,path,name,tag,registered,sync_enabled,position)
                    VALUES(?,?,?,?,1,?,?) ON CONFLICT(profile_id,path) DO UPDATE SET
                    name=excluded.name,tag=excluded.tag,registered=1,sync_enabled=excluded.sync_enabled,position=excluded.position""",
                    (pid, str(Path(project["path"]).resolve()), name, tag, int(project.get("sync_enabled", True)), position))
            con.execute("UPDATE profiles SET registry_migrated=1,revision=revision+1 WHERE id=?", (pid,))

    def load_state(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.connection() as con:
            profile = self.profile(con)
            if not profile or not profile["sync_migrated"]:
                return None
            tasks = {}
            for row in con.execute("""SELECT t.*,p.path FROM tasks t JOIN projects p ON p.id=t.project_id
                                       WHERE t.profile_id=?""", (profile["id"],)):
                tasks[row["uid"]] = {"ticktickId": row["remote_id"], "listId": row["list_id"],
                                     "local": row["local_signature"], "remote": row["remote_signature"],
                                     "section": row["section"], "project": row["path"],
                                     "syncedAt": row["synced_at"], "retired": bool(row["retired"]),
                                     "localSnapshot": json.loads(row["local_snapshot"]),
                                     "remoteSnapshot": json.loads(row["remote_snapshot"])}
            return {"version": 1, "listId": profile["list_id"], "activeOnly": bool(profile["active_only"]), "tasks": tasks}

    def save_state(self, data: dict[str, Any], operation: str = "", remote_id: str = "", complete: bool = True,
                   connection: sqlite3.Connection | None = None) -> None:
        with self.writer(connection) as con:
            pid = self.profile(con)["id"]
            con.execute("DELETE FROM tasks WHERE profile_id=?", (pid,))
            for uid, task in data["tasks"].items():
                path = task["project"]
                con.execute("""INSERT OR IGNORE INTO projects(profile_id,path,name,tag) VALUES(?,?,?,?)""",
                            (pid, path, Path(path).parent.name, Path(path).parent.name.lower()[:64]))
                project_id = con.execute("SELECT id FROM projects WHERE profile_id=? AND path=?", (pid, path)).fetchone()[0]
                con.execute("""INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (pid, uid, project_id, task["ticktickId"], task["listId"], task["local"], task["remote"],
                             task["section"], task["syncedAt"], int(task.get("retired", False)),
                             json.dumps(task.get("localSnapshot", {})), json.dumps(task.get("remoteSnapshot", {}))))
            con.execute("UPDATE profiles SET sync_migrated=1,list_id=?,active_only=?,revision=revision+1 WHERE id=?",
                        (data["listId"], int(data.get("activeOnly", False)), pid))
            if operation:
                con.execute("UPDATE operations SET status=?,remote_id=?,updated_at=? WHERE id=? AND profile_id=?",
                            ("succeeded" if complete else "running", remote_id, now(), operation, pid))

    def migrate_data(self, projects: list[dict[str, Any]], state: dict[str, Any]) -> None:
        """Import both legacy stores in one transaction, preserving all identities."""
        with self.connection(True) as con:
            self.save_projects(projects, con)
            self.save_state(state, connection=con)

    def begin_run(self, conflicts: list[dict[str, Any]]) -> str:
        run_id = uuid.uuid4().hex
        with self.connection(True) as con:
            pid = self.profile(con)["id"]
            con.execute("INSERT INTO sync_runs(id,profile_id,started_at,status) VALUES(?,?,?,'running')", (run_id, pid, now()))
            con.execute("DELETE FROM conflicts WHERE profile_id=?", (pid,))
            for conflict in conflicts:
                con.execute("INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, pid, conflict["uid"], conflict["remote_id"], conflict["reason"],
                             json.dumps(conflict), now()))
        return run_id

    def begin_operation(self, run_id: str, payload: dict[str, Any]) -> str:
        operation = uuid.uuid4().hex
        with self.connection(True) as con:
            pid = self.profile(con)["id"]
            con.execute("""INSERT INTO operations(id,run_id,profile_id,uid,action,payload,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,'running',?,?)""",
                        (operation, run_id, pid, payload["uid"], payload["action"], json.dumps(payload), now(), now()))
        return operation

    def fail_operation(self, operation: str, error: str, uncertain: bool = True) -> None:
        with self.connection(True) as con:
            pid = self.profile(con)["id"]
            con.execute("UPDATE operations SET status=?,error=?,updated_at=? WHERE id=? AND profile_id=?",
                        ("uncertain" if uncertain else "failed", error, now(), operation, pid))

    def recover_operation(self, operation: str, status: str) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("Recovery must verify success or make the operation retryable.")
        with self.connection(True) as con:
            con.execute("UPDATE operations SET status=?,updated_at=? WHERE id=? AND profile_id=?",
                        (status, now(), operation, self.profile(con)["id"]))

    def finish_run(self, run_id: str, summary: dict[str, int], failed: bool) -> None:
        with self.connection(True) as con:
            con.execute("UPDATE sync_runs SET finished_at=?,status=?,summary=? WHERE id=? AND profile_id=?",
                        (now(), "failed" if failed else "conflicted" if summary.get("conflict") else "succeeded",
                         json.dumps(summary), run_id, self.profile(con)["id"]))

    def operations(self, pending: bool = False) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.connection() as con:
            profile = self.profile(con)
            if not profile:
                return []
            sql = "SELECT * FROM operations WHERE profile_id=?"
            if pending:
                sql += " AND status IN ('running','uncertain')"
            rows = con.execute(sql + " ORDER BY created_at DESC", (profile["id"],))
            return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"database": str(self.path), "migrated": False, "pendingOperations": 0}
        with self.connection() as con:
            profile = self.profile(con)
            if not profile:
                return {"database": str(self.path), "migrated": False, "pendingOperations": 0}
            pid = profile["id"]
            runs = [dict(r, summary=json.loads(r["summary"])) for r in con.execute(
                "SELECT * FROM sync_runs WHERE profile_id=? ORDER BY started_at DESC LIMIT 10", (pid,))]
            return {"database": str(self.path), "migrated": bool(profile["sync_migrated"]),
                    "projects": con.execute("SELECT count(*) FROM projects WHERE profile_id=? AND registered=1", (pid,)).fetchone()[0],
                    "linkedTasks": con.execute("SELECT count(*) FROM tasks WHERE profile_id=?", (pid,)).fetchone()[0],
                    "conflicts": con.execute("SELECT count(*) FROM conflicts WHERE profile_id=?", (pid,)).fetchone()[0],
                    "pendingOperations": con.execute("SELECT count(*) FROM operations WHERE profile_id=? AND status IN ('running','uncertain')",
                                                     (pid,)).fetchone()[0],
                    "lastSuccessfulSync": con.execute("SELECT max(finished_at) FROM sync_runs WHERE profile_id=? AND status='succeeded'",
                                                      (pid,)).fetchone()[0], "runs": runs}

    def backup(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connection() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        return destination

"""Command-line entry point."""
from __future__ import annotations

from pathlib import Path
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import sys

from .projects import find_local, load_config, load_projects, register, resolve_todo, unregister
from .store import counts
from .db import Database


def current_version() -> str:
    try:
        return package_version("todox-tui")
    except PackageNotFoundError:  # running from a source checkout
        return "0.4.4"


HELP = """todo — Markdown tasks in your terminal

  todo                         Local TODO.md, or global Projects
  todo --global                Global Projects, even from a local directory
  todo --file path/TODO.md     Open a specific local file
  todo init                    Create TODO.md here
  todo add-project [path]      Register an existing TODO.md
  todo remove-project [path]   Unregister without deleting the file
  todo scan [directory]        Register immediate child projects
  todo projects                List registered files
  todo project NAME --local-only | --sync  Control project syncing
  todo stats [--json]          Task counts for scripts
  todo sync                    Two-way sync; short summary (--dry-run, --verbose, --json)
  todo sync status             Local sync status (no network)
  todo sync history            Recent runs (no network)
  todo sync recover            Recover interrupted bookkeeping (no remote writes)
  todo db migrate              Back up and migrate JSON bookkeeping to SQLite
  todo db backup               Create a consistent SQLite backup
  todo db export               Export registry and sync mappings as JSON
  todo doctor                  Check database and registered local files
  todo ticktick login [token]  Store a personal TickTick API token
  todo ticktick status         Show token and TODO.md list state
  todo --help                  Show this help
"""

def run(args: list[str] | None = None, cwd: Path | None = None) -> int:
    args = list(sys.argv[1:] if args is None else args)
    cwd = cwd or Path.cwd()
    command = args[0] if args else ""
    if command in {"-h", "--help"}:
        print(HELP)
        return 0
    if command == "--version":
        print(current_version())
        return 0
    if command == "init" and len(args) == 1:
        path = cwd / "TODO.md"
        with path.open("x", encoding="utf-8") as stream:
            stream.write("# TODO\n")
        register(path)
        print(f"Created and registered {path}")
        return 0
    if command in {"add-project", "remove-project"} and len(args) <= 2:
        path = cwd / args[1] if len(args) == 2 else cwd
        if command == "add-project":
            print(f"Registered {register(path).path}")
        else:
            unregister(path)
            print(f"Unregistered {path}; TODO.md untouched.")
        return 0
    if command == "scan" and len(args) <= 2:
        directory = cwd / args[1] if len(args) == 2 else cwd
        found = 0
        for child in directory.iterdir():
            if child.is_dir() and not child.name.startswith(".") and child.name != "node_modules" and (child / "TODO.md").is_file():
                register(child)
                found += 1
        print(f"{found} project(s) registered.")
        return 0
    if command == "project" and len(args) == 3 and args[2] in {"--local-only", "--sync"}:
        from dataclasses import replace
        from .projects import save_config
        revision = Database().revision()
        projects = load_config()
        matches = [p for p in projects if p.name.casefold() == args[1].casefold()]
        if len(matches) != 1:
            raise ValueError("Choose exactly one registered project name from todo projects.")
        selected = matches[0]
        enabled = args[2] == "--sync"
        save_config([replace(p, sync_enabled=enabled) if p.path == selected.path else p for p in projects], expected_revision=revision)
        print(f'{selected.name}: {"sync enabled" if enabled else "local-only"}; no remote tasks changed')
        return 0
    if command == "db" and len(args) == 2:
        from datetime import datetime, timezone
        from .sync import State, Sync
        from .ticktick import TickTick
        database = Database()
        if args[1] == "migrate":
            print(json.dumps(Sync(TickTick("")).migrate(), indent=2))
            return 0
        if args[1] == "backup":
            if not database.path.exists():
                raise ValueError("No database yet. Run todo db migrate first.")
            destination = database.path.parent / "backups" / ("database-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
            print(database.backup(destination))
            return 0
        if args[1] == "export":
            print(json.dumps({"projects": [{"name": p.name, "path": str(p.path), "syncEnabled": p.sync_enabled} for p in load_config()],
                              "sync": State.load().payload()}, indent=2))
            return 0
        raise ValueError("Usage: todo db migrate | backup | export")
    if command == "doctor" and len(args) == 1:
        database = Database()
        result = database.status()
        if database.path.exists():
            with database.connection() as con:
                result["integrity"] = con.execute("PRAGMA quick_check").fetchone()[0]
                result["foreignKeyErrors"] = len(con.execute("PRAGMA foreign_key_check").fetchall())
        result["fileErrors"] = [{"project": p.name, "error": p.error} for p in load_projects(load_config()) if p.error]
        print(json.dumps(result, indent=2))
        return 1 if result["fileErrors"] or result.get("foreignKeyErrors") or result.get("integrity", "ok") != "ok" else 0
    if command == "ticktick" and 1 < len(args) <= 3:
        from .sync import state_path
        from .ticktick import Auth, LIST_NAME, TickTick
        sub = args[1]
        auth = Auth.load()
        if sub == "login":
            token = (args[2] if len(args) == 3 else sys.stdin.readline()).strip()
            if not token:
                raise ValueError("Usage: todo ticktick login [token] (or pipe the token on stdin)")
            client = TickTick(token)
            visible = client.projects()
            list_id, list_name = client.ensure_list(auth.list_id)
            auth.token, auth.list_id, auth.list_name = token, list_id, list_name
            auth.save()
            print(f"Signed in · list “{list_name}” ({list_id}) · {len(visible)} lists visible")
            return 0
        if sub == "status":
            client = TickTick(auth.require_token())
            match = next((p for p in client.projects() if p.get("name") == LIST_NAME and p.get("kind") == "TASK"), None)
            if match:
                print(f'Token present · list “{match["name"]}” · {len(client.project_tasks(str(match["id"])))} task(s) · state {state_path()}')
            else:
                print(f"Token present · no {LIST_NAME} list yet; the first sync creates it · state {state_path()}")
            return 0
        raise ValueError("Usage: todo ticktick login [token] | todo ticktick status")
    if command == "sync":
        from .sync import Sync
        from .ticktick import Auth, TickTick
        if args[1:] in [["status"], ["history"], ["recover"]]:
            database = Database()
            if args[1] == "recover":
                report = Sync(TickTick("")).recover()
                print(json.dumps(report, indent=2))
                return 1 if any(item["status"] == "needs-review" for item in report) else 0
            status = database.status()
            print(json.dumps(status.get("runs", []) if args[1] == "history" else status, indent=2))
            return 0
        flags = args[1:]
        known = {"--dry-run", "--verbose", "--json", "--allow-deletes", "--cleanup-temp"}
        keep, keep_uid = "", ""
        rest: list[str] = []
        index = 0
        while index < len(flags):
            flag = flags[index]
            if flag in {"--keep-local", "--keep-remote"}:
                if keep:
                    raise ValueError("Choose exactly one conflict resolution flag.")
                if index + 1 >= len(flags):
                    raise ValueError(f"{flag} needs a task id from `todo sync --json`.")
                keep, keep_uid = flag.removeprefix("--keep-"), flags[index + 1]
                index += 2
                continue
            if flag not in known:
                raise ValueError(f"Unknown option: {flag}")
            rest.append(flag)
            index += 1
        if keep_uid and "--dry-run" in rest:
            raise ValueError("Conflict resolution cannot be combined with --dry-run; preview with todo sync --dry-run first.")
        client = TickTick(Auth.load().require_token())
        engine = Sync(client)
        projects = load_projects(load_config())
        if sys.stdout.isatty() and "--json" not in rest:
            print("Checking TickTick…" if "--dry-run" in rest else "Syncing with TickTick…", flush=True)
        if keep_uid:
            change = engine.resolve(projects, keep_uid, keep)
            print(f"Failed: {change.error}" if change.error else f"Kept {keep} for {keep_uid}: {change.text or change.reason}")
            return 1 if change.error else 0
        plan = engine.plan(projects, allow_deletes="--allow-deletes" in rest, cleanup_temp="--cleanup-temp" in rest)
        if "--dry-run" not in rest:
            engine.apply(plan)
        if "--json" in rest:
            print(json.dumps({"list": plan.list_name, "dryRun": "--dry-run" in rest,
                              "summary": plan.summary(), "byProject": plan.project_summary(), "changes": [
                 {"action": c.action, "uid": c.uid, "remoteId": c.remote_id, "text": c.text, "project": c.name or c.project,
                 "reason": c.reason, "error": c.error} for c in plan.changes]}, indent=2))
        else:
            if "--verbose" in rest:
                print("\n".join(plan.describe()))
                print(plan.status_message("--dry-run" in rest))
            else:
                print("\n".join(plan.compact_lines("--dry-run" in rest)))
        return 1 if any(c.error for c in plan.changes) else 0
    if args and args[0] in {"sync", "ticktick"}:
        raise ValueError("Unknown arguments. Run todo --help.")
    if command == "projects" and len(args) == 1:
        projects = load_config()
        print("\n".join(f"{p.name}\t{p.path}" for p in projects) or "No projects yet. Run todo add-project /path/to/project.")
        return 0
    if command == "stats" and (len(args) == 1 or args == ["stats", "--json"]):
        projects = load_projects(load_config())
        tally = counts([task for p in projects if p.doc for task in p.doc.tasks])
        result = {"projects": len(projects), "notStarted": tally[" "], "inProgress": tally["/"], "completed": tally["x"],
                  "errors": [{"path": str(p.path), "error": p.error} for p in projects if p.error]}
        print(json.dumps(result, indent=2) if "--json" in args else f'{result["projects"]} projects · {tally[" "]} todo · {tally["/"]} in progress · {tally["x"]} completed')
        return 0
    if args and args != ["--global"] and not (len(args) == 2 and args[0] == "--file"):
        raise ValueError("Unknown arguments. Run todo --help.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("The TUI needs an interactive terminal. Use todo stats --json for scripts.")
    local = None if command == "--global" else resolve_todo(cwd / args[1]) if command == "--file" else find_local(cwd)
    if local:
        try:
            if not any(p.path == local for p in load_config()):
                register(local)
        except (OSError, ValueError):
            # A broken registry must not prevent opening a user's Markdown.
            # TodoApp reports the registry error and still displays this file.
            pass
    from .ui import TodoApp
    TodoApp(local=local).run()
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        from .sync import friendly_error
        message = friendly_error(str(exc)) if sys.argv[1:2] == ["sync"] and "--verbose" not in sys.argv else str(exc)
        print(f"todo: {message}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

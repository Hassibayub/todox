"""Private TUI worker entry point; stdout is a single JSON response."""
from __future__ import annotations

from dataclasses import asdict
import json
import signal
import sys
from pathlib import Path

from .projects import load_config, load_projects
from .sync import Sync
from .ticktick import TickTick


class SyncCancelled(BaseException):
    pass


def cancel(signum, frame) -> None:
    # Raising unwinds file-lock finally blocks and SQLite transactions, even
    # while a synchronous socket request is waiting. It isn't an API failure.
    raise SyncCancelled()


def main() -> None:
    signal.signal(signal.SIGTERM, cancel)
    try:
        request = json.load(sys.stdin)
        registry = Path(request["registry"]) if request.get("registry") else None
        engine = Sync(TickTick(request["token"]), registry=registry)
        projects = load_projects(load_config(registry))
        if request.get("keep"):
            change = engine.resolve(projects, request["uid"], request["keep"])
            result = {"change": asdict(change)}
        else:
            plan = engine.plan(projects)
            if request.get("announce") or (engine.state.active_only and engine.database.load_state() is not None):
                engine.apply(plan)
            result = {"plan": asdict(plan), "applied": engine.applied}
        print(json.dumps(result))
    except SyncCancelled:
        pass  # In-flight journal records deliberately remain recoverable.
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()

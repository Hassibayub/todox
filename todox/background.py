"""An owned sync subprocess: network waits never delay the TUI's event loop.

Credentials travel over stdin, not command-line arguments. On cancellation the
child unwinds its current operation; SQLite's journal retains uncertain work.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys


class BackgroundSync:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None

    def stop(self) -> None:
        if self.process is not None and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    async def run(self, request: dict, command: list[str] | None = None) -> dict:
        if self.process is not None:
            raise ValueError("A background sync is already running.")
        process = await asyncio.create_subprocess_exec(
            *(command or [sys.executable, "-m", "todox.sync_worker"]),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.process = process
        try:
            stdout, _ = await process.communicate(json.dumps(request).encode())
            if process.returncode != 0:
                raise ValueError("Background sync stopped unexpectedly. Run todo sync for details.")
            try:
                result = json.loads(stdout)
            except (ValueError, UnicodeError) as exc:
                raise ValueError("Background sync returned an unreadable result. Run todo sync for details.") from exc
            if not isinstance(result, dict):
                raise ValueError("Background sync returned an unreadable result. Run todo sync for details.")
            if result.get("error"):
                raise ValueError(result["error"])
            return result
        finally:
            if process.returncode is None:
                self.stop()
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            self.process = None
            # A forced stop must not leave this worker's cooperative file locks
            # behind. Never remove a lock owned by another process.
            for source in request.get("files", []):
                lock = Path(str(source) + ".todox.lock")
                try:
                    if lock.read_text().strip() == str(process.pid):
                        lock.unlink()
                except (OSError, UnicodeError):
                    pass

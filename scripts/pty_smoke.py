#!/usr/bin/env python3
"""Exercise real alternate-screen startup and q shutdown in an isolated PTY."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time

root = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix="todo-pty-") as directory:
    folder = Path(directory)
    todo = folder / "TODO.md"
    todo.write_text("# TODO\n\n## Smoke\n- [/] Verify terminal\n")
    config = folder / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "smoke-project", "path": str(todo)}]}))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 90, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", TODOX_CONFIG=str(config))
    child = subprocess.Popen([shutil.which("bun"), str(root / "src/cli.ts"), "--global"], stdin=slave, stdout=slave, stderr=slave, cwd=directory, env=env)
    os.close(slave)
    output = b""
    try:
        deadline = time.monotonic() + 15
        quit_sent = False
        while time.monotonic() < deadline:
            # Keep draining the PTY while the app exits: a blocking wait here
            # can deadlock the renderer's final terminal-restoration writes.
            ready, _, _ = select.select([master], [], [], min(0.2, max(0, deadline - time.monotonic())))
            if ready:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            if b"Ready" in output and not quit_sent:
                os.write(master, b"q")
                quit_sent = True
            if child.poll() is not None:
                break
        if child.poll() is None:
            raise RuntimeError(f"TUI did not quit (q sent: {quit_sent}). Last output: {output[-4000:]!r}")
        if child.returncode != 0:
            raise RuntimeError(f"TUI exited {child.returncode}: {output[-4000:]!r}")
        if b"smoke-project" not in output:
            raise RuntimeError(f"Project never rendered: {output[-4000:]!r}")
        if todo.read_text() != "# TODO\n\n## Smoke\n- [/] Verify terminal\n":
            raise RuntimeError("Read-only smoke test unexpectedly changed task file")
        print("PASS: real PTY startup, global project rendering, q shutdown, source unchanged")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)

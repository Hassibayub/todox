#!/usr/bin/env python3
"""Verify real PTY startup, split-pane resize, output colors and terminal restoration."""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time

root = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix="todo-pty-") as directory:
    folder = Path(directory)
    todo = folder / "TODO.md"
    original = "# TODO\n\n## Smoke\n- [/] Verify terminal\n"
    todo.write_text(original)
    config = folder / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "smoke-project", "path": str(todo)}]}))
    master, slave = pty.openpty()
    initial_mode = termios.tcgetattr(slave)
    widths = [int(os.environ.get("SMOKE_COLUMNS", "90")), 160, 42, 70]
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, widths[0], 0, 0))
    env = dict(os.environ, TERM="xterm-256color", TODOX_CONFIG=str(config))
    command = os.environ.get("TODO_COMMAND", "").split() or [shutil.which("bun"), str(root / "src/cli.ts")]
    child = subprocess.Popen([*command, "--global"], stdin=slave, stdout=slave, stderr=slave, cwd=directory, env=env)
    output = b""
    phase, seen_frames, quit_sent = 0, 0, False
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            frames = output.count(b"Ready")
            if frames > seen_frames and not quit_sent:
                seen_frames = frames
                phase += 1
                if phase < len(widths):
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, widths[phase], 0, 0))
                    os.kill(child.pid, signal.SIGWINCH)
                else:
                    os.write(master, b"q")
                    quit_sent = True
            if child.poll() is not None:
                break
        if child.poll() is None:
            raise RuntimeError(f"TUI did not quit; frames={seen_frames}: {output[-2000:]!r}")
        if child.returncode != 0:
            raise RuntimeError(f"TUI exited {child.returncode}: {output[-2000:]!r}")
        if b"smoke-project" not in output or not quit_sent:
            raise RuntimeError("Project or resize frames did not render")
        if re.search(rb"\x1b\[(?:7|4[0-9]|10[0-7]|48[;:][0-9;:]+)m", output) or b"\x1b]11;" in output:
            raise RuntimeError("App changed terminal background or emitted inverse fill")
        if todo.read_text() != original:
            raise RuntimeError("Read-only smoke test changed task file")
        if termios.tcgetattr(slave) != initial_mode:
            raise RuntimeError("Terminal input mode was not restored")
        if b"\x1b[?25h" not in output or b"\x1b[?1049l" not in output:
            raise RuntimeError("Cursor/alternate screen not restored")
        print(f"PASS: PTY startup, resize {widths}, q shutdown, no background override, terminal restored")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(slave)
        os.close(master)

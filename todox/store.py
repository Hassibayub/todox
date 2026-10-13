"""Lossless TODO.md parsing and guarded, atomic task updates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import os
import re
import stat
import tempfile

STATUS = {" ": "todo", "/": "in progress", "x": "completed"}
DATE = re.compile(r" \(completed: (\d{4}-\d{2}-\d{2})\)$")
UID = re.compile(r"\s*<!--\s*todox:id=([0-9a-zA-Z_-]{6,32})\s*-->$")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
TASK = re.compile(r"^(\s*[-+*]\s+\[)([ /xX])(\]\s+)(.*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class Section:
    line: int  # zero based, -1 is the implicit Main group
    title: str
    end: int
    main: bool = False


@dataclass(frozen=True)
class Task:
    line: int  # zero based
    text: str
    status: str
    section: int
    completed: str | None = None
    uid: str | None = None


@dataclass(frozen=True)
class Document:
    path: Path
    content: str
    lines: tuple[str, ...]
    sections: tuple[Section, ...]
    tasks: tuple[Task, ...]
    newline: str

    def section(self, line: int) -> Section:
        return next(s for s in self.sections if s.line == line)


def parse(content: str, path: Path | str = "") -> Document:
    lines = tuple(part for part in re.findall(r"[^\r\n]*(?:\r\n|\n|\r|$)", content) if part)
    newline = next(("\r\n" if line.endswith("\r\n") else "\r" if line.endswith("\r") else "\n"
                    for line in lines if line.endswith(("\r", "\n"))), "\n")
    sections = [Section(-1, "Main", len(lines), True)]
    tasks: list[Task] = []
    current = -1
    fence: tuple[str, int] | None = None
    for index, raw in enumerate(lines):
        text = raw.rstrip("\r\n").removeprefix("\ufeff") if index == 0 else raw.rstrip("\r\n")
        block = FENCE.match(text)
        if fence:
            if block and block[1][0] == fence[0] and len(block[1]) >= fence[1] and not block[2].strip():
                fence = None
            continue
        if block:
            fence = (block[1][0], len(block[1]))
            continue
        heading = HEADING.match(text)
        if heading and len(heading[1]) == 2:
            previous = sections[-1]
            sections[-1] = Section(previous.line, previous.title, index, previous.main)
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2]).strip()
            # Main is never shown; older files may name the first group General.
            is_main = title.lower() == "main" or (len(sections) == 1 and title.lower() == "general")
            sections.append(Section(index, title, len(lines), is_main))
            current = index
            continue
        if heading:
            continue
        task = TASK.match(text)
        if task:
            raw_title = task[4]
            uid: str | None = None
            marker = UID.search(raw_title)
            if marker:
                uid = marker[1]
                raw_title = raw_title[:marker.start()].rstrip()
            suffix = DATE.search(raw_title)
            tasks.append(Task(index, raw_title[:suffix.start()] if suffix else raw_title,
                              task[2].lower(), current, suffix[1] if suffix else None, uid))
    return Document(Path(path), content, lines, tuple(sections), tuple(tasks), newline)


def read(path: Path | str) -> Document:
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Not a file: {path}")
    return parse(resolved.read_bytes().decode("utf-8"), resolved)


def main_section(doc: Document) -> Section:
    return next((s for s in doc.sections if s.main and s.line >= 0), doc.sections[0])


def _title(value: str, name: str = "task") -> str:
    if not value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError(f"Enter a non-empty, single-line {name}.")
    return value.strip()


def task_line(indent: str, status: str, text: str, completed: str | None = None, uid: str | None = None) -> str:
    """Render one checkbox line; the uid marker stays last so it parses on re-read."""
    suffix = f" (completed: {completed})" if completed else ""
    marker = f" <!-- todox:id={uid} -->" if uid else ""
    return f"{indent}- [{status}] {text}{suffix}{marker}"


def mutate(doc: Document, kind: str, *, line: int = -1, section: int = -1,
           value: str = "", status: str = "", today: date | None = None,
           completed: str | None = None, uid: str | None = None) -> str:
    lines = list(doc.lines)
    if kind == "remove-section":
        target = doc.section(section)
        if target.main or target.line < 0:
            raise ValueError("Main cannot be removed.")
        if any(raw.strip() for raw in lines[target.line + 1:target.end]):
            raise ValueError("Section is not empty. Remove its tasks or notes first.")
        lines.pop(target.line)
        return "".join(lines)
    if kind == "add-section":
        title = _title(value, "section name")
        if any(s.title.casefold() == title.casefold() for s in doc.sections if s.line >= 0):
            raise ValueError(f"Section already exists: {title}")
        if re.sub(r"[ \t]+#+[ \t]*$", "", title) != title:
            raise ValueError("Section names cannot end in Markdown closing # markers.")
        separator = "" if not doc.content or doc.content.endswith(doc.newline * 2) else doc.newline if doc.content.endswith(doc.newline) else doc.newline * 2
        updated = doc.content + separator + f"## {title}{doc.newline}"
        if len(parse(updated).sections) != len(doc.sections) + 1:
            raise ValueError("Close the Markdown code fence before adding a section.")
        return updated
    if kind == "add":
        title = _title(value)
        status = status or " "
        if status not in STATUS:
            raise ValueError("Invalid status.")
        if status == "x" and not completed:
            completed = (today or date.today()).isoformat()
        if status != "x":
            completed = None
        target = doc.section(section)
        at = target.end
        before = "".join(lines[:at])
        separator = "" if not before or before.endswith(("\n", "\r")) else doc.newline
        lines[at:at] = [separator + task_line("", status, title, completed, uid) + doc.newline]
        return "".join(lines)
    task = next((t for t in doc.tasks if t.line == line), None)
    if task is None:
        raise ValueError("Task changed or disappeared. Refresh before editing.")
    original = lines[line]
    eol = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else "\r" if original.endswith("\r") else ""
    match = TASK.match(original.removesuffix(eol))
    assert match
    if kind == "delete":
        lines.pop(line)
    elif kind == "uid":
        if uid and task.uid and task.uid != uid:
            raise ValueError("Task already has a different sync id.")
        lines[line] = match[1] + match[2] + match[3] + task.text + _completed(task.completed) + _marker(uid or task.uid) + eol
    elif kind == "edit":
        lines[line] = match[1] + match[2] + match[3] + _title(value) + _completed(task.completed) + _marker(task.uid) + eol
    elif kind == "status":
        if status not in STATUS:
            raise ValueError("Invalid status.")
        if status == "x":
            completed = completed or task.completed or (today or date.today()).isoformat()
        else:
            completed = None
        lines[line] = match[1] + status + match[3] + task.text + _completed(completed) + _marker(task.uid) + eol
    else:
        raise ValueError(f"Unknown mutation: {kind}")
    return "".join(lines)


def _completed(value: str | None) -> str:
    return f" (completed: {value})" if value else ""


def _marker(uid: str | None) -> str:
    return f" <!-- todox:id={uid} -->" if uid else ""


def save(doc: Document, kind: str, **kwargs: object) -> Document:
    path = doc.path.resolve(strict=True)
    lock = Path(str(path) + ".todox.lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temp: str | None = None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        if path.read_bytes().decode("utf-8") != doc.content:
            raise ValueError("TODO.md changed outside todox. Refresh and try again.")
        updated = mutate(doc, kind, **kwargs)
        mode = stat.S_IMODE(path.stat().st_mode)
        out, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(out, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(updated.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes().decode("utf-8") != doc.content:
            raise ValueError("TODO.md changed while saving. Refresh and try again.")
        os.replace(temp, path)
        temp = None
        return parse(updated, path)
    finally:
        if temp:
            os.unlink(temp)
        os.close(fd)
        lock.unlink()


def counts(tasks: tuple[Task, ...] | list[Task]) -> dict[str, int]:
    return {s: sum(task.status == s for task in tasks) for s in STATUS}

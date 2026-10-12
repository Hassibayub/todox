from datetime import date
from pathlib import Path

import pytest

from todox.store import main_section, mutate, parse, read, save
from todox.projects import find_local, load_config, register


def test_sections_and_main_and_fenced_tasks():
    doc = parse("# TODO\n- [ ] Main\n## Work\n- [/] Active\n```md\n- [x] Not a task\n```\n## Next\n- [x] Done (completed: 2026-10-12)\n")
    assert [(t.text, t.status, t.section, t.completed) for t in doc.tasks] == [
        ("Main", " ", -1, None), ("Active", "/", 2, None), ("Done", "x", 7, "2026-10-12")]
    assert main_section(doc).line == -1
    assert "- [ ] New\n## Work" in mutate(doc, "add", section=-1, value="New")
    assert "- [ ] New\n## Next" in mutate(doc, "add", section=2, value="New")


def test_old_general_is_hidden_main_and_new_sections():
    doc = parse("# TODO\n\n## General\n- [ ] One\n## Later\n")
    assert main_section(doc).line == 2
    assert doc.sections[1].main
    assert "- [ ] Two\n## Later" in mutate(doc, "add", section=2, value="Two")
    assert mutate(doc, "add-section", value="Further").endswith("\n\n## Further\n")
    assert parse("## C#\n- [ ] Code\n").sections[1].title == "C#"
    assert parse("## Work\n- [ ] One\n## Main\n- [ ] Two\n").sections[2].main
    with pytest.raises(ValueError, match="code fence"):
        mutate(parse("# TODO\n```md\n"), "add-section", value="Hidden")


def test_completion_date_is_added_preserved_and_removed():
    doc = parse("- [ ] Task\r\n")
    done = parse(mutate(doc, "status", line=0, status="x", today=date(2026, 10, 12)))
    assert done.content == "- [x] Task (completed: 2026-10-12)\r\n"
    assert mutate(done, "edit", line=0, value="Better") == "- [x] Better (completed: 2026-10-12)\r\n"
    assert mutate(done, "status", line=0, status="x", today=date(2026, 10, 13)) == done.content
    assert mutate(done, "status", line=0, status="/") == "- [/] Task\r\n"


def test_guarded_save_preserves_other_content(tmp_path: Path):
    path = tmp_path / "TODO.md"
    path.write_bytes(b"# Title\r\nnotes\r\n- [ ] A\r\n")
    first = read(path)
    done = save(first, "status", line=2, status="x", today=date(2026, 10, 12))
    assert b"notes\r\n- [x] A (completed: 2026-10-12)\r\n" in path.read_bytes()
    with pytest.raises(ValueError, match="changed outside"):
        save(first, "edit", line=2, value="Stale")
    assert not Path(str(path) + ".todox.lock").exists()
    assert done.tasks[0].completed == "2026-10-12"


def test_empty_sections_and_unicode_line_separator(tmp_path: Path):
    doc = parse("# TODO\n## Work\n\n## Notes\nText\u2028still one line\n")
    assert len(doc.lines) == 5
    assert mutate(doc, "remove-section", section=1) == "# TODO\n\n## Notes\nText\u2028still one line\n"
    with pytest.raises(ValueError, match="not empty"):
        mutate(doc, "remove-section", section=3)
    with pytest.raises(ValueError, match="Main"):
        mutate(doc, "remove-section", section=-1)


def test_local_discovery_and_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TODOX_CONFIG", str(tmp_path / "config" / "projects.json"))
    root = tmp_path / "work"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "TODO.md").write_text("# TODO\n")
    assert find_local(nested) == root / "TODO.md"
    register(root)
    assert load_config()[0].path == root / "TODO.md"
    (nested / ".git").mkdir()
    assert find_local(nested) is None

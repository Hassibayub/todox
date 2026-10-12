import { afterEach, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync, symlinkSync, statSync, existsSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { counts, mutateContent, parseMarkdown, readDocument, saveMutation } from "../src/markdown";
import { findLocalTodo, loadConfig, loadProjects, registerProject, unregisterProject } from "../src/projects";
const dirs: string[] = [];
function temp() { const d = realpathSync(mkdtempSync(join(tmpdir(), "todox-test-"))); dirs.push(d); return d; }
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }); });
const sample = "# TODO\n\n## Frontend\n- [x] create login page\n\n## Backend\n- [/] init db\n- [ ] create migration\n";

test("parses statuses, line numbers, nested headings and counts", () => {
  const doc = parseMarkdown(sample + "### Operations\n  * [X] deployed\n");
  expect(doc.tasks.map(t => [t.line, t.section, t.status])).toEqual([[4, "Frontend", "x"], [7, "Backend", "/"], [8, "Backend", " "], [10, "Backend / Operations", "x"]]);
  expect(counts(doc.tasks)).toEqual({ " ": 1, "/": 1, x: 2 });
});
test("ignores fenced examples and non-task lists", () => {
  const doc = parseMarkdown("```md\n## fake\n- [ ] fake\n````\n~~~\n- [x] fake\n~~~\n- regular\n- [ ] real\n");
  expect(doc.tasks.map(t => t.text)).toEqual(["real"]);
});
test("status/edit preserve BOM, CRLF, bullet, nesting and unrelated notes", () => {
  const source = "\uFEFF# TODO\r\n## Work\r\n  + [/] old task\r\n    keep note\r\n";
  const doc = parseMarkdown(source);
  expect(mutateContent(doc, { kind: "status", line: 3, status: "x" })).toBe(source.replace("[/]", "[x]"));
  expect(mutateContent(doc, { kind: "edit", line: 3, text: "new task" })).toBe(source.replace("old task", "new task"));
  expect(mutateContent(parseMarkdown("\uFEFF- [ ] first"), { kind: "status", line: 1, status: "/" })).toBe("\uFEFF- [/] first");
});
test("adds to selected section and supports an empty file or missing final newline", () => {
  const doc = parseMarkdown(sample);
  expect(mutateContent(doc, { kind: "add", text: "new UI", sectionId: 2 })).toContain("- [ ] new UI\n## Backend");
  expect(mutateContent(parseMarkdown(""), { kind: "add", text: "first" })).toBe("- [ ] first\n");
  expect(mutateContent(parseMarkdown("# TODO\r\n- [ ] old"), { kind: "add", text: "next" })).toBe("# TODO\r\n- [ ] old\r\n- [ ] next\r\n");
});
test("deletes only checkbox line; rejects multiline, blank and stale targets", () => {
  expect(mutateContent(parseMarkdown("- [ ] parent\n  note\n  - [ ] child\n"), { kind: "delete", line: 1 })).toBe("  note\n  - [ ] child\n");
  for (const text of ["", "  ", "injected\n- [ ] another", "hello\x1b"]) expect(() => mutateContent(parseMarkdown(sample), { kind: "add", text })).toThrow();
  expect(() => mutateContent(parseMarkdown(sample), { kind: "delete", line: 1 })).toThrow();
});
test("atomic writes preserve mode, reject external edits, clean up locks", () => {
  const path = join(temp(), "TODO.md"); writeFileSync(path, sample, { mode: 0o640 });
  const doc = readDocument(path);
  const updated = saveMutation(doc, { kind: "status", line: 8, status: "/" });
  expect(updated.tasks.at(-1)!.status).toBe("/");
  expect(statSync(path).mode & 0o777).toBe(0o640);
  writeFileSync(path, sample + "external change\n");
  expect(() => saveMutation(updated, { kind: "delete", line: 8 })).toThrow("changed outside");
  expect(readFileSync(path, "utf8")).toContain("external change");
  expect(existsSync(`${path}.todox.lock`)).toBe(false);
});
test("symlink target survives edits and cooperative locks prevent writers", () => {
  const d = temp(); const path = join(d, "real.md"); const link = join(d, "TODO.md");
  writeFileSync(path, sample); symlinkSync(path, link);
  const doc = readDocument(link); saveMutation(doc, { kind: "status", line: 8, status: "x" });
  expect(readFileSync(link, "utf8")).toContain("[x] create migration");
  writeFileSync(`${path}.todox.lock`, "");
  expect(() => saveMutation(readDocument(link), { kind: "delete", line: 8 })).toThrow("busy");
});
test("local discovery walks up but respects git boundaries", () => {
  const d = temp(); mkdirSync(join(d, "repo", "src"), { recursive: true });
  writeFileSync(join(d, "TODO.md"), sample); mkdirSync(join(d, "repo", ".git"));
  expect(findLocalTodo(join(d, "repo", "src"))).toBeUndefined();
  writeFileSync(join(d, "repo", "TODO.md"), sample);
  expect(findLocalTodo(join(d, "repo", "src"))).toBe(join(d, "repo", "TODO.md"));
});
test("registry deduplicates, uses relative paths, tolerates missing project files", () => {
  const d = temp(); const file = join(d, "TODO.md"); const registry = join(d, "config", "projects.json");
  writeFileSync(file, sample);
  registerProject(d, undefined, registry); registerProject(file, "Renamed", registry);
  expect(loadConfig(registry).projects).toEqual([{ name: "Renamed", path: file }]);
  rmSync(file);
  expect(loadProjects(loadConfig(registry).projects)[0]!.error).toBeTruthy();
  unregisterProject(d, registry);
  expect(loadConfig(registry).projects).toEqual([]);
  writeFileSync(registry, JSON.stringify({ version: 1, projects: [{ name: "relative", path: "../TODO.md" }] }));
  expect(loadConfig(registry).projects[0]!.path).toBe(file);
});

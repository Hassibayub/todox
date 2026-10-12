import { afterEach, expect, test } from "bun:test";
import { mkdtempSync, realpathSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mutateContent, parseMarkdown, readDocument, requireEmptySection, saveMutation } from "../src/markdown";
import { TodoModel } from "../src/model";

const dirs: string[] = [];
function fixture(content = "# TODO\n\n## Frontend\n- [ ] Login\n\n## Empty\n\n") {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "todox-sections-"))); dirs.push(dir);
  const file = join(dir, "TODO.md"); const registry = join(dir, "projects.json");
  writeFileSync(file, content); writeFileSync(registry, JSON.stringify({ version: 1, projects: [{ name: "web", path: file }] }));
  return { file, model: new TodoModel(file, registry) };
}
afterEach(() => { for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true }); });

test("creates an empty top-level heading without altering existing text or CRLF", () => {
  const source = "\uFEFF# TODO\r\n\r\n## Backend\r\n- [/] DB";
  const updated = mutateContent(parseMarkdown(source), { kind: "section-add", name: "Infrastructure" });
  expect(updated).toBe(source + "\r\n\r\n## Infrastructure\r\n");
  expect(parseMarkdown(updated).sections.at(-1)!.title).toBe("Infrastructure");
  expect(mutateContent(parseMarkdown(""), { kind: "section-add", name: "First" })).toBe("## First\n");
  expect(mutateContent(parseMarkdown("\uFEFF"), { kind: "section-add", name: "First" })).toBe("\uFEFF## First\n");
});
test("rejects duplicate/invalid names and appending inside an unclosed code fence", () => {
  const doc = parseMarkdown("# TODO\n## Backend\n");
  for (const name of ["", "  ", "backend", "Bad\n## injected", "ANSI\x1b", "Name ###"]) {
    expect(() => mutateContent(doc, { kind: "section-add", name })).toThrow();
  }
  expect(() => mutateContent(parseMarkdown("# TODO\n```md\nexample\n"), { kind: "section-add", name: "Real" })).toThrow("unclosed code fence");
});
test("preserves literal C# names while interpreting spaced closing ATX markers", () => {
  expect(parseMarkdown("## C#\n- [ ] Compile\n").tasks[0]!.section).toBe("C#");
  expect(parseMarkdown("## Backend ###\n- [ ] DB\n").tasks[0]!.section).toBe("Backend");
  expect(parseMarkdown(mutateContent(parseMarkdown("# TODO\n"), { kind: "section-add", name: "C#" })).sections.at(-1)!.title).toBe("C#");
});
test("removes only an empty heading, retaining surrounding whitespace and BOM", () => {
  const source = "# TODO\r\n\r\n## Empty\r\n \t\r\n## Work\r\n- [ ] Keep\r\n";
  expect(mutateContent(parseMarkdown(source), { kind: "section-remove", sectionId: 2 })).toBe(source.replace("## Empty\r\n", ""));
  expect(mutateContent(parseMarkdown("\uFEFF## Empty\r\n\r\n"), { kind: "section-remove", sectionId: 0 })).toBe("\uFEFF\r\n");
});
test("tasks, notes, comments, fences and even empty nested headings block removal", () => {
  for (const body of ["- [ ] Task\n", "- [x] Done\n", "Keep this note\n", "<!-- note -->\n", "```\n```\n", "### Child\n\n"]) {
    const doc = parseMarkdown(`# TODO\n## Parent\n${body}## Other\n`);
    expect(() => requireEmptySection(doc, 1)).toThrow("not empty");
    expect(() => mutateContent(doc, { kind: "section-remove", sectionId: 1 })).toThrow("not empty");
  }
});
test("protects document title and implicit General; child removal stops at sibling", () => {
  const doc = parseMarkdown("# TODO\n\n## Parent\n### Empty child\n\n### Filled child\n- [ ] Keep\n");
  expect(() => requireEmptySection(doc, -1)).toThrow("cannot be removed");
  expect(() => requireEmptySection(doc, 0)).toThrow("cannot be removed");
  expect(() => requireEmptySection(doc, 999)).toThrow("no longer exists");
  const updated = mutateContent(doc, { kind: "section-remove", sectionId: 3 });
  expect(updated).not.toContain("### Empty child"); expect(updated).toContain("### Filled child\n- [ ] Keep");
});
test("creates and selects a section, then adds a task into it", () => {
  const { model, file } = fixture(); model.setFilter("/");
  model.begin("section-add"); model.submit("Infrastructure");
  const node = model.selectedNode;
  expect(node?.kind === "section" && node.title).toBe("Infrastructure");
  expect(model.filter).toBe("all");
  model.begin("add"); model.submit("Deploy");
  expect(model.selected?.task.section).toBe("Infrastructure");
  expect(readFileSync(file, "utf8")).toContain("## Infrastructure\n- [ ] Deploy\n");
});
test("remove confirmation can be cancelled, and populated sections never open it", () => {
  const { model, file } = fixture(); const before = readFileSync(file, "utf8");
  model.begin("section-remove"); expect(model.dialog).toBeUndefined(); expect(model.error).toBe(true);
  expect(readFileSync(file, "utf8")).toBe(before);
  model.index = model.displayRows.findIndex(n => n.kind === "section" && n.title === "Empty");
  model.begin("section-remove"); expect(model.dialog?.kind).toBe("section-remove");
  model.cancel(); expect(readFileSync(file, "utf8")).toBe(before);
  model.begin("section-remove"); model.submit();
  expect(readFileSync(file, "utf8")).not.toContain("## Empty"); expect(readFileSync(file, "utf8")).toContain("- [ ] Login");
});
test("creation from the global task view opens the correct project and reveals the empty section", () => {
  const { model, file } = fixture(); model.allTasks(); model.begin("section-add"); model.submit("Global-created");
  expect(model.local).toBe(file);
  const node = model.selectedNode;
  expect(node?.kind === "section" && node.title).toBe("Global-created");
});
test("duplicate and stale section edits retain the dialog and never overwrite external work", () => {
  const { model, file } = fixture(); model.begin("section-add"); model.submit("frontend");
  expect(model.error).toBe(true); expect(model.dialog?.kind).toBe("section-add"); model.cancel();
  model.index = model.displayRows.findIndex(n => n.kind === "section" && n.title === "Empty"); model.begin("section-remove");
  writeFileSync(file, readFileSync(file, "utf8") + "external note\n"); model.submit();
  expect(model.dialog?.kind).toBe("section-remove"); expect(readFileSync(file, "utf8")).toContain("external note");
  model.cancel(); model.reload();
  expect(() => saveMutation(readDocument(file), { kind: "section-remove", sectionId: model.scope[0]!.doc!.sections.at(-1)!.id })).toThrow("not empty");
});

import { afterEach, expect, test } from "bun:test";
import { mkdtempSync, realpathSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { TodoModel } from "../src/model";

const dirs: string[] = [];
function fixture(content = "# TODO\n\n## Frontend\n- [ ] Login\n- [/] Session\n- [x] Styles\n\n## Tests\n- [ ] Sessions\n") {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), "todox-workspace-"))); dirs.push(directory);
  const file = join(directory, "TODO.md"); const registry = join(directory, "projects.json");
  writeFileSync(file, content);
  writeFileSync(registry, JSON.stringify({ version: 1, projects: [{ name: "web", path: file }, { name: "missing", path: join(directory, "missing.md") }] }));
  return { file, registry, model: new TodoModel(file, registry) };
}
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }); });

test("headings render once; completed tasks are folded, not discarded", () => {
  const { model, file } = fixture();
  expect(model.displayRows.filter(n => n.kind === "section").map(n => n.title)).toEqual(["Frontend", "Tests"]);
  expect(model.rows.length).toBe(4);
  expect(model.displayRows.filter(n => n.kind === "task").length).toBe(3);
  const completed = model.displayRows.findIndex(n => n.kind === "completed");
  model.index = completed; model.toggleSelected();
  expect(model.displayRows.filter(n => n.kind === "task").length).toBe(4);
  expect(readFileSync(file, "utf8")).toContain("- [x] Styles");
});
test("folding a section is view-only; search and filters reveal matches", () => {
  const { model, file } = fixture(); const before = readFileSync(file, "utf8");
  model.index = 0; model.toggleSelected();
  expect(model.displayRows[0]!.kind === "section" && model.displayRows[0]!.collapsed).toBe(true);
  expect(model.displayRows.some(n => n.kind === "task" && n.row.task.text === "Login")).toBe(false);
  model.begin("search"); model.input("Styles"); model.submit();
  expect(model.selected?.task.text).toBe("Styles");
  model.clearFilters(); model.setFilter("x");
  expect(model.selected?.task.text).toBe("Styles");
  expect(readFileSync(file, "utf8")).toBe(before);
});
test("explicit filters select without cycling; cancellation preserves the old filter", () => {
  const { model } = fixture(); model.begin("filter"); model.moveDialog(2); model.submit();
  expect(model.filter).toBe("/"); expect(model.selected?.task.text).toBe("Session");
  model.begin("filter"); model.moveDialog(1); model.cancel(); expect(model.filter).toBe("/");
});
test("sidebar scope navigation and unavailable projects have distinct states", () => {
  const { model } = fixture();
  expect(model.navigation.map(n => n.label)).toEqual(["Overview", "In progress", "All tasks", "web", "missing"]);
  model.setFocus("sidebar"); model.move(-2); model.openNavigation();
  expect(model.scopeId).toBe("active"); expect(model.filter).toBe("/"); expect(model.focus).toBe("content");
  model.activate(model.projects[1]!.path);
  expect(model.scope[0]!.error).toBeTruthy(); expect(model.selected).toBeUndefined();
});
test("marking a task done reveals its completed group and preserves selection", () => {
  const { model } = fixture(); expect(model.selected?.task.text).toBe("Login");
  model.changeStatus("x");
  expect(model.selected?.task.text).toBe("Login"); expect(model.selected?.task.status).toBe("x");
  expect(model.displayRows.find(n => n.kind === "completed")?.collapsed).toBe(false);
});
test("adding a duplicate title selects the actual insertion, not a later duplicate", () => {
  const { model } = fixture("# TODO\n## Frontend\n- [ ] Same\n## Backend\n- [ ] Same\n");
  model.begin("add"); model.submit("Same");
  expect(model.selected?.task.section).toBe("Frontend"); expect(model.selected?.task.line).toBe(4);
});
test("section identities survive insertions before later headings", () => {
  const { model } = fixture();
  model.index = model.displayRows.findIndex(n => n.kind === "section" && n.title === "Tests"); model.toggleSelected();
  const folded = [...model.collapsedSections][0];
  model.index = 1; model.begin("add"); model.submit("Another screen");
  expect(model.collapsedSections.has(folded!)).toBe(true);
  const testsSection = model.displayRows.find(n => n.kind === "section" && n.title === "Tests");
  expect(testsSection?.kind === "section" && testsSection.collapsed).toBe(true);
});
test("large lists expose folds without mounting one UI row per task", () => {
  const { model } = fixture("# TODO\n## Many tasks\n" + Array.from({ length: 1500 }, (_, i) => `- [ ] Task ${i}\n`).join(""));
  expect(model.rows.length).toBe(1500);
  model.setAllCollapsed(true); expect(model.displayRows.length).toBe(1);
  model.setAllCollapsed(false); expect(model.displayRows.length).toBe(1501);
});

test("a newly added task is visible even when adding from the Done filter", () => {
  const { model } = fixture(); model.setFilter("x"); model.begin("add"); model.submit("New pending task");
  expect(model.filter).toBe("all");
  expect(model.selected?.task.text).toBe("New pending task");
  expect(model.selected?.task.status).toBe(" ");
});

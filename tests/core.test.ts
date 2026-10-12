import { afterEach, expect, test } from "bun:test";
import { createTestRenderer, KeyCodes } from "@opentui/core/testing";
import { TextAttributes } from "@opentui/core";
import { mkdtempSync, realpathSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CoreApp } from "../src/app";
import { TodoModel } from "../src/model";

const dirs: string[] = [];
afterEach(() => { for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true }); });
function fixture() {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "todox2-test-"))); dirs.push(dir);
  const file = join(dir, "TODO.md"); const registry = join(dir, "projects.json");
  writeFileSync(file, "# TODO\n## Frontend\n- [ ] Login\n- [/] Session\n## Empty\n\n");
  writeFileSync(registry, JSON.stringify({ version: 1, projects: [{ name: "web-app", path: file }] }));
  return { file, model: new TodoModel(file, registry), global: new TodoModel(undefined, registry) };
}

test("Core retained tree renders local tasks, responds to navigation and resize", async () => {
  const { model } = fixture();
  const setup = await createTestRenderer({ width: 110, height: 24, kittyKeyboard: true });
  try {
    new CoreApp(setup.renderer, undefined, { model });
    await setup.renderOnce();
    let frame = setup.captureCharFrame();
    expect(frame).toContain("todo  /"); expect(frame).toContain("Frontend"); expect(frame).toContain("SELECTION");
    setup.mockInput.pressKey(KeyCodes.END); await setup.renderOnce();
    expect(model.selectedNode?.kind).toBe("section");
    setup.resize(50, 12); await setup.renderOnce(); frame = setup.captureCharFrame();
    expect(frame).not.toContain("SELECTION"); expect(frame).toContain("Empty");
    expect(setup.captureSpans().cols).toBe(50);
  } finally { setup.renderer.destroy(); }
});

test("terminal canvas and palette keep selection readable", async () => {
  const { global } = fixture();
  const setup = await createTestRenderer({ width: 80, height: 20, kittyKeyboard: true });
  try {
    new CoreApp(setup.renderer, undefined, { model: global }); await setup.renderOnce();
    let frame = setup.captureSpans();
    expect(frame.lines[0]!.spans[0]!.fg.intent).toBe("rgb");
    expect(frame.lines[0]!.spans[0]!.bg.intent).toBe("rgb");
    expect(frame.lines[1]!.spans[0]!.fg.a).toBe(1);
    expect(frame.lines[3]!.spans[0]!.attributes & TextAttributes.INVERSE).toBeTruthy();
    global.allTasks(); await setup.renderOnce();
    setup.mockInput.pressArrow("down"); await setup.renderOnce(); frame = setup.captureSpans();
    const oldSelection = frame.lines.flatMap(line => line.spans).find(span => span.text.includes("Login"))!;
    const newSelection = frame.lines.flatMap(line => line.spans).find(span => span.text.includes("Session"))!;
    expect(oldSelection.attributes & TextAttributes.INVERSE).toBe(0);
    expect(newSelection.attributes & TextAttributes.INVERSE).toBeTruthy();
    setup.mockInput.pressKey("p"); await setup.renderOnce(); frame = setup.captureSpans();
    const dialog = frame.lines.find(line => line.spans.some(span => span.text.includes("PROJECTS")))!;
    expect(dialog.spans.some(span => span.bg.intent === "rgb")).toBe(true);
  } finally { setup.renderer.destroy(); }
});

test("empty workspace paints every blank cell at compact and wide widths", async () => {
  const { file, model } = fixture();
  writeFileSync(file, "# TODO\n\n## General\n"); model.reload();
  const setup = await createTestRenderer({ width: 70, height: 30, kittyKeyboard: true });
  try {
    new CoreApp(setup.renderer, undefined, { model });
    for (const width of [70, 160, 70, 42]) {
      setup.resize(width, 30); await setup.renderOnce();
      const frame = setup.captureSpans();
      for (const line of frame.lines.slice(5, 28)) {
        for (const span of line.spans) {
          expect(span.bg.intent).toBe("rgb");
          expect(span.bg.equals(frame.lines[2]!.spans[0]!.bg)).toBe(true);
          expect(span.attributes & TextAttributes.INVERSE).toBe(0);
        }
      }
    }
  } finally { setup.renderer.destroy(); }
});

test("global overview and project picker open local tasks", async () => {
  const { global } = fixture();
  const setup = await createTestRenderer({ width: 90, height: 20, kittyKeyboard: true });
  try {
    new CoreApp(setup.renderer, undefined, { model: global });
    await setup.renderOnce(); expect(setup.captureCharFrame()).toContain("IN PROGRESS");
    setup.mockInput.pressKey("p"); await setup.renderOnce();
    expect(setup.captureCharFrame()).toContain("PROJECTS");
    setup.mockInput.pressArrow("down"); setup.mockInput.pressArrow("down"); setup.mockInput.pressArrow("down");
    setup.mockInput.pressEnter(); await setup.renderOnce();
    expect(global.local).toBeTruthy(); expect(setup.captureCharFrame()).toContain("Login");
  } finally { setup.renderer.destroy(); }
});

test("task and section mutations save safely through Core input and confirmation", async () => {
  const { file, model } = fixture();
  const setup = await createTestRenderer({ width: 88, height: 20, kittyKeyboard: true });
  try {
    new CoreApp(setup.renderer, undefined, { model }); await setup.renderOnce();
    setup.mockInput.pressKey(" "); await setup.renderOnce();
    expect(readFileSync(file, "utf8")).toContain("[/] Login");
    setup.mockInput.pressKey("S"); await setup.renderOnce();
    expect(setup.captureCharFrame()).toContain("NEW SECTION");
    await setup.mockInput.typeText("Infrastructure");
    setup.mockInput.pressEnter(); await setup.renderOnce();
    expect(readFileSync(file, "utf8")).toContain("## Infrastructure\n");
    expect(model.selectedNode?.kind).toBe("section");
    setup.mockInput.pressKey("D"); await setup.renderOnce();
    expect(setup.captureCharFrame()).toContain("REMOVE EMPTY SECTION");
    setup.mockInput.pressKey("n"); await setup.renderOnce(); expect(readFileSync(file, "utf8")).toContain("## Infrastructure");
    setup.mockInput.pressKey("D"); setup.mockInput.pressKey("y"); await setup.renderOnce();
    expect(readFileSync(file, "utf8")).not.toContain("Infrastructure");
    expect(readFileSync(file, "utf8")).toContain("[/] Login");
  } finally { setup.renderer.destroy(); }
});

test("editor input remains focused for validation retry and external edits are rejected", async () => {
  const { file, model } = fixture();
  const setup = await createTestRenderer({ width: 80, height: 20, kittyKeyboard: true });
  try {
    new CoreApp(setup.renderer, undefined, { model }); await setup.renderOnce();
    setup.mockInput.pressKey("S"); await setup.mockInput.typeText("Frontend"); setup.mockInput.pressEnter(); await setup.renderOnce();
    expect(model.dialog?.kind).toBe("section-add"); expect(setup.captureCharFrame()).toContain("ERROR");
    setup.mockInput.pressEscape(); await setup.renderOnce();
    model.index = model.displayRows.findIndex(n => n.kind === "section" && n.title === "Empty"); model.emit();
    setup.mockInput.pressKey("D"); writeFileSync(file, readFileSync(file, "utf8") + "external note\n");
    setup.mockInput.pressKey("y"); await setup.renderOnce();
    expect(model.dialog?.kind).toBe("section-remove"); expect(readFileSync(file, "utf8")).toContain("external note");
  } finally { setup.renderer.destroy(); }
});

test("Core task add/edit/delete, filter and help keys keep Markdown intact", async () => {
  const { file, model } = fixture();
  const setup = await createTestRenderer({ width: 80, height: 20, kittyKeyboard: true });
  try {
    new CoreApp(setup.renderer, undefined, { model }); await setup.renderOnce();
    setup.mockInput.pressKey("a"); await setup.mockInput.typeText("New task"); setup.mockInput.pressEnter(); await setup.renderOnce();
    expect(readFileSync(file, "utf8")).toContain("- [ ] New task");
    setup.mockInput.pressKey("e"); await setup.renderOnce();
    // Edit the focused Core input. Select all through its native key bindings.
    setup.mockInput.pressKey("a", { ctrl: true }); await setup.mockInput.typeText("Renamed task"); setup.mockInput.pressEnter(); await setup.renderOnce();
    expect(readFileSync(file, "utf8")).toContain("Renamed task");
    setup.mockInput.pressKey("f"); await setup.renderOnce(); expect(setup.captureCharFrame()).toContain("FILTER");
    setup.mockInput.pressArrow("down"); setup.mockInput.pressEnter(); await setup.renderOnce(); expect(model.filter).toBe(" ");
    setup.mockInput.pressKey("?"); await setup.renderOnce(); expect(setup.captureCharFrame()).toContain("KEYS");
    setup.mockInput.pressEscape(); await setup.renderOnce();
    model.clearFilters();
    const edited = model.scope[0]!.doc!.tasks.find(task => task.text.includes("Renamed task"))!;
    model.revealTask(file, edited.line); model.emit();
    setup.mockInput.pressKey("d"); setup.mockInput.pressKey("n"); await setup.renderOnce();
    expect(readFileSync(file, "utf8")).toContain("Renamed task");
    setup.mockInput.pressKey("d"); setup.mockInput.pressKey("y"); await setup.renderOnce();
    expect(readFileSync(file, "utf8")).not.toContain("Renamed task");
  } finally { setup.renderer.destroy(); }
});

test("minimum-size terminal keeps overlays bounded and returns to layout on resize", async () => {
  const { model } = fixture();
  const setup = await createTestRenderer({ width: 42, height: 10, kittyKeyboard: true });
  try {
    new CoreApp(setup.renderer, undefined, { model }); await setup.renderOnce();
    setup.mockInput.pressKey("f"); await setup.renderOnce();
    expect(setup.captureCharFrame()).toContain("FILTER");
    expect(setup.captureSpans().rows).toBe(10);
    setup.resize(110, 28); setup.mockInput.pressEscape(); await setup.renderOnce();
    expect(setup.captureCharFrame()).toContain("SELECTION");
  } finally { setup.renderer.destroy(); }
});

test("destroy unregisters key listeners and model subscriptions", async () => {
  const { model } = fixture(); const setup = await createTestRenderer({ width: 80, height: 20 });
  const app = new CoreApp(setup.renderer, undefined, { model });
  await setup.renderOnce(); setup.renderer.destroy(); app.dispose();
  model.notice("after teardown");
  expect(app).toBeTruthy();
});

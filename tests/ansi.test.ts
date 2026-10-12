import { afterEach, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AnsiApp, encodeFrame } from "../src/app";
import { KeyDecoder, type Key } from "../src/keys";
import { TodoModel } from "../src/model";
const dirs: string[] = [];
afterEach(() => { for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true }); });
function fixture(global = false) {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "todo-ansi-"))); dirs.push(dir);
  const file = join(dir, "TODO.md"), config = join(dir, "projects.json");
  writeFileSync(file, "# TODO\n## Frontend\n- [ ] Login\n- [/] Session\n## Empty\n");
  writeFileSync(config, JSON.stringify({ version: 1, projects: [{ name: "web-app", path: file }] }));
  const model = new TodoModel(global ? undefined : file, config);
  let quit = false;
  const app = new AnsiApp(model, undefined, () => { quit = true; });
  const decoder = new KeyDecoder();
  const send = (raw: string) => { for (const key of decoder.feed(raw)) app.handle(key); };
  return { app, model, file, send, quit: () => quit };
}
test("renders bounded frames at narrow and wide widths without background overrides", () => {
  const { app } = fixture();
  for (const [width, height] of [[42, 10], [70, 24], [160, 40], [15, 5]]) {
    const frame = app.frame(width!, height!);
    expect(frame.length).toBe(height!);
    const ansi = encodeFrame(frame, width!);
    expect(ansi).not.toMatch(/\x1b\[(?:7|4[0-9]|10[0-7]|48[;:][0-9;:]+)m/);
    for (const row of ansi.split(/\x1b\[\d+;1H/).slice(1)) expect(Bun.stringWidth(Bun.stripANSI(row))).toBeLessThanOrEqual(width! - 1);
  }
  app.dispose();
});
test("keys navigate, filter, show help and quit", () => {
  const f = fixture();
  f.send("j"); expect(f.model.selected?.task.text).toBe("Session");
  f.send("f\x1b[B\r"); expect(f.model.filter).toBe(" ");
  f.send("?"); expect(f.app.frame(42, 10).some(l => l.text.includes("KEYS"))).toBe(true);
  f.app.handle({ name: "escape" }); f.send("q"); expect(f.quit()).toBe(true); f.app.dispose();
});
test("overview opens a project and all task scopes remain available", () => {
  const f = fixture(true);
  expect(f.app.frame(60, 12).some(l => l.text.includes("web-app"))).toBe(true);
  f.send("\r"); expect(f.model.local).toBe(f.file);
  f.send("w"); expect(f.model.filter).toBe("/");
  f.send("t"); expect(f.model.filter).toBe("all"); f.app.dispose();
});
test("adds, edits and deletes tasks safely through inline input", () => {
  const f = fixture();
  f.send("aNew task\r"); expect(readFileSync(f.file, "utf8")).toContain("- [ ] New task");
  f.send("e\x01Renamed\r"); expect(readFileSync(f.file, "utf8")).toContain("- [ ] Renamed");
  f.send("dn"); expect(readFileSync(f.file, "utf8")).toContain("Renamed");
  f.send("dy"); expect(readFileSync(f.file, "utf8")).not.toContain("Renamed"); f.app.dispose();
});
test("creates and removes empty sections, blocks populated sections", () => {
  const f = fixture(); f.send("SInfrastructure\r");
  expect(readFileSync(f.file, "utf8")).toContain("## Infrastructure");
  f.send("Dn"); expect(readFileSync(f.file, "utf8")).toContain("Infrastructure");
  f.send("Dy"); expect(readFileSync(f.file, "utf8")).not.toContain("Infrastructure");
  f.send("\x1b[H"); f.send("D"); expect(f.model.error).toBe(true); f.app.dispose();
});
test("stale file save rejects edits and retains retry input", () => {
  const f = fixture(); f.send("e\x01Changed");
  writeFileSync(f.file, readFileSync(f.file, "utf8") + "external note\n");
  f.send("\r"); expect(f.model.error).toBe(true); expect(f.model.dialog?.kind).toBe("edit");
  expect(readFileSync(f.file, "utf8")).toContain("external note");
  expect(readFileSync(f.file, "utf8")).not.toContain("Changed"); f.app.dispose();
});
test("inline editor handles graphemes, arrows, delete, selection and pasted newlines", () => {
  const f = fixture(); f.send("a😀é"); f.send("\x7f");
  expect(f.model.dialog && "value" in f.model.dialog && f.model.dialog.value).toBe("😀");
  f.send("\x1b[DZ\x1b[3~");
  expect(f.model.dialog && "value" in f.model.dialog && f.model.dialog.value).toBe("Z");
  f.send("\x01\x1b[200~Safe\ntext\x1b[201~");
  expect(f.model.dialog && "value" in f.model.dialog && f.model.dialog.value).toBe("Safe text");
  f.app.dispose();
});
test("paste never triggers command shortcuts outside editor", () => {
  const f = fixture(); const original = readFileSync(f.file, "utf8");
  f.send("\x1b[200~qdS\r\x1b[201~"); expect(f.quit()).toBe(false);
  expect(f.model.dialog).toBeUndefined(); expect(readFileSync(f.file, "utf8")).toBe(original); f.app.dispose();
});
test("escape restores search and disposes subscriptions", () => {
  const f = fixture(); f.send("/Session"); expect(f.model.query).toBe("Session");
  f.app.handle({ name: "escape" }); expect(f.model.query).toBe(""); f.app.dispose(); f.model.emit();
});
test("decoder handles split escapes, bracketed paste and unsupported control sequences", () => {
  const d = new KeyDecoder();
  expect(d.feed("\x1b[")).toEqual([]); expect(d.feed("A")).toEqual([{ name: "up" }]);
  expect(d.feed("\x1b")).toEqual([]); expect(d.flush()).toEqual([{ name: "escape" }]);
  expect(d.feed("\x1b[200~hello\x1b[20")).toEqual([]);
  expect(d.feed("1~")).toEqual([{ name: "paste", text: "hello", paste: true }]);
  expect(d.feed("\x1b[999~")).toEqual([]);
  expect(d.feed("\x03")).toEqual([{ name: "c", ctrl: true }]);
});

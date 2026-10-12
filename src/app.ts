import { basename } from "node:path";
import { StringDecoder } from "node:string_decoder";
import { clean, fit, pack, windowStart } from "./terminal";
import { filters, TodoModel, type Dialog, type TaskRow } from "./model";
import { statusNames } from "./markdown";
import { KeyDecoder, type Key } from "./keys";

const ESC = "\x1b[";
const RESET = `${ESC}0m`;
const HELP = ["j/k arrows move · Enter open/fold", "Tab focus · p projects", "g overview · w active · t all", "Space status · 1/2/3 set status", "a/e/d add/edit/delete task", "S/D create/remove empty section", "f filter · / search · Esc reset", "za fold · zM/zR collapse/expand", "o open in Neovim", "r reload · q quit · ? help"];
export interface Line { text: string; style?: number }
const line = (text: string, style?: number): Line => ({ text, style });
const graphemes = (text: string) => [...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text)].map(x => x.segment);

/** ANSI only: no background overrides, palette queries or inverse-fill state. */
export function encodeFrame(lines: Line[], columns: number): string {
  const width = Math.max(1, columns - 1); // Avoid autowrap at the physical right edge.
  return lines.map((row, index) => `${ESC}${index + 1};1H${RESET}${ESC}2K${row.style ? `${ESC}${row.style}m` : ""}${fit(row.text, width)}${RESET}`).join("");
}

export class AnsiApp {
  private unsubscribe: () => void;
  private dialogRef?: Dialog;
  private cursor = 0;
  private replaceSelection = false;
  constructor(readonly model: TodoModel, private changed: () => void = () => {}, private quit: () => void = () => {}, private editor: (row: TaskRow) => Promise<void> = async () => {}) {
    this.unsubscribe = model.subscribe(changed);
    model.notice("Ready · ? help · q quit");
  }
  dispose() { this.unsubscribe(); }
  private syncInput() {
    const d = this.model.dialog;
    if (d !== this.dialogRef) {
      this.dialogRef = d; this.cursor = d && "value" in d ? graphemes(d.value).length : 0; this.replaceSelection = false;
    }
  }
  frame(columns: number, rows: number): Line[] {
    const w = Math.max(1, columns - 1), h = Math.max(1, rows);
    const output = Array.from({ length: h }, () => line(""));
    const m = this.model;
    if (w < 20 || h < 6) { output[0] = line("Resize terminal · q quit"); return output; }
    const c = m.totals;
    output[0] = line(pack(` todo / ${m.title}`, `[ ] ${c[" "]} [/] ${c["/"]} [x] ${c.x}`, w), 1);
    output[1] = line(`${m.focus === "sidebar" ? ">" : " "}g overview · w active · t all · p projects`, 36);
    const capacity = h - 4;
    let body: Line[] = [];
    if (m.view === "projects") {
      output[2] = line(pack(" PROJECT", "PEND ACTIVE DONE", w), 1);
      const start = windowStart(m.index, m.projectRows.length, Math.max(1, Math.floor(capacity / 2)));
      body = m.projectRows.slice(start, start + Math.max(1, Math.floor(capacity / 2))).map((p, i) => {
        const tasks = p.doc?.tasks || [];
        return line(pack(`${i + start === m.index ? ">" : " "} ${p.name}`, p.error ? "unavailable" : `${tasks.filter(t => t.status === " ").length}    ${tasks.filter(t => t.status === "/").length}    ${tasks.filter(t => t.status === "x").length}`, w), p.error ? 31 : i + start === m.index ? 1 : undefined);
      });
      if (!body.length) body.push(line("No projects · todo add-project /path", 33));
      body.push(line(`IN PROGRESS · ${m.activeQueue.length}`, 1));
      body.push(...m.activeQueue.map(row => line(`[/] ${row.project.name} / ${row.task.text}`, 36)));
    } else {
      output[2] = line(` TASKS · ${m.filter === "all" ? "ALL" : statusNames[m.filter]}${m.query ? ` / ${m.query}` : ""}`, 1);
      const nodes = m.displayRows;
      const start = windowStart(m.index, nodes.length, capacity);
      body = nodes.slice(start, start + capacity).map((node, i) => {
        const mark = i + start === m.index ? ">" : " ";
        if (node.kind === "section") return line(pack(`${mark} ${node.collapsed ? "▸" : "▾"} ${node.title}`, `${node.count}`, w), 33);
        if (node.kind === "completed") return line(`${mark}   ${node.collapsed ? "▸" : "▾"} Completed (${node.count})`, 32);
        return line(`${mark}   [${node.row.task.status}] ${node.row.task.text}`, i + start === m.index ? 1 : node.row.task.status === "/" ? 36 : undefined);
      });
      if (!body.length) body.push(line(m.scope[0]?.error ? "Cannot read TODO.md · r refresh" : "No tasks · a add · S section", 33));
    }
    for (let i = 0; i < Math.min(body.length, capacity); i++) output[3 + i] = body[i]!;
    output[h - 1] = line(m.error ? `ERROR: ${m.message}` : m.message || "j/k move · a add · ? help · q quit", m.error ? 31 : undefined);
    const d = m.dialog;
    if (d) {
      this.syncInput();
      for (let i = 2; i < h - 1; i++) output[i] = line("");
      const title = { help: "KEYS", filter: "FILTER", projects: "PROJECTS", search: "SEARCH", add: "ADD TASK", edit: "EDIT TASK", delete: "DELETE TASK?", "section-add": "NEW SECTION", "section-remove": "REMOVE EMPTY SECTION?" }[d.kind];
      output[2] = line(` ${title}`, 1);
      let items: Line[] = [];
      if ("value" in d) {
        const parts = graphemes(d.value);
        const before = parts.slice(0, this.cursor).join("");
        const after = parts.slice(this.cursor).join("");
        // Show the insertion point and nearby text even on small terminals.
        const tail = graphemes(before);
        while (Bun.stringWidth(tail.join("")) > Math.max(0, w - 8)) tail.shift();
        items.push(line(` > ${tail.join("")}|${after}`));
        if (d.kind === "add") items.push(line(`Section: ${d.doc.sections.find(s => s.id === d.sectionId)?.title || "General"} · Tab changes`));
        if (m.error) items.push(line(`ERROR: ${m.message}`, 31));
        output[h - 1] = line("Enter save · Esc cancel · Ctrl+a select all");
      } else if (d.kind === "help") {
        items = HELP.slice(d.index, d.index + h - 4).map(t => line(t));
        output[h - 1] = line("j/k scroll · Esc close");
      } else if (d.kind === "filter" || d.kind === "projects") {
        const choices = d.kind === "filter" ? filters.map(f => f === "all" ? "All" : `[${f}] ${statusNames[f]}`) : m.navigation.map(n => `${n.label}${n.error ? " (unavailable)" : ""}`);
        const start = windowStart(d.index, choices.length, h - 4);
        items = choices.slice(start, start + h - 4).map((t, i) => line(`${start + i === d.index ? ">" : " "} ${t}`));
        output[h - 1] = line("j/k choose · Enter apply · Esc cancel");
      } else {
        items = [line(d.kind === "delete" ? d.row.task.text : d.title)];
        if (m.error) items.push(line(`ERROR: ${m.message}`, 31));
        output[h - 1] = line("y confirm · n/Esc cancel");
      }
      for (let i = 0; i < Math.min(items.length, h - 4); i++) output[i + 3] = items[i]!;
    }
    return output;
  }
  handle(key: Key, height = 24) {
    const m = this.model, c = key.text;
    if (key.ctrl && key.name === "c") { this.quit(); return; }
    if (m.busy) return;
    this.syncInput();
    const d = m.dialog;
    if (key.paste && (!d || !("value" in d))) return;
    if (d) {
      if (key.name === "escape") { m.cancel(); return; }
      if ("value" in d) {
        let parts = graphemes(d.value);
        if (key.ctrl && key.name === "a") { this.replaceSelection = true; this.changed(); return; }
        if (key.name === "left") this.cursor = Math.max(0, this.cursor - 1);
        else if (key.name === "right") this.cursor = Math.min(parts.length, this.cursor + 1);
        else if (key.name === "home") this.cursor = 0;
        else if (key.name === "end") this.cursor = parts.length;
        else if (key.name === "return") { m.submit(); return; }
        else if (key.name === "tab") { if (d.kind === "add") m.cycleSection(); return; }
        else if (key.name === "backspace" || key.name === "delete") {
          if (this.replaceSelection) { parts = []; this.cursor = 0; }
          else if (key.name === "backspace" && this.cursor > 0) parts.splice(--this.cursor, 1);
          else if (key.name === "delete") parts.splice(this.cursor, 1);
          this.replaceSelection = false; m.input(parts.join("")); return;
        } else if (c && !key.ctrl) {
          if (this.replaceSelection) { parts = []; this.cursor = 0; }
          const added = graphemes(clean(c)); parts.splice(this.cursor, 0, ...added);
          this.cursor = graphemes(parts.slice(0, this.cursor + added.length).join("")).length;
          this.replaceSelection = false; m.input(parts.join("")); return;
        }
        this.replaceSelection = false; this.changed(); return;
      }
      if (d.kind === "delete" || d.kind === "section-remove") { if (c === "y") m.submit(); else if (c === "n") m.cancel(); return; }
      if (c === "q") { m.cancel(); return; }
      if (key.name === "return") { if (d.kind === "help") m.cancel(); else m.submit(); return; }
      const amount = c === "j" || key.name === "down" ? 1 : c === "k" || key.name === "up" ? -1 : 0;
      if (d.kind === "help") { d.index = Math.max(0, Math.min(HELP.length - 1, d.index + amount)); m.emit(); }
      else m.moveDialog(amount);
      return;
    }
    if (m.pendingKey) { m.pendingKey = ""; if (c === "a") m.toggleSelected(); else if (c === "M" || c === "R") m.setAllCollapsed(c === "M"); else m.emit(); return; }
    if (c === "q") { this.quit(); return; }
    if (c === "g") { m.global(); return; }
    if (c === "w") { m.inProgress(); return; }
    if (c === "t") { m.allTasks(); return; }
    if (c === "p") { m.begin("projects"); return; }
    if (c === "r") { m.reload(); return; }
    if (c === "?") { m.begin("help"); return; }
    if (key.name === "tab") { m.setFocus(m.focus === "content" ? "sidebar" : "content"); return; }
    if (key.name === "down" || c === "j") { m.move(1); return; }
    if (key.name === "up" || c === "k") { m.move(-1); return; }
    if (key.name === "home") { m.move(-m.length); return; }
    if (key.name === "end") { m.move(m.length); return; }
    if (key.name === "pagedown" || (key.ctrl && key.name === "d")) { m.move(Math.max(1, height - 4)); return; }
    if (key.name === "pageup" || (key.ctrl && key.name === "u")) { m.move(-Math.max(1, height - 4)); return; }
    if (key.name === "return") { if (m.focus === "sidebar") m.openNavigation(); else if (m.view === "projects") m.openProject(); else if (m.selectedNode?.kind !== "task") m.toggleSelected(); else if (!m.local) m.openTaskProject(); return; }
    if (key.name === "escape") { if (m.focus === "sidebar") m.setFocus("content"); else if (m.query || m.filter !== "all") m.clearFilters(); else m.global(); return; }
    if (m.focus !== "content") return;
    if (c === "/") { m.begin("search"); return; }
    if (m.view !== "tasks") return;
    if (c === "f") { m.begin("filter"); return; }
    if (c === "z") { m.pendingKey = "z"; m.emit(); return; }
    if (key.name === "space") { m.changeStatus(); return; }
    if (c && ["1", "2", "3"].includes(c)) { if (m.selected) m.changeStatus(filters[Number(c)] as " " | "/" | "x"); return; }
    if (c === "S") { m.begin("section-add"); return; }
    if (c === "D" || (c === "d" && m.selectedNode?.kind === "section")) { m.begin("section-remove"); return; }
    if (c === "a" || c === "e" || c === "d") { m.begin(({ a: "add", e: "edit", d: "delete" } as const)[c]); return; }
    if (c === "o" && m.selected) {
      const row = m.selected; m.busy = true; m.notice("Neovim is open");
      void this.editor(row).then(() => m.reload()).catch(error => m.notice(String(error), true)).finally(() => { m.busy = false; m.emit(); });
    }
  }
}

export async function launch(local?: string) {
  const input = process.stdin, output = process.stdout;
  const keys = new KeyDecoder(), utf8 = new StringDecoder("utf8");
  let app: AnsiApp | undefined, stopped = false, suspended = false, scheduled = false;
  let escapeTimer: ReturnType<typeof setTimeout> | undefined;
  const originalRaw = !!input.isRaw;
  const enter = () => { output.write(`${ESC}?1049h${ESC}?25l${ESC}?2004h${RESET}`); input.setRawMode(true); input.resume(); };
  const leave = () => { output.write(`${RESET}${ESC}?2004l${ESC}?25h${ESC}?1049l`); input.setRawMode(originalRaw); };
  const render = () => {
    scheduled = false;
    if (!stopped && !suspended && app) output.write(encodeFrame(app.frame(output.columns || 80, output.rows || 24), output.columns || 80));
  };
  const changed = () => { if (!scheduled && !stopped) { scheduled = true; queueMicrotask(render); } };
  const onData = (data: Buffer) => {
    clearTimeout(escapeTimer);
    for (const key of keys.feed(utf8.write(data))) {
      if (stopped) break;
      app?.handle(key, output.rows || 24);
    }
    escapeTimer = setTimeout(() => { for (const key of keys.flush()) app?.handle(key, output.rows || 24); }, 35);
  };
  const stop = () => {
    if (stopped) return;
    stopped = true; clearTimeout(escapeTimer); app?.dispose();
    input.off("data", onData); input.pause(); output.off("resize", changed);
    process.off("SIGINT", stop); process.off("SIGTERM", stop); process.off("SIGHUP", stop); process.off("exit", stop);
    leave();
  };
  const editor = async (row: TaskRow) => {
    suspended = true; input.off("data", onData); clearTimeout(escapeTimer); input.pause(); leave();
    try {
      const child = Bun.spawn(["nvim", `+${row.task.line}`, "--", row.doc.path], { stdin: "inherit", stdout: "inherit", stderr: "inherit" });
      const code = await child.exited;
      if (code !== 0) throw new Error(`Neovim exited ${code}`);
    } finally { if (!stopped) { enter(); input.on("data", onData); suspended = false; changed(); } }
  };
  try {
    enter();
    process.on("SIGINT", stop); process.on("SIGTERM", stop); process.on("SIGHUP", stop); process.on("exit", stop);
    app = new AnsiApp(new TodoModel(local), changed, stop, editor);
    input.on("data", onData); output.on("resize", changed); render();
  } catch (error) { stop(); throw error; }
}

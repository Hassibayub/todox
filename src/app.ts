import { basename } from "node:path";
import { BoxRenderable, InputRenderable, InputRenderableEvents, TextRenderable, createCliRenderer, type CliRenderer, type KeyEvent } from "@opentui/core";
import { statusNames } from "./markdown";
import { filters, TodoModel, type Dialog, type DisplayRow, type TaskRow } from "./model";
import { clean, fit, pack, windowStart } from "./terminal";

const color = { bg: "#111b22", rail: "#1a2931", surface: "#21343d", ink: "#e6ede9", muted: "#aac0bd", selected: "#365257", cyan: "#96d4ce", green: "#a9dcae", amber: "#e4c997", red: "#ffaaaa" };
const HELP = [
  "j/k arrows move · Enter open/fold",
  "Tab switch navigation/list · p projects",
  "g overview · w active · t all",
  "Space cycle task · 1/2/3 set status",
  "a add · e edit · d delete task",
  "S create section · D remove empty",
  "f filter · / search · Esc reset",
  "z a fold · z M/R collapse/expand",
  "o open file at line in Neovim",
  "r reload · q quit · ? help",
];

/** Imperative, retained renderable tree: row slots survive model updates. */
export class CoreApp {
  readonly model: TodoModel;
  private root: BoxRenderable;
  private header: TextRenderable;
  private nav: TextRenderable;
  private subhead: TextRenderable;
  private list: BoxRenderable;
  private rowPool: TextRenderable[] = [];
  private detail: BoxRenderable;
  private detailLines: TextRenderable[] = [];
  private footer: TextRenderable;
  private overlay?: BoxRenderable;
  private dialogRef?: Dialog;
  private input?: InputRenderable;
  private modalRows: TextRenderable[] = [];
  private unsubscribe: () => void;
  private onKey: (key: KeyEvent) => void;
  private onResize: () => void;
  private wide = false;
  private cols = 80;
  private rows = 24;
  private listCols = 80;
  private listRows = 15;
  private destroyed = false;
  private onEditor: (row: TaskRow) => Promise<void>;

  constructor(readonly renderer: CliRenderer, local?: string, options: { model?: TodoModel; onEditor?: (row: TaskRow) => Promise<void> } = {}) {
    this.model = options.model ?? new TodoModel(local);
    this.onEditor = options.onEditor ?? (row => openInNeovim(renderer, row));
    this.root = new BoxRenderable(renderer, { id: "core-workspace", width: "100%", height: "100%", flexDirection: "column", backgroundColor: color.bg });
    this.header = this.text(this.root, "", color.ink, color.rail);
    this.nav = this.text(this.root, "", color.cyan, color.rail);
    this.subhead = this.text(this.root, "", color.muted, color.bg);
    const body = new BoxRenderable(renderer, { id: "body", flexGrow: 1, flexDirection: "row", overflow: "hidden" });
    this.root.add(body);
    this.list = new BoxRenderable(renderer, { id: "list", flexGrow: 1, flexDirection: "column", overflow: "hidden" });
    body.add(this.list);
    this.detail = new BoxRenderable(renderer, { id: "detail", width: 32, flexDirection: "column", backgroundColor: color.rail, border: ["left"], borderColor: color.surface, paddingLeft: 1 });
    body.add(this.detail);
    for (let i = 0; i < 12; i++) this.detailLines.push(this.text(this.detail));
    this.footer = this.text(this.root, "", color.muted, color.surface);
    renderer.root.add(this.root);
    this.unsubscribe = this.model.subscribe(() => this.paint());
    this.onKey = key => { if (this.key(key)) key.preventDefault(); };
    this.onResize = () => this.paint();
    renderer.keyInput.on("keypress", this.onKey);
    renderer.on("resize", this.onResize);
    renderer.once("destroy", () => this.dispose());
    this.model.notice("Ready · ? help · q quit");
    this.paint();
  }

  private text(parent: BoxRenderable, content = "", fg = color.ink, bg = color.bg): TextRenderable {
    const item = new TextRenderable(this.renderer, { content, height: 1, wrapMode: "none", fg, bg, selectable: false });
    parent.add(item); return item;
  }
  private ensureRows(count: number) {
    while (this.rowPool.length < count) this.rowPool.push(this.text(this.list));
    for (let i = 0; i < this.rowPool.length; i++) this.rowPool[i]!.visible = i < count;
  }
  private detailText(lines: string[]) {
    for (let i = 0; i < this.detailLines.length; i++) {
      const node = this.detailLines[i]!;
      node.content = fit(lines[i] || "", 29);
      node.fg = i === 0 ? color.cyan : color.muted;
    }
  }
  paint() {
    if (this.destroyed) return;
    const model = this.model;
    this.cols = this.renderer.width;
    this.rows = this.renderer.height;
    if (this.cols < 42 || this.rows < 10) {
      this.header.content = "todo — resize terminal (42×10) · q quit";
      this.nav.content = ""; this.subhead.content = ""; this.footer.content = "";
      this.list.visible = false; this.detail.visible = false; this.hideDialog(); return;
    }
    this.list.visible = true;
    this.wide = this.cols >= 104 && model.view === "tasks";
    this.detail.visible = this.wide;
    this.listCols = this.cols - (this.wide ? 32 : 0);
    this.listRows = Math.max(1, this.rows - 4);
    const c = model.totals;
    this.header.content = pack(` todo  /  ${model.title}`, `[ ] ${c[" "]}   [/] ${c["/"]}   [x] ${c.x} `, this.cols);
    const project = model.local ? model.scope[0]?.name : undefined;
    const nav = `[g] Overview    [w] In progress    [t] All tasks${project ? `    / ${project}` : ""}`;
    this.nav.content = fit(`${model.focus === "sidebar" ? ">" : " "}${nav}`, this.cols);
    this.nav.fg = model.focus === "sidebar" ? color.amber : color.cyan;
    const selected = model.selected;
    const active = model.selectedNode;
    const status = `TASKS  ${model.filter === "all" ? "ALL" : `[${model.filter}] ${statusNames[model.filter].toUpperCase()}`}  ${model.query ? `/ ${model.query}` : ""}`;
    this.subhead.content = model.view === "projects" ? pack(" PROJECT", "PEND   ACTIVE   DONE  ", this.listCols) : fit(` ${status}`, this.listCols);
    this.detailText(model.view === "projects" ? ["WORKSPACE", "", `${model.projects.length} projects`, `${model.globalTotals["/"]} active tasks`, "", "Enter opens a project", "w shows active work", "p picks a project"] : ["SELECTION", "", selected ? `[${selected.task.status}] ${selected.task.text}` : active?.kind === "section" ? active.title : "Select a task", "", selected?.project.name || model.title, selected ? `${basename(selected.doc.path)}:${selected.task.line}` : "", "", "Space status", "a add task  S section", "D remove empty section", "o open in Neovim"]);

    if (model.view === "projects") this.paintProjects(); else this.paintTasks();
    const info = model.error ? `ERROR: ${model.message}` : model.message || (model.focus === "sidebar" ? "Tab tasks · j/k scope · Enter open" : "j/k move · Enter open/fold · a task · S section · f filter · ? help");
    this.footer.content = pack(` ${info}`, model.selected ? `${basename(model.selected.doc.path)}:${model.selected.task.line} ` : "q quit ", this.cols);
    this.footer.fg = model.error ? color.red : color.muted;
    this.paintDialog();
  }
  private paintProjects() {
    const projects = this.model.projectRows;
    const active = this.model.activeQueue;
    const projectHeight = Math.max(1, Math.min(projects.length, Math.floor(this.listRows * 0.55)));
    const start = windowStart(this.model.index, projects.length, projectHeight);
    const lines: { content: string; fg?: string; bg?: string }[] = [];
    for (let i = start; i < Math.min(projects.length, start + projectHeight); i++) {
      const p = projects[i]!;
      const c = p.doc ? { " ": p.doc.tasks.filter(t => t.status === " ").length, "/": p.doc.tasks.filter(t => t.status === "/").length, x: p.doc.tasks.filter(t => t.status === "x").length } : null;
      lines.push({ content: pack(`${i === this.model.index ? ">" : " "} ${p.name}`, c ? `${c[" "]}      ${c["/"]}      ${c.x}  ` : "unavailable ", this.listCols), fg: p.error ? color.red : color.ink, bg: i === this.model.index ? color.selected : color.bg });
    }
    if (!projects.length) lines.push({ content: this.model.projects.length ? " No matching projects · Esc clears search" : " No projects · todo add-project /path/to/project", fg: color.amber });
    lines.push({ content: "─".repeat(this.listCols), fg: color.surface });
    lines.push({ content: pack(` IN PROGRESS  ${active.length}`, "w open ", this.listCols), fg: color.cyan });
    for (const row of active.slice(0, Math.max(0, this.listRows - lines.length))) lines.push({ content: fit(` [/] ${row.project.name}  /  ${row.task.text}`, this.listCols), fg: color.ink });
    if (!active.length) lines.push({ content: " Nothing active right now", fg: color.muted });
    this.renderLines(lines);
  }
  private paintTasks() {
    const nodes = this.model.displayRows;
    const start = windowStart(this.model.index, nodes.length, this.listRows);
    const lines: { content: string; fg?: string; bg?: string }[] = [];
    for (let i = start; i < Math.min(nodes.length, start + this.listRows); i++) {
      const node = nodes[i]!;
      const mark = i === this.model.index ? ">" : " ";
      let text: string; let fg = color.ink;
      if (node.kind === "task") {
        const task = node.row.task;
        text = `${mark}   ${task.indent.replace(/\t/g, "  ").slice(0, 6)}[${task.status}] ${task.text}`;
        fg = task.status === "/" ? color.cyan : task.status === "x" ? color.muted : color.ink;
      } else if (node.kind === "section") { text = pack(`${mark} ${node.collapsed ? "▸" : "▾"} ${node.title}`, `${node.count} `, this.listCols); fg = color.amber; }
      else { text = pack(`${mark}   ${node.collapsed ? "▸" : "▾"} Completed`, `${node.count} `, this.listCols); fg = color.green; }
      lines.push({ content: fit(text, this.listCols), fg, bg: i === this.model.index ? color.selected : color.bg });
    }
    if (!nodes.length) lines.push({ content: fit(this.model.scope[0]?.error ? ` Cannot read TODO.md: ${this.model.scope[0]!.error}` : this.model.query || this.model.filter !== "all" ? " No matches · Esc clears filters" : " No tasks · S makes a section, a adds a task", this.listCols), fg: color.amber });
    this.renderLines(lines);
  }
  private renderLines(lines: { content: string; fg?: string; bg?: string }[]) {
    this.ensureRows(Math.min(lines.length, this.listRows));
    for (let i = 0; i < Math.min(lines.length, this.listRows); i++) {
      const node = this.rowPool[i]!, line = lines[i]!;
      node.content = line.content; node.fg = line.fg ?? color.ink; node.bg = line.bg ?? color.bg;
    }
  }
  private hideDialog() {
    if (this.overlay) { this.overlay.destroyRecursively(); this.overlay = undefined; this.input = undefined; this.modalRows = []; }
    this.dialogRef = undefined;
  }
  private paintDialog() {
    const d = this.model.dialog;
    if (!d) { this.hideDialog(); return; }
    const width = Math.min(68, this.cols - 4);
    if (!this.overlay || d !== this.dialogRef) {
      this.hideDialog(); this.dialogRef = d;
      this.overlay = new BoxRenderable(this.renderer, { id: "dialog", position: "absolute", top: 3, left: Math.floor((this.cols - width) / 2), width, flexDirection: "column", border: true, borderColor: color.cyan, backgroundColor: color.rail, paddingX: 1 });
      this.root.add(this.overlay);
      for (let i = 0; i < 8; i++) this.modalRows.push(this.text(this.overlay, "", color.ink, color.rail));
      if ("value" in d) {
        this.input = new InputRenderable(this.renderer, { id: "dialog-input", width: width - 4, value: d.value, backgroundColor: color.surface, focusedBackgroundColor: color.surface, textColor: color.ink, cursorColor: color.cyan });
        this.overlay.add(this.input);
        this.input.on(InputRenderableEvents.INPUT, (value: string) => this.model.input(value));
        this.input.on(InputRenderableEvents.ENTER, (value: string) => this.model.submit(value));
        this.input.focus();
      }
    }
    this.overlay.width = width;
    this.overlay.left = Math.floor((this.cols - width) / 2);
    this.overlay.top = this.rows < 14 ? 1 : 3;
    const title = ({ help: "KEYS", filter: "FILTER", projects: "PROJECTS", search: "SEARCH", add: "ADD TASK", edit: "EDIT TASK", delete: "DELETE TASK?", "section-add": "NEW SECTION", "section-remove": "REMOVE EMPTY SECTION?" } as const)[d.kind];
    const lineItems: string[] = [title];
    if (d.kind === "help") lineItems.push(...HELP.slice(Math.max(0, d.index - 2), Math.max(0, d.index - 2) + Math.max(1, Math.min(5, this.rows - 8))));
    else if (d.kind === "filter") lineItems.push(...filters.map((f, i) => `${i === d.index ? ">" : " "} ${f === "all" ? "All" : `[${f}] ${statusNames[f]}`}`));
    else if (d.kind === "projects") lineItems.push(...this.model.navigation.slice(windowStart(d.index, this.model.navigation.length, 5), windowStart(d.index, this.model.navigation.length, 5) + 5).map(item => `${this.model.navigation[d.index] === item ? ">" : " "} ${item.label}${item.error ? " (unavailable)" : ""}`));
    else if (d.kind === "delete") lineItems.push(fit(d.row.task.text, width - 4), "y delete line · n/Esc cancel");
    else if (d.kind === "section-remove") lineItems.push(fit(d.title, width - 4), "Empty heading only · y remove · n/Esc cancel");
    else if (d.kind === "section-add") lineItems.push("Appends ## heading to current file", `File: ${basename(d.doc.path)}`);
    else if (d.kind === "add") lineItems.push(`Section: ${d.doc.sections.find(s => s.id === d.sectionId)?.title || "General"} · Tab changes`);
    if (this.model.error) lineItems.push(`ERROR: ${this.model.message}`);
    if (this.input) lineItems.push("Enter save · Esc cancel");
    else if (d.kind === "help") lineItems.push("j/k scroll · Esc close");
    else if (d.kind === "filter" || d.kind === "projects") lineItems.push("j/k choose · Enter apply · Esc cancel");
    for (let i = 0; i < this.modalRows.length; i++) {
      const row = this.modalRows[i]!;
      // Input remains last in tree; hint lines preceding it are intentional.
      row.visible = i < Math.min(lineItems.length, Math.max(1, this.rows - 6));
      row.content = fit(` ${lineItems[i] || ""}`, width - 3);
      row.fg = lineItems[i]?.startsWith("ERROR:") ? color.red : i === 0 ? color.cyan : color.ink;
    }
  }
  private key(key: KeyEvent): boolean {
    const m = this.model, c = key.sequence;
    if (key.ctrl && key.name === "c") { this.renderer.destroy(); return true; }
    if (m.busy) return true;
    const d = m.dialog;
    if (d) {
      if (key.name === "escape") { m.cancel(); return true; }
      if ("value" in d) { if (key.name === "tab" && d.kind === "add") { m.cycleSection(); return true; } return false; }
      if (d.kind === "delete" || d.kind === "section-remove") { if (c === "y") m.submit(); else if (c === "n") m.cancel(); return true; }
      if (c === "j" || key.name === "down") { if (d.kind === "help") { d.index = Math.min(HELP.length - 1, d.index + 1); m.emit(); } else m.moveDialog(1); return true; }
      if (c === "k" || key.name === "up") { if (d.kind === "help") { d.index = Math.max(0, d.index - 1); m.emit(); } else m.moveDialog(-1); return true; }
      if (key.name === "return") { if (d.kind === "help") m.cancel(); else m.submit(); return true; }
      if (c === "q") { m.cancel(); return true; }
      return true;
    }
    if (m.pendingKey) { m.pendingKey = ""; if (c === "a") m.toggleSelected(); else if (c === "M" || c === "R") m.setAllCollapsed(c === "M"); else m.emit(); return true; }
    if (c === "q") { this.renderer.destroy(); return true; }
    if (c === "g") { m.global(); return true; }
    if (c === "w") { m.inProgress(); return true; }
    if (c === "t") { m.allTasks(); return true; }
    if (c === "p") { m.begin("projects"); return true; }
    if (c === "r") { m.reload(); return true; }
    if (c === "?") { m.begin("help"); return true; }
    if (key.name === "tab") { m.setFocus(m.focus === "content" ? "sidebar" : "content"); return true; }
    if (key.name === "down" || c === "j") { m.move(1); return true; }
    if (key.name === "up" || c === "k") { m.move(-1); return true; }
    if (key.name === "home") { m.move(-m.length); return true; }
    if (key.name === "end") { m.move(m.length); return true; }
    if (key.ctrl && key.name === "d") { m.move(this.listRows); return true; }
    if (key.ctrl && key.name === "u") { m.move(-this.listRows); return true; }
    if (key.name === "return") { if (m.focus === "sidebar") m.openNavigation(); else if (m.view === "projects") m.openProject(); else if (m.selectedNode?.kind !== "task") m.toggleSelected(); else if (!m.local) m.openTaskProject(); return true; }
    if (key.name === "escape") { if (m.focus === "sidebar") m.setFocus("content"); else if (m.query || m.filter !== "all") m.clearFilters(); else m.global(); return true; }
    if (m.focus !== "content") return true;
    if (c === "/") { m.begin("search"); return true; }
    if (m.view !== "tasks") return true;
    if (c === "f") { m.begin("filter"); return true; }
    if (c === "z") { m.pendingKey = "z"; m.emit(); return true; }
    if (key.name === "space" || c === " ") { m.changeStatus(); return true; }
    if (["1", "2", "3"].includes(c)) { if (m.selected) m.changeStatus(filters[Number(c)] as " " | "/" | "x"); return true; }
    if (c === "S") { m.begin("section-add"); return true; }
    if (c === "D" || (c === "d" && m.selectedNode?.kind === "section")) { m.begin("section-remove"); return true; }
    if (c === "a" || c === "e" || c === "d") { m.begin(({ a: "add", e: "edit", d: "delete" } as const)[c]); return true; }
    if (c === "o" && m.selected) {
      const row = m.selected; m.busy = true; m.notice("Neovim is open");
      void this.onEditor(row).then(() => m.reload()).catch(error => m.notice(String(error), true)).finally(() => { m.busy = false; m.emit(); });
      return true;
    }
    return false;
  }
  dispose() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.unsubscribe();
    this.renderer.keyInput.off("keypress", this.onKey);
    this.renderer.off("resize", this.onResize);
    // Renderer owns and recursively destroys the attached tree.
  }
}

export async function openInNeovim(renderer: CliRenderer, row: TaskRow) {
  renderer.suspend();
  try {
    const child = Bun.spawn(["nvim", `+${row.task.line}`, "--", row.doc.path], { stdin: "inherit", stdout: "inherit", stderr: "inherit" });
    const code = await child.exited;
    if (code !== 0) throw new Error(`Neovim exited with status ${code}. Press r to reload.`);
  } finally { renderer.resume(); }
}
export async function launch(local?: string) {
  const renderer = await createCliRenderer({ exitOnCtrlC: true, backgroundColor: color.bg, consoleMode: "disabled" });
  try { new CoreApp(renderer, local); }
  catch (error) { renderer.destroy(); throw error; }
}

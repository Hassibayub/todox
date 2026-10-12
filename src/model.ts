import { basename, dirname } from "node:path";
import { counts, readDocument, requireEmptySection, saveMutation, statuses, type Document, type Mutation, type Status, type Task } from "./markdown";
import { loadConfig, loadProjects, type LoadedProject } from "./projects";

export interface TaskRow { project: LoadedProject; doc: Document; task: Task }
export type Filter = Status | "all";
export const filters: Filter[] = ["all", ...statuses];
export type DisplayRow =
  | { kind: "section"; key: string; group: string; project: LoadedProject; doc: Document; sectionId: number; title: string; count: number; total: number; collapsed: boolean }
  | { kind: "completed"; key: string; group: string; project: LoadedProject; doc: Document; sectionId: number; count: number; collapsed: boolean }
  | { kind: "task"; key: string; group: string; row: TaskRow };
export interface NavigationItem { id: string; label: string; count: number; error?: string }
export type Dialog =
  | { kind: "search"; value: string; previous: string }
  | { kind: "add" | "edit"; value: string; doc: Document; line?: number; sectionId: number }
  | { kind: "delete"; row: TaskRow }
  | { kind: "section-add"; value: string; doc: Document }
  | { kind: "section-remove"; doc: Document; sectionId: number; title: string }
  | { kind: "filter"; index: number }
  | { kind: "projects"; index: number }
  | { kind: "help"; index: number };

export class TodoModel {
  projects: LoadedProject[] = [];
  local?: string;
  view: "projects" | "tasks";
  focus: "sidebar" | "content" = "content";
  sidebarIndex = 0;
  index = 0;
  filter: Filter = "all";
  query = "";
  dialog?: Dialog;
  message = "";
  error = false;
  busy = false;
  pendingKey = "";
  readonly collapsedSections = new Set<string>();
  readonly expandedCompleted = new Set<string>();
  private activeScope = false;
  private revision = 0;
  private listeners = new Set<() => void>();

  constructor(local?: string, readonly registry?: string) {
    this.local = local;
    this.view = local ? "tasks" : "projects";
    this.reload();
    this.selectFirstTask();
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.revision;
  emit() { this.revision++; for (const listener of this.listeners) listener(); }
  notice(message: string, error = false) { this.message = message; this.error = error; this.emit(); }
  get scopeId() { return this.local ?? (this.view === "projects" ? "overview" : this.activeScope ? "active" : "all"); }
  get title() { return this.local ? this.scope[0]?.name || "Local" : this.view === "projects" ? "Overview" : this.activeScope ? "In progress" : "All tasks"; }
  get scope() { return this.local ? this.projects.filter(p => p.path === this.local) : this.projects; }
  get totals() { return counts(this.scope.flatMap(p => p.doc?.tasks || [])); }
  get globalTotals() { return counts(this.projects.flatMap(p => p.doc?.tasks || [])); }
  get navigation(): NavigationItem[] {
    const c = this.globalTotals;
    return [
      { id: "overview", label: "Overview", count: this.projects.length },
      { id: "active", label: "In progress", count: c["/"] },
      { id: "all", label: "All tasks", count: c[" "] + c["/"] + c.x },
      ...this.projects.map(p => ({ id: p.path, label: p.name, count: p.doc?.tasks.length || 0, error: p.error })),
    ];
  }
  get projectRows() { const q = this.query.toLowerCase(); return this.projects.filter(p => `${p.name} ${p.path}`.toLowerCase().includes(q)); }
  get rows(): TaskRow[] {
    const q = this.query.toLowerCase();
    return this.scope.flatMap(project => project.doc ? project.doc.tasks.map(task => ({ project, doc: project.doc!, task })) : [])
      .filter(row => (this.filter === "all" || row.task.status === this.filter) && `${row.task.text} ${row.task.section} ${row.project.name}`.toLowerCase().includes(q));
  }
  get displayRows(): DisplayRow[] {
    const result: DisplayRow[] = [];
    const matching = this.rows;
    for (const project of this.scope) {
      const doc = project.doc;
      if (!doc) continue;
      const occurrences = new Map<string, number>();
      for (const section of doc.sections) {
        const occurrence = occurrences.get(section.title) || 0;
        occurrences.set(section.title, occurrence + 1);
        const group = `${doc.path}\0${section.title}\0${occurrence}`;
        const tasks = matching.filter(row => row.doc.path === doc.path && row.task.sectionId === section.id);
        const total = doc.tasks.filter(task => task.sectionId === section.id).length;
        const emptyLocalSection = !!this.local && !this.query && this.filter === "all" && !total && (section.level > 1 || doc.sections.length === 1);
        if (!tasks.length && !emptyLocalSection) continue;
        // Search and explicit filters reveal matches, even in folded groups.
        const foldable = this.filter === "all" && !this.query;
        const collapsed = foldable && this.collapsedSections.has(group);
        result.push({ kind: "section", key: `${group}:section`, group, project, doc, sectionId: section.id, title: this.local ? section.title : `${project.name} / ${section.title}`, count: tasks.length, total, collapsed });
        if (collapsed) continue;
        const completed = foldable ? tasks.filter(row => row.task.status === "x") : [];
        for (const row of tasks.filter(row => !foldable || row.task.status !== "x")) result.push({ kind: "task", key: `${doc.path}:${row.task.line}`, group, row });
        if (completed.length) {
          const hidden = !this.expandedCompleted.has(group);
          result.push({ kind: "completed", key: `${group}:completed`, group, project, doc, sectionId: section.id, count: completed.length, collapsed: hidden });
          if (!hidden) for (const row of completed) result.push({ kind: "task", key: `${doc.path}:${row.task.line}`, group, row });
        }
      }
    }
    return result;
  }
  get selectedNode() { return this.view === "tasks" ? this.displayRows[this.index] : undefined; }
  get selected() { const node = this.selectedNode; return node?.kind === "task" ? node.row : undefined; }
  get length() { return this.view === "projects" ? this.projectRows.length : this.displayRows.length; }
  get activeQueue() {
    const q = this.query.toLowerCase();
    return this.projects.flatMap(project => project.doc ? project.doc.tasks.filter(task => task.status === "/").map(task => ({ project, doc: project.doc!, task })) : [])
      .filter(row => `${row.project.name} ${row.task.text} ${row.task.section}`.toLowerCase().includes(q));
  }
  reload() {
    try {
      this.projects = loadProjects(loadConfig(this.registry).projects);
      if (this.local && !this.projects.some(p => p.path === this.local)) {
        try { this.projects.push({ name: basename(dirname(this.local)), path: this.local, doc: readDocument(this.local) }); }
        catch (error) { this.projects.push({ name: basename(dirname(this.local)), path: this.local, error: String(error) }); }
      }
      this.clamp();
      this.sidebarIndex = Math.max(0, this.navigation.findIndex(item => item.id === this.scopeId));
      this.message = "Refreshed from disk"; this.error = false;
    } catch (error) { this.message = String(error); this.error = true; }
    this.emit();
  }
  clamp() { this.index = Math.max(0, Math.min(this.index, this.length - 1)); }
  move(amount: number) {
    if (this.focus === "sidebar") this.sidebarIndex = Math.max(0, Math.min(this.sidebarIndex + amount, this.navigation.length - 1));
    else { this.index += amount; this.clamp(); }
    this.emit();
  }
  setFocus(focus: "sidebar" | "content") {
    this.focus = focus;
    if (focus === "sidebar") this.sidebarIndex = Math.max(0, this.navigation.findIndex(item => item.id === this.scopeId));
    this.pendingKey = ""; this.emit();
  }
  activate(id: string) {
    if (!["overview", "active", "all"].includes(id) && !this.projects.some(p => p.path === id)) return;
    this.local = ["overview", "active", "all"].includes(id) ? undefined : id;
    this.view = id === "overview" ? "projects" : "tasks";
    this.activeScope = id === "active";
    this.filter = this.activeScope ? "/" : "all";
    this.query = ""; this.index = 0; this.focus = "content"; this.dialog = undefined;
    this.sidebarIndex = Math.max(0, this.navigation.findIndex(item => item.id === id));
    this.selectFirstTask(); this.emit();
  }
  global() { this.activate("overview"); }
  allTasks() { this.activate("all"); }
  inProgress() { this.activate("active"); }
  openNavigation() { const item = this.navigation[this.sidebarIndex]; if (item) this.activate(item.id); }
  openProject() { const project = this.projectRows[this.index]; if (project) this.activate(project.path); }
  openTaskProject() {
    const row = this.selected;
    if (!row) return;
    this.activate(row.project.path);
    this.revealTask(row.doc.path, row.task.line);
    this.emit();
  }
  selectFirstTask() {
    if (this.view !== "tasks") return;
    const first = this.displayRows.findIndex(node => node.kind === "task");
    this.index = first >= 0 ? first : 0;
    this.clamp();
  }
  revealTask(path: string, line: number) {
    const doc = this.projects.find(p => p.doc?.path === path)?.doc;
    const task = doc?.tasks.find(task => task.line === line);
    if (!task) return;
    // Compute the section key with folds temporarily cleared for this lookup.
    const node = this.displayRows.find(node => node.kind === "section" && node.doc.path === path && node.sectionId === task.sectionId);
    if (node) { this.collapsedSections.delete(node.group); if (task.status === "x") this.expandedCompleted.add(node.group); }
    const index = this.displayRows.findIndex(node => node.kind === "task" && node.row.doc.path === path && node.row.task.line === line);
    if (index >= 0) this.index = index;
    this.clamp();
  }
  setFilter(filter: Filter) {
    this.filter = filter;
    if (this.activeScope && filter !== "/") this.activeScope = false;
    this.dialog = undefined; this.index = 0; this.selectFirstTask(); this.emit();
  }
  cycleFilter() { this.setFilter(filters[(filters.indexOf(this.filter) + 1) % filters.length]!); }
  toggleSelected() {
    const node = this.selectedNode;
    if (!node) return;
    if (this.query || this.filter !== "all") { this.notice("Folds are opened while searching or filtering. Esc clears the filter."); return; }
    const key = node.kind === "completed" ? node.key : `${node.group}:section`;
    if (node.kind === "completed") {
      if (this.expandedCompleted.has(node.group)) this.expandedCompleted.delete(node.group); else this.expandedCompleted.add(node.group);
    } else {
      if (this.collapsedSections.has(node.group)) this.collapsedSections.delete(node.group); else this.collapsedSections.add(node.group);
    }
    const index = this.displayRows.findIndex(row => row.key === key);
    if (index >= 0) this.index = index;
    this.clamp(); this.emit();
  }
  setAllCollapsed(collapsed: boolean) {
    if (this.filter !== "all" || this.query) { this.notice("Clear search and filters before folding sections."); return; }
    for (const node of this.displayRows.filter(row => row.kind === "section")) {
      if (collapsed) this.collapsedSections.add(node.group);
      else { this.collapsedSections.delete(node.group); this.expandedCompleted.add(node.group); }
    }
    this.index = 0; this.clamp(); this.emit();
  }
  save(doc: Document, mutation: Mutation) {
    try {
      const updated = saveMutation(doc, mutation);
      this.projects = this.projects.map(p => p.doc?.path === doc.path ? { ...p, doc: updated, error: undefined } : p);
      this.dialog = undefined;
      if (mutation.kind === "edit" || mutation.kind === "status") this.revealTask(updated.path, mutation.line);
      else if (mutation.kind === "add") {
        // Newly added tasks are pending. Do not strand them behind a Done,
        // Active, or search filter after a successful write.
        this.filter = "all"; this.query = ""; this.activeScope = false;
        const insertionLine = doc.sections.find(section => section.id === mutation.sectionId)?.end;
        const added = updated.tasks.find(task => task.line === (insertionLine ?? doc.lines.length) + 1);
        if (added) this.revealTask(updated.path, added.line);
      }
      else if (mutation.kind === "section-add") {
        const project = this.projects.find(p => p.doc?.path === updated.path)!;
        this.activate(project.path);
        const sectionId = updated.sections.at(-1)!.id;
        this.index = this.displayRows.findIndex(node => node.kind === "section" && node.doc.path === updated.path && node.sectionId === sectionId);
      }
      this.clamp();
      this.notice(mutation.kind === "section-add" ? "Section created · a adds a task" : mutation.kind === "section-remove" ? "Empty section removed" : "Saved to TODO.md");
    } catch (error) { this.notice(error instanceof Error ? error.message : String(error), true); }
  }
  changeStatus(status?: Status) {
    const row = this.selected;
    if (!row) { this.toggleSelected(); return; }
    this.save(row.doc, { kind: "status", line: row.task.line, status: status ?? statuses[(statuses.indexOf(row.task.status) + 1) % statuses.length]! });
  }
  begin(kind: "add" | "edit" | "delete" | "search" | "help" | "filter" | "projects" | "section-add" | "section-remove") {
    this.pendingKey = "";
    if (kind === "search") this.dialog = { kind, value: this.query, previous: this.query };
    else if (kind === "help") this.dialog = { kind, index: 0 };
    else if (kind === "filter") this.dialog = { kind, index: filters.indexOf(this.filter) };
    else if (kind === "projects") this.dialog = { kind, index: Math.max(0, this.navigation.findIndex(item => item.id === this.scopeId)) };
    else if (kind === "section-add" || kind === "section-remove") {
      const row = this.selected;
      const node = this.selectedNode;
      const doc = row?.doc || (node && node.kind !== "task" ? node.doc : undefined) || (this.local ? this.scope[0]?.doc : undefined);
      if (!doc) { this.notice("Open a readable project before managing sections.", true); return; }
      if (kind === "section-add") {
        this.error = false; this.message = "";
        this.dialog = { kind, value: "", doc };
      } else {
        const sectionId = row?.task.sectionId ?? (node && node.kind !== "task" ? node.sectionId : undefined);
        if (sectionId === undefined) { this.notice("Select a section heading to remove.", true); return; }
        try {
          const section = requireEmptySection(doc, sectionId);
          this.error = false; this.message = "";
          this.dialog = { kind, doc, sectionId, title: section.title };
        } catch (error) { this.notice(error instanceof Error ? error.message : String(error), true); return; }
      }
    }
    else {
      const row = this.selected;
      const node = this.selectedNode;
      if (kind === "delete") { if (!row) return; this.dialog = { kind, row }; }
      else {
        const doc = row?.doc || (node && node.kind !== "task" ? node.doc : undefined) || (this.local ? this.scope[0]?.doc : undefined);
        if (!doc) { this.notice("Open a project with Enter before adding a task.", true); return; }
        if (kind === "edit" && !row) return;
        this.dialog = { kind, value: kind === "edit" ? row!.task.text : "", doc, line: row?.task.line, sectionId: row?.task.sectionId ?? (node && node.kind !== "task" ? node.sectionId : doc.sections.at(-1)!.id) };
      }
    }
    this.emit();
  }
  moveDialog(amount: number) {
    const d = this.dialog;
    if (d?.kind !== "filter" && d?.kind !== "projects") return;
    const length = d.kind === "filter" ? filters.length : this.navigation.length;
    d.index = Math.max(0, Math.min(d.index + amount, length - 1)); this.emit();
  }
  input(value: string) {
    if (!this.dialog || !("value" in this.dialog)) return;
    this.dialog.value = value;
    if (this.dialog.kind === "search") { this.query = value; this.index = 0; this.selectFirstTask(); }
    this.emit();
  }
  cycleSection() {
    const d = this.dialog;
    if (d?.kind !== "add") return;
    const sections = d.doc.sections.filter(s => s.id !== -1 || s.end > 0 || d.doc.sections.length === 1);
    d.sectionId = sections[(sections.findIndex(s => s.id === d.sectionId) + 1) % sections.length]!.id; this.emit();
  }
  submit(value?: string) {
    const d = this.dialog; if (!d) return;
    if (value !== undefined && "value" in d) d.value = value;
    if (d.kind === "search") { this.query = d.value; this.dialog = undefined; this.clamp(); this.emit(); }
    else if (d.kind === "filter") this.setFilter(filters[d.index]!);
    else if (d.kind === "projects") this.activate(this.navigation[d.index]!.id);
    else if (d.kind === "add") this.save(d.doc, { kind: "add", text: d.value, sectionId: d.sectionId });
    else if (d.kind === "edit") this.save(d.doc, { kind: "edit", text: d.value, line: d.line! });
    else if (d.kind === "delete") this.save(d.row.doc, { kind: "delete", line: d.row.task.line });
    else if (d.kind === "section-add") this.save(d.doc, { kind: "section-add", name: d.value });
    else if (d.kind === "section-remove") this.save(d.doc, { kind: "section-remove", sectionId: d.sectionId });
  }
  cancel() {
    if (this.dialog?.kind === "search") this.query = this.dialog.previous;
    this.dialog = undefined; this.clamp(); this.emit();
  }
  clearFilters() { this.query = ""; this.filter = this.activeScope ? "/" : "all"; this.index = 0; this.selectFirstTask(); this.emit(); }
}

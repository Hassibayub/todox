import { closeSync, existsSync, openSync, readFileSync, realpathSync, renameSync, statSync, unlinkSync, writeFileSync, fsyncSync, fchmodSync } from "node:fs";
import { dirname, basename, join } from "node:path";
import { randomUUID } from "node:crypto";

export type Status = " " | "/" | "x";
export const statuses: Status[] = [" ", "/", "x"];
export const statusNames: Record<Status, string> = { " ": "Not started", "/": "In progress", x: "Completed" };
export interface Line { text: string; eol: string; offset: number }
export interface Section { id: number; title: string; level: number; end: number }
export interface Task { line: number; text: string; status: Status; section: string; sectionId: number; markerOffset: number; textOffset: number; indent: string }
export interface Document { path: string; content: string; lines: Line[]; tasks: Task[]; sections: Section[]; newline: string }

export function parseMarkdown(content: string, path = ""): Document {
  const lines: Line[] = [];
  const pattern = /([^\r\n]*)(\r\n|\n|\r|$)/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(content)) && match[0].length) {
    lines.push({ text: match[1]!, eol: match[2]!, offset: match.index });
  }
  const tasks: Task[] = [];
  const sections: Section[] = [{ id: -1, title: "General", level: 0, end: lines.length }];
  let current = sections[0]!;
  let fence: { char: string; length: number } | undefined;
  const headings: { level: number; title: string }[] = [];
  for (const [index, line] of lines.entries()) {
    const text = index === 0 ? line.text.replace(/^\uFEFF/, "") : line.text;
    const block = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(text);
    if (fence) {
      if (block && block[1]![0] === fence.char && block[1]!.length >= fence.length && !block[2]!.trim()) fence = undefined;
      continue;
    }
    if (block) { fence = { char: block[1]![0]!, length: block[1]!.length }; continue; }
    const heading = /^ {0,3}(#{1,6})[ \t]+(.+?)\s*$/.exec(text);
    if (heading) {
      current.end = index;
      const level = heading[1]!.length;
      while (headings.length && headings.at(-1)!.level >= level) headings.pop();
      // Closing ATX markers require preceding whitespace; C# is a name.
      headings.push({ level, title: heading[2]!.replace(/[ \t]+#+[ \t]*$/, "").trim() });
      current = { id: index, title: headings.filter(h => h.level > 1).map(h => h.title).join(" / ") || "General", level, end: lines.length };
      sections.push(current);
      continue;
    }
    const task = /^(\s*[-+*]\s+\[)([ /xX])(\]\s+)(.*)$/.exec(text);
    if (task) {
      const prefix = task[1]!;
      const bomOffset = line.text.length - text.length;
      tasks.push({ line: index + 1, text: task[4]!, status: task[2]!.toLowerCase() as Status, section: current.title, sectionId: current.id,
        markerOffset: line.offset + bomOffset + prefix.length, textOffset: line.offset + bomOffset + prefix.length + 1 + task[3]!.length,
        indent: /^\s*/.exec(text)![0] });
    }
  }
  return { path, content, lines, tasks, sections, newline: lines.find(l => l.eol)?.eol || "\n" };
}

export function readDocument(path: string): Document {
  const resolved = realpathSync(path);
  if (!statSync(resolved).isFile()) throw new Error(`Not a file: ${path}`);
  return parseMarkdown(readFileSync(resolved, "utf8"), resolved);
}

export type Mutation = { kind: "status"; line: number; status: Status } | { kind: "edit"; line: number; text: string } | { kind: "delete"; line: number } | { kind: "add"; text: string; sectionId?: number }
  | { kind: "section-add"; name: string } | { kind: "section-remove"; sectionId: number };

/** A parent is not empty if it contains even an empty nested heading. */
export function requireEmptySection(doc: Document, sectionId: number): Section {
  const section = doc.sections.find(s => s.id === sectionId);
  if (!section) throw new Error("Section no longer exists. Refresh first.");
  if (section.id < 0 || section.level < 2) throw new Error("The document title and implicit General group cannot be removed.");
  const end = doc.sections.find(s => s.id > section.id && s.level <= section.level)?.id ?? doc.lines.length;
  if (doc.lines.slice(section.id + 1, end).some(line => line.text.trim())) {
    throw new Error(`Section is not empty. Move/remove its contents and nested headings first: “${section.title}”.`);
  }
  return section;
}

function validateText(text: string, label = "task title"): string {
  if (!text.trim() || /[\r\n\x00-\x1f\x7f]/.test(text)) throw new Error(`Use a non-empty, single-line ${label} without control characters.`);
  return text.trim();
}
export function mutateContent(doc: Document, mutation: Mutation): string {
  const content = doc.content;
  if (mutation.kind === "section-add") {
    const name = validateText(mutation.name, "section name");
    if (parseMarkdown(`## ${name}\n`).sections.at(-1)!.title !== name) throw new Error("Use a section name without closing Markdown # markers.");
    if (doc.sections.some(section => section.level === 2 && section.title.toLowerCase() === name.toLowerCase())) {
      throw new Error(`A top-level section named “${name}” already exists.`);
    }
    const body = content.replace(/^\uFEFF/, "");
    const separator = !body || /(?:\r\n\r\n|\n\n|\r\r)$/.test(body) ? "" : /[\r\n]$/.test(body) ? doc.newline : doc.newline.repeat(2);
    const updated = content + separator + `## ${name}${doc.newline}`;
    if (parseMarkdown(updated).sections.length !== doc.sections.length + 1) {
      throw new Error("Cannot create a section inside an unclosed code fence. Close the fence in Neovim first.");
    }
    return updated;
  }
  if (mutation.kind === "section-remove") {
    const section = requireEmptySection(doc, mutation.sectionId);
    const line = doc.lines[section.id]!;
    const bom = section.id === 0 && line.text.startsWith("\uFEFF") ? "\uFEFF" : "";
    // Remove only the empty heading; preserve surrounding whitespace verbatim.
    return content.slice(0, line.offset) + bom + content.slice(line.offset + line.text.length + line.eol.length);
  }
  if (mutation.kind === "add") {
    const title = validateText(mutation.text);
    const section = doc.sections.find(s => s.id === (mutation.sectionId ?? doc.sections.at(-1)!.id));
    if (!section) throw new Error("Section no longer exists. Refresh first.");
    const position = doc.lines[section.end]?.offset ?? content.length;
    const before = content.slice(0, position);
    const separator = before && !/[\r\n]$/.test(before) ? doc.newline : "";
    return before + separator + `- [ ] ${title}${doc.newline}` + content.slice(position);
  }
  const task = doc.tasks.find(t => t.line === mutation.line);
  if (!task) throw new Error("Task no longer exists. Refresh first.");
  if (mutation.kind === "status") {
    if (!statuses.includes(mutation.status)) throw new Error("Invalid status.");
    return content.slice(0, task.markerOffset) + mutation.status + content.slice(task.markerOffset + 1);
  }
  const line = doc.lines[task.line - 1]!;
  if (mutation.kind === "edit") {
    return content.slice(0, task.textOffset) + validateText(mutation.text) + content.slice(line.offset + line.text.length);
  }
  // Delete only the checkbox line; never silently remove its notes or children.
  return content.slice(0, line.offset) + content.slice(line.offset + line.text.length + line.eol.length);
}

/** Snapshot comparison + exclusive cooperative lock + same-directory atomic replace. */
export function saveMutation(doc: Document, mutation: Mutation): Document {
  const path = realpathSync(doc.path);
  const lock = `${path}.todox.lock`;
  let fd: number;
  try { fd = openSync(lock, "wx", 0o600); }
  catch { throw new Error(`File is busy: ${lock}. If no todox process is writing, remove the stale lock.`); }
  const temp = join(dirname(path), `.${basename(path)}.${randomUUID()}.tmp`);
  try {
    if (readFileSync(path, "utf8") !== doc.content) throw new Error("TODO.md changed outside todox. Press r to refresh; your edit was not saved.");
    const content = mutateContent(doc, mutation);
    const mode = statSync(path).mode & 0o777;
    const out = openSync(temp, "wx", mode);
    try { writeFileSync(out, content, "utf8"); fchmodSync(out, mode); fsyncSync(out); } finally { closeSync(out); }
    if (readFileSync(path, "utf8") !== doc.content) throw new Error("TODO.md changed while saving. Refresh before editing again.");
    renameSync(temp, path);
    return parseMarkdown(content, path);
  } finally {
    if (existsSync(temp)) unlinkSync(temp);
    closeSync(fd);
    unlinkSync(lock);
  }
}

export function counts(tasks: Task[]): Record<Status, number> {
  const result = { " ": 0, "/": 0, x: 0 };
  for (const task of tasks) result[task.status]++;
  return result;
}

#!/usr/bin/env bun
import { existsSync, readdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { configPath, findLocalTodo, loadConfig, loadProjects, registerProject, resolveTodo, unregisterProject } from "./projects";
import { counts } from "./markdown";

const help = `todo — OpenTUI Core edition

  todo                        Local TODO.md, or global overview
  todo --global               All registered projects
  todo --file path/TODO.md     Open a specific file
  todo init                   Create TODO.md here (never overwrite)
  todo add-project [path]      Register a project's TODO.md
  todo remove-project [path]   Unregister; never deletes TODO.md
  todo scan [directory]        Register immediate child projects
  todo projects               List registered files
  todo stats [--json]          Global status counts
  todo --help                 This help

Shared registry: ${configPath()} (override with TODOX_CONFIG)
`;

export async function main(args = process.argv.slice(2), cwd = process.cwd()): Promise<void> {
  const command = args[0];
  if (command === "--help" || command === "-h") { console.log(help); return; }
  if (command === "--version") { console.log("0.2.0"); return; }
  if (command === "init") {
    if (args.length > 1) throw new Error("Usage: todo init");
    const path = join(cwd, "TODO.md");
    writeFileSync(path, "# TODO\n\n## General\n", { flag: "wx" });
    registerProject(path); console.log(`Created and registered ${path}`); return;
  }
  if (command === "add-project" || command === "remove-project") {
    if (args.length > 2) throw new Error(`Usage: todo ${command} [path]`);
    const path = resolve(cwd, args[1] || ".");
    if (command === "add-project") console.log(`Registered ${registerProject(path).path}`);
    else { unregisterProject(path); console.log(`Unregistered ${path}; task file untouched.`); }
    return;
  }
  if (command === "scan") {
    if (args.length > 2) throw new Error("Usage: todo scan [directory]");
    const directory = resolve(cwd, args[1] || "."); let found = 0;
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.name.startsWith(".") || entry.name === "node_modules") continue;
      const candidate = join(directory, entry.name, "TODO.md");
      if (!existsSync(candidate)) continue;
      console.log(`Registered ${registerProject(candidate).path}`); found++;
    }
    console.log(`${found} project(s) registered. Scan is one level deep.`); return;
  }
  if (command === "projects") {
    if (args.length > 1) throw new Error("Usage: todo projects");
    const projects = loadConfig().projects;
    console.log(projects.length ? projects.map(p => `${p.name}\t${p.path}`).join("\n") : "No projects yet. Run todo add-project /path/to/project."); return;
  }
  if (command === "stats") {
    if (args.length > 2 || (args[1] && args[1] !== "--json")) throw new Error("Usage: todo stats [--json]");
    const projects = loadProjects(loadConfig().projects);
    const count = counts(projects.flatMap(p => p.doc?.tasks || []));
    const result = { projects: projects.length, notStarted: count[" "], inProgress: count["/"], completed: count.x, errors: projects.filter(p => p.error).map(p => ({ path: p.path, error: p.error })) };
    console.log(args.includes("--json") ? JSON.stringify(result, null, 2) : `${result.projects} projects · ${result.notStarted} not started · ${result.inProgress} in progress · ${result.completed} completed${result.errors.length ? `\n${result.errors.map(e => `${e.path}: ${e.error}`).join("\n")}` : ""}`); return;
  }
  if (args.length && !(args.length === 1 && command === "--global") && !(args.length === 2 && command === "--file")) throw new Error("Unknown arguments. Run todo --help.");
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error("The TUI needs an interactive terminal. Use todo stats --json for scripts.");
  const local = command === "--global" ? undefined : command === "--file" ? resolveTodo(resolve(cwd, args[1]!)) : findLocalTodo(cwd);
  if (local && !loadConfig().projects.some(p => p.path === local)) registerProject(local);
  const { launch } = await import("./app");
  await launch(local);
}
if (import.meta.main) {
  try { await main(); }
  catch (error) { console.error(`todo: ${error instanceof Error ? error.message : String(error)}`); process.exitCode = 1; }
}

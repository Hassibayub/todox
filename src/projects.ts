import { existsSync, mkdirSync, readFileSync, realpathSync, renameSync, statSync, writeFileSync, openSync, closeSync, unlinkSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { homedir } from "node:os";
import { randomUUID } from "node:crypto";
import { readDocument, type Document } from "./markdown";

export interface Project { name: string; path: string }
export interface LoadedProject extends Project { doc?: Document; error?: string }
export interface Config { version: 1; projects: Project[] }
export function configPath(): string {
  return resolve(process.env.TODOX_CONFIG || join(process.env.XDG_CONFIG_HOME || join(homedir(), ".config"), "todox", "projects.json"));
}
export function loadConfig(path = configPath()): Config {
  if (!existsSync(path)) return { version: 1, projects: [] };
  const value = JSON.parse(readFileSync(path, "utf8"));
  if (value.version !== 1 || !Array.isArray(value.projects) || !value.projects.every((p: Project) => typeof p.name === "string" && typeof p.path === "string")) {
    throw new Error(`Invalid project registry: ${path}`);
  }
  return { version: 1, projects: value.projects.map((p: Project) => {
    const absolute = isAbsolute(p.path) ? p.path : resolve(dirname(path), p.path);
    return { name: p.name, path: existsSync(absolute) ? realpathSync(absolute) : absolute };
  }) };
}
function updateConfig(path: string, update: (config: Config) => Config): Config {
  mkdirSync(dirname(path), { recursive: true });
  const lock = `${path}.lock`;
  let fd: number;
  try { fd = openSync(lock, "wx", 0o600); } catch { throw new Error(`Project registry is busy: ${lock}`); }
  const temp = `${path}.${randomUUID()}.tmp`;
  try {
    const config = update(loadConfig(path));
    writeFileSync(temp, JSON.stringify(config, null, 2) + "\n", { mode: 0o600 });
    renameSync(temp, path);
    return config;
  } finally {
    if (existsSync(temp)) unlinkSync(temp);
    closeSync(fd); unlinkSync(lock);
  }
}
export function resolveTodo(path: string): string {
  const absolute = resolve(path);
  const candidate = statSync(absolute).isDirectory() ? join(absolute, "TODO.md") : absolute;
  if (!statSync(candidate).isFile()) throw new Error(`Not a TODO file: ${candidate}`);
  return realpathSync(candidate);
}
export function registerProject(path: string, name?: string, registry = configPath()): Project {
  const todo = resolveTodo(path);
  const project = { name: name || basename(dirname(todo)), path: todo };
  updateConfig(registry, config => ({ version: 1, projects: [...config.projects.filter(p => p.path !== todo), project] }));
  return project;
}
export function unregisterProject(path: string, registry = configPath()): void {
  const absolute = resolve(path);
  const candidate = existsSync(absolute) && statSync(absolute).isFile() ? absolute : /\.md$/i.test(absolute) ? absolute : join(absolute, "TODO.md");
  const todo = existsSync(candidate) ? realpathSync(candidate) : existsSync(dirname(candidate)) ? join(realpathSync(dirname(candidate)), basename(candidate)) : candidate;
  updateConfig(registry, config => ({ version: 1, projects: config.projects.filter(p => p.path !== todo) }));
}
export function findLocalTodo(cwd: string): string | undefined {
  let directory = resolve(cwd);
  while (true) {
    const candidate = join(directory, "TODO.md");
    if (existsSync(candidate) && statSync(candidate).isFile()) return realpathSync(candidate);
    // A repository boundary prevents inheriting a parent workspace's TODO.
    if (existsSync(join(directory, ".git"))) return undefined;
    const parent = dirname(directory);
    if (parent === directory) return undefined;
    directory = parent;
  }
}
export function loadProjects(projects: Project[]): LoadedProject[] {
  return projects.map(project => {
    try { return { ...project, doc: readDocument(project.path) }; }
    catch (error) { return { ...project, error: error instanceof Error ? error.message : String(error) }; }
  });
}

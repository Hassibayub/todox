import { afterEach, expect, test } from "bun:test";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
const cli = resolve(import.meta.dir, "../src/cli.ts");
const dirs: string[] = [];
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }); });
function fixture() {
  const cwd = realpathSync(mkdtempSync(join(tmpdir(), "todo-cli-"))); dirs.push(cwd);
  const config = join(cwd, "registry.json");
  const run = (...args: string[]) => {
    const result = Bun.spawnSync([process.execPath, cli, ...args], { cwd, env: { ...process.env, TODOX_CONFIG: config }, stdout: "pipe", stderr: "pipe" });
    return { code: result.exitCode, out: result.stdout.toString(), err: result.stderr.toString() };
  };
  return { cwd, config, run };
}
test("separate command manages same registry format without overwriting TODO", () => {
  const { cwd, config, run } = fixture();
  expect(run("--version").out.trim()).toBe("0.2.0");
  expect(run("--help").out).toContain("todo --global");
  expect(run("bad-argument").err).toContain("todo: Unknown arguments");
  expect(run("init").code).toBe(0); expect(run("init").code).toBe(1);
  const path = join(cwd, "TODO.md");
  writeFileSync(path, "# TODO\n- [ ] Later\n- [/] Now\n- [x] Done\n");
  expect(run("add-project").code).toBe(0);
  expect(JSON.parse(run("stats", "--json").out)).toEqual({ projects: 1, notStarted: 1, inProgress: 1, completed: 1, errors: [] });
  expect(run("projects").out).toContain(path);
  expect(run("remove-project").code).toBe(0);
  expect(JSON.parse(readFileSync(config, "utf8")).projects).toEqual([]);
  expect(existsSync(path)).toBe(true);
});
test("scan is shallow and interactive UI rejects pipes", () => {
  const { cwd, config, run } = fixture();
  mkdirSync(join(cwd, "app", "deep"), { recursive: true });
  writeFileSync(join(cwd, "app", "TODO.md"), "- [ ] One\n");
  writeFileSync(join(cwd, "app", "deep", "TODO.md"), "- [ ] Two\n");
  expect(run("scan").code).toBe(0);
  expect(JSON.parse(readFileSync(config, "utf8")).projects.length).toBe(1);
  expect(run("--global").err).toContain("interactive terminal");
});

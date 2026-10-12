import { createTestRenderer } from "@opentui/core/testing";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { CoreApp } from "../src/app";
import { TodoModel } from "../src/model";

const model = new TodoModel(undefined, resolve(import.meta.dir, "../examples/config.json"));
const dir = resolve(import.meta.dir, "../.pi/review"); mkdirSync(dir, { recursive: true });
const setup = await createTestRenderer({ width: 110, height: 28, kittyKeyboard: true });
new CoreApp(setup.renderer, undefined, { model, onEditor: async () => {} });
async function capture(name: string) {
  await setup.renderOnce();
  writeFileSync(`${dir}/${name}.txt`, setup.captureCharFrame());
}
try {
  await capture("overview-wide");
  model.openProject(); await capture("project-wide");
  setup.resize(60, 16); await capture("project-compact");
  model.begin("section-add"); await capture("new-section-compact");
  model.cancel(); model.begin("filter"); await capture("filter-compact");
  model.cancel(); model.inProgress(); await capture("active-compact");
} finally { setup.renderer.destroy(); }
console.log(`Read-only frames: ${dir}`);

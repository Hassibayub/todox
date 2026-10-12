import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { AnsiApp } from "../src/app";
import { TodoModel } from "../src/model";

const model = new TodoModel(undefined, resolve(import.meta.dir, "../examples/config.json"));
const dir = resolve(import.meta.dir, "../.pi/review"); mkdirSync(dir, { recursive: true });
const app = new AnsiApp(model);
function capture(name: string, width: number, height: number) {
  const frame = app.frame(width, height);
  writeFileSync(`${dir}/${name}.txt`, frame.map(row => row.text).join("\n"));
  writeFileSync(`${dir}/${name}.json`, JSON.stringify(frame));
}
try {
  capture("overview-wide", 110, 24);
  model.move(1); model.openProject(); capture("project-wide", 110, 20);
  capture("project-compact", 42, 12);
  model.begin("section-add"); capture("new-section-compact", 42, 12);
  model.cancel(); model.begin("filter"); capture("filter-compact", 42, 12);
  model.cancel(); model.inProgress(); capture("active-compact", 42, 12);
} finally { app.dispose(); }
console.log(`Read-only frames: ${dir}`);

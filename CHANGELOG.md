# Changelog

All notable changes to this project are recorded here. Versions follow [semantic versioning](https://semver.org/).

The distribution is named `todox-tui` (the PyPI name `todox` belongs to an unrelated project); the import package is `todox` and the command is `todo`.

## 0.4.4

- Sync with TickTick once when the app starts, staying silent when nothing changed and never opening the conflict screen from a launch sync. `Y` still syncs on demand; `TODOX_AUTO_SYNC=0` starts quietly.

## 0.4.3

- Read completed TickTick tasks during sync. TickTick's project endpoint returns open tasks only, which made every completed task look deleted and produced false conflicts; the completed view is now merged in, with per-year windows as a fallback past the 200-task cap.

## 0.4.2

- `y` copies the selected task (or section name, or project path); TickTick sync moved to `Y`.

## 0.4.1

- Draw with the terminal's own palette: the app now uses the ANSI theme, leaves the terminal background untouched, marks the selected row with `>` and bold instead of a filled bar, and dims secondary text. `TODOX_THEME` overrides the theme.

## 0.4.0

- Optional two-way TickTick sync: a dedicated `TODO.md` list, project names as tags, hidden inline task ids, conflicts paused instead of overwritten, deletions opt-in.
- Rewrote the app in Python with [Textual](https://textual.textualize.io/): responsive Projects, global Tasks and per-project views, a hidden Main section, completed/date toggles, and completion dates written to `TODO.md`.
- Local `TODO.md` files keep their line endings and unrelated Markdown, guarded by a cooperative lock, a stale-file check, and atomic replacement.

## 0.2.0

- Last release of the hand-written ANSI version built with Bun and TypeScript.

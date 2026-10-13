# Changelog

All notable changes to this project are recorded here. Versions follow [semantic versioning](https://semver.org/).

The distribution is named `todox-tui` (the PyPI name `todox` belongs to an unrelated project); the import package is `todox` and the command is `todo`.

## Unreleased

- Open local tasks immediately and sync in an owned background process. Quit stops and reaps it promptly instead of waiting for a network thread; interrupted uploads retain their journal records. View changes and display workers no longer cancel sync.
- Short, plain-language sync output by default; hide normal skips and internal paths, summarize optional cleanup once, show actual changes and actionable review items. `--verbose` retains the detailed report; JSON output stays compatible.
- Keep conflict-resolution failures visible and run resolution in the background, without blocking keyboard navigation or quitting.
- Delete tasks and remove sections with `x`/`Backspace` then a single `y`; no need to type DELETE. Any other key cancels and keeps its normal meaning.

- Audit fixes: preserve edits made during upload, reject stale local/remote writes and retirement, retain remote-only metadata, handle mixed-case tags consistently, and keep failed TUI resolutions visible. Read-only previews cannot execute conflict resolution; uncertain server errors cannot blindly repeat creation.

- SQLite-backed project registry and sync mappings, with read-only legacy JSON fallback, atomic migration, private snapshots and consistent database backups. Markdown remains the editable local task source.
- Durable sync-run/operation history, recoverable interrupted imports, unknown-upload retry protection, field-level baseline snapshots and safe independent text/status merging.
- Sync project tags across Inbox and all accessible TickTick lists, retain actual remote list ids, and pause removed or ambiguous project tags. Capped discovery stops rather than implying deletion.
- Local status/history/recovery/doctor commands, JSON export, and per-project local-only/sync controls.

- Active-only first uploads: completed local history stays local. Approved completed-copy cleanup retains local-only sync records, and reopened tasks restore their remote copy.
- First launch previews rather than writing after the active-only upgrade; the first manual sync backs up local files and state. Custom registries require explicit auto-sync opt-in.
- Read-only previews no longer create lists. JSON output includes per-project counts and remote ids; `--cleanup-temp` previews tracked temporary-workspace pollution without deleting untracked tasks.
- Preserve personal tags, block duplicate ids and ambiguous project tags, pause duplicate-title imports, protect unreadable files, checkpoint writes, and reject overlapping/stale writers.
- Isolate test credentials, registry, and state, and block real network access in tests.

- `Esc` now dismisses the status line (`Dates visible`, `Task copied`, `Sync failed · …`), so the key hints come back. On a local project screen `Esc` had nothing else to do and a status message stayed on screen until another action replaced it.
- Rows use the full terminal width instead of a centered 100-cell column, and a wrapped row's continuation lines line up under its own text instead of starting at the left edge. The wrap is structural: the checkbox is its own column and the text wraps beside it.
- `w` switches long rows between wrapping and one-line clipping, ending in `…`; `TODOX_WRAP=0` starts clipped. Section titles and the global task list follow the same setting.

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

# todox

A small, keyboard-first todo app for the terminal. Tasks live in ordinary, editable `TODO.md` files; a private SQLite database keeps the project registry and sync bookkeeping. Built with Python and [Textual](https://textual.textualize.io/).

No account or database server required, and no daemon. Optional two-way [TickTick sync](#ticktick-sync-optional) if you want your tasks on your phone too.

![Projects view showing per-project task counts](assets/screenshot.png)

## Requirements

- Python 3.10 or newer
- A terminal. The TUI needs an interactive terminal; the script-friendly commands (`stats`, `projects`, `sync`) do not.

## Install

The package is not on PyPI — the `todox` name there belongs to a different project — so install it from this repository.

With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/Hassibayub/todox
```

With [pipx](https://pipx.pypa.io/):

```sh
pipx install git+https://github.com/Hassibayub/todox
```

From a checkout (editable, handy while developing):

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/todo --help
```

Upgrade later with `uv tool install --force git+https://github.com/Hassibayub/todox` or `pipx install --force git+https://github.com/Hassibayub/todox`.

The command is `todo`. To try the bundled sample projects without installing anything:

```sh
TODOX_CONFIG=examples/config.json python3 -m todox --global
```

`python -m todox` is equivalent to the `todo` command.

## Use

```sh
cd my-project
todo init                         # Create and register TODO.md
todo                              # Open the nearest local TODO.md
todo add-project ~/code/my-app     # Register an existing TODO.md
todo scan ~/code                   # Register immediate child projects
todo --global                      # Open global Projects explicitly
todo projects                      # List registered TODO.md files
todo stats --json                  # Counts for scripts
```

A normal `todo` invocation opens the local file if one exists; from that screen you cannot navigate to global views. Without a local file, the app opens **Projects**. `todo --global` deliberately opens Projects even when a local file exists. Registry and sync metadata are stored in `~/.config/todox/todox.sqlite3` (or `$XDG_CONFIG_HOME/todox/todox.sqlite3`); override with `TODOX_DB`. `TODOX_CONFIG` selects a registry profile and its legacy JSON import source. Existing `projects.json` and `sync.json` are read until migration, backed up, and never dual-written afterward. `TODOX_SYNC_STATE=/path/sync.json` remains a compatibility override, using `/path/sync.sqlite3` for new writes.

## Screens and keys

**Projects** shows each project's completed, in-progress, and todo counts. `Enter` opens it; `c` copies its directory path; `t` opens the global list of todo and in-progress tasks. `p` returns to Projects from a globally opened project or the task list.

**Project tasks** show tasks before the first `##` heading without a visible Main heading. An older `## General` or `## Main` as the first section is also hidden. Each other `##` heading groups its tasks. `n` on a section adds there; `n` elsewhere adds to Main.

| Key | Action |
|---|---|
| `j/k`, arrows | Move through a scrolling list |
| `Enter` | Open project, or edit task; on a section, add there |
| `n`, `e` | Add or edit task |
| `Space`, `1/2/3` | Cycle or set todo / in progress / completed |
| `u` | Hide or show completed tasks |
| `d` | Hide or show completion dates in the UI |
| `w` | Wrap long rows, or clip them to one line with `…` |
| `s` | Add a `##` section |
| `x`, `Backspace` | Delete a task or remove an empty section (confirmation required) |
| `/`, `Esc` | Search; clear search, dismiss the status line, or return from a global project |
| `o` | Open selected task in Neovim |
| `y` | Copy the selected task text (section name or project path on those rows) |
| `Y` | Sync with TickTick |
| `r`, `?`, `q` | Reload, help, quit |

Completion adds a visible suffix, for example `- [x] Ship (completed: 2026-10-12)`. Moving the task back to todo or in progress removes the suffix. `d` only changes whether the app displays that date; it never removes it from the file. Existing files without dates still work.

The layout scrolls and reflows with the terminal size, using the full width of the window; long rows wrap and their continuation lines line up under the task text instead of snapping back to the left edge. `w` switches wrapping off so each row stays on one line and ends in `…`, and `TODOX_WRAP=0` starts that way. Narrow project rows put counts beneath the name; short screens hide secondary hints, not tasks. Task files retain their line endings and unrelated Markdown. Updates use a cooperative lock, a stale-file check, and atomic replacement.

The interface draws with your terminal's own palette: the ANSI theme leaves the terminal background untouched, secondary text is dim, and the selected row is marked with `>` and bold rather than a filled bar. Set `TODOX_THEME` to any Textual theme name to override it.

## TickTick sync (optional)

`todox` can mirror tasks to a TickTick account through the official [Open API](https://developer.ticktick.com/docs/index.html#/openapi). Setup needs one personal token; nothing is uploaded until you sync.

```sh
# TickTick web app: avatar > Settings > Account > API Token
todo ticktick login PASTE_TOKEN
# or pipe it: printf '%s' "$TOKEN" | todo ticktick login
todo sync --dry-run        # preview
todo sync                  # apply
todo ticktick status       # token and list state
```

The token is stored in `~/.config/todox/ticktick.json` with owner-only permissions, or supplied via `TODOX_TICKTICK_TOKEN`. The first launch after upgrading **previews instead of writing** until the profile is migrated and active-only sync has been acknowledged. Review `todo sync --dry-run --json`, then run `todo sync` (or press `Y`) to enable subsequent launch syncs. Before that first write, a private snapshot of the registry, sync state, and affected local files is saved under the database directory's `backups/`. Set `TODOX_AUTO_SYNC=0` to disable launch sync. Custom registries do not auto-sync unless `TODOX_AUTO_SYNC=1` is explicitly set; demo scripts should also use isolated credentials and `TODOX_DB`.

**Mapping.** Project tags drive sync across **Inbox and all accessible TickTick lists**, not just `TODO.md`. A task tagged `web-app` imports into the registered `web-app/TODO.md` file wherever it lives remotely. Its original list is retained for updates, completion, deletion, and reopening after retirement. New local tasks upload into the dedicated `TODO.md` list (created only when needed). `[ ]` and `[/]` stay open in TickTick, with `[/]` also carrying an `inprogress` tag because TickTick has no in-progress state. Historical `[x]` tasks stay local and are not uploaded. Sections stay local. Tasks matching multiple project tags pause for review; removing a linked task's project tag pauses propagation without deleting either copy. Empty, colliding, or reserved (`inprogress`) project tags pause rather than guessing a mapping. Personal TickTick tags are preserved during updates. Incomplete or capped API discovery stops sync rather than inferring deletions.

**Completed projects.** An existing synced task is marked completed on TickTick when you complete it locally. To remove its completed remote copy while retaining local history, preview `todo sync --dry-run --allow-deletes --json`, then approve with `todo sync --allow-deletes`. `retire` means a completed remote copy will be deleted, not the local task. Its sync id stays recorded as local-only, so subsequent syncs do not re-upload it; reopening the task creates a fresh remote copy. Completion detected remotely is first pulled into the local file, and becomes eligible for cleanup on the next sync. The shared `TODO.md` list is never deleted. TickTick may retain empty sidebar tags: automatic tag deletion is not implemented because a supported official endpoint has not been verified.

**Temporary-workspace cleanup.** If previous demo runs polluted the real account, use `todo sync --dry-run --allow-deletes --cleanup-temp --json`. This identifies tracked tasks whose files belong to unregistered temporary workspaces; other orphaned tasks are left untouched. Review the remote ids and counts before repeating without `--dry-run`. This still includes normal reconciliation and completed-task cleanup, so review the entire preview. Untracked remote tasks are never deleted by this cleanup. Do not use blanket `--allow-deletes` as a shortcut for reviewing pollution.

**Safety.** Only tasks this tool linked are ever updated. Either side changing alone propagates; independent text/status edits merge when baseline snapshots are available, and conflicting field edits pause. Remote deletion requires `--allow-deletes`. Unreadable files, disabled projects, and unregistered projects never trigger ordinary remote deletion. Writers are serialized and stale plans are rejected; successful operations and their mappings are checkpointed transactionally. Existing tasks are imported only when a registered project tag matches. Matching unlinked tasks and duplicate titles pause for review instead of silently uploading or importing duplicates. JSON previews include overall and per-project counts plus remote ids; failed operations cause a nonzero CLI exit status. A preview does not initialize/migrate the database or modify files or remote tasks.

**Bookkeeping and recovery.** SQLite stores project registrations, stable local/remote identities, per-task list ids, baseline snapshots, conflicts, run history, and an operation journal. Markdown remains the local task source: external file edits are re-read and guarded before saving. Network requests and file writes are never performed inside database transactions. Unknown upload outcomes are blocked from automatic retry; titles alone are never proof of identity. `todo sync recover` can restore an interrupted import's mapping from its existing local uid without appending another task. Unknown creations require identity review instead of automatic replay.

```sh
todo db migrate                       # bookkeeping only: backup + atomic JSON import; no network
todo sync status                      # local state, pending operations and last successful sync
todo sync history                     # recent runs; no network
todo sync recover                     # recover provable bookkeeping; no remote writes
todo project web-app --local-only     # pause this project; preserve both sides
todo project web-app --sync           # re-enable
todo doctor                           # SQLite integrity and local file checks
todo db backup                        # consistent SQLite backup via the backup API
todo db export                        # portable registry and mappings JSON (stdout)
```

The database uses WAL, foreign keys, private file permissions and strong write durability. Keep it on a local filesystem, not a shared drive or a live cloud-synced folder. Back it up using `todo db backup`, not by copying an open database file without its WAL. It is not encrypted. Snapshots and journals contain task text but never credentials. Restore a consistent backup with the app and other sync processes stopped; restoring SQLite does not undo TickTick deletions. Database schema versions newer than this app are rejected. The old JSON files are migration backups, not an alternative writable registry after migration.

TickTick offers no webhooks, so refresh happens when the app starts or when you press `Y`; nothing polls in the background.

**Conflicts.** `todo sync --json` lists them; resolve one with `todo sync --keep-local <uid>` or `todo sync --keep-remote <uid>`. In the TUI the conflict screen uses `1` for your file and `2` for TickTick. Each synced task carries a hidden `<!-- todox:id=… -->` marker so text edits and line moves stay matched.

Completion dates in your local file remain authoritative for local display.

## Develop

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests_py -q
.venv/bin/ruff check todox tests_py
uv build                                   # sdist + wheel
```

The tests use temporary task files, registries, and a fake TickTick transport, so they never touch your real data or the network. See [CHANGELOG.md](CHANGELOG.md) for release history. MIT licensed; see [LICENSE](LICENSE).

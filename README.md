# todox

A small, keyboard-first todo app for the terminal. Tasks live in ordinary `TODO.md` files; projects are a registry of those files, not another database. Built with Python and [Textual](https://textual.textualize.io/).

![Projects view showing per-project task counts](assets/screenshot.png)

## Install

Python 3.10+ is required. With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install .
```

Or in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/todo --help
```

If an older Bun-linked `todo` is still on your `PATH`, remove that link or invoke the command from your virtual environment. To try sample projects without installing the command:

```sh
TODOX_CONFIG=examples/config.json python3 -m todox.cli --global
```

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

A normal `todo` invocation opens the local file if one exists; from that screen you cannot navigate to global views. Without a local file, the app opens **Projects**. `todo --global` deliberately opens Projects even when a local file exists. The registry is `~/.config/todox/projects.json` or `$XDG_CONFIG_HOME/todox/projects.json`; override it with `TODOX_CONFIG`.

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
| `s` | Add a `##` section |
| `x`, `Backspace` | Delete a task or remove an empty section (confirmation required) |
| `/`, `Esc` | Search; clear search or return from a global project |
| `o` | Open selected task in Neovim |
| `y` | Sync with TickTick |
| `r`, `?`, `q` | Reload, help, quit |

Completion adds a visible suffix, for example `- [x] Ship (completed: 2026-10-12)`. Moving the task back to todo or in progress removes the suffix. `d` only changes whether the app displays that date; it never removes it from the file. Existing files without dates still work.

The layout scrolls and reflows with the terminal size. Narrow project rows put counts beneath the name; short screens hide secondary hints, not tasks. Task files retain their line endings and unrelated Markdown. Updates use a cooperative lock, a stale-file check, and atomic replacement.

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

The token is stored in `~/.config/todox/ticktick.json` with owner-only permissions, or supplied via `TODOX_TICKTICK_TOKEN`. In the TUI, `y` runs a sync and shows the result in the footer.

**Mapping.** Sync uses one dedicated TickTick list named `TODO.md` (created on first sync). Each registered project name becomes a task tag, so `web-app/TODO.md` becomes the `web-app` tag. `[ ]` and `[/]` stay open in TickTick, with `[/]` also carrying an `inprogress` tag because TickTick has no in-progress state; `[x]` becomes completed and `(completed: …)` supplies the date. Sections stay local.

**Safety.** Only tasks this tool linked are ever updated. Either side changing alone propagates; both sides changing pauses as a conflict. Nothing is deleted on the other side unless you pass `--allow-deletes` or confirm in the app. Existing TickTick tasks are never imported unless they sit in the `TODO.md` list carrying a matching project tag, and unrecognised tags are reported instead of guessed.

**Conflicts.** `todo sync --json` lists them; resolve one with `todo sync --keep-local <uid>` or `todo sync --keep-remote <uid>`. In the TUI the conflict screen uses `1` for your file and `2` for TickTick. Each synced task carries a hidden `<!-- todox:id=… -->` marker so text edits and line moves stay matched.

Because TickTick's create endpoint cannot set a completion timestamp, a task already completed in `TODO.md` is completed at import time; the date in your file stays authoritative for display afterwards.

## Develop

```sh
python3 -m pytest tests_py -q
```

The tests use temporary task files and registries. MIT licensed; see [LICENSE](LICENSE).

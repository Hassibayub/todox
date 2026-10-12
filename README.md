# todox

A small, keyboard-first todo app for the terminal. Tasks live in ordinary `TODO.md` files; projects are a registry of those files, not another database. Built with Python and [Textual](https://textual.textualize.io/).

No account, no database, no daemon. Optional two-way [TickTick sync](#ticktick-sync-optional) if you want your tasks on your phone too.

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

The token is stored in `~/.config/todox/ticktick.json` with owner-only permissions, or supplied via `TODOX_TICKTICK_TOKEN`. With a token in place the app catches up **once at launch**; press `Y` whenever you want to sync again, or set `TODOX_AUTO_SYNC=0` to start quietly instead.

**Mapping.** Sync uses one dedicated TickTick list named `TODO.md` (created on first sync). Each registered project name becomes a task tag, so `web-app/TODO.md` becomes the `web-app` tag. `[ ]` and `[/]` stay open in TickTick, with `[/]` also carrying an `inprogress` tag because TickTick has no in-progress state; `[x]` becomes completed and `(completed: …)` supplies the date. Sections stay local.

**Safety.** Only tasks this tool linked are ever updated. Either side changing alone propagates; both sides changing pauses as a conflict. Nothing is deleted on the other side unless you pass `--allow-deletes` or confirm in the app. Existing TickTick tasks are never imported unless they sit in the `TODO.md` list carrying a matching project tag, and unrecognised tags are reported instead of guessed.

TickTick offers no webhooks, so refresh happens when the app starts or when you press `Y`; nothing polls in the background.

**Conflicts.** `todo sync --json` lists them; resolve one with `todo sync --keep-local <uid>` or `todo sync --keep-remote <uid>`. In the TUI the conflict screen uses `1` for your file and `2` for TickTick. Each synced task carries a hidden `<!-- todox:id=… -->` marker so text edits and line moves stay matched.

Because TickTick's create endpoint cannot set a completion timestamp, a task already completed in `TODO.md` is completed at import time; the date in your file stays authoritative for display afterwards.

## Develop

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests_py -q     # 32 tests
.venv/bin/ruff check todox tests_py
uv build                                   # sdist + wheel
```

The tests use temporary task files, registries, and a fake TickTick transport, so they never touch your real data or the network. See [CHANGELOG.md](CHANGELOG.md) for release history. MIT licensed; see [LICENSE](LICENSE).

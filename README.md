# HX

A terminal coding agent. Python core, Textual TUI, OpenRouter for models.

HX runs in your project directory, reads and edits your code, runs commands in
a sandboxed shell, and shows you what every turn costs.

---

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/aletisunil/hx/main/install.sh | sh
```

The script bootstraps [uv](https://docs.astral.sh/uv/) if you don't have it,
then installs HX as an isolated tool with a pinned Python. Re-running it
upgrades in place.

If you'd rather not pipe a script into a shell:

```sh
uv tool install hx-cli      # or: pipx install hx-cli
```

### `hx-cli` on PyPI, `hx` in your terminal

The package is published as **`hx-cli`**. The command it installs is **`hx`**,
and that is what you type — the longer name never appears again after install.

The plain name `hx` on PyPI is registered by someone else and has no releases,
so `uv tool install hx` fails with "no versions of hx". Install `hx-cli`.

| | Name |
|---|---|
| PyPI package | `hx-cli` |
| Command | `hx` |
| Python import | `hx` |
| Config directory | `~/.hx` |

If the command isn't found after installing, add uv's bin directory to your
`PATH`:

```sh
export PATH="$(uv tool dir --bin):$PATH"
```

Requires Python 3.11+. macOS and Linux. Update with `hx upgrade`.

### The API key

On first run HX asks for an OpenRouter key and saves it to `~/.hx/auth.json`
with mode 0600. Get one at <https://openrouter.ai/keys>.

To change it later, `/configure` inside the TUI, or from a shell:

```sh
hx auth          # is a key set, and where does it come from?
hx auth set      # paste a new one (hidden input)
hx auth clear    # remove the saved key
```

Resolution order is `HX_OPENROUTER_API_KEY`, then `OPENROUTER_API_KEY`, then
the saved file. The environment wins, and both `/configure` and `hx auth` say
so — otherwise saving a key while a variable is set looks like a no-op.

---

## Running it

```sh
hx                          # interactive TUI in the current directory
hx -p "explain this repo"   # headless: streams to stdout, tool activity to stderr
hx resume                   # resume the last session here
hx resume <session-id>      # resume a specific one
hx --model openai/gpt-5     # override the model for one run
hx --mode plan              # start read-only
hx --cwd ../other-project   # run against a different directory
hx --no-sandbox             # disable OS sandboxing (rules still apply)
hx mcp list|add|remove      # manage MCP servers
hx auth [set|clear]         # manage the API key
hx upgrade                  # update to the latest release
```

Print mode is the scriptable one: stdout carries only the assistant's text, so
it pipes cleanly.

### Keys

Every key below is defined once, in `src/hx/keys.py`. `/help`, the hints line
under the prompt and this table all read from that registry, and any of them can
be rebound (see [Keybindings](#keybindings)).

| Key | Does |
|---|---|
| `enter` | send |
| `ctrl+j` | newline (also `shift+enter`) |
| `esc` | interrupt the current turn |
| `ctrl+c` | clear the prompt; twice on an empty prompt exits |
| `ctrl+d` | exit, when the prompt is empty |
| `ctrl+z` | suspend to the background |
| `shift+tab` | cycle permission mode |
| `ctrl+p` | command palette |
| `ctrl+l` | model picker |
| `ctrl+o` | expand tool output (also `ctrl+r`) |
| `ctrl+t` | toggle the todo sidebar |
| `ctrl+x` | copy the selected message to the clipboard |
| `ctrl+up` | previous message |
| `ctrl+down` | next message |
| `pgup` | scroll the transcript up |
| `pgdn` | scroll the transcript down |
| `ctrl+home` | jump to the start of the transcript |
| `ctrl+end` | jump back to the newest output |
| `@path` | complete a file path |
| `!command` | run a shell command directly, no model turn |

`!` still goes through the permission engine and the sandbox — it skips the
model, not the safety layers.

The prompt is a readline-style editor: `ctrl+a`/`ctrl+e` for line start and end,
`ctrl+b`/`ctrl+f` by character, `alt+b`/`alt+f` by word, `ctrl+w` and `alt+d` to
kill a word, `ctrl+u` and `ctrl+k` to kill to the start or end of a line, then
`ctrl+y` to yank it back and `alt+y` to walk further down the kill ring.
`ctrl+z` undoes, `ctrl+shift+z` redoes.

Typing `/` or `@` opens a completion list above the prompt; `tab` cycles it,
`enter` accepts, `esc` dismisses.

#### Copying

`ctrl+x` copies the message the cursor is on, and `/copy` copies the last reply.
HX writes through the platform's own clipboard tool first (`pbcopy`, `wl-copy`,
`xclip`, `xsel`) and falls back to OSC 52, which is also always sent over SSH so
the text lands on the machine you are actually sitting at.

Because `ctrl+c` clears the prompt, drag-selecting in the transcript is your
terminal's own selection rather than the TUI's — use your terminal's copy key
for that. HX does not emit OSC 133 prompt markers yet, so shell-integration
features that jump between prompts will not see HX's messages.

#### Keybindings

Any key can be rebound in `~/.hx/keybindings.json`, keyed by the action ids in
`src/hx/keys.py`:

```json
{
  "app.tools.expand": "ctrl+r",
  "app.message.copy": ["ctrl+x", "alt+c"]
}
```

Conflicts and unknown action names are reported as a notice at startup rather
than being silently resolved.

### Commands

| Command | Does |
|---|---|
| `/model [query]` | pick a model; shows context window, price per Mtok, cache support |
| `/models refresh` | re-fetch the catalogue |
| `/configure` | session settings and the API key |
| `/mode [name]` | `plan`, `default`, `acceptEdits`, `bypass` |
| `/permissions` | active rules and what is enforcing them |
| `/context` | what is filling the context window |
| `/cost` | tokens, cache savings, spend |
| `/compact [focus]` | summarise older turns now |
| `/clear` | fresh session, same directory |
| `/resume` | reopen a previous session |
| `/todos` | toggle the sidebar |
| `/skills` | installed skills |
| `/agents` | subagent types |
| `/mcp` | server status |
| `/theme [name]` | `dark`, `light`, `ansi`, or any theme in `~/.hx/themes` |
| `/copy` | copy the last reply to the clipboard |
| `/init` | generate an `HX.md` for the project |
| `/help` | list commands and keys |
| `/quit` | exit (also `/exit`, `/q`) |

### The status bar

Two lines: working directory and permission mode above; token counts, cache
read/write with hit rate, spend, last-turn latency, context gauge and model
below. The cache and cost fields are the point of it — a hit rate that
collapses after an edit is the visible symptom of a broken prefix.

---

## Configuration

Settings are JSON, merged lowest to highest:

```
defaults  <  ~/.hx/settings.json  <  ./.hx/settings.json  <  HX_* env  <  CLI flags
```

Permission rule lists are unioned across layers, so a project can add a deny
rule without discarding yours. Everything else is replaced.

```jsonc
{
  "theme": "dark",                    // dark | light | ansi, or a file in ~/.hx/themes
  "quiet_startup": false,             // skip the startup header
  "telemetry": false,

  "models": {
    "model": "anthropic/claude-sonnet-4.5",
    "subagent_model": null,           // defaults to "model"
    "max_tokens": 8192,
    "temperature": null
  },

  "permissions": {
    "mode": "default",                // plan | default | acceptEdits | bypass
    "allow": ["Bash(git status:*)"],
    "ask":   [],
    "deny":  ["Read(**/.env)"],
    "sandbox": true,
    "allow_network": false            // outbound network for sandboxed commands
  },

  "context": {
    "compact_at": 0.80,               // fraction of the window that triggers compaction
    "keep_recent_turns": 6,           // turns kept verbatim across a compaction
    "tool_output_char_cap": 25000,
    "tool_output_line_cap": 2000
  },

  "bash": {
    "timeout_seconds": 120,
    "max_timeout_seconds": 600,       // ceiling; caps what the model may ask for
    "shell": null                     // defaults to $SHELL
  }
}
```

Environment overrides: `HX_MODEL`, `HX_SUBAGENT_MODEL`, `HX_MAX_TOKENS`,
`HX_PERMISSION_MODE`, `HX_SANDBOX`, `HX_COMPACT_AT`, `HX_THEME`,
`HX_QUIET_STARTUP`. Also `HX_HOME` to relocate user state.

### Themes

A theme is a JSON file: a `vars` block of raw colours, and a `colors` block
mapping semantic roles onto them. Drop one in `~/.hx/themes/mine.json` and
`/theme mine` picks it up — no restart, no code change.

```jsonc
{
  "name": "mine",
  "dark": true,
  "vars": { "green": "#b5bd68", "gray": "#808080" },
  "colors": {
    "background": "#18181e",
    "success": "green",           // a vars key, or a literal
    "muted": "gray"
    // ...every role; a missing one is an error, not a silent black
  }
}
```

The role names are pi's, so [pi](https://github.com/earendil-works/pi) theme
files load here unchanged. `src/hx/tui/themes/dark.json` is the reference.

### Where things live

| Path | What |
|---|---|
| `~/.hx/settings.json` | your settings |
| `~/.hx/auth.json` | API key, mode 0600 |
| `~/.hx/models.json` | cached model catalogue, refreshed daily |
| `~/.hx/themes/` | your themes, one JSON file each |
| `~/.hx/keybindings.json` | your key overrides |
| `~/.hx/sessions/` | transcripts, spilled tool output, subagent sessions |
| `~/.hx/skills/`, `~/.hx/agents/` | your skills and agents |
| `./.hx/settings.json` | project settings, checked in if you like |
| `./.hx/mcp.json` | project MCP servers |
| `./.hx/skills/`, `./.hx/agents/` | project skills and agents |
| `./HX.md` | project instructions, loaded into every session |

`HX.md` is the place for things a newcomer would get wrong: how to run the
tests, conventions, what not to touch. `/init` writes a first draft. It is
loaded once per session and frozen, so it costs one prefix, not one per turn.

---

## Safety

Two independent layers guard every tool call, and both must pass.

**Permission rules** are `Tool(specifier)` strings — `Bash(git commit:*)`,
`Edit(src/**)`, `Read(**/.ssh/**)`. Deny beats ask beats allow, and a deny
holds even in bypass mode. Shell commands are decomposed into their real
segments first, so an allow rule for `git status` does not carry
`&& rm -rf /` along with it; a command that cannot be decomposed with
confidence prompts rather than passing.

**An OS sandbox** wraps command execution: Seatbelt on macOS, bubblewrap on
Linux. The filesystem is readable, writes are confined to the project and the
temp dir, credential paths (`~/.ssh`, `~/.aws`, and HX's own `auth.json`) are
unreadable, and outbound network is off. If neither backend is present the
status bar says `no-sandbox` rather than implying protection that is not there.

Modes cycle with shift+tab: `plan` (read-only — mutating tools are not even
offered to the model), `default`, `acceptEdits`, `bypass`.

---

## Context engineering

Long sessions are the normal case, so the harness is built around keeping the
provider's KV cache warm and the window from filling up.

**A stable prefix.** System prompt, tool schemas and project context are
assembled in a fixed order and never mutated mid-session. Cache breakpoints sit
at the end of that static block and at a rolling point before the recent turns,
which only advances once enough tokens have accumulated behind it.

**Late injection** carries everything that changes per turn — the todo list,
files that changed on disk since HX read them — on the tail of the newest user
message rather than in the prefix. Stale copies are stripped and regenerated
each turn, so six todo updates leave one copy in context, not six.

**Compaction** fires at 80% of the window, or on `/compact [focus]`. Older
turns are replaced by a structured summary; the recent turns and the todo list
survive verbatim, and the boundary snaps to a turn edge so a tool call is never
severed from its results. Superseded messages are flagged, not deleted, so
resume replays exactly what happened.

**Output capping** keeps the head and tail of a large tool result, spills the
rest to the session directory, and hands the model that path to grep.

---

## Extending it

**Skills** are directories containing `SKILL.md` with YAML frontmatter:

```markdown
---
name: deploy
description: Tag, build and ship a release
allowed-tools: Read, Bash          # optional; narrows the toolset while active
---

1. Run the tests.
2. Tag the commit.
```

Drop them in `.hx/skills/<name>/` or `~/.hx/skills/<name>/`; a project skill
shadows a user one of the same name. Only the name and description enter the
context — the body loads when the model calls `Skill(name)`, so a hundred
installed skills cost a hundred lines, not a hundred documents.

**Subagents** run in their own context with their own transcript, tool
allowlist and model. Only the final report returns to the parent, so a long
search costs the caller one paragraph instead of every intermediate tool
result. `explore`, `plan` and `general` ship built in; add your own as
`.hx/agents/<name>.md`:

```markdown
---
name: reviewer
description: Reviews a diff against the project's conventions
tools: Read, Grep
model: openai/gpt-5                 # optional
---

You review code. Be specific and cite file:line.
```

A subagent never gets the `Task` tool, so recursion is impossible by
construction.

**MCP servers** go in `.hx/mcp.json`:

```json
{
  "mcpServers": {
    "local": { "command": "python", "args": ["server.py"] },
    "remote": { "url": "https://example.com/mcp" }
  }
}
```

Or `hx mcp add local python server.py`. Tools arrive namespaced
`mcp__<server>__<tool>` in a deterministic order. Servers connect concurrently
with a per-server timeout; one that is broken or slow logs a warning and is
dropped rather than taking the session with it.

---

## Development

```sh
git clone https://github.com/aletisunil/hx && cd hx
uv sync --extra dev

uv run pytest                # the suite; live tests are deselected
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict
uv run hx                    # run from the checkout
```

Tests marked `live` hit the real OpenRouter API and cost money:

```sh
OPENROUTER_API_KEY=... uv run pytest -m live
```

Tests marked `sandbox` exercise the real OS sandbox and are skipped where no
backend exists.

The TUI has SVG layout snapshots. Read the diff before accepting a change to
them - they exist to catch a frame that quietly lost a row:

```sh
uv run pytest tests/tui/test_snapshots.py --snapshot-update
```

### Layout

```
src/hx/
  cli.py config.py paths.py frontmatter.py
  core/         loop, context assembly, compaction, late injection, sessions, usage
  providers/    OpenRouter, the model catalogue, a scripted provider for tests
  tools/        Bash, Read, Write, Edit, Glob, Grep, TodoWrite, Task, output capping
  permissions/  rule engine, shell decomposition, Seatbelt/bubblewrap
  skills/ agents/ mcp/
  keys.py       keybinding registry: ids, defaults, descriptions, user overrides
  tui/          Textual app, commands, per-tool renderers, widgets
    themes/     the shipped palettes, as JSON
```

The core is headless and emits events; the TUI and print mode are both just
consumers. Nothing under `core/`, `tools/`, `providers/` or `permissions/`
imports `tui/`.

---

## Releasing

For maintainers. HX publishes to PyPI as **`hx-cli`** from CI, on a tag.

One-time setup:

1. Push the repo to `github.com/<owner>/hx` and update the URLs in
   `pyproject.toml`.
2. Create a GitHub environment named `pypi`.
3. On PyPI, add a [trusted publisher](https://docs.pypi.org/trusted-publishers/)
   for the project: owner, repo `hx`, workflow `ci.yml`, environment `pypi`.
   No API token is stored anywhere.

Each release:

```sh
# bump __version__ in src/hx/__init__.py, commit
git tag v$(uv run hx --version | cut -d' ' -f2) && git push origin main --tags
```

The `publish` job runs only on `refs/tags/v*` and only after lint, the test
matrix, and an install-script run in a clean Debian container have passed. It
builds with `uv build` and uploads via OIDC.

To check a build before tagging:

```sh
uv build && ls dist/
```

---

## Status

Feature complete against the original plan: the agent loop, OpenRouter
streaming with prefix caching and accurate cost accounting, session persistence
and resume, the tool suite, the permission engine and OS sandbox, late
injection, compaction, output capping, skills, subagents, MCP, and the TUI.

Published to PyPI as [`hx-cli`](https://pypi.org/project/hx-cli/), released
from CI on a tag.

The one thing still unproven is a live OpenRouter call: the `live` tests exist
and cover the wire format, tool use and a genuine cache hit, but they need a
key and are deselected by default.

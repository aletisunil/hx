# HX

A terminal coding agent. Python core, Textual TUI, OpenRouter provider.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/sunilaleti/hx/main/install.sh | sh
```

Then run `hx` in any project directory. On first run it prompts for an
OpenRouter API key and saves it to `~/.hx/auth.json` (mode 0600).

To change it later, use `/configure` in the TUI or `hx auth set` from a shell.
`hx auth` shows whether a key is set and where it comes from, masked. The
environment (`HX_OPENROUTER_API_KEY`, then `OPENROUTER_API_KEY`) takes
precedence over the saved file, and both surfaces say so.

## Usage

```sh
hx                        # interactive TUI
hx -p "explain this repo" # headless, streams to stdout
hx resume                 # resume the last session in this directory
hx --model openai/gpt-5   # override the model for one run
```

Inside the TUI: `enter` sends, `ctrl+j` newline, `esc` interrupts,
`shift+tab` cycles permission mode, `ctrl+p` opens the command palette,
`ctrl+r` expands the last tool output, `ctrl+t` toggles todos. `@path`
completes a file, and `!command` runs a shell command directly - through the
same permission engine and sandbox, without spending a model turn.

Commands: `/model` `/models` `/clear` `/compact` `/resume` `/cost` `/context`
`/permissions` `/mode` `/skills` `/agents` `/mcp` `/todos` `/init` `/theme`
`/configure` `/help` `/quit`. `/help` lists them with keys.

The status bar carries the numbers that matter: model, context used against the
window, tokens in/out, **cache read and write tokens with hit rate**, session
cost, last-turn latency, and permission mode.

## Safety

Two independent layers guard every tool call, and both must pass.

**Permission rules** are `Tool(specifier)` strings in `settings.json` -
`Bash(git commit:*)`, `Edit(src/**)`, `Read(**/.ssh/**)`. Deny beats ask beats
allow, and a deny holds even in bypass mode. Shell commands are decomposed into
their real segments first, so an allow rule for `git status` does not carry
`&& rm -rf /` along with it; a command that cannot be decomposed with
confidence prompts rather than passing.

**An OS sandbox** wraps command execution: Seatbelt on macOS, bubblewrap on
Linux. The filesystem is readable, writes are confined to the project and the
temp dir, credential paths (`~/.ssh`, `~/.aws`, and HX's own `auth.json`) are
unreadable, and outbound network is off. If neither backend is present the
status bar says `no-sandbox` rather than implying protection that is not there.

Modes cycle with shift+tab: `plan` (read-only - mutating tools are not even
offered to the model), `default`, `acceptEdits`, `bypass`.

## Context engineering

Long sessions are the normal case, so the harness is built around keeping the
provider's KV cache warm and the window from filling up.

**A stable prefix.** System prompt, tool schemas and project context are
assembled in a fixed order and never mutated mid-session. Cache breakpoints sit
at the end of that static block and at a rolling point before the recent turns,
which only advances once enough tokens have accumulated behind it.

**Late injection** carries everything that changes per turn - the todo list,
files that changed on disk since HX read them - on the tail of the newest user
message rather than in the prefix. Stale copies are stripped and regenerated
each turn, so six todo updates leave one copy in context, not six.

**Compaction** fires at 80% of the window, or on `/compact [focus]`. Older
turns are replaced by a structured summary; the recent turns and the todo list
survive verbatim, and the boundary snaps to a turn edge so a tool call is never
severed from its results. Superseded messages are flagged, not deleted, so
resume replays exactly what happened.

**Output capping** keeps the head and tail of a large tool result, spills the
rest to the session directory, and hands the model that path to grep.

`/context` shows what is filling the window; `/cost` breaks down tokens, cache
savings and spend.

## Extending it

**Skills** are directories containing `SKILL.md` with YAML frontmatter, found
in `.hx/skills/` (project) and `~/.hx/skills/` (user); a project skill shadows
a user one of the same name. Only the name and description enter the context -
the body loads when the model calls `Skill(name)`, so a hundred installed
skills cost a hundred lines, not a hundred documents. `/skills` lists them.

**Subagents** run in their own context with their own transcript, tool
allowlist and model. Only the final report returns to the parent, so a long
search costs the caller one paragraph instead of every intermediate tool
result. `explore`, `plan` and `general` ship built in; add your own as
`.hx/agents/<name>.md`. A subagent never gets the `Task` tool, so recursion is
impossible by construction. `/agents` lists them.

**MCP servers** are configured in `.hx/mcp.json` (or `~/.hx/mcp.json`) and
managed with `hx mcp list|add|remove`. Tools arrive namespaced
`mcp__<server>__<tool>` in a deterministic order. Servers connect concurrently
with a per-server timeout; one that is broken or slow logs a warning and is
dropped rather than taking the session with it. `/mcp` shows their status.

## Status

Feature complete against the original plan. Working: the agent loop; OpenRouter
streaming with prefix caching and accurate cost accounting; session persistence
and resume; the tool suite (Bash with a persistent sandboxed shell, Read,
Write, Edit, Glob, Grep, TodoWrite, Task, Skill); the permission engine and OS
sandbox; late injection, compaction and output capping; skills, subagents and
MCP; and the TUI.

The one thing not yet exercised for real is a live OpenRouter call: the
`live`-marked tests exist and are ready, but they need a key and are
deselected by default. Run them with:

```sh
OPENROUTER_API_KEY=... uv run pytest -m live
```

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check . && uv run mypy
```

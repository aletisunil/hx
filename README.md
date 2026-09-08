# HX

A terminal coding agent. Python core, Textual TUI, OpenRouter provider.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/sunilaleti/hx/main/install.sh | sh
```

Then run `hx` in any project directory. On first run it prompts for an
OpenRouter API key and saves it to `~/.hx/auth.json` (mode 0600); you can also
set `OPENROUTER_API_KEY`.

## Usage

```sh
hx                        # interactive TUI
hx -p "explain this repo" # headless, streams to stdout
hx resume                 # resume the last session in this directory
hx --model openai/gpt-5   # override the model for one run
```

Inside the TUI: `enter` sends, `ctrl+j` newline, `esc` interrupts,
`shift+tab` cycles permission mode, `ctrl+r` expands the last tool output,
`ctrl+t` toggles todos. `/help` lists the slash commands.

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

## Status

Working: the agent loop; OpenRouter streaming with prefix caching and accurate
cost accounting; session persistence and resume; late injection; the tool suite
(Bash with a persistent sandboxed shell, Read, Write, Edit, Glob, Grep); the
permission engine and OS sandbox; tool-output capping; and the TUI with its
`/model` `/models` `/clear` `/resume` `/cost` `/context` `/help` `/quit`
commands.

Next: compaction, then skills, subagents and MCP.

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check . && uv run mypy
```

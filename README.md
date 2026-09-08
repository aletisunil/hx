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

## Status

Working: the agent loop, OpenRouter streaming with prefix caching and accurate
cost accounting, session persistence and resume, late injection, the TUI, and
the `/model` `/models` `/clear` `/resume` `/cost` `/context` `/help` `/quit`
commands.

Next: the tool suite (Bash, Read, Write, Edit, Glob, Grep), the permission
engine and OS sandbox, compaction and output capping, then skills, subagents
and MCP.

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check . && uv run mypy
```

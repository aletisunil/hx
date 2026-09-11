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

### Credentials

HX reaches models over two routes. Which one serves a turn is decided by the
model id alone, so "what paid for that" is always answerable by reading it:

| Model id | Route | Billing |
|---|---|---|
| `anthropic/claude-sonnet-4.5`, `openai/gpt-5`, … | OpenRouter | per token, API key |
| `openai-codex/gpt-5.6-terra` | ChatGPT Plus/Pro | your subscription |

Credentials live in `~/.hx/auth.json`, mode 0600, one entry per route.

**OpenRouter.** On first run HX asks for a key. Get one at
<https://openrouter.ai/keys>. Change it later with `/configure` in the TUI, or:

```sh
hx auth          # which routes have a credential, and where from
hx auth set      # paste a new OpenRouter key (hidden input)
hx auth clear    # remove the saved key
```

Resolution order is `HX_OPENROUTER_API_KEY`, then `OPENROUTER_API_KEY`, then
the saved file. The environment wins, and both `/configure` and `hx auth` say
so — otherwise saving a key while a variable is set looks like a no-op.

**ChatGPT Plus/Pro.** Sign in over OAuth and run Codex models against your
subscription instead of paying per token
([OpenAI endorses this for OSS harnesses](https://developers.openai.com/community/codex-for-oss)):

```sh
hx auth login openai-codex      # or /login inside the TUI
hx --model openai-codex/gpt-5.6-terra
hx auth logout openai-codex
```

The browser opens to `auth.openai.com` and redirects to a loopback listener on
port 1455. Over SSH that redirect cannot reach your machine, so paste the final
URL into the prompt instead — or set `HX_LOGIN_DEVICE_CODE=1` for the
device-code flow. Access tokens are refreshed automatically; `/model` only
offers routes you are actually signed in to.

`hx auth` names the account's plan next to the login, because "which account is
this and what is it paying for" is otherwise a JWT to decode by hand:

```sh
hx auth
# openrouter     key sk-or-…8abd     ~/.hx/auth.json
# openai-codex   signed in (plus)    ~/.hx/auth.json
```

Codex is documented as requiring a paid ChatGPT plan, and HX says so when the
login is on a free one. It does not refuse to try: the plan on the credential
has been observed deciding nothing — the backend refuses every Codex model on a
`plus` account with the same error it gives a `free` one — so the switch is
allowed and the backend's own answer is what reports the problem.

**If every Codex model is refused**, the error says so in those terms: it is
the account that is being refused, not the model, and a different model id will
not help. Check that the subscription is active and Codex is enabled for the
account at <https://chatgpt.com/codex>, and that the official Codex CLI can
reach it at all.

**Which Codex models you get** is decided by your ChatGPT account, not by HX.
Signing in fetches the account's own list from the Codex backend, and `/model`
offers exactly that; `/models refresh` asks again. Entitlement is per model —
the same `plus` account can run `gpt-5.6-sol` and be refused `gpt-5.3-codex` —
so a model the picker does not list is one the subscription does not cover.

**How hard they think** comes from the same fetch. Each Codex model publishes
its own reasoning levels and its own default — GPT-6 Astra starts at `low`,
GPT-5.6 Terra at `medium` — and HX asks for the model's default rather than one
fixed depth. `/effort` changes it:

```
/effort            # pick from the levels this model offers
/effort high       # none | minimal | low | medium | high | xhigh | max | ultra
/effort default    # back to the model's own default
```

It takes effect on the next turn, is saved to `~/.hx/settings.json` for later
sessions, and shows on the status bar next to the model — `gpt-5.6-terra (sub)
· high`. Levels differ per model, so a choice is clamped to what the current
one offers: `max` runs at `xhigh` on GPT-5.5 rather than being refused, and the
bar shows what is actually happening. Set it by hand instead with:

```jsonc
// ~/.hx/settings.json
{ "models": { "reasoning_effort": "high" } }
```

When the model list cannot be fetched (no network, an old cache), HX falls back
to the models it last shipped. To add an id the backend does not advertise:

```jsonc
// ~/.hx/settings.json
{ "models": { "codex_models": ["gpt-5.6-terra"] } }
```

They join the `/model` picker on the next start. HX cannot ask how large an
unknown model's context window is, so it assumes a conservative 200k — that
costs an accurate context gauge, not the use of the model.

**Web search.** Optional, and the one credential that is not a model route.
It powers two tools:

| Tool | Does |
|---|---|
| `WebSearch` | search the web, returning ranked results with a snippet each |
| `WebFetch` | read up to 5 URLs, returned as markdown |

[Tavily](https://app.tavily.com) bills per search rather than per token, so it
works identically on both routes — including the ChatGPT subscription one,
where a provider-native search tool is not on offer.

Get a key at <https://app.tavily.com> — the free tier is 1000 searches a month
and takes no card — then save it:

```sh
hx auth set tavily      # paste the key, hidden input, saved to ~/.hx/auth.json
hx auth                 # confirm: `tavily  key tvly-a…f3d9  …/auth.json`
hx auth clear tavily    # remove it again
```

Or keep it in the environment instead, which wins only when nothing is saved:

```sh
export TAVILY_API_KEY=tvly-...     # HX_TAVILY_API_KEY is read first
```

**Without a key, HX works exactly as before** — `WebSearch` and `WebFetch` are
simply not registered, and `hx auth` says so:

```
tavily         not configured      WebSearch and WebFetch are off
```

There is no nag, no failing tool and no startup warning. Leaving them out is
deliberate: an advertised tool that always fails still costs cached-prefix
tokens every turn, and a model told "no key" just calls it again. Adding a key
turns both on from the next `hx`; no other setting changes.

Credits are reported per call and per session at the foot of every result —
`[Tavily: 1 credit, 4 this session]`. A search costs 1 credit, `search_depth:
"advanced"` costs 2, and fetching costs 1 per 5 URLs. Tavily's docs describe a
`usage` field for this, but the live API does not send one, so HX prices every
call from those published rates and marks the figure `~1` rather than passing
an estimate off as measured. Check `app.tavily.com` for the authoritative
number.

Credits are *not* in the status bar's cost, which prices tokens: `/cost`
under-reports a session that searched.

**Behind a TLS-inspecting proxy.** HX verifies certificates against the
operating system's trust store, so a corporate CA the machine already trusts
needs no setup. When the CA lives only in a file, name it:

```sh
export HX_CA_BUNDLE=/path/to/corp-ca.pem   # SSL_CERT_FILE and REQUESTS_CA_BUNDLE also work
```

`HX_SSL_NO_VERIFY=1` turns verification off entirely; it prints a warning at
startup every session, because it is not a fix. A TLS failure is reported in
one line - `/model` and the startup notice both name the certificate problem
rather than reporting an empty catalogue with no cause.

There is no Claude Pro/Max route. Anthropic rejects OAuth tokens unless the
request impersonates Claude Code, which HX will not do. Claude models stay
available through OpenRouter.

---

## Running it

```sh
hx                          # interactive TUI in the current directory
hx -p "explain this repo"   # headless: streams to stdout, tool activity to stderr
hx resume                   # resume the last session here
hx resume <session-id>      # resume a specific one
hx prompt                   # print the system prompt this directory would use
hx --model openai/gpt-5     # override the model for one run
hx --mode plan              # start read-only
hx --cwd ../other-project   # run against a different directory
hx --no-sandbox             # disable OS sandboxing (rules still apply)
hx --system-prompt @p.md    # replace the system prompt for one run
hx --append-system-prompt "Always run the tests"   # add to it; repeatable
hx mcp list|add|remove      # manage MCP servers
hx auth [set|clear]         # manage the OpenRouter key
hx auth login [provider]    # sign in (openrouter, openai-codex)
hx auth logout <provider>   # forget a stored credential
hx docs [section]           # the manual; no argument lists its sections
hx changelog [version]      # what shipped in each version
hx upgrade                  # update to the latest release
```

Print mode is the scriptable one: stdout carries only the assistant's text, so
it pipes cleanly.

### The docs ship with the binary

`README.md` and [`CHANGELOG.md`](CHANGELOG.md) are included in the wheel, so
the installed tool can print the manual and the release record for the version
you are actually running:

```sh
hx docs                     # the section titles
hx docs credentials         # one section, printed whole
hx docs --all               # the entire manual
hx changelog                # every release, newest first
hx changelog 0.1.5          # one release
hx changelog unreleased     # what has landed since the last tag
```

The system prompt points a session at these two commands, so "how do I set the
API key?" or "what changed in the last release?" is answered from the shipped
documentation rather than from the model's recollection of some other version.
The call still goes through the permission engine like any other command;
allowlist it if you would rather not be asked:

```jsonc
{ "permissions": { "allow": ["Bash(hx docs:*)", "Bash(hx changelog:*)"] } }
```

`CHANGELOG.md` lists what each version added, and a release cannot skip it:
`tests/test_docs.py` fails when `__version__` has no section (see
[Releasing](#releasing)).

### Keys

Every key below is defined once, in `src/hx/keys.py`. `/help`, the hints line
under the prompt and this table all read from that registry, and any of them can
be rebound (see [Keybindings](#keybindings)).

| Key | Does |
|---|---|
| `enter` | send |
| `ctrl+j` | newline (also `shift+enter`) |
| `esc` | interrupt the current turn |
| `ctrl+c` | copy the selected text; with nothing selected, clear the prompt (twice on an empty prompt exits) |
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

Drag-select in the transcript and `ctrl+c` copies the selection — assistant
prose and tool output included. With nothing selected, `ctrl+c` keeps its usual
meaning and clears the prompt. See [Selecting and copying
text](#selecting-and-copying-text) for the terminal's own selection.

HX does not emit OSC 133 prompt markers yet, so shell-integration features that
jump between prompts will not see HX's messages.

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
| `/effort [level]` | how hard a reasoning model thinks, from the levels it offers |
| `/configure` | session settings and the API key |
| `/login [provider]` | sign in to a model route |
| `/logout <provider>` | forget a stored credential |
| `/mode [name]` | `plan`, `default`, `acceptEdits`, `bypass` |
| `/permissions` | active rules and what is enforcing them |
| `/context` | what is filling the context window |
| `/cost` | tokens, cache savings, spend |
| `/compact [focus]` | summarise older turns now |
| `/clear` | fresh session, same directory |
| `/resume` | reopen a previous session, listed by name |
| `/rewind` | go back to an earlier prompt, restoring the files HX changed |
| `/title [text]` | show or set this session's name |
| `/prompt` | the system prompt this session is running with |
| `/todos` | toggle the sidebar |
| `/skills` | installed skills |
| `/agents` | subagent types |
| `/mcp` | server status |
| `/theme [name]` | `dark`, `light`, `ansi`, or any theme in `~/.hx/themes` |
| `/queue [steer <n>\|clear]` | messages waiting for the turn to end, and what to do with them |
| `/copy` | copy the last reply to the clipboard |
| `/mouse [on\|off]` | mouse reporting, and with it your terminal's own text selection |
| `/init` | generate an `AGENTS.md` for the project |
| `/help` | list commands and keys |
| `/quit` | exit (also `/exit`, `/q`) |

### Selecting and copying text

Drag to select and `ctrl+c` to copy. HX copies through `pbcopy`, `wl-copy` or
`xclip` first and falls back to OSC 52, so it works in terminals that ignore
OSC 52 - macOS Terminal ignores it outright, and iTerm2 ships with it off. The
notice says which mechanism took the text, so a copy that did nothing says so.

While HX is running, the terminal reports mouse events to it, which means the
terminal's *own* click-and-drag selection is unavailable. Two ways around that:

* Hold `alt`/`option` while dragging. Most terminals - macOS Terminal, iTerm2,
  GNOME Terminal - take that as "this drag is mine", and you get the native
  selection without changing anything.
* `/mouse off` turns mouse reporting off for the session. Native selection comes
  back everywhere, and HX stops seeing scroll and clicks until `/mouse on`.

### Steering a running turn

Type while HX is working and `enter` queues the message: it runs when the turn
ends, which is what you want for "and then do this".

`alt+enter` steers instead. The message goes into the turn that is running now -
the model call is cut off mid-stream and the next one starts from what you just
said. Tools already running are left to finish first, because cancelling a
half-written file is worse than waiting a second for it.

With nothing typed, `alt+enter` steers the message at the front of the queue, so
a correction you already queued does not have to be typed twice. `/queue` lists
what is waiting; `/queue steer 2` sends one of them now.

Set `tui.enterWhileBusy` to `"steer"` to swap the two keys around.

### The status bar

Two lines: working directory and permission mode above; token counts, cache
read/write with hit rate, spend, last-turn latency, context gauge and model
below. The cache and cost fields are the point of it — a hit rate that
collapses after an edit is the visible symptom of a broken prefix.

---

## Configuration

Settings are JSON, merged lowest to highest:

```
defaults  <  ~/.hx/settings.json  <  ./.hx/settings.json  <  ~/.hx/projects/<project>/settings.local.json  <  HX_* env  <  CLI flags
```

Permission rule lists and `prompt.append` are unioned across layers, so a
project can add a deny rule — or a line to the system prompt — without
discarding yours. Everything else is replaced.

`./.hx/settings.json` is the project's shared file, yours to check in, and
nothing writes to it but you — HX only ever reads it. Versions before the local
layer existed did append grants there; those are lifted out into the file below
on the next run, once, and a rule you put back by hand afterwards stays put.
Only `allow` moves: a project's `deny` and `ask` are the guardrails it shares,
and HX never wrote them.

Anything HX decides on your behalf - an "always allow" grant, for one - goes in
`~/.hx/projects/<project>/settings.local.json` instead. Still per project,
because a grant is: allowing `pytest` in one repo should not allow it
everywhere. But under your home rather than in the checkout, so HX never leaves
a file in your repository for you to find in a diff. If an older version
already wrote `./.hx/settings.local.json`, it is moved there on the next run,
along with the `.hx/.gitignore` it added to hide it.

```jsonc
{
  "theme": "dark",                    // dark | light | ansi, or a file in ~/.hx/themes
  "quiet_startup": false,             // skip the startup header
  "telemetry": false,

  "models": {
    "model": "anthropic/claude-sonnet-4.5",
    "subagent_model": null,           // defaults to "model"
    "title_model": null,              // model that names sessions; defaults to "model"
    "max_tokens": 8192,
    "temperature": null,
    "reasoning_effort": null,         // Codex route: null = the model's own default
    "codex_models": []                // extra Codex ids, e.g. ["gpt-5.6-terra"]
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
    "tool_output_line_cap": 2000,
    "git_notices": true               // late-inject the branch and outside file changes
  },

  "bash": {
    "timeout_seconds": 120,
    "max_timeout_seconds": 600,       // ceiling; caps what the model may ask for
    "shell": null                     // defaults to $SHELL
  },

  "prompt": {
    "system": null,                   // replaces the built-in system prompt
    "append": []                      // added after it; unioned across layers
  },

  "tui": {
    "enterWhileBusy": "queue"         // queue | steer - what enter does mid-turn
  }
}
```

Environment overrides: `HX_MODEL`, `HX_SUBAGENT_MODEL`, `HX_MAX_TOKENS`,
`HX_PERMISSION_MODE`, `HX_SANDBOX`, `HX_COMPACT_AT`, `HX_GIT_NOTICES`,
`HX_THEME`, `HX_QUIET_STARTUP`. Also `HX_HOME` to relocate user state, `HX_TAVILY_API_KEY`
for web search, and `HX_CA_BUNDLE` / `HX_SSL_NO_VERIFY` for TLS (see
[Credentials](#credentials)).

### The system prompt

`hx prompt` prints the prompt this directory resolves to, with its source on
stderr so stdout stays pipeable. `/prompt` shows the same thing inside a running
session.

The built-in prompt is `SYSTEM_PROMPT` in `src/hx/core/context.py`. It is
replaced by the first of these that exists:

| Source | Scope |
|---|---|
| `--system-prompt TEXT` or `@path`, or `prompt.system` | one run |
| `./.hx/system-prompt.md` | this project |
| `~/.hx/system-prompt.md` | you, everywhere |
| the built-in prompt | fallback |

Appended text is added after whichever prompt won, in this order:
`~/.hx/system-prompt-append.md`, `./.hx/system-prompt-append.md`, then each
`--append-system-prompt` value (and `prompt.append`) in the order given. Nothing
appended is ever discarded by a later layer.

This is a different lever from `AGENTS.md`: `AGENTS.md` describes *the project* and is
injected as its own context section, while these files change *the agent's
instructions*. Both are read once at startup and frozen, because they sit above
the first cache breakpoint — an override edited mid-session applies on the next
run, and `/prompt` says so when it spots one.

### Sessions are named

After the first exchange, HX asks the model for a short name for the session and
writes it to `~/.hx/sessions/<id>/meta.json`, so `/resume` lists work rather
than timestamps. It is one small call — cap 32 output tokens, `models.title_model`
if you want a cheaper model for it — and it is counted in `/cost` like any other.
If the call fails the session is still named, from your first message. `/title
<text>` renames it.

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
| `~/.hx/auth.json` | API keys and logins, mode 0600 |
| `~/.hx/models.json` | cached model catalogue, refreshed daily |
| `~/.hx/themes/` | your themes, one JSON file each |
| `~/.hx/keybindings.json` | your key overrides |
| `~/.hx/system-prompt.md` | your system prompt, replacing the built-in one |
| `~/.hx/system-prompt-append.md` | text appended to whichever prompt is in force |
| `~/.hx/sessions/` | transcripts, spilled tool output, subagent sessions |
| `~/.hx/skills/`, `~/.hx/agents/` | your skills and agents |
| `~/.hx/projects/<project>/settings.local.json` | this machine's settings for one project - where "always allow" lands |
| `./.hx/settings.json` | project settings, checked in if you like — read by HX, never written |
| `./.hx/mcp.json` | project MCP servers |
| `./.hx/system-prompt.md`, `./.hx/system-prompt-append.md` | project prompt overrides |
| `./.hx/skills/`, `./.hx/agents/` | project skills and agents |
| `./AGENTS.md` | project instructions, loaded into every session |

`AGENTS.md` is the place for things a newcomer would get wrong: how to run the
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

**Late injection** carries everything that changes per turn - the todo list,
files that changed on disk since HX read them, the git branch and any file the
working tree gained or lost outside the session - on the tail of the newest
user message rather than in the prefix. Stale copies are stripped and
regenerated each turn, so six todo updates leave one copy in context, not six.

**Working-tree notices** come from one `git status --porcelain=v2 --branch -z`
per provider call, run without a shell and bounded by a two-second timeout. It
runs in a worker thread, as every injector does: a turn makes one of these per
tool round-trip, and on the event loop a slow repository would freeze the UI
each time. A file HX has already read is left to the stale-file notice, so no
path is reported twice; outside a repository the watcher disables itself for
the session. Turn it off with `"context": {"git_notices": false}`.

**Rewind** (`/rewind`) picks an earlier prompt and takes the session back to
it: the transcript is cut there and every file HX wrote after it is restored
from a content-addressed snapshot taken before each Write and Edit. A file
someone else changed in the meantime is reported and left alone, and shell
commands are outside the scheme - the report says so rather than implying the
tree was fully returned. The prompt that was cut comes back in the input box,
because a rewind is usually the first half of saying it differently. Nothing is
deleted from the session file: the rewind is one more append, and replaying the
file reproduces the state - which is also how an undone compaction gives its
messages back. The cost ledger is deliberately not rewound; those tokens were
spent.

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

Tests marked `live` hit a real API. The OpenRouter ones cost money:

```sh
OPENROUTER_API_KEY=... uv run pytest -m live tests/test_live.py

# the Codex route draws on your ChatGPT subscription instead
hx auth login openai-codex
uv run pytest -m live tests/test_live_codex.py
```

They are the only place the wire formats, streaming, tool use and a genuine
cache hit are proven against a real service; everything else runs against the
scripted provider.

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
  cli.py config.py paths.py frontmatter.py git.py
  core/         loop, context assembly, compaction, late injection, sessions, usage
  auth/         credential store, OAuth flows, per-route resolution
  providers/    OpenRouter, Codex, the model catalogue, a scripted provider for tests
  tools/        Bash, Read, Write, Edit, Glob, Grep, TodoWrite, Task, WebSearch,
                WebFetch, output capping
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
# 1. move CHANGELOG.md's [Unreleased] entries under the new version, dated,
#    and leave a fresh empty [Unreleased] behind
# 2. bump __version__ in src/hx/__init__.py
# 3. uv run pytest tests/test_docs.py     # the version must have a section
git commit -am "release: X.Y.Z"
git tag v$(uv run hx --version | cut -d' ' -f2) && git push origin main --tags
```

Step 1 is not optional and is not a convention: `tests/test_docs.py` fails the
whole suite when `__version__` has no `CHANGELOG.md` section, so a release that
skips it cannot get past CI to the `publish` job. The file ships inside the
wheel and `hx changelog` prints it, so a version with no section there reaches
users as a blank release.

The `publish` job runs only on `refs/tags/v*` and only after lint, the test
suite and an install-script run in a clean Debian container have passed. It builds with
`uv build` and uploads via OIDC.

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

Two routes to a model: an OpenRouter API key, or a ChatGPT Plus/Pro
subscription over OAuth. The model id decides which.

Published to PyPI as [`hx-cli`](https://pypi.org/project/hx-cli/), released
from CI on a tag.

The one thing still unproven in CI is a live call on either route: the `live`
tests exist and cover both wire formats, tool use, reasoning replay and a
genuine cache hit, but they need a credential and are deselected by default.

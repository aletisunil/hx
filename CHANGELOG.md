# Changelog

What shipped in each released version of HX, newest first.

This file is the release record and part of the product: it ships inside the
wheel, `hx changelog` prints it, and a session can answer "what's new?" or
"which version added web search?" from it rather than from a guess. `hx docs`
prints the manual next to it.

The rules, enforced by `tests/test_docs.py` so a release cannot forget them:

- Every released version has a `## [x.y.z] - YYYY-MM-DD` section here, and the
  version in `src/hx/__init__.py` always has one. Bumping the version without
  writing the section fails the suite before the tag exists.
- Work that has landed but is not tagged lives under `## [Unreleased]`. At
  release time that heading is renamed to the new version and dated, and a
  fresh empty `## [Unreleased]` takes its place.
- Entries say what a user can now do, in one line each, naming the command,
  setting or key that reaches the feature. A changelog nobody can act on is a
  git log with extra steps.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project follows [semantic versioning](https://semver.org/).

---

## [Unreleased]

---

## [0.1.9] - 2026-09-11

### Changed

- Project instructions live in `AGENTS.md`, the cross-agent standard, instead
  of `HX.md`. A repo already carrying an `AGENTS.md` for another coding agent
  is understood by HX with no extra file; `/init` writes one. `HX.md` and
  `.hx/HX.md` are no longer read - rename yours.
- Python 3.13 and 3.14 are supported in fact, not just by the `>=3.11` floor.
  CI runs the suite on every supported minor, so installing on a current
  interpreter is covered by a test run rather than by hope.

### Fixed

- `/model` lists the Codex models your ChatGPT account can actually run. HX
  shipped a hard-coded pair — `gpt-5.3-codex` and its spark — that the backend
  now refuses outright, so a paid subscription offered two models and both
  failed. Signing in fetches the account's own list instead; `/models refresh`
  asks again, and the last shipped list stands in when the fetch cannot happen.
- A Codex model that the account is refused now says so in terms you can act
  on. The backend answers with a 400 naming the model, which reads as a bad
  model id. The message now says entitlement is per model and per account, and
  points at `/models refresh` to get the list that is true for your account.
- `hx auth` names the plan behind a ChatGPT login (`signed in (plus)`), and
  `/login` says when it is a free one, since Codex is documented as needing a
  paid plan. Neither blocks a model: entitlement is per model, and the fetched
  catalogue is what decides it, so HX reports the plan and lets the backend
  answer.
- `/model` says what pays for each model. Subscription models showed
  `$0.00/$0.00`, indistinguishable from a free OpenRouter one; they now read
  `subscription`. The status bar tags them too — `gpt-5.6-terra (sub)`.
- `/resume` counts prompts, not wire-format records. A session you sent one
  prompt to listed as "31 msgs"; it now reads `1 prompt · 31 msgs`, and `/cost`
  says "17 API requests across 1 prompt" so the two numbers reconcile.
- The status bar follows the branch. It read `git HEAD` once at startup, so
  checking out in an IDE or another terminal left it showing a branch you had
  left; it now re-reads on a timer, worktrees included.
- Selecting assistant prose or a tool block with the mouse copies it. Textual
  can only extract text from widgets that render plain text, so dragging over
  those blocks highlighted them and copied nothing.
- Copying goes through `pbcopy`/`wl-copy`/`xclip` before OSC 52. Textual's own
  copy is OSC 52 only, which macOS Terminal ignores and iTerm2 ships disabled,
  so `ctrl+c` on a selection silently did nothing there.
- The prompt names the steer key the way your keyboard does. It read
  `alt+enter` everywhere, while `/help` two keystrokes away read `option+enter`
  on macOS - the same key, spelled two ways in one window. It now comes from
  the keybinding registry, so it follows the platform and follows a rebind.
- `ctrl+c` reaches the copy at all. The prompt holds focus for the whole session
  and binds that key itself, so a transcript selection was never what it
  copied. It now copies the selection when there is one, and clears the prompt
  when there is not, exactly as before.

### Added

- `models.codex_models` in settings adds Codex model ids to the `/model`
  picker, on top of the ones fetched for your account — for an id the backend
  serves but does not advertise, or a machine that cannot reach it.
  `{"models": {"codex_models": ["gpt-5.6-terra"]}}` adds your own.
- `/effort` picks how hard the current model thinks, from the levels that model
  actually offers, and the status bar shows the depth in force next to the
  model — `gpt-5.6-terra (sub) · high`. It applies to the next turn and is
  saved for later sessions. Switching models re-resolves it, so a choice the
  new model cannot reach shows as the depth it will really run at.
- `models.reasoning_effort` sets how hard a reasoning model thinks —
  `none` through `ultra`. Left unset, each Codex model runs at the default its
  own catalogue entry names (`low` on GPT-6 Astra, `medium` on GPT-5.6 Terra)
  instead of one fixed depth for every model. A value is clamped to what the
  chosen model advertises, so `max` runs at `xhigh` on GPT-5.5 rather than
  being refused.
- `/mouse [on|off]` hands drag-selection back to your terminal, for when you
  want its own selection rather than HX's. Holding alt/option while dragging
  does the same thing without the toggle.

### Changed

- "Always allow" grants move out of your repository. They were written to
  `./.hx/settings.local.json`, with a `./.hx/.gitignore` added to hide them —
  two files of HX's in your checkout. They now live in
  `~/.hx/projects/<project>/settings.local.json`, still scoped to the project,
  and an existing local file is migrated on the next run.
- Grants that much older versions appended to `./.hx/settings.json` — the file
  a project checks in — are lifted into that same local file on the next run,
  and HX says which ones it moved. Only `allow` moves; a project's `deny` and
  `ask` stay shared. It happens once, so a rule you later write there by hand
  is left alone, and an emptied `./.hx` is removed.
- HX no longer rewrites `./.hx/settings.json` at all. Its one remaining writer
  there, the startup pass that widens stale whole-command rules, now reports
  them and leaves the file to its owner.

---

## [0.1.8] - 2026-09-10

### Fixed

- Publishes everything listed under 0.1.7. That tag was cut but never shipped:
  a type annotation on the transcript's pending-approval list failed the lint
  job, so the release build stopped before it reached PyPI. `hx upgrade` moves
  from 0.1.6 straight to here.

---

## [0.1.7] - 2026-09-10

### Added

- **Steer a running turn.** `alt+enter` pushes a message into the turn already
  in flight: the model call is cut off mid-stream and the next one starts from
  what you just said. Tools already running are left to finish, so nothing is
  half-written. With an empty prompt it steers the front of the queue, so a
  correction you already queued need not be retyped. `/queue` lists what is
  waiting, `/queue steer <n>` sends one now, `/queue clear` drops them, and the
  status bar shows the depth. Set `tui.enterWhileBusy` to `"steer"` to swap the
  two keys.
- **Sessions are renamed as they close.** The name written after the first
  exchange described an opening question; `/resume` now lists what the session
  turned into. Also on `/clear` and `/resume`, which leave a session behind.

### Changed

- **Approvals happen in the transcript, not in a modal.** The permission prompt
  is now a block at the end of the conversation, directly under the sentence
  where the model said what it intended to do. It takes the keyboard while it is
  unanswered (`y` / `s` / `a` / `n`, `v` for the rest of a clipped diff), and
  answering collapses it into a one-line record of what was granted and how
  widely, so a session shows every rule it accumulated.
- **More commands are recognised as read-only, and fewer are taken on trust.**
  The allowlist that skips the prompt entirely grew from roughly thirty entries
  to a hundred - digests, `ps`, `readlink`, most text filters, and the read-only
  half of `git` (`rev-parse`, `ls-files`, `cat-file`, `stash list`, and the
  rest). Arguments now decide alongside the name: `sed -i`, `find -exec`,
  `sort -o`, `yq -i`, `git branch -D`, an `awk` program that calls `system()`,
  and `git -c core.pager=…` all still ask. Review it if you run HX on anything
  you would not hand a shell.
- **"Always allow" writes to `.hx/settings.local.json`.** Grants no longer land
  in `.hx/settings.json`, the file a project checks in - one machine's absolute
  paths were being committed to everyone who cloned the repo. The new layer sits
  above project settings, and HX adds a `.hx/.gitignore` covering it. Existing
  rules in either file keep working.

### Fixed

- **`/login` no longer strands you in a dead modal.** The sign-in screen was
  driven before it had finished mounting, so the flow died on its first call and
  left a prompt that took keystrokes and ignored Enter. Opening the browser and
  tearing down the loopback listener also ran on the event loop, freezing the
  TUI - including the Escape that would have cancelled.
- Text alongside tool results reaches the model on OpenRouter-shaped routes
  instead of being silently dropped.

---

## [0.1.6] - 2026-09-10

### Added

- **Web search, over Tavily.** `WebSearch` returns ranked results with a
  snippet each; `WebFetch` reads up to 5 URLs as markdown. Both are registered
  only when a key resolves, so HX without one is unchanged and pays no
  cached-prefix cost for a tool that would always fail. Set the key with
  `hx auth set tavily`, or `TAVILY_API_KEY` / `HX_TAVILY_API_KEY` in the
  environment. Credits spent are reported per call and per session.
- **Corporate TLS support.** Certificates verify against the operating
  system's trust store, `HX_CA_BUNDLE` (also `SSL_CERT_FILE`,
  `REQUESTS_CA_BUNDLE`) names a CA file, and `HX_SSL_NO_VERIFY=1` turns
  verification off with a warning every session. A TLS failure is now named in
  one line at startup and in `/model` instead of showing up as an empty model
  catalogue with no cause.
- **Working-tree notices.** The git branch and files that changed on disk
  outside the session are late-injected onto the newest user message, from one
  `git status --porcelain=v2 --branch -z` per provider call, run in a worker
  thread so it never blocks the UI. Off with
  `"context": {"git_notices": false}` or `HX_GIT_NOTICES=0`.
- **File checkpoints.** Write and Edit park a file's previous bytes in a
  content-addressed store under the session directory, keyed to the position in
  the transcript, so a rewind can undo edits and not just words. A file whose
  bytes moved since HX wrote them is reported and left alone rather than
  overwritten.
- **`hx docs` and `hx changelog`.** The manual and this file, printed from the
  installed package. `hx docs` with no argument lists the sections;
  `hx docs credentials` prints one. `hx changelog 0.1.5` prints one release.
  The system prompt points at both, so questions about HX itself are answered
  from the shipped documentation.

### Changed

- The model catalogue keeps the last good copy when a refresh fails, and
  records why it failed so `/model` can say so. The static routes (the Codex
  models) are registered even when there is no cache file and no network.

---

## [0.1.5] - 2026-09-09

### Added

- **A ChatGPT Plus/Pro route.** Sign in with `hx auth login openai-codex` (or
  `/login` in the TUI) and run `openai-codex/gpt-5.3-codex` against your
  subscription instead of paying per token. OAuth with PKCE against
  `auth.openai.com`, a loopback listener on port 1455, a paste-the-URL fallback
  for SSH and `HX_LOGIN_DEVICE_CODE=1` for the device-code flow; access tokens
  refresh themselves.
- Credentials became one store per route in `~/.hx/auth.json` (mode 0600).
  `hx auth` reports which routes have a credential and where it came from, and
  `hx auth logout <provider>` forgets one.
- `/model` offers only routes you are actually signed in to, so the picker
  cannot select a model the session has no way to reach.

The route serving a turn is decided by the model id alone: `openai-codex/*`
goes to the subscription, everything else to OpenRouter.

---

## [0.1.4] - 2026-09-09

### Fixed

- TUI snapshot tests no longer embed the release version, so bumping the
  version stopped failing the suite.

---

## [0.1.3] - 2026-09-09

### Fixed

- An exact slash command runs on the first `enter` instead of only selecting
  itself in the completion list.

---

## [0.1.2] - 2026-09-09

### Added

- **Themes as data.** A theme is a JSON file - a `vars` block of colours and a
  `colors` block mapping semantic roles onto them. Drop one in
  `~/.hx/themes/mine.json` and `/theme mine` picks it up, no restart. Syntax
  highlighting is theme-driven, and pi's theme files load unchanged.
- **One keybinding registry.** Every key is defined once in `src/hx/keys.py`;
  `/help`, the hints line and the README table all read from it, and any action
  id can be rebound in `~/.hx/keybindings.json`. Conflicts and unknown ids are
  reported at startup rather than silently resolved.
- **A completion popup** above the prompt for `/` commands and `@` paths, with
  fuzzy matching; `tab` cycles, `enter` accepts, `esc` dismisses.
- **A readline-style prompt** with a kill ring (`ctrl+w`, `ctrl+u`, `ctrl+k`,
  `ctrl+y`, `alt+y`), undo and redo.
- **System clipboard support.** `ctrl+x` copies the selected message and
  `/copy` the last reply, through `pbcopy`/`wl-copy`/`xclip`/`xsel` with an
  OSC 52 fallback that also works over SSH.
- The prompt is framed and the working status lives in its border.
- `/exit` as an alias for `/quit`.

### Fixed

- Permission grants stay inside what the user actually approved.
- A `/model` choice persists to user settings.
- Slash commands run in a worker, so a slow one no longer blocks the UI.

---

## [0.1.0] - 2026-09-08

First release, published to PyPI as [`hx-cli`](https://pypi.org/project/hx-cli/).

### Added

- **The agent loop**, streaming from OpenRouter with prefix caching, accurate
  per-turn cost accounting and a stable request prefix.
- **The tool suite**: Bash (with a background job runner), Read, Write, Edit,
  Glob, Grep, TodoWrite and Task, with output capping that keeps the head and
  tail of a large result and spills the rest to the session directory.
- **The permission engine and OS sandbox.** `Tool(specifier)` rules where deny
  beats ask beats allow, shell commands decomposed into their real segments
  first, and Seatbelt on macOS / bubblewrap on Linux confining writes and
  network. Modes cycle with `shift+tab`: `plan`, `default`, `acceptEdits`,
  `bypass`.
- **Context engineering**: late injection for per-turn state, compaction at 80%
  of the window (or `/compact [focus]`), and cache breakpoints that only
  advance once enough tokens have accumulated.
- **Sessions**: persisted transcripts, `hx resume` / `/resume`, and a model-named
  session title you can change with `/title`.
- **Skills, subagents and MCP.** Skills are `SKILL.md` directories under
  `.hx/skills/` or `~/.hx/skills/`, loaded on demand so a hundred installed
  skills cost a hundred lines. Subagents (`explore`, `plan`, `general`, plus
  your own in `.hx/agents/`) run in their own context and return one report.
  MCP servers go in `.hx/mcp.json` or `hx mcp add`.
- **The TUI**: transcript, status bar with token, cache and cost counters, todo
  sidebar, model picker, command palette and per-tool renderers.
- **Print mode** (`hx -p`), where stdout carries only the assistant's text, so
  it pipes.
- **Configuration** merged from defaults, `~/.hx/settings.json`,
  `./.hx/settings.json`, `HX_*` environment variables and CLI flags, with
  permission lists and prompt appends unioned rather than replaced.
- `/configure` and `hx auth` for the OpenRouter key, `hx upgrade` for
  self-update, and `install.sh` bootstrapping uv with a pinned Python.

[Unreleased]: https://github.com/aletisunil/hx/compare/v0.1.8...HEAD
[0.1.8]: https://github.com/aletisunil/hx/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/aletisunil/hx/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/aletisunil/hx/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/aletisunil/hx/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/aletisunil/hx/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/aletisunil/hx/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/aletisunil/hx/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/aletisunil/hx/releases/tag/v0.1.0

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

[Unreleased]: https://github.com/aletisunil/hx/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/aletisunil/hx/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/aletisunil/hx/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/aletisunil/hx/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/aletisunil/hx/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/aletisunil/hx/releases/tag/v0.1.0

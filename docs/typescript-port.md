# The TypeScript port

HX is being rewritten from Python to TypeScript, one module at a time, with no
new features. The Python build stays the reference until the port is complete.

## How parity is checked

The end-to-end suite in `tests/e2e` drives `hx` only from the outside: a pty, a
stub model server, files and exit codes. It never imports HX, so it tests
either build:

```sh
uv run pytest tests/e2e                    # the Python build
npm run e2e                                # the TypeScript build, ported tests only
```

`tests/e2e/typescript.txt` lists the tests the TypeScript build must pass. Each
phase adds its tests to the list; CI runs it in the `typescript` job.

Run the whole suite against both builds and compare the reports to catch
differences the assertions do not look at:

```sh
uv run pytest tests/e2e
HX_E2E_BIN=dist/hx.js HX_E2E_REPORT=e2e-report-ts uv run pytest tests/e2e
diff -r e2e-report e2e-report-ts
```

## Layout

The TypeScript sources sit in `src/` beside the Python package in `src/hx/`,
one module for each Python module. When the port is complete, `src/hx/` is
deleted and nothing else moves.

`src/py.ts`, `src/pathlib.ts` and `src/json.ts` reproduce the Python behaviour
that HX's output and files depend on: string whitespace, path printing,
`json.dumps` spacing and escapes. Use them in place of the JavaScript built-ins
wherever the result reaches a user or a file.

## Phases

| Phase | Scope | E2E gate | State |
|---|---|---|---|
| 0 | Harness, scaffold, `--version`, `--help`, `docs`, `changelog`, `prompt` | 7 of `test_cli` | done |
| 1 | Config, auth and logins, TLS, OpenRouter, models, sessions, the agent loop, tools, permissions, sandbox, hooks, skills, subagents, web search, `-p` | every test that does not open the TUI, bar MCP and trace | done |
| 2 | Terminal renderer, keys, editor, markdown, TUI views, slash commands | `test_chat`, `test_input`, `test_queue`, `test_commands`, the TUI tests in `test_tools`, `test_edits`, `test_reads`, `test_shell`, `test_extend` | done |
| 3 | MCP, `hx mcp`, `hx trace` and `/trace`, the Symbols tool | the rest of `test_extend` and `test_cli`, `test_trace_writes_a_self_contained_page` | done |
| 4 | Codex, Claude Code and Devin providers; live tests | `test_claude_code`, `test_plan_usage`, `test_effort_carries_into_every_new_session` | done |
| 5 | Delete the Python build; npm release, `install.sh`, `hx upgrade` | the whole suite | npm beta out; Python still in the tree |

## Publishing the beta

The TypeScript build is published to npm as `@sunilaleti/hx` from the
`ts-rewrite` branch, on the `next` tag, while it is tested. The Python build
stays in the tree until the branch is merged; the merge deletes it.

```sh
npm login                       # once, as sunilaleti
npm run e2e                     # the whole suite, on the bundle npm will ship
npm publish --tag next          # runs check and build first (prepublishOnly)
```

A pre-release version must go out with `--tag`, so a beta never becomes what a
plain `npm install --global @sunilaleti/hx` gets. For the next beta, add a
`## [x.y.z-beta.n]` section to CHANGELOG.md, bump `version` in package.json,
and publish again.

## Known differences

Behaviour that is deliberately not reproduced. Most of it only a broken input
reaches:

- A settings file that is not valid JSON is reported with JavaScript's parser
  message, not Python's.
- A settings section that is not an object (`"models": "x"`) is ignored. Python
  stopped with a traceback.
- `~name/...` paths for another user are not expanded.
- An unexpected error prints a Node stack trace where Python printed a
  traceback.
- A frontmatter file whose YAML does not parse is reported with the `yaml`
  package's message, not PyYAML's.
- An image that has to be re-encoded - too large, rotated, or not PNG, JPEG,
  GIF or WebP - is re-encoded by sharp, so its bytes differ from Pillow's. An
  image within the limits is still sent byte for byte.
- Grep's built-in search, used when `rg` is not on `PATH`, runs JavaScript
  regular expressions. Python's `(?P<name>)`, `(?P=name)`, `\A` and `\Z` are
  translated; anything else outside the common subset is reported as invalid.

- `@/abs…` in the prompt completes nothing. Python's `pathlib` raised on an
  absolute pattern there.
- `/resume` into another directory starts the new HX as a child process that
  takes over the terminal. Python replaced its own process with `exec`, which
  Node cannot do; the exit status is passed through.

- The Symbols tool reads its grammars from `@vscode/tree-sitter-wasm`, where
  Python used the `tree-sitter-language-pack` wheels. It is optional in both
  builds and off until installed: `npm install -g @vscode/tree-sitter-wasm`
  beside HX. Its answers match Python's on Python, TypeScript, TSX,
  JavaScript, Go and Rust.
- Times in session files and traces are recorded to the millisecond, where
  Python recorded them to the microsecond.
- When several MCP servers fail at once, their warnings can reach stderr in
  a different order.

- HTTP requests carry Node's defaults where Python carried httpx's: no
  `User-Agent` of httpx's own, and no `Accept-Encoding: gzip, deflate`, so
  servers answer uncompressed. The Devin route's Connect frames are gzipped by
  Node's zlib, so their bytes differ while what they decompress to does not.

No end-to-end test reaches the Devin route, so it was checked against the
Python build directly: both builds talked to one scripted Cascade server, and
every request body, the parsed model catalogue, every streamed item and every
error came out the same.

Bugs in the Python build that are kept, so the two builds stay comparable
until the cutover, and then fixed:

- `/mouse` toggles mouse reporting but then says the terminal driver has none.
- `ctrl+t` and `/todos` say "No plan yet." even when the model wrote one: they
  read a plan the session never holds.
- Startup notices (a moved permission grant, a TLS override, a failed
  catalogue refresh) are collected but never shown in the TUI.

Behaviour that was a bug in the Python build and is not carried over:

- The OpenRouter route retried a stream that failed after text had already
  arrived, and the text then arrived twice. It now retries only before the
  first token, as its own comment said it did.

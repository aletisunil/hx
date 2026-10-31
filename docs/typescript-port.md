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
| 2 | Terminal renderer, keys, editor, markdown, TUI views, slash commands | `test_chat`, `test_input`, `test_queue`, `test_commands`, `test_plan_usage`, the TUI tests in `test_tools`, `test_edits`, `test_reads`, `test_shell` | |
| 3 | MCP, `hx mcp`, `hx trace`, the Symbols tool | `test_extend`, the rest of `test_cli` | |
| 4 | Codex, Claude Code and Devin providers; live tests | `test_claude_code` | |
| 5 | Delete the Python build; npm release, `install.sh`, `hx upgrade` | the whole suite | |

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

Behaviour that was a bug in the Python build and is not carried over:

- The OpenRouter route retried a stream that failed after text had already
  arrived, and the text then arrived twice. It now retries only before the
  first token, as its own comment said it did.

"""Session traces: one self-contained HTML page holding everything that happened.

A trace is the whole session made readable - the system prompt, the tool
schemas, every prompt, every reply, every thinking block, every tool call with
the result it produced, the per-turn token ledger and the files HX changed.

Two rules shape the module:

Nothing is summarised. A trace exists to answer "what actually went through",
so the messages are serialised with :func:`hx.core.messages.to_dict` - the same
function that writes the session JSONL - rather than a second, prettier
serialiser that could disagree with the record. Ephemeral and compacted
messages are included and labelled, because a trace that hides the reminders
and the superseded turns is not a trace of the session that ran.

Nothing is fetched. The page carries its payload inline and its stylesheet and
script with it, so it opens from a file:// URL on a machine with no network and
renders identically a year later. That also keeps a trace of a private
repository private: writing one sends nothing anywhere.
"""

from __future__ import annotations

import html
import json
import re
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hx import __version__
from hx.core.messages import to_dict
from hx.core.session import Session

TRACE_FILENAME = "trace.html"
"""Default name for a trace written into its own session directory."""


def build_trace(
    session: Session,
    *,
    system_prompt: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    project_context: str | None = None,
    skills_index: str | None = None,
    live: bool = False,
) -> dict[str, Any]:
    """Assemble the JSON payload for a trace.

    The prompt and the tool schemas come from the session's recorded
    :class:`~hx.core.session.Environment` unless they are passed in. Passing
    them is for a live session traced before its first provider call, when
    nothing has been recorded yet but the caller is holding the loop and knows
    the answer anyway.

    Args:
        session: The session to trace. An in-memory one is traced as it stands,
            which is what makes a trace of a running session current rather than
            current as of the last flush.
        system_prompt: Overrides the recorded prompt.
        tools: Overrides the recorded tool schemas.
        project_context: Overrides the recorded project context.
        skills_index: Overrides the recorded skills index.
        live: Whether the session is still running. Shown on the page, because a
            trace of a live session is a snapshot and saying so costs nothing.

    Returns:
        A JSON-serialisable dict. It is also the machine-readable form of the
        trace: the page embeds it verbatim, so anything the page can show can be
        pulled back out of the file with a JSON parser.
    """
    recorded = session.environment
    return {
        "hx_version": __version__,
        "generated_at": time.time(),
        "live": live,
        "session": asdict(session.meta),
        "system_prompt": system_prompt
        if system_prompt is not None
        else (recorded.system_prompt if recorded else None),
        "project_context": project_context
        if project_context is not None
        else (recorded.project_context if recorded else None),
        "skills_index": skills_index
        if skills_index is not None
        else (recorded.skills_index if recorded else None),
        "tools": list(tools if tools is not None else (recorded.tools if recorded else [])),
        "messages": [
            {"index": index, **to_dict(message)} for index, message in enumerate(session.messages)
        ],
        "usage": {
            "turns": [asdict(turn) for turn in session.usage.turns],
            "context_tokens": session.usage.context_tokens,
            "context_window": session.usage.context_window,
        },
        "checkpoints": [checkpoint.as_dict() for checkpoint in session.checkpoints],
    }


_PLACEHOLDER = re.compile("__HX_TRACE_(?:TITLE|DATA)__")


def render_html(trace: dict[str, Any]) -> str:
    """Render a trace payload as one self-contained HTML document.

    One pass, because chained :meth:`str.replace` calls let the first
    substitution's text be rescanned by the second: a session titled
    ``__HX_TRACE_DATA__`` would have the whole payload spliced into its
    ``<title>``. Substituting through a function also keeps the replacement
    literal, so no backslash in the payload is read as a group reference.
    """
    meta = trace.get("session") or {}
    title = meta.get("title") or meta.get("session_id") or "session"
    filled = {
        "__HX_TRACE_TITLE__": html.escape(f"hx trace - {title}"),
        "__HX_TRACE_DATA__": _embed(trace),
    }
    return _PLACEHOLDER.sub(lambda match: filled[match.group(0)], _TEMPLATE)


def write_trace(trace: dict[str, Any], path: Path) -> Path:
    """Render and write a trace. Returns the path written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(trace), encoding="utf-8")
    return path


def default_trace_path(session: Session) -> Path:
    """Where a trace lands when the user names no path.

    Inside the session's own directory, next to the transcript it describes.
    Not the working directory: a trace holds the whole conversation, and a tool
    that drops that into somebody's checkout for `git status` to find has made
    its output the user's problem.
    """
    from hx.paths import session_dir

    return session_dir(session.meta.session_id) / TRACE_FILENAME


def open_in_browser(path: Path) -> bool:
    """Best-effort open in a *graphical* browser. False when there is none.

    Never raises: failing to open a file that was written successfully is a
    notice, not an error, and over SSH it is the normal case.

    Only a graphical browser, because ``webbrowser`` falls back to a terminal
    one - ``lynx``, ``w3m``, ``links`` - when nothing else is registered. That
    launches into the terminal HX is drawing on, and the user loses the session
    display to a text browser they did not ask for. On a headless box the right
    answer is the path, which the caller prints either way.
    """
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return False
    if isinstance(browser, webbrowser.GenericBrowser) or _is_terminal_browser(browser):
        return False
    try:
        return bool(browser.open(path.resolve().as_uri()))
    except Exception:
        return False


_TERMINAL_BROWSERS = frozenset({"www-browser", "links", "elinks", "lynx", "w3m"})


def _is_terminal_browser(browser: object) -> bool:
    """Whether ``webbrowser`` handed back one that draws in the terminal."""
    name = getattr(browser, "name", "") or getattr(browser, "basename", "")
    return Path(str(name)).name in _TERMINAL_BROWSERS


def _embed(trace: dict[str, Any]) -> str:
    """Serialise the payload for embedding inside a ``<script>`` element.

    The HTML parser reads script content as raw text until it sees ``</script``,
    so a tool result containing that string would end the element early and
    spill the rest of the session into the document as markup. Escaping every
    ``<`` as ``\\u003c`` makes that unrepresentable rather than unlikely; ``>``
    and ``&`` go with it so no comment or entity can start either. U+2028 and
    U+2029 are escaped because they terminate a line in JavaScript but not in
    JSON, which a strict JSON parser in the page would otherwise reject.
    """
    return (
        json.dumps(trace, ensure_ascii=False, default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__HX_TRACE_TITLE__</title>
<style>
:root {
  color-scheme: light;
  --bg: #fbfbfd;
  --panel: #ffffff;
  --panel-alt: #f4f4f8;
  --border: #e0e0e8;
  --text: #1c1c22;
  --muted: #6a6a78;
  --accent: #3b5bdb;
  --user: #2f6f4f;
  --assistant: #3b5bdb;
  --thinking: #8452c4;
  --tool: #a86400;
  --error: #c03030;
  --shadow: 0 1px 2px rgba(0, 0, 0, .06);
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #16161a;
  --panel: #1d1d22;
  --panel-alt: #25252c;
  --border: #33333d;
  --text: #e4e4ea;
  --muted: #9a9aa8;
  --accent: #8aa4ff;
  --user: #7fcfa4;
  --assistant: #8aa4ff;
  --thinking: #c4a2f0;
  --tool: #e0b060;
  --error: #ff8f8f;
  --shadow: none;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #16161a;
    --panel: #1d1d22;
    --panel-alt: #25252c;
    --border: #33333d;
    --text: #e4e4ea;
    --muted: #9a9aa8;
    --accent: #8aa4ff;
    --user: #7fcfa4;
    --assistant: #8aa4ff;
    --thinking: #c4a2f0;
    --tool: #e0b060;
    --error: #ff8f8f;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.6 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
.wrap { max-width: 1000px; margin: 0 auto; padding: 24px 16px 96px; }
header.top {
  display: flex; align-items: flex-start; gap: 16px; flex-wrap: wrap;
  padding-bottom: 16px; border-bottom: 1px solid var(--border); margin-bottom: 20px;
}
header.top .who-what { flex: 1 1 320px; min-width: 0; }
header.top h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
header.top button { flex: 0 0 auto; }
.sub { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
button {
  font: inherit; color: var(--text); background: var(--panel); cursor: pointer;
  border: 1px solid var(--border); border-radius: 7px; padding: 5px 11px;
}
button:hover { border-color: var(--accent); color: var(--accent); }
button[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--bg); }
.controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 20px; }
input[type="search"] {
  font: inherit; color: var(--text); background: var(--panel); flex: 1 1 200px; min-width: 160px;
  border: 1px solid var(--border); border-radius: 7px; padding: 5px 10px;
}
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); gap: 10px; margin-bottom: 22px; }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 9px;
  padding: 10px 12px; box-shadow: var(--shadow);
}
.card .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
.card .v { font-size: 17px; margin-top: 2px; }
details.panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 9px;
  padding: 0 14px; margin-bottom: 10px; box-shadow: var(--shadow);
}
details.panel > summary { cursor: pointer; padding: 11px 0; font-weight: 600; list-style: none; }
details.panel > summary::-webkit-details-marker { display: none; }
details.panel > summary::before {
  content: "\\25b8"; color: var(--muted); display: inline-block; width: 1.1em;
}
details.panel[open] > summary::before { content: "\\25be"; }
details.panel > summary .count { color: var(--muted); font-weight: 400; margin-left: 6px; font-size: 13px; }
.msg {
  background: var(--panel); border: 1px solid var(--border); border-left: 3px solid var(--border);
  border-radius: 9px; padding: 12px 14px; margin-bottom: 10px; box-shadow: var(--shadow);
}
.msg.role-user { border-left-color: var(--user); }
.msg.role-assistant { border-left-color: var(--assistant); }
.msg.dim { opacity: .58; }
.msg > .head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.who { font-weight: 600; }
.role-user .who { color: var(--user); }
.role-assistant .who { color: var(--assistant); }
.meta { color: var(--muted); font-size: 12px; }
.badge {
  font-size: 11px; padding: 1px 7px; border-radius: 999px;
  border: 1px solid var(--border); color: var(--muted); background: var(--panel-alt);
}
.badge.warn { color: var(--tool); border-color: var(--tool); }
.badge.err { color: var(--error); border-color: var(--error); }
pre.body {
  margin: 0; padding: 0; white-space: pre-wrap; overflow-wrap: anywhere;
  font-family: inherit; font-size: 14px;
}
/* Prose inside a panel, which supplies the side padding but no bottom. */
pre.body.prose { margin: 2px 0 12px; color: var(--text); }
.block { margin-top: 10px; }
.block:first-child { margin-top: 0; }
.block > .label {
  font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); margin-bottom: 4px;
}
.block.thinking { border-left: 2px solid var(--thinking); padding-left: 10px; }
.block.thinking > .label { color: var(--thinking); }
.block.thinking pre.body { color: var(--muted); font-style: italic; }
.tool {
  border: 1px solid var(--border); border-radius: 8px; background: var(--panel-alt);
  padding: 9px 11px; margin-top: 10px;
}
.tool > .name { font-weight: 600; color: var(--tool); }
.tool > .name .id { color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 6px; }
pre.code {
  margin: 6px 0 0; padding: 9px 10px; background: var(--bg); border: 1px solid var(--border);
  border-radius: 6px; font-size: 12.5px; white-space: pre-wrap; overflow-wrap: anywhere;
}
pre.code.error { border-color: var(--error); color: var(--error); }
.clamped { max-height: 17em; overflow: hidden; position: relative; }
.clamped::after {
  content: ""; position: absolute; inset: auto 0 0 0; height: 3.4em; pointer-events: none;
  background: linear-gradient(transparent, var(--panel));
}
.more {
  display: inline-block; margin-top: 6px; font-size: 12px; color: var(--accent);
  background: none; border: 0; padding: 0; cursor: pointer;
}
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 4px 0 12px; }
th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--border); }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; }
td.path { text-align: left; word-break: break-all; }
.empty { color: var(--muted); padding: 24px 0; text-align: center; }
h2.sec { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 26px 0 10px; }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="who-what">
      <h1 id="title">Session trace</h1>
      <div class="sub" id="subtitle"></div>
    </div>
    <button id="theme" type="button" title="Switch between dark and light"></button>
  </header>

  <div class="cards" id="stats"></div>

  <div id="panels"></div>

  <h2 class="sec">Transcript</h2>
  <div class="controls">
    <input type="search" id="search" placeholder="Filter messages by text..." autocomplete="off">
    <button id="toggle-ephemeral" type="button" aria-pressed="false">Late-injected</button>
    <button id="toggle-compacted" type="button" aria-pressed="false">Compacted</button>
    <button id="expand-all" type="button" aria-pressed="false">Expand all</button>
  </div>
  <div id="transcript"></div>
</div>

<script id="trace-data" type="application/json">__HX_TRACE_DATA__</script>
<script>
"use strict";

var TRACE = JSON.parse(document.getElementById("trace-data").textContent);

/* --- theme ------------------------------------------------------------- */
/* An explicit choice is remembered; without one the page follows the OS, so a
   trace opened on a dark desktop is dark without anybody choosing anything. */
var THEME_KEY = "hx-trace-theme";
function storedTheme() {
  try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
}
function applyTheme(value) {
  if (value) { document.documentElement.setAttribute("data-theme", value); }
  else { document.documentElement.removeAttribute("data-theme"); }
}
function systemPrefersDark() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}
function currentTheme() {
  var explicit = document.documentElement.getAttribute("data-theme");
  if (explicit) { return explicit; }
  return systemPrefersDark() ? "dark" : "light";
}
var themeButton = document.getElementById("theme");
function labelTheme() {
  themeButton.textContent = currentTheme() === "dark" ? "\u2600 Light" : "\u263e Dark";
}
applyTheme(storedTheme());
labelTheme();
themeButton.addEventListener("click", function () {
  var next = currentTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  labelTheme();
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* private mode */ }
});
/* Without an explicit choice the page follows the OS, including a change made
   while it is open - the label has to follow with it. */
if (window.matchMedia) {
  var watcher = window.matchMedia("(prefers-color-scheme: dark)");
  var onSystemChange = function () {
    if (!document.documentElement.getAttribute("data-theme")) { labelTheme(); }
  };
  if (watcher.addEventListener) { watcher.addEventListener("change", onSystemChange); }
  else if (watcher.addListener) { watcher.addListener(onSystemChange); }
}

/* --- helpers ----------------------------------------------------------- */
function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) { node.className = cls; }
  if (text !== undefined && text !== null) { node.textContent = String(text); }
  return node;
}
function tokens(n) {
  n = n || 0;
  if (n < 1000) { return String(n); }
  if (n < 1000000) { return (n / 1000).toFixed(1).replace(/\\.0$/, "") + "k"; }
  return (n / 1000000).toFixed(1).replace(/\\.0$/, "") + "M";
}
function money(n) {
  n = n || 0;
  if (n >= 100) { return "$" + n.toFixed(0); }
  if (n >= 1) { return "$" + n.toFixed(2); }
  if (n > 0) { return "$" + n.toFixed(4); }
  return "$0";
}
/* A subscription route prices every call at zero, so the sum is a real 0 and
   not a missing number. "$0.0000" beside a million cache-read tokens reads as
   a rounding artefact; the session cost nothing and the card should say so. */
function totalCost() {
  var priced = false;
  var total = 0;
  for (var i = 0; i < turns.length; i++) {
    var value = turns[i].cost_usd;
    if (value === null || value === undefined) { continue; }
    priced = true;
    total += value;
  }
  return priced ? money(total) : "-";
}
function clock(ts) {
  if (!ts) { return ""; }
  return new Date(ts * 1000).toLocaleTimeString();
}
function stamp(ts) {
  if (!ts) { return "unknown"; }
  return new Date(ts * 1000).toLocaleString();
}
function duration(seconds) {
  if (!seconds || seconds < 0) { return "0s"; }
  if (seconds < 60) { return Math.round(seconds) + "s"; }
  if (seconds < 3600) { return Math.floor(seconds / 60) + "m " + Math.round(seconds % 60) + "s"; }
  return Math.floor(seconds / 3600) + "h " + Math.round((seconds % 3600) / 60) + "m";
}
function pretty(value) {
  try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
}

/* Long bodies are folded rather than truncated: a trace that drops the tail of
   a tool result is the thing a trace exists to replace. */
var CLAMP_CHARS = 1200;
/* A folded body is `div > pre.foldable[.clamped] + button.more`, so the button
   is the node's next sibling and the whole fold can be driven from outside
   without holding a callback per body. */
function setFolded(node, folded) {
  if (folded) { node.classList.add("clamped"); }
  else { node.classList.remove("clamped"); }
  var button = node.nextElementSibling;
  if (button && button.classList.contains("more")) {
    button.textContent = folded ? node.getAttribute("data-fold-label") : "Show less";
  }
}
function clampable(node, text) {
  if (!text || text.length <= CLAMP_CHARS) { return node; }
  var box = el("div");
  node.classList.add("foldable");
  node.setAttribute("data-fold-label", "Show all " + text.length.toLocaleString() + " characters");
  var button = el("button", "more");
  button.type = "button";
  button.addEventListener("click", function () {
    setFolded(node, !node.classList.contains("clamped"));
  });
  box.appendChild(node);
  box.appendChild(button);
  setFolded(node, !EXPANDED);
  return box;
}

/* "Expand all" means the page, not the transcript: the panels holding the
   prompt and the schemas are `<details>` and stay shut on their own, which is
   the one thing a reader who clicked "Expand all" did not ask for. Tracked in
   a variable as well as applied, so a body built later - by clearing a filter,
   or switching the hidden kinds on - is born in the state the reader chose. */
var EXPANDED = false;
function applyExpansion(scope) {
  var boxes = (scope || document).querySelectorAll("details");
  for (var i = 0; i < boxes.length; i++) { boxes[i].open = EXPANDED; }
  var bodies = (scope || document).querySelectorAll(".foldable");
  for (var j = 0; j < bodies.length; j++) { setFolded(bodies[j], !EXPANDED); }
}

/* --- header and stats --------------------------------------------------- */
var meta = TRACE.session || {};
var usage = TRACE.usage || { turns: [] };
var turns = usage.turns || [];
var messages = TRACE.messages || [];

document.title = "hx trace - " + (meta.title || meta.session_id || "session");
document.getElementById("title").textContent = meta.title || "Session trace";

var subtitle = [];
if (meta.session_id) { subtitle.push(meta.session_id); }
if (meta.cwd) { subtitle.push(meta.cwd); }
if (meta.model) { subtitle.push(meta.model); }
subtitle.push("traced " + stamp(TRACE.generated_at));
if (TRACE.live) { subtitle.push("session still running - snapshot"); }
subtitle.push("hx " + (TRACE.hx_version || "?"));
document.getElementById("subtitle").textContent = subtitle.join("  \\u00b7  ");

function sum(key) {
  var total = 0;
  for (var i = 0; i < turns.length; i++) { total += turns[i][key] || 0; }
  return total;
}
var input = sum("input_tokens");
var cacheRead = sum("cache_read_tokens");
var promptTokens = input + cacheRead;
var stats = [
  ["prompts", meta.prompt_count || 0],
  ["messages", messages.length],
  ["provider calls", turns.length],
  ["input", tokens(input)],
  ["output", tokens(sum("output_tokens"))],
  ["cache read", tokens(cacheRead)],
  ["cache write", tokens(sum("cache_write_tokens"))],
  ["cache hit", promptTokens ? Math.round((cacheRead / promptTokens) * 100) + "%" : "-"],
  ["reasoning", tokens(sum("reasoning_tokens"))],
  ["cost", totalCost()],
  ["duration", duration((meta.updated_at || 0) - (meta.created_at || 0))]
];
var statsBox = document.getElementById("stats");
for (var s = 0; s < stats.length; s++) {
  var card = el("div", "card");
  card.appendChild(el("div", "k", stats[s][0]));
  card.appendChild(el("div", "v", stats[s][1]));
  statsBox.appendChild(card);
}

/* --- collapsible panels ------------------------------------------------- */
/* A session with no turns has nothing below the fold, so a page of shut bars
   is the whole page. Open the panels when the transcript cannot compete for
   the reader's attention; leave them shut when it can, because on a long
   session the prompt and the schemas are reference material, not the story. */
var OPEN_PANELS = messages.length === 0;
var panels = document.getElementById("panels");
function panel(title, count, build) {
  var box = el("details", "panel");
  if (OPEN_PANELS) { box.open = true; }
  var head = el("summary", null, title);
  if (count !== null && count !== undefined) { head.appendChild(el("span", "count", count)); }
  box.appendChild(head);
  box.appendChild(build());
  panels.appendChild(box);
}
/* Prose, not payload: the prompt and the project context are written for a
   person and read as markdown, so they get the body font. JSON keeps the
   monospace box it needs to stay aligned. */
function textPanel(title, text) {
  if (!text) { return; }
  panel(title, text.length.toLocaleString() + " chars", function () {
    var pre = el("pre", "body prose", text);
    return clampable(pre, text);
  });
}
textPanel("System prompt", TRACE.system_prompt);
textPanel("Project context", TRACE.project_context);
textPanel("Skills index", TRACE.skills_index);

if (TRACE.tools && TRACE.tools.length) {
  panel("Tool schemas", TRACE.tools.length + " tools", function () {
    var box = el("div");
    for (var i = 0; i < TRACE.tools.length; i++) {
      var tool = TRACE.tools[i];
      var item = el("details", "panel");
      var head = el("summary", null, tool.name || "(unnamed)");
      head.appendChild(el("span", "count", (tool.description || "").split("\\n")[0]));
      item.appendChild(head);
      item.appendChild(el("pre", "code", pretty(tool.input_schema)));
      box.appendChild(item);
    }
    return box;
  });
}

if (turns.length) {
  panel("Per-call usage", turns.length + " calls", function () {
    var table = el("table");
    var head = el("tr");
    var columns = ["#", "input", "output", "cache read", "cache write", "reasoning", "cost", "latency"];
    for (var c = 0; c < columns.length; c++) { head.appendChild(el("th", null, columns[c])); }
    table.appendChild(head);
    for (var i = 0; i < turns.length; i++) {
      var t = turns[i];
      var row = el("tr");
      var cells = [
        i + 1, tokens(t.input_tokens), tokens(t.output_tokens),
        tokens(t.cache_read_tokens), tokens(t.cache_write_tokens),
        tokens(t.reasoning_tokens),
        t.cost_usd === null || t.cost_usd === undefined ? "-" : money(t.cost_usd),
        Math.round(t.latency_ms || 0) + "ms"
      ];
      for (var k = 0; k < cells.length; k++) { row.appendChild(el("td", null, cells[k])); }
      table.appendChild(row);
    }
    return table;
  });
}

if (TRACE.checkpoints && TRACE.checkpoints.length) {
  panel("Files changed", TRACE.checkpoints.length + " snapshots", function () {
    var table = el("table");
    var head = el("tr");
    var columns = ["file", "at message", "existed", "snapshot"];
    for (var c = 0; c < columns.length; c++) { head.appendChild(el("th", null, columns[c])); }
    table.appendChild(head);
    for (var i = 0; i < TRACE.checkpoints.length; i++) {
      var cp = TRACE.checkpoints[i];
      var row = el("tr");
      row.appendChild(el("td", "path", cp.path));
      row.appendChild(el("td", null, cp.index));
      row.appendChild(el("td", null, cp.existed ? "yes" : "created"));
      row.appendChild(el("td", null, cp.captured ? "captured" : "too large"));
      table.appendChild(row);
    }
    return table;
  });
}

/* --- transcript --------------------------------------------------------- */
/* A tool result rides a user-role message to match the provider wire format.
   Showing it there would separate every call from its answer, so results are
   drawn under the call they belong to and the carrier message is dropped -
   unless it holds a result with no matching call, which is worth seeing. */
var calls = {};
var resultOf = {};
for (var m = 0; m < messages.length; m++) {
  var blocks = messages[m].content || [];
  for (var b = 0; b < blocks.length; b++) {
    if (blocks[b].type === "tool_use") { calls[blocks[b].id] = true; }
  }
}
for (var m2 = 0; m2 < messages.length; m2++) {
  var blocks2 = messages[m2].content || [];
  for (var b2 = 0; b2 < blocks2.length; b2++) {
    var block = blocks2[b2];
    if (block.type === "tool_result" && calls[block.tool_use_id] && !resultOf[block.tool_use_id]) {
      resultOf[block.tool_use_id] = block;
    }
  }
}
function isCarrier(message) {
  var blocks = message.content || [];
  if (!blocks.length) { return false; }
  for (var i = 0; i < blocks.length; i++) {
    if (blocks[i].type !== "tool_result") { return false; }
    if (!calls[blocks[i].tool_use_id]) { return false; }
  }
  return true;
}

var WHO = { user: "You", assistant: "Assistant", system: "System", tool: "Tool" };

/* Why a thinking block can be one line long.

   A reasoning model does not always hand back what it thought. The Responses
   API returns a short summary in plaintext and the reasoning itself as
   `encrypted_content`, which HX stores in the block's signature to replay on
   the next call and cannot read any more than the reader can. Labelling that
   "thinking" invites the conclusion that the trace dropped the rest, so the
   label says which of the two is on the page. */
function thinkingLabel(block) {
  if (!block.signature) { return "thinking"; }
  var items;
  try { items = JSON.parse(block.signature); } catch (e) { return "thinking (signed)"; }
  if (!items || !items.length) { return "thinking (signed)"; }
  var sealed = 0;
  for (var i = 0; i < items.length; i++) {
    if (items[i] && items[i].encrypted_content) { sealed++; }
  }
  if (!sealed) { return "thinking (signed)"; }
  return "thinking - summary only, reasoning encrypted";
}

function renderResult(result) {
  var box = el("div");
  var label = el("div", "label", result.is_error ? "result - error" : "result");
  box.appendChild(label);
  var pre = el("pre", result.is_error ? "code error" : "code", result.content || "");
  box.appendChild(clampable(pre, result.content || ""));
  if (result.spilled_path) {
    box.appendChild(el("div", "meta", "Output was capped; the whole of it is at " + result.spilled_path));
  }
  return box;
}

function renderBlock(block) {
  if (block.type === "text") {
    var text = el("div", "block");
    var body = el("pre", "body", block.text || "");
    text.appendChild(clampable(body, block.text || ""));
    return text;
  }
  if (block.type === "thinking") {
    var think = el("div", "block thinking");
    think.appendChild(el("div", "label", thinkingLabel(block)));
    if (block.text) {
      var thought = el("pre", "body", block.text);
      think.appendChild(clampable(thought, block.text));
    } else {
      think.appendChild(el("div", "meta", "The provider returned no summary for this block."));
    }
    return think;
  }
  if (block.type === "tool_use") {
    var call = el("div", "tool");
    var name = el("div", "name", block.name || "tool");
    name.appendChild(el("span", "id", block.id || ""));
    call.appendChild(name);
    call.appendChild(el("pre", "code", pretty(block.input)));
    var result = resultOf[block.id];
    if (result) { call.appendChild(renderResult(result)); }
    else { call.appendChild(el("div", "meta", "No result recorded - the turn ended first.")); }
    return call;
  }
  if (block.type === "tool_result") {
    var orphan = el("div", "tool");
    orphan.appendChild(el("div", "name", "orphaned result"));
    orphan.appendChild(el("div", "meta", "tool_use_id " + block.tool_use_id + " has no call in this transcript"));
    orphan.appendChild(renderResult(block));
    return orphan;
  }
  var unknown = el("div", "block");
  unknown.appendChild(el("div", "label", "unrecognised block: " + block.type));
  unknown.appendChild(el("pre", "code", pretty(block)));
  return unknown;
}

function renderMessage(message) {
  var dim = message.ephemeral || message.compacted;
  var node = el("div", "msg role-" + message.role + (dim ? " dim" : ""));
  var head = el("div", "head");
  head.appendChild(el("span", "who", WHO[message.role] || message.role));
  head.appendChild(el("span", "meta", "#" + message.index + "  " + clock(message.timestamp)));
  if (message.model) { head.appendChild(el("span", "meta", message.model)); }
  if (message.ephemeral) { head.appendChild(el("span", "badge warn", "late-injected")); }
  if (message.compacted) { head.appendChild(el("span", "badge", "compacted")); }
  if (message.metadata && message.metadata.compaction_summary) {
    head.appendChild(el("span", "badge", "compaction summary"));
  }
  node.appendChild(head);

  var blocks = message.content || [];
  if (!blocks.length) { node.appendChild(el("div", "meta", "(no content)")); }
  for (var i = 0; i < blocks.length; i++) { node.appendChild(renderBlock(blocks[i])); }

  var keys = message.metadata ? Object.keys(message.metadata) : [];
  if (keys.length) {
    var extra = el("details", "panel");
    extra.appendChild(el("summary", null, "metadata"));
    extra.appendChild(el("pre", "code", pretty(message.metadata)));
    node.appendChild(extra);
  }
  return node;
}

/* Searchable text is built once per message, from every block, so a filter
   reaches thinking and tool payloads and not only the prose. */
function searchText(message) {
  var parts = [message.role];
  var blocks = message.content || [];
  for (var i = 0; i < blocks.length; i++) {
    var block = blocks[i];
    if (block.text) { parts.push(block.text); }
    if (block.name) { parts.push(block.name); }
    if (block.input) { parts.push(pretty(block.input)); }
    if (block.content) { parts.push(block.content); }
    var result = block.type === "tool_use" ? resultOf[block.id] : null;
    if (result) { parts.push(result.content || ""); }
  }
  return parts.join("\\n").toLowerCase();
}

var rows = [];
for (var i = 0; i < messages.length; i++) {
  var message = messages[i];
  if (isCarrier(message)) { continue; }
  rows.push({ message: message, haystack: searchText(message), node: null });
}

var view = document.getElementById("transcript");
var state = { query: "", ephemeral: false, compacted: false };

function draw() {
  view.textContent = "";
  var shown = 0;
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    if (row.message.ephemeral && !state.ephemeral) { continue; }
    if (row.message.compacted && !state.compacted) { continue; }
    if (state.query && row.haystack.indexOf(state.query) === -1) { continue; }
    if (!row.node) {
      row.node = renderMessage(row.message);
      if (EXPANDED) { applyExpansion(row.node); }
    }
    view.appendChild(row.node);
    shown++;
  }
  if (!shown) {
    view.appendChild(el("div", "empty", rows.length
      ? "Nothing matches. Clear the filter, or switch on the hidden message kinds."
      : "This session has no messages yet."));
  }
}

function counted(kind) {
  var total = 0;
  for (var i = 0; i < rows.length; i++) { if (rows[i].message[kind]) { total++; } }
  return total;
}
function wireToggle(id, kind, label) {
  var button = document.getElementById(id);
  var total = counted(kind);
  if (!total) { button.remove(); return; }
  button.textContent = label + " (" + total + ")";
  button.addEventListener("click", function () {
    state[kind] = !state[kind];
    button.setAttribute("aria-pressed", state[kind] ? "true" : "false");
    draw();
  });
}
wireToggle("toggle-ephemeral", "ephemeral", "Late-injected");
wireToggle("toggle-compacted", "compacted", "Compacted");

document.getElementById("search").addEventListener("input", function (event) {
  state.query = event.target.value.trim().toLowerCase();
  draw();
});
var expandButton = document.getElementById("expand-all");
expandButton.addEventListener("click", function () {
  EXPANDED = !EXPANDED;
  applyExpansion();
  expandButton.textContent = EXPANDED ? "Collapse all" : "Expand all";
  expandButton.setAttribute("aria-pressed", EXPANDED ? "true" : "false");
});

draw();
</script>
</body>
</html>
"""

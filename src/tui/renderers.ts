/**
 * Per-tool presentation.
 *
 * A tool call is the most common thing on screen, so it is worth drawing well.
 * Every builtin gets a renderer that says what happened in the vocabulary of
 * the tool, and unknown tools - MCP servers, extensions - fall back to a
 * generic header rather than to nothing.
 *
 * Renderers are pure: they take the call, its output and its metadata, and
 * return strings. They never touch the loop or a view.
 *
 * The same renderer draws a call in the transcript and the detail of an
 * approval for that call, so the user agrees to the same shape of a thing they
 * then watch run.
 */

import { SequenceMatcher } from "../difflib.ts";
import { keymap } from "../keys.ts";
import { basename, expandUser, home, join, parent, relativeTo, resolve } from "../pathlib.ts";
import {
  expandTabs,
  formatFixed,
  purePath,
  repr,
  rstrip,
  SPACE,
  splitLines,
  strip,
  toInt,
  truthy,
} from "../py.ts";
import { bg as paintBg, fg as paintFg } from "../term/ansi.ts";
import { plainText } from "../term/sanitize.ts";
import { cellWidth, truncateToWidth } from "../term/width.ts";
import { count } from "../text.ts";
import { diffRows, HUNK_HEADER, countChanges as toolCountChanges } from "../tools/edit.ts";
import { BASH, ELLIPSIS, TODO_ACTIVE, TODO_DONE, TODO_PENDING } from "./glyphs.ts";
import { EXPANDED_MAX, PREVIEW_LINES, RECORD_WIDTH } from "./limits.ts";
import { color, colorMode, fg, highlightCode } from "./paint.ts";

const SPACES = SPACE.replace(/[\\\]^-]/g, "\\$&");
/** Python's `\s+|\w+|[^\w\s]`, with `str` semantics for both classes. */
const WORD = new RegExp(`[${SPACES}]+|[\\p{L}\\p{N}_]+|[^\\p{L}\\p{N}_${SPACES}]`, "gu");

export type Params = Record<string, unknown>;

/** `… (12 more lines, ctrl+o to expand)`. One wording, one key, one place. */
export function expandNote(hidden: number): string {
  return (
    fg("muted", `${ELLIPSIS} (${count(hidden, "more line")}, `) +
    fg("dim", keymap().primary("app.tools.expand")) +
    fg("muted", " to expand)")
  );
}

/** Everything a renderer is allowed to know about one tool call. */
export interface ToolCall {
  readonly name: string;
  readonly params: Params;
  readonly cwd: string;
  readonly output: string;
  readonly summary: string;
  readonly metadata: Record<string, unknown> | null;
  readonly isError: boolean;
  readonly finished: boolean;
  readonly expanded: boolean;
  readonly durationMs: number;
  readonly interrupted: boolean;
}

/** A call with the defaults for every field not given. */
export function toolCall(
  fields: Pick<ToolCall, "name" | "params" | "cwd"> & Partial<ToolCall>,
): ToolCall {
  return {
    output: "",
    summary: "",
    metadata: null,
    isError: false,
    finished: false,
    expanded: false,
    durationMs: 0,
    interrupted: false,
    ...fields,
  };
}

/** Fields of a `ToolCall` carrying text somebody else wrote. */
export const SANITIZED_FIELDS = new Set(["output", "summary", "params"]);

/**
 * The subset of `changes` that has to be cleaned, cleaned. A caller replacing
 * one field pays for that field only.
 */
export function sanitizedFields(changes: Partial<ToolCall>): Partial<ToolCall> {
  const out: Record<string, unknown> = { ...changes };
  for (const [name, value] of Object.entries(changes)) {
    if (!SANITIZED_FIELDS.has(name)) continue;
    if (name === "params") {
      out[name] = Object.fromEntries(
        Object.entries((value as Params | null) ?? {}).map(([key, item]) => [
          key,
          typeof item === "string" ? plainText(item) : item,
        ]),
      );
    } else if (name === "summary" && typeof value === "string") {
      // Drawn as the one row under a header: a raw break there splits the
      // row, and the screen refuses the frame.
      out[name] = singleRow(plainText(value));
    } else if (typeof value === "string") {
      out[name] = plainText(value);
    }
  }
  return out as Partial<ToolCall>;
}

/** A call whose strings are safe to draw - output, summary and parameters alike. */
export function sanitizedCall(call: ToolCall): ToolCall {
  return {
    ...call,
    ...sanitizedFields({ output: call.output, summary: call.summary, params: call.params }),
  };
}

/** `call` with some parameters swapped, the rest kept. */
export function replaceParams(call: ToolCall, params: Params): ToolCall {
  return { ...call, params: { ...call.params, ...params } };
}

export const LINE_BREAK = "↵";

/** Python's `str(value)` for a parameter. */
function str(value: unknown): string {
  if (typeof value === "string") return value;
  return repr(value);
}

/** A parameter as text for a one-row header, its line breaks shown as `↵`. */
export function singleRow(value: unknown): string {
  if (value === null || value === undefined) return "";
  return str(value).replace(/\r\n|\r|\n/g, LINE_BREAK);
}

/**
 * Path as a human would name it: relative to the project, else under `~`.
 *
 * The directories are resolved and the name is not, so a symlink is shown by
 * its own name rather than by its target's.
 */
export function displayPath(raw: unknown, cwd: string): string {
  if (!truthy(raw)) return "";
  const text = str(raw);
  let named: string;
  try {
    let path = purePath(expandUser(purePath(text)));
    path = path.startsWith("/") ? path : purePath(join(cwd, path));
    const name = basename(path);
    named = ["", ".", ".."].includes(name) ? resolve(path) : join(resolve(parent(path)), name);
  } catch {
    return singleRow(text);
  }
  for (const [base, prefix] of [
    [cwd, ""],
    [home(), "~/"],
  ] as const) {
    const relative = relativeTo(named, resolve(base));
    if (relative !== null) return singleRow(prefix + relative);
  }
  return singleRow(text);
}

const LEXERS: Record<string, string> = {
  ".py": "python",
  ".pyi": "python",
  ".ts": "typescript",
  ".tsx": "tsx",
  ".js": "javascript",
  ".jsx": "jsx",
  ".json": "json",
  ".toml": "toml",
  ".yaml": "yaml",
  ".yml": "yaml",
  ".md": "markdown",
  ".rs": "rust",
  ".go": "go",
  ".sh": "bash",
  ".bash": "bash",
  ".zsh": "bash",
  ".css": "css",
  ".tcss": "css",
  ".html": "html",
  ".sql": "sql",
  ".c": "c",
  ".h": "c",
  ".cpp": "cpp",
  ".java": "java",
  ".rb": "ruby",
};

/** `Path(path).suffix`. */
function suffix(path: string): string {
  const name = basename(path);
  const index = name.lastIndexOf(".");
  return index > 0 && index < name.length - 1 ? name.slice(index) : "";
}

export function lexerFor(path: string): string | null {
  return LEXERS[suffix(path).toLowerCase()] ?? null;
}

/** Syntax-highlight `code` when the file type is known, else dim text. */
export function highlight(code: string, path: string): string[] {
  const language = lexerFor(path);
  if (language === null) return code.split("\n").map((line) => fg("tool_output", line));
  return highlightCode(code, language);
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${formatFixed(ms, 0)}ms`;
  return `${formatFixed(ms / 1000, 1)}s`;
}

/** Last `limit` lines, plus how many were dropped. */
function tail(text: string, limit: number, expanded: boolean): [string[], number] {
  const lines = splitLines(text);
  if (expanded) return [lines.slice(-EXPANDED_MAX), Math.max(0, lines.length - EXPANDED_MAX)];
  if (lines.length <= limit) return [lines, 0];
  return [lines.slice(-limit), lines.length - limit];
}

function head(text: string, limit: number, expanded: boolean): [string[], number] {
  const lines = splitLines(text);
  if (expanded) return [lines.slice(0, EXPANDED_MAX), Math.max(0, lines.length - EXPANDED_MAX)];
  if (lines.length <= limit) return [lines, 0];
  return [lines.slice(0, limit), lines.length - limit];
}

const errorLines = (output: string) =>
  strip(output)
    .split("\n")
    .map((line) => fg("error", line));

// --------------------------------------------------------------------------- //
// Diffs
// --------------------------------------------------------------------------- //

/**
 * Highlight the words that actually changed within a modified line, by
 * inverting only the changed run. Inversion is reserved for this and for the
 * text cursor, so seeing it always means the same thing.
 */
function wordDiff(before: string, after: string): [string, string] {
  const oldWords = before.match(WORD) ?? [];
  const newWords = after.match(WORD) ?? [];
  const removed: string[] = [];
  const added: string[] = [];
  const mode = colorMode();
  const plain = (text: string, role: string) => paintFg(color(role), text, mode);
  const mark = (text: string, fgRole: string, bgRole: string) =>
    paintBg(color(bgRole), paintFg(color(fgRole), text, mode, { bold: true }), mode);

  const matcher = new SequenceMatcher(oldWords, newWords, false);
  for (const [op, i1, i2, j1, j2] of matcher.getOpcodes()) {
    const oldRun = oldWords.slice(i1, i2).join("");
    const newRun = newWords.slice(j1, j2).join("");
    if (op === "equal") {
      removed.push(plain(oldRun, "diff_removed"));
      added.push(plain(newRun, "diff_added"));
      continue;
    }
    removed.push(
      strip(oldRun) ? mark(oldRun, "diff_removed", "tool_error_bg") : plain(oldRun, "diff_removed"),
    );
    added.push(
      strip(newRun) ? mark(newRun, "diff_added", "tool_success_bg") : plain(newRun, "diff_added"),
    );
  }
  return [removed.join(""), added.join("")];
}

const lineNo = (number: number) => String(number).padStart(5);

/**
 * A unified diff, numbered and coloured the way pi draws one. Line numbers
 * come from the hunk headers rather than being shown as `@@` noise.
 *
 * The whole diff is rendered. Clipping belongs to whoever is showing it.
 */
export function renderDiff(diffText: string): string[] {
  const out: string[] = [];
  let oldNo = 0;
  let newNo = 0;
  let pendingRemoved: [number, string][] = [];
  let pendingAdded: [number, string][] = [];

  const flush = () => {
    const [onlyRemoved] = pendingRemoved;
    const [onlyAdded] = pendingAdded;
    if (pendingRemoved.length === 1 && pendingAdded.length === 1 && onlyRemoved && onlyAdded) {
      const [removed, added] = wordDiff(onlyRemoved[1], onlyAdded[1]);
      out.push(fg("diff_removed", `-${lineNo(onlyRemoved[0])} `) + removed);
      out.push(fg("diff_added", `+${lineNo(onlyAdded[0])} `) + added);
    } else {
      for (const [number, text] of pendingRemoved)
        out.push(fg("diff_removed", `-${lineNo(number)} ${text}`));
      for (const [number, text] of pendingAdded)
        out.push(fg("diff_added", `+${lineNo(number)} ${text}`));
    }
    pendingRemoved = [];
    pendingAdded = [];
  };

  // File headers are left out: the approval or the edit header names the file.
  for (const row of diffRows(diffText)) {
    if (row.kind === "file") continue;
    if (row.header !== null) {
      flush();
      oldNo = Number(row.header[1]);
      newNo = Number(row.header[3]);
      if (out.length) out.push(fg("diff_context", ELLIPSIS));
      continue;
    }
    const line = row.text;
    if (line.startsWith("-")) {
      pendingRemoved.push([oldNo, expandTabs(line.slice(1), 4)]);
      oldNo++;
    } else if (line.startsWith("+")) {
      pendingAdded.push([newNo, expandTabs(line.slice(1), 4)]);
      newNo++;
    } else {
      flush();
      out.push(fg("diff_context", ` ${lineNo(newNo)} ${expandTabs(line.slice(1), 4)}`));
      oldNo++;
      newNo++;
    }
  }
  flush();
  return out;
}

/** Whether `text` is a unified diff: it needs a real hunk header. */
export function looksLikeDiff(text: string): boolean {
  return splitLines(text).some((line) => HUNK_HEADER.test(line));
}

export const countChanges = toolCountChanges;

// --------------------------------------------------------------------------- //
// Renderers
// --------------------------------------------------------------------------- //

/** Draws one tool. Subclasses override the parts they care about. */
export class ToolRenderer {
  verb = "";

  /** How much output survives collapsing. */
  previewLines = PREVIEW_LINES;

  /**
   * Drawn for an approval rather than for the transcript. In the transcript a
   * call is a step quieter than the answer around it; an approval is the one
   * place the call is what has to be read.
   */
  loud = false;

  /** This renderer, drawing at full strength. The shape is the same. */
  forApproval(): ToolRenderer {
    const louder = Object.assign(Object.create(Object.getPrototypeOf(this)), this) as ToolRenderer;
    louder.loud = true;
    return louder;
  }

  protected get title(): string {
    return this.loud ? "text" : "tool_title";
  }

  protected get args(): string {
    return this.loud ? "accent" : "tool_args";
  }

  /** The single line always on screen, collapsed or not. */
  header(call: ToolCall): string {
    return (
      fg(this.title, this.verb || call.name.toLowerCase(), { bold: true }) +
      fg("muted", ` ${brief(call.params)}`)
    );
  }

  /** One line on what came back, such as `3 results`. Empty for none. */
  summary(_call: ToolCall): string {
    return "";
  }

  /** What the call gave back, shown under the summary. */
  body(call: ToolCall): string[] {
    return plainOutput(call, this.previewLines);
  }

  /** Everything hung under the header: the summary first, then the body. */
  result(call: ToolCall): string[] {
    const summary = this.summary(call);
    return summary ? [fg("dim", summary), ...this.body(call)] : this.body(call);
  }

  /** What an approval shows when asked to permit this call. */
  detail(call: ToolCall): string[] {
    return [this.header(call)];
  }
}

class ReadRenderer extends ToolRenderer {
  override verb = "read";

  override header(call: ToolCall): string {
    const path = displayPath(call.params.file_path || call.params.path, call.cwd);
    let header = fg(this.title, "read ", { bold: true }) + fg(this.args, path);
    const { offset, limit } = call.params;
    if (truthy(offset) || truthy(limit)) {
      const start = toInt(truthy(offset) ? offset : 1);
      const end = truthy(limit) ? start + toInt(limit) - 1 : "";
      header += fg("warning", `:${start}${end ? `-${end}` : ""}`);
    }
    return header;
  }

  override summary(call: ToolCall): string {
    return call.finished && !call.isError ? call.summary : "";
  }

  /** Collapsed reads show nothing: the model read the file, the user did not.
   * Expanding shows the window it actually saw, highlighted. */
  override body(call: ToolCall): string[] {
    if (!call.output || (!call.expanded && !call.isError)) return [];
    if (call.isError) return errorLines(call.output);
    const path = str(call.params.file_path || call.params.path || "");
    const [lines, hidden] = head(call.output, this.previewLines, call.expanded);
    const out = highlight(lines.map(stripLineNumber).join("\n"), path);
    return hidden ? [...out, expandNote(hidden)] : out;
  }
}

class WriteRenderer extends ToolRenderer {
  override verb = "write";

  override header(call: ToolCall): string {
    const path = displayPath(call.params.file_path, call.cwd);
    let header = fg(this.title, "write ", { bold: true }) + fg(this.args, path);
    const content = str(call.params.content || "");
    if (content) header += fg("dim", `  ${count(splitLines(content).length, "line")}`);
    return header;
  }

  override body(call: ToolCall): string[] {
    if (call.isError) return errorLines(call.output);
    const content = str(call.params.content || "");
    if (!content) return [];
    const [lines, hidden] = head(content, this.previewLines, call.expanded);
    const out = highlight(lines.join("\n"), str(call.params.file_path || ""));
    return hidden ? [...out, expandNote(hidden)] : out;
  }

  override detail(call: ToolCall): string[] {
    return [this.header(call), ...this.body(call)];
  }
}

class EditRenderer extends ToolRenderer {
  override verb = "edit";

  override header(call: ToolCall): string {
    const path = displayPath(call.params.file_path, call.cwd);
    let header = fg(this.title, "edit ", { bold: true }) + fg(this.args, path);
    const diff = EditRenderer.diff(call);
    if (diff) {
      const [added, removed] = countChanges(diff);
      header += fg("diff_added", `  +${added}`) + fg("diff_removed", ` -${removed}`);
    }
    return header;
  }

  private static diff(call: ToolCall): string {
    return str(call.metadata?.diff || "");
  }

  /** The diff is the whole point of an edit, so it is shown collapsed too -
   * just clipped to a reviewable height. */
  override body(call: ToolCall): string[] {
    if (call.isError) return errorLines(call.output);
    const diff = EditRenderer.diff(call);
    if (!diff) return [];
    return clip(renderDiff(diff), this.previewLines, call.expanded);
  }

  override detail(call: ToolCall): string[] {
    const diff = EditRenderer.diff(call);
    if (!diff) return [this.header(call)];
    return [this.header(call), ...renderDiff(diff)];
  }
}

class BashRenderer extends ToolRenderer {
  override verb = "$";

  /**
   * The command's first line, and how many more there are. A header is one
   * row: a script's newlines written into it raw would break the frame.
   */
  override header(call: ToolCall): string {
    const [first, rest] = BashRenderer.commandLines(call);
    // Not bold in the transcript: a whole command in bold was the loudest
    // thing on screen, louder than the answer it was run to find.
    let header =
      fg(this.args, BASH, { bold: true }) + fg(this.title, first || ELLIPSIS, { bold: this.loud });
    if (rest.length) header += fg("muted", `  +${count(rest.length, "line")}`);
    const timeout = call.params.timeout;
    if (truthy(timeout)) header += fg("muted", ` (timeout ${str(timeout)}s)`);
    if (truthy(call.params.run_in_background)) header += fg("warning", "  background");
    return header;
  }

  protected static commandLines(call: ToolCall): [string, string[]] {
    const lines = strip(str(call.params.command || "")).split(/\r\n|\r|\n/);
    return [strip(lines[0] ?? ""), lines.slice(1).map((line) => rstrip(line))];
  }

  protected continuation(rest: string[]): string[] {
    // Under the first line's text, not under the `$`: one left edge.
    const indent = " ".repeat(cellWidth(BASH));
    return rest.map((line) => fg(this.title, `${indent}${line}`, { bold: this.loud }));
  }

  /**
   * The tail, not the head: a command's answer is at the end of it. Expanded,
   * the rest of a multi-line command comes first.
   */
  override body(call: ToolCall): string[] {
    const out: string[] = [];
    const [, rest] = BashRenderer.commandLines(call);
    if (rest.length && call.expanded) out.push(...this.continuation(rest));
    const output = strip(call.output, "\n");
    if (output) {
      const [lines, hidden] = tail(output, this.previewLines, call.expanded);
      if (hidden) out.push(expandNote(hidden));
      // Cut off, it printed what it printed: that is output, not an error.
      const role = call.isError && !call.interrupted ? "error" : "tool_output";
      out.push(...lines.map((line) => fg(role, line)));
    }
    if (call.finished && call.durationMs && !call.interrupted)
      out.push(fg("dim", `Took ${formatDuration(call.durationMs)}`));
    return out;
  }

  /** The whole command, a row per line - an approval shows what will run. */
  override detail(call: ToolCall): string[] {
    const [first, rest] = BashRenderer.commandLines(call);
    const head = BashRenderer.prototype.header.call(this, replaceParams(call, { command: first }));
    return [head, ...this.continuation(rest)];
  }
}

class GrepRenderer extends ToolRenderer {
  override verb = "grep";

  override header(call: ToolCall): string {
    let header =
      fg(this.title, "grep ", { bold: true }) + fg(this.args, singleRow(call.params.pattern));
    const shown = elsewhere(call.params.path, call.cwd);
    if (shown) header += fg("muted", ` in ${shown}`);
    const glob = singleRow(call.params.glob);
    if (glob) header += fg("muted", ` (${glob})`);
    return header;
  }

  override summary(call: ToolCall): string {
    return call.finished ? call.summary : "";
  }
}

class GlobRenderer extends ToolRenderer {
  override verb = "glob";

  override header(call: ToolCall): string {
    let header =
      fg(this.title, "glob ", { bold: true }) + fg(this.args, singleRow(call.params.pattern));
    const shown = elsewhere(call.params.path, call.cwd);
    if (shown) header += fg("muted", ` in ${shown}`);
    return header;
  }

  override summary(call: ToolCall): string {
    return call.finished ? call.summary : "";
  }
}

const TODO_MARKERS: Record<string, string> = {
  completed: TODO_DONE,
  in_progress: TODO_ACTIVE,
  pending: TODO_PENDING,
};

class TodoRenderer extends ToolRenderer {
  override verb = "todos";

  override header(_call: ToolCall): string {
    return fg(this.title, "todos", { bold: true });
  }

  override summary(call: ToolCall): string {
    return call.summary;
  }

  /** The list itself - unless it was rejected, when the reason is the only
   * useful thing to show. */
  override body(call: ToolCall): string[] {
    if (call.isError && call.output) return errorLines(call.output);
    return renderTodos(call.params.todos);
  }
}

/** One plan, drawn the same way wherever it appears. */
export function renderTodos(todos: unknown): string[] {
  if (!Array.isArray(todos) || !todos.length) return [];
  const out: string[] = [];
  for (const todo of todos) {
    if (typeof todo !== "object" || todo === null || Array.isArray(todo)) continue;
    const item = todo as Record<string, unknown>;
    const status = str(item.status || "pending");
    const marker = TODO_MARKERS[status] ?? TODO_PENDING;
    // The plan is the model's own words, so it is sanitized like any other
    // text it wrote.
    const content = plainText(str(item.content || ""));
    const label = status === "in_progress" ? plainText(str(item.active_form || content)) : content;
    const markerRole = ({ completed: "success", in_progress: "accent" } as Record<string, string>)[
      status
    ];
    const labelRole = ({ completed: "dim", in_progress: "accent" } as Record<string, string>)[
      status
    ];
    out.push(fg(markerRole ?? "dim", `${marker} `) + fg(labelRole ?? "muted", label));
  }
  return out;
}

class SymbolsRenderer extends ToolRenderer {
  override verb = "symbols";

  override header(call: ToolCall): string {
    const mode = singleRow(call.params.mode);
    // The symbol for a search, the file for an outline.
    const subject = singleRow(call.params.symbol);
    let header = fg(this.title, `symbols ${mode} `, { bold: true }) + fg(this.args, subject);
    const shown = elsewhere(call.params.file_path || call.params.path, call.cwd);
    if (shown) header += fg("muted", `${subject ? " in " : ""}${shown}`);
    return header;
  }

  override summary(call: ToolCall): string {
    return call.finished ? call.summary : "";
  }
}

class TaskRenderer extends ToolRenderer {
  override verb = "task";

  override header(call: ToolCall): string {
    let header =
      fg(this.title, "task ", { bold: true }) +
      fg(this.args, singleRow(call.params.subagent_type) || "agent");
    const description = singleRow(call.params.description);
    if (description) header += fg("muted", `  ${description}`);
    return header;
  }
}

const RENDERERS: Record<string, ToolRenderer> = {
  read: new ReadRenderer(),
  write: new WriteRenderer(),
  edit: new EditRenderer(),
  bash: new BashRenderer(),
  bashoutput: new BashRenderer(),
  grep: new GrepRenderer(),
  glob: new GlobRenderer(),
  symbols: new SymbolsRenderer(),
  todowrite: new TodoRenderer(),
  task: new TaskRenderer(),
};
const FALLBACK = new ToolRenderer();

/** The renderer for `name`, or the generic one for tools HX does not own. */
export function rendererFor(name: string): ToolRenderer {
  return RENDERERS[name.toLowerCase()] ?? FALLBACK;
}

/** Keep `limit` lines, appending a note about what was dropped. */
export function clip(lines: string[], limit: number, expanded: boolean): string[] {
  const ceiling = expanded ? EXPANDED_MAX : limit;
  if (lines.length <= ceiling) return lines;
  return [...lines.slice(0, ceiling), expandNote(lines.length - ceiling)];
}

/** The path, unless it is the directory the session is already in. */
function elsewhere(path: unknown, cwd: string): string {
  const shown = displayPath(path, cwd);
  return shown === "" || shown === "." ? "" : shown;
}

/** Drop the `   12\t` prefix the read tool adds, keeping the code. */
function stripLineNumber(line: string): string {
  const tab = line.indexOf("\t");
  if (tab < 0) return line;
  return /^\d+$/.test(strip(line.slice(0, tab))) ? line.slice(tab + 1) : line;
}

function plainOutput(call: ToolCall, limit: number): string[] {
  if (!strip(call.output)) return [];
  const [lines, hidden] = head(call.output, limit, call.expanded);
  const role = call.isError && !call.interrupted ? "error" : "tool_output";
  const out = lines.map((line) => fg(role, line));
  return hidden ? [...out, expandNote(hidden)] : out;
}

/** Compact `key=value` preview for tools without a renderer of their own. */
function brief(params: Params, limit = RECORD_WIDTH): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    let rendered: string;
    if (Array.isArray(value)) rendered = `[${count(value.length, "item")}]`;
    else if (typeof value === "object" && value !== null) rendered = "{…}";
    else if (typeof value === "string" && cellWidth(value) > limit)
      rendered = repr(truncateToWidth(value, limit - 1) + ELLIPSIS);
    else rendered = repr(value);
    parts.push(`${key}=${rendered}`);
  }
  const joined = parts.join(", ");
  if (cellWidth(joined) <= limit) return joined;
  return truncateToWidth(joined, limit - 1) + ELLIPSIS;
}

/**
 * Edit tool: exact string replacement, or replacement by content anchor.
 *
 * Exact-match replacement rather than diff application: the model either
 * matched the file or it did not, and an ambiguous match is an error instead of
 * a guess.
 *
 * `hashline` is the second shape. Instead of retyping the lines it wants gone,
 * the model names the anchors `Read` printed beside them and supplies only the
 * replacement. Anchors are recomputed from disk at edit time, so a file that
 * moved under the model fails to resolve rather than being patched at stale
 * coordinates.
 */

import { readFileSync } from "node:fs";
import type { CheckpointStore } from "../core/checkpoints.ts";
import { unifiedDiff } from "../difflib.ts";
import { basename, isFile } from "../pathlib.ts";
import { pyCount, pyReplace, repr, splitLines, splitLinesKeepEnds, strip, truthy } from "../py.ts";
import { count } from "../text.ts";
import { AnchorError, replaceSpan, resolveAnchor, split } from "./anchors.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";
import { hashlineEnabled, readExact, resolvePath } from "./read.ts";
import { READ_FIRST, throughLink, writeAtomic } from "./write.ts";

const DESCRIPTION = `Replace part of a file. Read the file first.

Two ways to say what to replace:

- \`old_string\`/\`new_string\` - exact match. \`old_string\` must appear exactly
  once unless \`replace_all\` is set. Pass \`edits\` for several of these applied
  to one file atomically.
- \`hashline\` - the anchors Read printed beside each line. \`{"start": "a3f9",
  "end": "b7c2", "new_string": "..."}\` replaces lines a3f9 through b7c2
  inclusive. Omit \`end\` to replace one line, and pass an empty \`new_string\` to
  delete the span. Cheaper than retyping the block, and a stale anchor is
  rejected instead of applied to the wrong lines.

Either shape, not both.`;

export interface EditOp {
  readonly oldString: string;
  readonly newString: string;
  readonly replaceAll: boolean;
}

export interface HashlineOp {
  readonly start: string;
  readonly end: string;
  readonly newString: string;
}

export class EditTool extends Tool {
  readonly name = "Edit";
  readonly description = DESCRIPTION;
  override readonly mutating = true;

  /** See `WriteTool` - the pre-image `/rewind` restores. */
  constructor(private readonly checkpoints: CheckpointStore | null = null) {
    super();
  }

  schema() {
    const editProperties = {
      old_string: { type: "string" },
      new_string: { type: "string" },
      replace_all: { type: "boolean", default: false },
    };
    return {
      type: "object",
      properties: {
        file_path: { type: "string" },
        old_string: { type: "string" },
        new_string: { type: "string" },
        replace_all: { type: "boolean", default: false },
        edits: {
          type: "array",
          description: "Several edits applied to one file, in order",
          items: {
            type: "object",
            properties: editProperties,
            required: ["old_string", "new_string"],
          },
        },
        hashline: {
          type: "array",
          description: "Line spans named by the anchors Read printed",
          items: {
            type: "object",
            properties: {
              start: { type: "string", description: "First line's anchor" },
              end: { type: "string", description: "Last line's anchor. Defaults to start." },
              new_string: { type: "string" },
            },
            required: ["start", "new_string"],
          },
        },
      },
      required: ["file_path"],
    };
  }

  override permissionSpecifier(params: Params): string {
    return String(params.file_path ?? "");
  }

  /** Applies every edit or none - a partial application would leave the file
   * in a state neither the model nor the user asked for. */
  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const path = resolvePath(String(params.file_path), ctx.cwd);
    if (!isFile(path)) throw new ToolError(`${path}: no such file`);
    if (!ctx.reads.wasRead(path))
      throw new ToolError(`${path} has not been read in this session. ${READ_FIRST}`);
    if (ctx.reads.changedSinceRead(path))
      throw new ToolError(`${path} changed on disk since it was read. Read it again.`);

    let before: string;
    try {
      before = readExact(path);
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
      throw new ToolError(
        `${path} is not UTF-8 text (${invalidUtf8(path)}). Edit only changes UTF-8 files.`,
      );
    }

    let after: string;
    let applied: number;
    if (truthy(params.hashline)) {
      if (!hashlineEnabled(ctx))
        throw new ToolError("hashline edits are disabled (tools.hashline is false)");
      if (truthy(params.edits) || "old_string" in params) {
        throw new ToolError(
          "Edit takes hashline or old_string/edits, not both. Applying one and " +
            "dropping the other would silently lose half of what was asked for.",
        );
      }
      const spans = parseHashline(params);
      after = applyHashline(before, spans);
      applied = spans.length;
    } else {
      const edits = parseEdits(params);
      after = applyEdits(before, edits);
      applied = edits.length;
    }

    this.checkpoints?.capture(path);
    writeAtomic(path, after);
    ctx.reads.markRead(path);
    this.checkpoints?.settle(path);

    const diff = unifiedDiffOf(before, after, path);
    const [added, removed] = countChanges(diff);
    return toolResult(
      `Applied ${count(applied, "edit")} to ${path}${throughLink(path)}.\n\n${diff}`,
      {
        summary: `${basename(path)} +${added} -${removed}`,
        metadata: { path, diff },
      },
    );
  }
}

/** Python's `UnicodeDecodeError` reason: `invalid start byte at byte 12`. */
function invalidUtf8(path: string): string {
  const bytes = readFileSync(path);
  for (let index = 0; index < bytes.length; ) {
    const byte = bytes[index] as number;
    const length =
      byte < 0x80
        ? 1
        : byte >= 0xc2 && byte < 0xe0
          ? 2
          : byte >= 0xe0 && byte < 0xf0
            ? 3
            : byte >= 0xf0 && byte < 0xf5
              ? 4
              : 0;
    if (length === 0) return `invalid start byte at byte ${index}`;
    for (let k = 1; k < length; k++) {
      const next = bytes[index + k];
      if (next === undefined) return `unexpected end of data at byte ${index}`;
      if ((next & 0xc0) !== 0x80) return `invalid continuation byte at byte ${index}`;
    }
    index += length;
  }
  return "invalid utf-8";
}

/**
 * Pure function over file content. Throws `ToolError` when a match is missing
 * or ambiguous. Shared with the approval preview, so the diff a user approves
 * is computed exactly the way the edit will be applied.
 *
 * In a CRLF file the text arrives with bare newlines - Read shows lines without
 * their carriage returns - so a multi-line `old_string` is matched in its CRLF
 * spelling when the literal one is absent, and `new_string` is given CRLF
 * endings either way.
 */
export function applyEdits(content: string, edits: EditOp[]): string {
  const crlf = usesCrlf(split(content)[0]);
  let result = content;
  edits.forEach((edit, i) => {
    const index = i + 1;
    if (edit.oldString === edit.newString)
      throw new ToolError(`edit ${index}: old_string and new_string are identical`);
    let old = edit.oldString;
    let replacement = edit.newString;
    if (crlf) {
      replacement = toCrlf(replacement);
      if (pyCount(result, old) === 0) old = toCrlf(old);
      // Compared again once both are spelled the file's way: an edit that only
      // changed line endings would otherwise replace nothing and still be
      // reported as applied.
      if (old === replacement) {
        throw new ToolError(
          `edit ${index}: old_string and new_string are identical once their ` +
            "line endings follow the file's CRLF. Edit keeps a file's line endings.",
        );
      }
    }
    const occurrences = pyCount(result, old);
    if (occurrences === 0) {
      throw new ToolError(
        `edit ${index}: old_string not found in the file. It must match exactly, including whitespace and indentation.`,
      );
    }
    if (occurrences > 1 && !edit.replaceAll) {
      throw new ToolError(
        `edit ${index}: old_string appears ${occurrences} times. Add surrounding context to make it unique, or set replace_all.`,
      );
    }
    result = pyReplace(result, old, replacement, edit.replaceAll ? -1 : 1);
  });
  return result;
}

/** Whether most of a file's lines end in CRLF - the ending an edit should write. */
const usesCrlf = (lines: string[]) =>
  lines.filter((line) => line.endsWith("\r")).length * 2 > lines.length;

/** `text` with every line ending spelled CRLF. */
const toCrlf = (text: string) => text.replaceAll("\r\n", "\n").replaceAll("\n", "\r\n");

/**
 * Resolve each span against the current content and replace it. Spans are
 * applied in order, each against the result of the last - anchors name
 * content, not coordinates, so an earlier replacement only disturbs a later
 * anchor when the two spans touch.
 */
export function applyHashline(content: string, spans: HashlineOp[]): string {
  let [lines] = split(content);
  const trailingNewline = split(content)[1];
  // Replacement text arrives with bare newlines; in a CRLF file it is given CRLF.
  const crlf = usesCrlf(lines);
  spans.forEach((span, i) => {
    const index = i + 1;
    let start: number;
    let end: number;
    try {
      start = resolveAnchor(lines, span.start);
      end = span.end !== span.start ? resolveAnchor(lines, span.end) : start;
    } catch (error) {
      if (error instanceof AnchorError) throw new ToolError(`hashline ${index}: ${error.message}`);
      throw error;
    }
    if (end < start) {
      throw new ToolError(
        `hashline ${index}: end anchor ${repr(span.end)} is at line ${end + 1}, before start anchor ${repr(span.start)} at line ${start + 1}`,
      );
    }
    lines = replaceSpan(lines, start, end, replacementLines(span.newString, crlf));
  });
  const result = lines.join("\n");
  return trailingNewline && result ? `${result}\n` : result;
}

/**
 * The lines a span becomes, ending the way the rest of the file's lines do. A
 * newline at the end of the text closes its last line rather than opening one
 * more; a lone newline is still one blank line, and empty text deletes the span.
 */
function replacementLines(replacement: string, crlf: boolean): string[] {
  if (!replacement) return [];
  const parts = replacement.replaceAll("\r\n", "\n").split("\n");
  if (parts.length > 1 && parts.at(-1) === "") parts.pop();
  return crlf ? parts.map((part) => `${part}\r`) : parts;
}

const typeName = (value: unknown) =>
  value === null
    ? "NoneType"
    : Array.isArray(value)
      ? "list"
      : typeof value === "string"
        ? "str"
        : typeof value === "number"
          ? Number.isInteger(value)
            ? "int"
            : "float"
          : typeof value === "boolean"
            ? "bool"
            : "dict";

/** `str(value).strip().lstrip("#")`. */
const cleanAnchor = (value: unknown) => strip(pyStr(value)).replace(/^#+/, "");

function pyStr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  return String(value);
}

export function parseHashline(params: Params): HashlineOp[] {
  const raw = (params.hashline as unknown[]) || [];
  const spans: HashlineOp[] = [];
  for (const item of raw) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      throw new ToolError(`each hashline entry must be an object, got ${typeName(item)}`);
    }
    const entry = item as Record<string, unknown>;
    if (!("start" in entry) || !("new_string" in entry))
      throw new ToolError("each hashline entry needs start and new_string");
    const start = cleanAnchor(entry.start);
    spans.push({
      start,
      end: truthy(entry.end) ? cleanAnchor(entry.end) : cleanAnchor(start),
      newString: pyStr(entry.new_string),
    });
  }
  if (spans.length === 0) throw new ToolError("hashline was empty");
  return spans;
}

export function parseEdits(params: Params): EditOp[] {
  const raw = params.edits;
  if (truthy(raw)) {
    return (raw as Record<string, unknown>[]).map((item) => {
      if (!("old_string" in item)) throw new KeyError("old_string");
      if (!("new_string" in item)) throw new KeyError("new_string");
      return {
        oldString: pyStr(item.old_string),
        newString: pyStr(item.new_string),
        replaceAll: truthy(item.replace_all ?? false),
      };
    });
  }
  if (!("old_string" in params) || !("new_string" in params)) {
    throw new ToolError("Edit needs either old_string/new_string or a list of edits");
  }
  return [
    {
      oldString: pyStr(params.old_string),
      newString: pyStr(params.new_string),
      replaceAll: truthy(params.replace_all ?? false),
    },
  ];
}

/** A missing key, reported as Python reported it: `KeyError: 'old_string'`. */
class KeyError extends Error {
  override name = "KeyError";
  constructor(key: string) {
    super(repr(key));
  }
}

export const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/** One meaningful line of a unified diff. */
export interface DiffRow {
  readonly kind: "file" | "hunk" | "line";
  /** The file's name, the hunk header, or the line with its `-`/`+`/space. */
  readonly text: string;
  /** For a hunk: its parsed header, which numbers the lines that follow. */
  readonly header: RegExpMatchArray | null;
}

/**
 * The files, hunks and changed or context lines of a unified diff.
 *
 * A hunk ends where its header says, after its count of old and new lines -
 * not at the next line that looks like a header. A changed line can start with
 * `--` or `++` (a SQL comment, `++i`), so telling headers apart by their sign
 * would hide those lines.
 */
export function* diffRows(diffText: string): Generator<DiffRow> {
  const lines = splitLines(diffText);
  let oldLeft = 0;
  let newLeft = 0;
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] as string;
    index++;
    const header = HUNK_HEADER.exec(line);
    if (header) {
      oldLeft = header[2] !== undefined ? Number(header[2]) : 1;
      newLeft = header[4] !== undefined ? Number(header[4]) : 1;
      yield { kind: "hunk", text: line, header };
    } else if (oldLeft > 0 || newLeft > 0) {
      if (line.startsWith("\\")) continue; // "\ No newline at end of file" belongs to no side
      if (line.startsWith("-")) oldLeft--;
      else if (line.startsWith("+")) newLeft--;
      else {
        oldLeft--;
        newLeft--;
      }
      yield { kind: "line", text: line, header: null };
    } else if (
      line.startsWith("--- ") &&
      index < lines.length &&
      (lines[index] as string).startsWith("+++ ")
    ) {
      yield {
        kind: "file",
        text: fileName(line.slice(4), (lines[index] as string).slice(4)),
        header: null,
      };
      index++;
    }
  }
}

/** The path a `---`/`+++` pair is about, without git's `a/`/`b/` prefixes or a timestamp. */
function fileName(oldSide: string, newSide: string): string {
  let old = oldSide.split("\t")[0] as string;
  let fresh = newSide.split("\t")[0] as string;
  const sides: [string, string][] = [
    [old, "a/"],
    [fresh, "b/"],
  ];
  if (sides.every(([side, prefix]) => side.startsWith(prefix) || side === "/dev/null")) {
    [old, fresh] = sides.map(([side]) => (side === "/dev/null" ? side : side.slice(2))) as [
      string,
      string,
    ];
  }
  return fresh === "/dev/null" ? old : fresh;
}

/** Added and removed line counts, ignoring the `+++`/`---` file headers. */
export function countChanges(diffText: string): [number, number] {
  let added = 0;
  let removed = 0;
  for (const row of diffRows(diffText)) {
    if (row.kind === "line" && row.text.startsWith("+")) added++;
    else if (row.kind === "line" && row.text.startsWith("-")) removed++;
  }
  return [added, removed];
}

/** Diff for the TUI edit block and the permission prompt - the user sees the
 * exact change before approving it. */
export function unifiedDiffOf(before: string, after: string, path: string): string {
  return unifiedDiff(splitLinesKeepEnds(before), splitLinesKeepEnds(after), path, path, 3);
}

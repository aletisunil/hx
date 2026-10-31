/** Read tool. */

import { closeSync, existsSync, openSync, readFileSync, readSync, statSync } from "node:fs";
import { describe, ImageError, isImagePath, loadImageFile, sniffImage } from "../core/images.ts";
import { expandUser, isDir, join } from "../pathlib.ts";
import { osErrorText, purePath, pyLen, pySlice, toInt } from "../py.ts";
import { count } from "../text.ts";
import { compute, split } from "./anchors.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";

const DESCRIPTION = `Read a file from the local filesystem.

Returns numbered lines, each labelled with a short content anchor:
\`   12 a3f9\\tcontent\`. Pass those anchors to Edit's \`hashline\` argument to
replace a span without retyping it. Use \`offset\`/\`limit\` for large files.
Prefer reading the part you need over reading a whole large file.

An image file (PNG, JPEG, GIF, WebP, BMP, TIFF) is returned as the image
itself, for you to look at - screenshots, diagrams, mockups.`;

const DEFAULT_LINE_LIMIT = 2000;
/** Individual lines longer than this are truncated - a minified bundle on one
 * line would otherwise blow the window in a single read. */
const MAX_LINE_LENGTH = 2000;
const BINARY_SNIFF_BYTES = 8192;

export class ReadTool extends Tool {
  readonly name = "Read";
  readonly description = DESCRIPTION;

  schema() {
    return {
      type: "object",
      properties: {
        file_path: { type: "string", description: "Absolute or cwd-relative path" },
        offset: { type: "integer", description: "First line to read (1-based)" },
        limit: { type: "integer", description: "How many lines to read" },
      },
      required: ["file_path"],
    };
  }

  override permissionSpecifier(params: Params): string {
    return String(params.file_path ?? "");
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const path = resolvePath(String(params.file_path), ctx.cwd);
    if (!existsSync(path)) throw new ToolError(`${path}: no such file`);
    if (isDir(path)) throw new ToolError(`${path} is a directory. Use Glob to list its contents.`);
    // Sniffed only when the file is binary anyway, so a text file that happens
    // to open with an image signature is still read as text.
    if (isImagePath(path) || (isBinary(path) && looksLikeImage(path))) return readImage(path);
    if (isBinary(path)) {
      throw new ToolError(
        `${path}: binary file (${count(statSync(path).size, "byte")}), not read as text`,
      );
    }

    const offset = Math.max(toInt(params.offset || 1), 1);
    const limit = toInt(params.limit || DEFAULT_LINE_LIMIT);

    let content: string;
    try {
      content = readExact(path, false);
    } catch (error) {
      throw new ToolError(`${path}: ${osErrorText(error)}`);
    }

    // Split on newlines alone, so the lines an anchor indexes are the ones
    // Edit will reassemble. A line keeps its `\r` for hashing and loses it
    // only on the way to the screen.
    const [lines] = split(content);
    const window = pySlice(lines, offset - 1, offset - 1 + limit);

    let rendered: string;
    if (hashlineEnabled(ctx)) {
      // Anchors are computed over the whole file, not the window: the hash
      // covers each line's neighbours, and Edit resolves against the whole file.
      const [labels] = compute(lines);
      rendered = window
        .map(
          (line, i) =>
            `${String(offset + i).padStart(6)} ${labels[offset + i - 1]}\t${clip(withoutCr(line))}`,
        )
        .join("\n");
    } else {
      rendered = window
        .map((line, i) => `${String(offset + i).padStart(6)}\t${clip(withoutCr(line))}`)
        .join("\n");
    }

    ctx.reads.markRead(path);

    const remaining = lines.length - (offset - 1 + window.length);
    if (remaining > 0) {
      rendered += `\n\n... ${count(remaining, "more line")}. Re-read with offset=${offset + window.length}.`;
    }
    if (window.length === 0)
      rendered = lines.length === 0 ? "(empty file)" : `(no lines at offset ${offset})`;

    return toolResult(rendered, { summary: `read ${count(window.length, "line")}` });
  }
}

/**
 * An image, attached to the result for the model to look at. The text names it
 * too: that line is all a text-only model gets, once the loop has withheld the
 * image, and it is what the transcript shows in place of the picture.
 */
async function readImage(path: string): Promise<ToolResult> {
  let image: Awaited<ReturnType<typeof loadImageFile>>;
  try {
    image = await loadImageFile(path);
  } catch (error) {
    if (error instanceof ImageError) throw new ToolError(`${path}: ${error.message}`);
    throw error;
  }
  return toolResult(`Image ${path} (${describe(image)}), attached.`, {
    summary: `read image ${image.width}x${image.height}`,
    images: [image],
  });
}

/** An image saved without an extension - `screenshot`, a temp file. */
function looksLikeImage(path: string): boolean {
  try {
    return sniffImage(head(path, 16));
  } catch {
    return false;
  }
}

function head(path: string, bytes: number): Buffer {
  const fd = openSync(path, "r");
  try {
    const buffer = Buffer.alloc(bytes);
    const read = readSync(fd, buffer, 0, bytes, 0);
    return buffer.subarray(0, read);
  } finally {
    closeSync(fd);
  }
}

/** A CRLF file's `\r` is part of the line but not part of what to print. */
const withoutCr = (line: string) => (line.endsWith("\r") ? line.slice(0, -1) : line);

/** Whether anchors are rendered and accepted. */
export function hashlineEnabled(ctx: ToolContext): boolean {
  return ctx.settings?.tools?.hashline ?? true;
}

/**
 * A file's text with its line endings exactly as they are on disk. Read and
 * Edit both read here, so an anchor hashes the same bytes on both sides.
 * `strict` refuses bytes that are not UTF-8; otherwise they read as U+FFFD.
 */
export function readExact(path: string, strict = true): string {
  return new TextDecoder("utf-8", { fatal: strict, ignoreBOM: true }).decode(readFileSync(path));
}

/**
 * Resolve a model-supplied path against the session cwd. Relative paths are
 * common in model output even when the tool asks for absolute ones, so they
 * are accepted rather than rejected.
 */
export function resolvePath(raw: string, cwd: string): string {
  const path = purePath(expandUser(raw));
  return path.startsWith("/") ? path : purePath(join(cwd, path));
}

export function isBinary(path: string): boolean {
  try {
    return head(path, BINARY_SNIFF_BYTES).includes(0);
  } catch {
    return false;
  }
}

function clip(line: string): string {
  if (pyLen(line) <= MAX_LINE_LENGTH) return line;
  return `${[...line].slice(0, MAX_LINE_LENGTH).join("")} … [line truncated]`;
}

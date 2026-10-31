/**
 * Content-hash line anchors.
 *
 * `Read` labels every line with a short hash and `Edit` accepts those hashes in
 * place of retyped line content. The model points at a span instead of copying
 * it, which is both cheaper and safer: an anchor that no longer resolves means
 * the file moved under the model, so the patch is rejected rather than applied
 * to whatever now sits at those coordinates.
 *
 * The hash covers a three-line window - the line and its two neighbours - not
 * the line alone, so blank lines get usable anchors too.
 */

import { createHash } from "node:crypto";
import { repr, strip } from "../py.ts";

/** Anchor widths tried in order. The narrowest that adds no collision wins. */
export const WIDTHS = [4, 6, 8] as const;

/** Marks which of the three lines is the anchored one, so a missing neighbour
 * is an empty field rather than a shorter window. */
const SEPARATOR = "\x00";

/**
 * Split into lines on `\n` alone, and say whether the file ended with one.
 * Splitting on every line break Python knows would rewrite each of them on the
 * way back; Read and Edit both split here, so an anchor indexes the same line
 * on both sides.
 */
export function split(content: string): [string[], boolean] {
  const lines = content.split("\n");
  const trailing = lines.length > 0 && lines.at(-1) === "";
  if (trailing) lines.pop();
  return [lines, trailing];
}

/** The hashed neighbourhood of `lines[index]`: the line and its neighbours. */
export function window(lines: string[], index: number): string {
  const previous = index > 0 ? (lines[index - 1] as string) : "";
  const following = index + 1 < lines.length ? (lines[index + 1] as string) : "";
  return [previous, lines[index] as string, following].join(SEPARATOR);
}

export function digests(lines: string[]): string[] {
  return lines.map((_, i) => createHash("sha1").update(window(lines, i), "utf8").digest("hex"));
}

/** Narrowest width in `WIDTHS` that introduces no *new* collision. */
export function anchorWidth(full: string[]): number {
  const distinct = new Set(full).size;
  for (const width of WIDTHS) {
    if (new Set(full.map((value) => value.slice(0, width))).size === distinct) return width;
  }
  return full.length ? (full[0] as string).length : WIDTHS[0];
}

/** Anchors for every line, and the width they were rendered at. */
export function compute(lines: string[]): [string[], number] {
  if (lines.length === 0) return [[], WIDTHS[0]];
  const full = digests(lines);
  const width = anchorWidth(full);
  return [full.map((value) => value.slice(0, width)), width];
}

/** The zero-based index the anchor points at. Throws `AnchorError` when it
 * matches no line, or more than one. */
export function resolveAnchor(lines: string[], anchor: string): number {
  const cleaned = strip(anchor).replace(/^#+/, "");
  if (!cleaned) throw new AnchorError("empty anchor");
  const full = digests(lines);
  const matches = full.flatMap((value, i) =>
    value.slice(0, cleaned.length) === cleaned ? [i] : [],
  );
  if (matches.length === 0) {
    throw new AnchorError(
      `anchor ${repr(cleaned)} does not match any line - the file changed since it ` +
        "was read. Read it again to get current anchors.",
    );
  }
  if (matches.length > 1) {
    const where = matches
      .slice(0, 5)
      .map((i) => String(i + 1))
      .join(", ");
    throw new AnchorError(
      `anchor ${repr(cleaned)} is ambiguous: it matches lines ${where}. ` +
        "Use old_string for this edit, or span a wider range.",
    );
  }
  return matches[0] as number;
}

/** Replace `lines[start..end]` with the `replacement` lines. */
export function replaceSpan(
  lines: string[],
  start: number,
  end: number,
  replacement: string[],
): string[] {
  return [...lines.slice(0, start), ...replacement, ...lines.slice(end + 1)];
}

/** An anchor that does not resolve to exactly one line. */
export class AnchorError extends Error {
  override name = "AnchorError";
}

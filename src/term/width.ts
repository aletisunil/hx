/**
 * How wide is this text, in terminal cells.
 *
 * Every other invariant in the library is enforced with `cellWidth`, so where
 * it disagrees with the terminal the whole layout shifts: a line measured short
 * leaves stale characters behind it, and a line measured long wraps and
 * cascades down the screen.
 *
 * Three things make a character's width not its length:
 *
 * - **Escape sequences** occupy no cells.
 * - **East Asian Wide and Fullwidth** characters occupy two cells, and
 *   combining marks occupy none - the tables `wcwidth` measured with, generated
 *   into `unicode-tables.ts`.
 * - **Grapheme clusters** - a base character plus the marks, joiners, variation
 *   selectors and modifiers that combine with it - are drawn as a single glyph.
 */

import { CONTROL, MARKS, WIDE, ZERO } from "./unicode-tables.ts";

/** Cells a tab occupies. Terminals disagree and none of them ask; three is
 * narrow enough that a mismeasured tab cannot push a line over the width. */
export const TAB_WIDTH = 3;

const ANSI_SOURCE =
  "\\x1b\\[[0-?]*[ -/]*[@-~]" + // CSI - colours, cursor moves
  "|\\x1b\\][\\s\\S]*?(?:\\x07|\\x1b\\\\)" + // OSC - hyperlinks, titles
  "|\\x1b_[\\s\\S]*?(?:\\x07|\\x1b\\\\)" + // APC - our own cursor marker
  "|\\x1bP[\\s\\S]*?(?:\\x07|\\x1b\\\\)" + // DCS
  "|\\x1b[@-Z\\\\-_]"; // two-character escapes

const ANSI = new RegExp(ANSI_SOURCE, "g");
const ANSI_AT = new RegExp(ANSI_SOURCE, "y");

const ZWJ = "‍";
/** Request the text presentation: narrow. */
const VS15 = "︎";
/** Request the emoji presentation: wide. */
const VS16 = "️";
const KEYCAP = "⃣";

const PRINTABLE_ASCII = /^[\x20-\x7e]*$/;

/** The text without any escape sequence, which is what the user sees. */
export function stripAnsi(text: string): string {
  return text.replace(ANSI, "");
}

/** Whether `cp` falls in a flat `[start, end, start, end, ...]` range table. */
function inTable(table: Uint32Array, cp: number): boolean {
  let lo = 0;
  let hi = table.length / 2 - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (cp < (table[mid * 2] as number)) hi = mid - 1;
    else if (cp > (table[mid * 2 + 1] as number)) lo = mid + 1;
    else return true;
  }
  return false;
}

/** `wcwidth(char)`: 0, 1, 2, or -1 for a character the terminal will not print. */
export function wcwidth(cp: number): number {
  if (cp >= 0x20 && cp < 0x7f) return 1;
  if (inTable(CONTROL, cp)) return -1;
  if (inTable(ZERO, cp)) return 0;
  if (inTable(WIDE, cp)) return 2;
  return 1;
}

const isRegionalIndicator = (cp: number) => cp >= 0x1f1e6 && cp <= 0x1f1ff;
const isSkinTone = (cp: number) => cp >= 0x1f3fb && cp <= 0x1f3ff;

/** Does this code point attach to the cluster before it? Mn/Me are always
 * combining; Mc is a spacing mark some scripts use as part of the same glyph. */
function extendsCluster(char: string): boolean {
  if (char === ZWJ || char === VS15 || char === VS16 || char === KEYCAP) return true;
  const cp = char.codePointAt(0) as number;
  return isSkinTone(cp) || inTable(MARKS, cp);
}

/**
 * Split into what the terminal draws as one glyph each. Deliberately not a full
 * UAX #29 implementation: it covers combining marks, emoji with modifiers and
 * variation selectors, ZWJ sequences, flags and keycaps, and treats anything
 * else as its own cluster.
 */
export function* graphemeClusters(text: string): Generator<string> {
  const chars = [...text];
  let index = 0;
  while (index < chars.length) {
    const start = index;
    const char = chars[index] as string;
    index++;
    // A flag is exactly two regional indicators; a third starts a new one.
    if (isRegionalIndicator(char.codePointAt(0) as number)) {
      if (
        index < chars.length &&
        isRegionalIndicator((chars[index] as string).codePointAt(0) as number)
      )
        index++;
      yield chars.slice(start, index).join("");
      continue;
    }
    while (index < chars.length) {
      const following = chars[index] as string;
      if (!extendsCluster(following)) break;
      index++;
      // A joiner is only a joiner if something follows it to join to.
      if (following === ZWJ && index < chars.length) index++;
    }
    yield chars.slice(start, index).join("");
  }
}

/** Cells one glyph occupies. */
function clusterWidth(cluster: string): number {
  // The emoji presentation is always wide, even where the base character alone
  // is narrow (⚠ and ⚠️ are different widths).
  if (cluster.includes(VS16) || cluster.includes(KEYCAP)) return 2;
  const first = cluster.codePointAt(0) as number;
  if ([...cluster].length >= 2 && isRegionalIndicator(first)) return 2;
  const base = wcwidth(first);
  // A control character the terminal will not print: measuring short only
  // under-fills a line, while measuring long wraps it.
  if (base < 0) return 0;
  if (cluster.includes(VS15)) return 1;
  return base;
}

const MEASURED = new Map<string, number>();

function measure(text: string): number {
  const known = MEASURED.get(text);
  if (known !== undefined) return known;
  let total = 0;
  for (const cluster of graphemeClusters(text)) total += clusterWidth(cluster);
  if (MEASURED.size >= 8192) MEASURED.delete(MEASURED.keys().next().value as string);
  MEASURED.set(text, total);
  return total;
}

/**
 * Columns `text` occupies once drawn, ignoring any styling in it. Called on
 * every line of every frame, so printable ASCII - the overwhelming majority of
 * a transcript - never reaches the Unicode tables at all.
 */
export function cellWidth(text: string): number {
  if (PRINTABLE_ASCII.test(text)) return text.length;
  let visible = stripAnsi(text);
  if (PRINTABLE_ASCII.test(visible)) return visible.length;
  if (visible.includes("\t")) visible = visible.replaceAll("\t", " ".repeat(TAB_WIDTH));
  return measure(visible);
}

/**
 * The longest prefix of `text` that fits, cutting between glyphs. Styling is
 * carried along, and a wide glyph that would straddle the edge is dropped
 * rather than half-drawn.
 */
export function truncateToWidth(text: string, width: number): string {
  if (width <= 0) return "";
  if (PRINTABLE_ASCII.test(text)) return text.slice(0, width);
  let out = "";
  let used = 0;
  let index = 0;
  while (index < text.length) {
    ANSI_AT.lastIndex = index;
    const sequence = ANSI_AT.exec(text);
    if (sequence) {
      out += sequence[0];
      index = ANSI_AT.lastIndex;
      continue;
    }
    // Measure one glyph at a time so a cluster is never split.
    const remainder = text.slice(index);
    const cluster = graphemeClusters(remainder).next().value ?? remainder.slice(0, 1);
    const step = clusterWidth(cluster);
    if (used + step > width) break;
    out += cluster;
    used += step;
    index += cluster.length;
  }
  return out;
}

/** `text` padded with spaces to exactly `width` cells. Over-wide text is
 * truncated: one wrapped line pushes every line below it down forever. */
export function padToWidth(text: string, width: number): string {
  let used = cellWidth(text);
  let fitted = text;
  if (used > width) {
    fitted = truncateToWidth(text, width);
    used = cellWidth(fitted);
  }
  return fitted + " ".repeat(Math.max(0, width - used));
}

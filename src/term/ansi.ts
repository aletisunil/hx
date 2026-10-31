/**
 * Turning colours into escape sequences, and keeping them from leaking.
 *
 * **Reset only what you set.** `fg` ends with `\x1b[39m` and `bg` with
 * `\x1b[49m`, never with `\x1b[0m`. A full reset inside a block with a
 * background punches a hole in it, so each resets only its own channel and a
 * coloured span nests inside a filled block correctly.
 *
 * **Reset everything at the end of a line.** `SEGMENT_RESET` closes any style
 * and any hyperlink: terminals carry SGR state across a newline.
 */

import { pyRound, rstrip } from "../py.ts";
import { cellWidth, graphemeClusters, stripAnsi, truncateToWidth } from "./width.ts";

export type ColorMode = "truecolor" | "256color";

/** Restore the default foreground, leaving any background alone. */
export const FG_RESET = "\x1b[39m";
/** Restore the default background, leaving any foreground alone. */
export const BG_RESET = "\x1b[49m";
/** Ends every rendered line: all attributes off, and any hyperlink closed. */
export const SEGMENT_RESET = "\x1b[0m\x1b]8;;\x07";

/** The eight named slots plus their bright variants, as the theme spells them:
 * whatever the user's terminal palette says. */
const ANSI_SLOTS: Record<string, number | null> = {
  ansi_default: null,
  ansi_black: 0,
  ansi_red: 1,
  ansi_green: 2,
  ansi_yellow: 3,
  ansi_blue: 4,
  ansi_magenta: 5,
  ansi_cyan: 6,
  ansi_white: 7,
  ansi_bright_black: 8,
  ansi_bright_red: 9,
  ansi_bright_green: 10,
  ansi_bright_yellow: 11,
  ansi_bright_blue: 12,
  ansi_bright_magenta: 13,
  ansi_bright_cyan: 14,
  ansi_bright_white: 15,
};

/** The 6x6x6 colour cube's axis values, and the 24-step grey ramp, as xterm defines them. */
const CUBE = [0, 95, 135, 175, 215, 255];
const GREY_START = 8;
const GREY_STEP = 10;
const GREY_COUNT = 24;

const TRUECOLOR_TERMS = new Set([
  "kitty",
  "xterm-kitty",
  "ghostty",
  "wezterm",
  "iterm2",
  "alacritty",
  "contour",
]);

type Rgb = [number, number, number];

export function hexToRgb(color: string): Rgb {
  let value = color.replace(/^#+/, "");
  if (value.length === 3) value = [...value].map((char) => char + char).join("");
  return [
    Number.parseInt(value.slice(0, 2), 16),
    Number.parseInt(value.slice(2, 4), 16),
    Number.parseInt(value.slice(4, 6), 16),
  ];
}

/** Distance weighted the way the eye weights the channels, so a quantised
 * palette stays recognisable. */
function distance(a: Rgb, b: Rgb): number {
  const [dr, dg, db] = [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  return 0.299 * dr * dr + 0.587 * dg * dg + 0.114 * db * db;
}

const NEAREST = new Map<string, number>();

/**
 * The 256-colour index closest to an RGB triple. Grey only wins for a colour
 * that is already near-neutral: otherwise a muted blue-grey snaps to grey and
 * the theme loses its tint.
 */
export function rgbTo256(red: number, green: number, blue: number): number {
  const key = `${red},${green},${blue}`;
  const known = NEAREST.get(key);
  if (known !== undefined) return known;
  const target: Rgb = [red, green, blue];
  let bestIndex = 16;
  let bestDistance = Number.POSITIVE_INFINITY;
  CUBE.forEach((r, ri) => {
    CUBE.forEach((g, gi) => {
      CUBE.forEach((b, bi) => {
        const candidate = distance(target, [r, g, b]);
        if (candidate < bestDistance) {
          bestDistance = candidate;
          bestIndex = 16 + 36 * ri + 6 * gi + bi;
        }
      });
    });
  });
  let result = bestIndex;
  if (Math.max(...target) - Math.min(...target) < 10) {
    const level = Math.max(
      0,
      Math.min(GREY_COUNT - 1, pyRound(((red + green + blue) / 3 - GREY_START) / GREY_STEP)),
    );
    const grey = GREY_START + level * GREY_STEP;
    if (distance(target, [grey, grey, grey]) < bestDistance) result = 232 + level;
  }
  NEAREST.set(key, result);
  return result;
}

/**
 * Whether this terminal can be handed 24-bit colour. `COLORTERM` is the
 * standard signal, the well-known truecolour terminals are recognised by name,
 * and `HX_TRUE_COLOR` forces the answer either way.
 */
export function detectColorMode(env: NodeJS.ProcessEnv = process.env): ColorMode {
  const forced = (env.HX_TRUE_COLOR ?? "").trim().toLowerCase();
  if (["1", "true", "yes"].includes(forced)) return "truecolor";
  if (["0", "false", "no"].includes(forced)) return "256color";
  if (["truecolor", "24bit"].includes((env.COLORTERM ?? "").trim().toLowerCase()))
    return "truecolor";
  const termProgram = (env.TERM_PROGRAM ?? "").trim().toLowerCase();
  const term = (env.TERM ?? "").trim().toLowerCase();
  return TRUECOLOR_TERMS.has(termProgram) || TRUECOLOR_TERMS.has(term) ? "truecolor" : "256color";
}

/** The escape that selects `color`, or nothing if it means "leave it". */
function sgr(color: string, mode: ColorMode, background: boolean): string {
  if (!color) return "";
  if (color.startsWith("ansi")) {
    const slot = ANSI_SLOTS[color];
    if (slot === null || slot === undefined) return ""; // the terminal's own choice, already in force
    const base = background ? 40 : 30;
    return slot < 8 ? `\x1b[${base + slot}m` : `\x1b[${base + 60 + slot - 8}m`;
  }
  if (!color.startsWith("#")) return "";
  const [red, green, blue] = hexToRgb(color);
  const channel = background ? 48 : 38;
  if (mode === "truecolor") return `\x1b[${channel};2;${red};${green};${blue}m`;
  return `\x1b[${channel};5;${rgbTo256(red, green, blue)}m`;
}

export interface Style {
  readonly bold?: boolean;
  readonly italic?: boolean;
  readonly underline?: boolean;
}

/** `text` in `color`, resetting only the foreground afterwards. */
export function fg(
  color: string,
  text: string,
  mode: ColorMode = "truecolor",
  style: Style = {},
): string {
  if (!text) return text;
  let start = sgr(color, mode, false);
  let end = start ? FG_RESET : "";
  if (style.bold) [start, end] = [`\x1b[1m${start}`, `${end}\x1b[22m`];
  if (style.italic) [start, end] = [`\x1b[3m${start}`, `${end}\x1b[23m`];
  if (style.underline) [start, end] = [`\x1b[4m${start}`, `${end}\x1b[24m`];
  return `${start}${text}${end}`;
}

/** `text` on `color`, resetting only the background afterwards. */
export function bg(color: string, text: string, mode: ColorMode = "truecolor"): string {
  const start = sgr(color, mode, true);
  return start ? `${start}${text}${BG_RESET}` : text;
}

/** The background escape for `color` alone, for a caller that fills a span itself. */
export const bgCode = (color: string, mode: ColorMode = "truecolor") => sgr(color, mode, true);

/** Swap foreground and background - reserved for the text cursor and the
 * changed words inside a one-line diff, so inversion keeps meaning one thing. */
export const inverse = (text: string) => `\x1b[7m${text}\x1b[27m`;

/** An OSC 8 hyperlink. Terminals that do not support it just show `label`. */
export const hyperlink = (url: string, label: string) => `\x1b]8;;${url}\x07${label}\x1b]8;;\x07`;

/** End a rendered line: styles off, hyperlink closed. Idempotent. */
export const terminate = (line: string) =>
  line.endsWith(SEGMENT_RESET) ? line : line + SEGMENT_RESET;

const ANY_ESCAPE_SOURCE =
  "\\x1b\\[[0-?]*[ -/]*[@-~]|\\x1b\\][\\s\\S]*?(?:\\x07|\\x1b\\\\)|\\x1b_[\\s\\S]*?(?:\\x07|\\x1b\\\\)";
const ANY_ESCAPE = new RegExp(ANY_ESCAPE_SOURCE, "g");
const ANY_ESCAPE_AT = new RegExp(ANY_ESCAPE_SOURCE, "y");
const SGR_PATTERN = /\x1b\[([0-9;]*)m/g;
const SGR_FULL = /^\x1b\[([0-9;]*)m$/;

const codesOf = (params: string) =>
  (params || "0").split(";").map((code) => Number.parseInt(code || "0", 10));

/**
 * The background escape still in force at the end of `text`, for re-opening on
 * the next line of a wrap. A sequence that runs out of parameters part-way is
 * ignored rather than read past the end: malformed is a normal input here.
 */
export function activeBackground(text: string): string {
  let current = "";
  for (const match of text.matchAll(SGR_PATTERN)) {
    const codes = codesOf(match[1] as string);
    let index = 0;
    while (index < codes.length) {
      const code = codes[index] as number;
      if (code === 0 || code === 49) current = "";
      else if ((code >= 40 && code <= 47) || (code >= 100 && code <= 107))
        current = `\x1b[${code}m`;
      else if (code === 48 && index + 1 < codes.length) {
        if (codes[index + 1] === 5 && index + 2 < codes.length) {
          current = `\x1b[48;5;${codes[index + 2]}m`;
          index += 2;
        } else if (codes[index + 1] === 2 && index + 4 < codes.length) {
          current = `\x1b[48;2;${codes[index + 2]};${codes[index + 3]};${codes[index + 4]}m`;
          index += 4;
        } else break; // truncated: nothing legible to re-open
      } else if (code === 38 && index + 1 < codes.length) {
        // A foreground's parameters are not codes.
        const span = codes[index + 1] === 5 ? 3 : codes[index + 1] === 2 ? 5 : 0;
        if (!span || index + span > codes.length) break;
        index += span - 1;
      }
      index++;
    }
  }
  return current;
}

/**
 * `[reopen, close]` for the styling still in force at the end of `text`:
 * foreground, bold, italic, underline and a hyperlink - everything but the
 * background, which `activeBackground` carries.
 */
export function openStyles(text: string): [string, string] {
  let foreground = "";
  let link = "";
  let bold = false;
  let italic = false;
  let underline = false;
  for (const match of text.matchAll(ANY_ESCAPE)) {
    const sequence = match[0];
    if (sequence.startsWith("\x1b]8;")) {
      const second = sequence.indexOf(";", 4);
      const url = second < 0 ? "" : sequence.slice(second + 1);
      link = rstrip(url, "\x07\x1b\\") ? sequence : "";
      continue;
    }
    const sgrMatch = SGR_FULL.exec(sequence);
    if (!sgrMatch) continue;
    const codes = codesOf(sgrMatch[1] as string);
    let index = 0;
    while (index < codes.length) {
      const code = codes[index] as number;
      if (code === 0) [foreground, bold, italic, underline] = ["", false, false, false];
      else if (code === 1) bold = true;
      else if (code === 22) bold = false;
      else if (code === 3) italic = true;
      else if (code === 23) italic = false;
      else if (code === 4) underline = true;
      else if (code === 24) underline = false;
      else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97))
        foreground = `\x1b[${code}m`;
      else if (code === 39) foreground = "";
      else if ((code === 38 || code === 48) && index + 1 < codes.length) {
        const span = codes[index + 1] === 5 ? 3 : codes[index + 1] === 2 ? 5 : 0;
        if (!span || index + span > codes.length) break;
        if (code === 38) foreground = `\x1b[${codes.slice(index, index + span).join(";")}m`;
        index += span - 1;
      }
      index++;
    }
  }
  let reopen = "";
  let close = "";
  if (foreground) [reopen, close] = [reopen + foreground, close + FG_RESET];
  for (const [on, start, end] of [
    [bold, "1", "22"],
    [italic, "3", "23"],
    [underline, "4", "24"],
  ] as const) {
    if (on) [reopen, close] = [`${reopen}\x1b[${start}m`, `${close}\x1b[${end}m`];
  }
  if (link) [reopen, close] = [reopen + link, `${close}\x1b]8;;\x07`];
  return [reopen, close];
}

/**
 * One rendered line: padded to `width`, tinted, and terminated. The padding is
 * inside the background, so a block's tint reaches the right edge.
 */
export function fillLine(text: string, width: number, background = ""): string {
  let fitted = text;
  let used = cellWidth(fitted);
  if (used > width) {
    fitted = truncateToWidth(fitted, width);
    used = cellWidth(fitted);
  }
  let padded = fitted + " ".repeat(Math.max(0, width - used));
  if (background) padded = `${background}${padded}${BG_RESET}`;
  return terminate(padded);
}

/**
 * Word-wrap styled text, carrying the active style across each break. A word
 * longer than the whole width is broken rather than allowed to overflow.
 */
export function wrap(text: string, width: number): string[] {
  if (width <= 0) return [""];
  if (!stripAnsi(text)) return text ? [text] : [""];
  return text.split("\n").flatMap((paragraph) => wrapOne(paragraph, width));
}

type Token = [boolean, string];

/** `[isEscape, chunk]` pairs: escapes, then single glyphs, so a wrap never
 * lands inside a glyph and escapes are carried without being measured. */
export function tokenize(text: string): Token[] {
  const tokens: Token[] = [];
  let index = 0;
  while (index < text.length) {
    ANY_ESCAPE_AT.lastIndex = index;
    const sequence = ANY_ESCAPE_AT.exec(text);
    if (sequence) {
      tokens.push([true, sequence[0]]);
      index = ANY_ESCAPE_AT.lastIndex;
      continue;
    }
    const cluster = graphemeClusters(text.slice(index)).next().value ?? (text[index] as string);
    tokens.push([false, cluster]);
    index += cluster.length;
  }
  return tokens;
}

/**
 * Wrap one paragraph, breaking at spaces where there is one to break at. Every
 * line consumes at least one glyph, so a glyph wider than the whole width ends
 * up alone on its line rather than spinning forever.
 */
function wrapOne(text: string, width: number): string[] {
  if (cellWidth(text) <= width) return [text];
  const lines: string[] = [];
  let line: string[] = [];
  let used = 0;
  // Where the line could be broken instead, and what it had consumed there.
  let breakAt: number | null = null;
  let breakUsed = 0;
  let carry = "";

  const flush = (upto: number | null) => {
    let emitted: string;
    let rest: string[];
    if (upto === null) {
      emitted = line.join("");
      rest = [];
    } else {
      emitted = line.slice(0, upto).join("");
      rest = line.slice(upto + 1); // drop the space itself
    }
    lines.push(carry + emitted);
    carry = activeBackground(carry + emitted) + openStyles(carry + emitted)[0];
    line = rest;
    used = tokenize(rest.join("")).reduce(
      (total, [isEscape, chunk]) => total + (isEscape ? 0 : cellWidth(chunk)),
      0,
    );
    breakAt = null;
    breakUsed = 0;
  };

  for (const [isEscape, chunk] of tokenize(text)) {
    if (isEscape) {
      line.push(chunk);
      continue;
    }
    const step = cellWidth(chunk);
    if (used + step > width && line.length) {
      if (breakAt !== null && breakUsed > 0) flush(breakAt);
      else flush(null);
    }
    if (chunk === " " && line.length) {
      breakAt = line.length;
      breakUsed = used;
    }
    line.push(chunk);
    used += cellWidth(chunk);
  }
  if (line.length) lines.push(carry + line.join(""));
  return lines.length ? lines : [""];
}

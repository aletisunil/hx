/**
 * Making text that came from somewhere else safe to draw.
 *
 * Everything in the terminal library trusts the escape sequences in the strings
 * it is handed - that is what lets a component colour its own output. None of
 * it is true of text HX did not write. A file read by a tool, a command's
 * stdout, a model's reply, a paste - a terminal acts on what it is given:
 * `\x1b]52` writes the clipboard, `\x1b]0` renames the window, `\x1b[2J`, `\r`
 * and `\x08` move or erase what the renderer's diff believes is on screen, and
 * `\x1b_hx:c\x07` is the renderer's own cursor marker.
 *
 * So untrusted text is plain text. Every escape and every control character
 * comes off, including colour. Newlines survive, because they are structure;
 * tabs are expanded here so the width of a line is the same whoever asks.
 */

import { expandTabs } from "../py.ts";
import { TAB_WIDTH } from "./width.ts";

/**
 * Every escape sequence, terminated or not. An unterminated one is dropped to
 * the end of the string: left in place, the next chunk of a streaming result
 * could supply the terminator and reassemble the payload.
 */
const ESCAPES = new RegExp(
  "\\x1b\\][\\s\\S]*?(?:\\x07|\\x1b\\\\|$(?![\\s\\S]))" + // OSC - clipboard, title, links
    "|\\x1b_[\\s\\S]*?(?:\\x07|\\x1b\\\\|$(?![\\s\\S]))" + // APC - our own cursor marker
    "|\\x1bP[\\s\\S]*?(?:\\x07|\\x1b\\\\|$(?![\\s\\S]))" + // DCS
    "|\\x1b\\^[\\s\\S]*?(?:\\x07|\\x1b\\\\|$(?![\\s\\S]))" + // PM
    "|\\x1bX[\\s\\S]*?(?:\\x07|\\x1b\\\\|$(?![\\s\\S]))" + // SOS
    "|\\x1b\\[[0-?]*[ -/]*[@-~]" + // CSI - colour, cursor, erase
    "|\\x1b\\[[0-?]*[ -/]*$(?![\\s\\S])" + // CSI still arriving, or cut short
    "|\\x1b[\\s\\S]" + // two-character escapes
    "|\\x1b$(?![\\s\\S])", // a lone trailing escape
  "g",
);

/** C0 minus newline and tab, DEL, and the C1 range - where a terminal reads
 * 0x9b as CSI and 0x9d as OSC, the same attack without the escape byte. */
const CONTROLS = /[\x00-\x08\x0b-\x1f\x7f-\x9f]/g;

/**
 * `text` with every escape sequence and control character removed. The one
 * function to reach for whenever a string arrives from a tool, a model, a file
 * or the clipboard.
 */
export function plainText(text: string): string {
  if (!text) return text;
  let out = text;
  if (out.includes("\x1b")) out = out.replace(ESCAPES, "");
  if (out.includes("\t")) out = expandTabs(out, TAB_WIDTH);
  return out.replace(CONTROLS, "");
}

/** `text` sanitized and split into lines. */
export const plainLines = (text: string) => plainText(text).split("\n");

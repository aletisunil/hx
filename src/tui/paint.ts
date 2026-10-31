/**
 * Painting theme roles onto text. Everything goes through a role, so `/theme`
 * moves the whole app and a user's theme file reaches every colour.
 */

import * as ansi from "../term/ansi.ts";
import { Painter, type PaintStyle } from "../term/markdown.ts";
import { highlight } from "../term/syntax.ts";
import { THEME } from "./theme.ts";

let mode: ansi.ColorMode | null = null;

/** Truecolour or 256, detected once. */
export function colorMode(): ansi.ColorMode {
  mode ??= ansi.detectColorMode();
  return mode;
}

/** Override the detected depth. `null` re-detects. */
export function setColorMode(next: ansi.ColorMode | null): void {
  mode = next;
}

/** The raw colour for a role, as the palette spells it. */
export const color = (role: string) => THEME.color(role);

/** `text` painted in `role`, resetting only the foreground. */
export const fg = (role: string, text: string, style: PaintStyle = {}) =>
  ansi.fg(THEME.color(role), text, colorMode(), style);

/** `text` on `role`, resetting only the background. */
export const bg = (role: string, text: string) => ansi.bg(THEME.color(role), text, colorMode());

/** A background filler. Backgrounds mean whose turn this is, how a tool call
 * ended, and that a run of text is code - never emphasis. */
export const tint = (role: string) => (line: string) =>
  ansi.bg(THEME.color(role), line, colorMode());

/** A colour for a rule. */
export const rule =
  (role = "border") =>
  (line: string) =>
    ansi.fg(THEME.color(role), line, colorMode());

/** A hyperlinked label. Terminals without OSC 8 just show the label. */
export const link = (url: string, label: string, role = "md_link") =>
  ansi.hyperlink(url, fg(role, label));

/** Highlight code with the active palette's syntax roles. */
export const highlightCode = (
  source: string,
  language: string | null,
  filename: string | null = null,
) => highlight(source, language, color, colorMode(), filename);

/** Bridges the markdown renderer to HX's palette and syntax highlighting. */
export class ThemePainter extends Painter {
  override paint(role: string, text: string, style: PaintStyle = {}): string {
    return fg(role, text, style);
  }

  /** The role as a background, unless the theme leaves it to the terminal. */
  override fill(role: string, text: string): string | null {
    const value = color(role);
    if (!value || value === "ansi_default") return null;
    return bg(role, text);
  }

  override code(source: string, language: string | null): string[] {
    return highlightCode(source, language);
  }
}

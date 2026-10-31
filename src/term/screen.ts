/**
 * Getting the document onto the terminal, touching as little as possible.
 *
 * The document is a flat list of lines, reconciled against the one already on
 * screen, and the shape of the reconciliation is what makes the transcript real
 * scrollback rather than a repainted buffer:
 *
 * - Lines that have scrolled off the top are **never** rewritten.
 * - New content is appended with `\r\n`, which pushes old lines into scrollback.
 * - Only the changed tail is redrawn.
 * - A document shorter than the window is padded above its dock, so the prompt
 *   opens on the bottom row instead of creeping down the screen.
 *
 * A change *above* the visible region cannot be reached by moving the cursor,
 * so it forces a full repaint; the common cause is a resize.
 *
 * `/fullscreen` paints a window of the same document into a fixed rectangle on
 * the alternate screen instead, with the dock pinned to the last line.
 *
 * Every update is wrapped in synchronized-output mode.
 */

import { repr } from "../py.ts";
import { terminate } from "./ansi.ts";
import type { Component } from "./component.ts";
import type { Terminal } from "./terminal.ts";
import { cellWidth, stripAnsi } from "./width.ts";

export const SYNC_START = "\x1b[?2026h";
export const SYNC_END = "\x1b[?2026l";
const ERASE_LINE = "\x1b[2K";
const ERASE_BELOW = "\x1b[J";
const CLEAR_ALL = "\x1b[2J\x1b[H\x1b[3J";
const CURSOR_HIDE = "\x1b[?25l";
const CURSOR_SHOW = "\x1b[?25h";

/**
 * Zero-width marker naming where the hardware cursor belongs. The visible
 * cursor is an inverse-video cell, but an input method's candidate window
 * follows the *hardware* cursor, so it is parked in the right place too.
 */
export const CURSOR_MARKER = "\x1b_hx:c\x07";

/** Bytes per write: a long transcript's first paint is megabytes. */
const WRITE_CHUNK = 1 << 20;

/** A component returned a line wider than the width it was given. Fatal on
 * purpose: one over-wide line wraps, and every later diff is wrong. */
export class LineTooWide extends Error {
  override name = "LineTooWide";
}

/** A component returned a line with a line break inside it. */
export class LineHasBreak extends LineTooWide {
  override name = "LineHasBreak";
}

/** A document whose last rows are pinned to the bottom of the screen. */
interface Docked {
  footerHeight(width: number): number;
}

type Cursor = [number, number] | null;

/** Draws a component tree into the terminal's normal screen. */
export class MainScreen {
  private previous: string[] = [];
  private previousWidth = -1;
  private previousHeight = -1;
  /** Where the hardware cursor sits, as an index into `previous`. */
  private cursorRow = 0;
  private cursorVisible = false;
  private alt = false;
  /** The rectangle currently on the alternate screen, row by row. */
  private painted: string[] = [];
  /** Rows the fullscreen viewport is lifted off the bottom of the document. */
  private scroll = 0;
  /** The frame left on the normal screen when `/fullscreen` covered it, for
   * `close` to erase. */
  private parked: [string[], number] = [[], 0];

  constructor(
    private readonly terminal: Terminal,
    private readonly root: Component,
    private readonly onError: ((report: string) => void) | null = null,
  ) {}

  get fullscreen(): boolean {
    return this.alt;
  }

  /** Move between the scrollback renderer and the fullscreen one. What was on
   * the normal screen is remembered: the alternate screen only covers it. */
  setFullscreen(enabled: boolean): void {
    if (enabled === this.alt) return;
    this.parked = enabled ? [[...this.previous], this.cursorRow] : [[], 0];
    this.alt = enabled;
    this.scroll = 0;
    this.terminal.setAltScreen(enabled);
    this.invalidate();
  }

  /** Forget what is on screen, so the next render paints all of it. */
  invalidate(): void {
    this.previous = [];
    this.previousWidth = -1;
    this.previousHeight = -1;
    this.cursorRow = 0;
  }

  /** Erase the screen, and the scrollback above it, then repaint. */
  clear(): void {
    this.write(CLEAR_ALL);
    this.invalidate();
    this.render();
  }

  // Scrolling, only in fullscreen: on the normal screen the terminal scrolls.

  /** Move the fullscreen viewport, returning whether anything moved. */
  scrollBy(rows: number): boolean {
    if (!this.alt) return false;
    const height = this.terminal.size[1];
    const highest = Math.max(0, this.previous.length - height);
    const target = Math.max(0, Math.min(highest, this.scroll - rows));
    if (target === this.scroll) return false;
    this.scroll = target;
    return true;
  }

  scrollToTop(): boolean {
    if (!this.alt) return false;
    const height = this.terminal.size[1];
    return this.scrollBy(-(Math.max(0, this.previous.length - height) - this.scroll));
  }

  scrollToBottom(): boolean {
    if (!this.alt || this.scroll === 0) return false;
    this.scroll = 0;
    return true;
  }

  /** Reconcile the document with what is on screen. */
  render(): void {
    const [width, height] = this.terminal.size;
    const raw = this.root.render(width).map(terminate);
    let [lines, cursor] = this.extractCursor(
      this.alt ? raw : this.seatFooter(raw, width, height),
      width,
    );
    this.checkWidths(lines, width);

    if (this.alt) {
      this.renderViewport(lines, cursor, width, height);
      return;
    }
    if (this.previous.length === 0) this.paintAll(lines, false);
    else if (width !== this.previousWidth || height !== this.previousHeight) {
      // Wrapping changed, so every line already on screen is wrong.
      this.paintAll(lines, true);
    } else {
      const first = firstDifference(this.previous, lines);
      if (first === null) {
        this.placeCursor(cursor);
        return;
      }
      const viewportTop = Math.max(0, this.previous.length - height);
      if (first < viewportTop) {
        // Above the fold: unreachable by cursor movement. The clear takes the
        // scrollback with it, so the rows the old document held need no keeping.
        [lines, cursor] = this.extractCursor(this.seatFooter(raw, width, height, false), width);
        this.paintAll(lines, true);
      } else this.paintFrom(first, lines);
    }
    this.previous = lines;
    this.previousWidth = width;
    this.previousHeight = height;
    this.placeCursor(cursor);
  }

  /** Paint a window of the document into a fixed rectangle, the dock pinned
   * to the bottom and the transcript scrolling above it. */
  private renderViewport(lines: string[], cursor: Cursor, width: number, height: number): void {
    const footer = this.footerHeight(width);
    const body = lines.slice(0, Math.max(0, lines.length - footer));
    const tail = lines.slice(body.length);
    const room = Math.max(0, height - tail.length);
    const highest = Math.max(0, body.length - room);
    this.scroll = Math.min(this.scroll, highest);
    const end = body.length - this.scroll;
    const start = Math.max(0, end - room);
    let window = body.slice(start, end);
    // Short conversation: the gap goes between it and the dock.
    window = [...window, ...Array<string>(room - window.length).fill("")];
    const frame = [...window, ...tail].slice(0, height);

    const repaint = width !== this.previousWidth || height !== this.previousHeight;
    const out = [SYNC_START, CURSOR_HIDE];
    if (repaint) out.push(CLEAR_ALL);
    frame.forEach((line, row) => {
      if (!repaint && row < this.painted.length && this.painted[row] === line) return;
      out.push(`\x1b[${row + 1};1H${ERASE_LINE}${line}`);
    });
    out.push(SYNC_END);
    this.write(out.join(""));
    this.previous = lines;
    this.previousWidth = width;
    this.previousHeight = height;
    this.painted = frame;
    this.placeCursorInFrame(cursor, body.length, start, room, height);
  }

  /**
   * Blank rows between the conversation and the dock, so the dock sits on the
   * bottom row of a screen the conversation has not filled yet. Rows that have
   * scrolled off the top cannot be handed back, so a document that shrinks
   * keeps the rows it has and the space opens above the dock.
   */
  private seatFooter(
    lines: string[],
    width: number,
    height: number,
    keepPrevious = true,
  ): string[] {
    const footer = this.footerHeight(width);
    if (footer <= 0 || footer >= height) return lines;
    let target = height;
    if (
      keepPrevious &&
      this.previous.length &&
      width === this.previousWidth &&
      height === this.previousHeight
    ) {
      target = Math.max(target, this.previous.length);
    }
    const missing = target - lines.length;
    if (missing <= 0) return lines;
    const split = Math.max(0, lines.length - footer);
    return [...lines.slice(0, split), ...Array<string>(missing).fill(""), ...lines.slice(split)];
  }

  /** Rows at the end of the document that stay on the bottom row, asked of
   * the document: what is pinned is a property of the UI. */
  private footerHeight(width: number): number {
    const measure = (this.root as Partial<Docked>).footerHeight;
    if (measure === undefined) return 0;
    try {
      return Math.max(0, Math.trunc(measure.call(this.root, width)));
    } catch {
      return 0;
    }
  }

  /** Put the hardware cursor where its document row landed on screen. */
  private placeCursorInFrame(
    cursor: Cursor,
    bodyRows: number,
    start: number,
    room: number,
    height: number,
  ): void {
    const hide = () => {
      if (this.cursorVisible) {
        this.write(CURSOR_HIDE);
        this.cursorVisible = false;
      }
    };
    if (cursor === null) {
      hide();
      return;
    }
    const [documentRow, column] = cursor;
    let row: number;
    if (documentRow >= bodyRows)
      row = room + (documentRow - bodyRows); // in the pinned dock
    else if (documentRow >= start && documentRow < start + room) row = documentRow - start;
    else {
      hide(); // scrolled out of the window
      return;
    }
    row = Math.max(0, Math.min(height - 1, row));
    this.cursorRow = row;
    this.write(`\x1b[${row + 1};${column + 1}H${CURSOR_SHOW}`);
    this.cursorVisible = true;
  }

  private paintAll(lines: string[], clear: boolean): void {
    const out = [SYNC_START, CURSOR_HIDE];
    if (clear) out.push(CLEAR_ALL);
    out.push("\r");
    lines.forEach((line, index) => {
      if (index) out.push("\r\n");
      out.push(ERASE_LINE, line);
    });
    out.push(ERASE_BELOW, SYNC_END);
    this.cursorRow = Math.max(0, lines.length - 1);
    this.write(out.join(""));
  }

  /** Rewrite from the first changed line to the end of the document - the
   * tail is short in the case that matters, the dock at the bottom. */
  private paintFrom(first: number, lines: string[]): void {
    const out = [SYNC_START, CURSOR_HIDE, this.moveTo(first)];
    for (let index = first; index < lines.length; index++) {
      if (index > first) out.push("\r\n");
      out.push(ERASE_LINE, lines[index] as string);
    }
    // The document shrank; the lines it used to occupy are still there.
    if (lines.length < this.previous.length) out.push(ERASE_BELOW);
    out.push(SYNC_END);
    this.cursorRow = Math.max(first, lines.length - 1);
    this.write(out.join(""));
  }

  /** Cursor movement from where it is to the start of `row`. Downwards by
   * `\r\n`, which can scroll, never `CSI B`, which cannot. */
  private moveTo(row: number): string {
    const delta = row - this.cursorRow;
    if (delta < 0) return `\x1b[${-delta}A\r`;
    if (delta > 0) return `${"\r\n".repeat(delta)}\r`;
    return "\r";
  }

  /** Pull the cursor marker out, remembering where it was. */
  private extractCursor(lines: string[], width: number): [string[], Cursor] {
    let position: Cursor = null;
    const cleaned = lines.map((line, row) => {
      const at = line.indexOf(CURSOR_MARKER);
      if (at < 0) return line;
      position = [row, Math.min(cellWidth(line.slice(0, at)), Math.max(0, width - 1))];
      return line.replaceAll(CURSOR_MARKER, "");
    });
    return [cleaned, position];
  }

  private placeCursor(cursor: Cursor): void {
    if (cursor === null) {
      if (this.cursorVisible) {
        this.write(CURSOR_HIDE);
        this.cursorVisible = false;
      }
      return;
    }
    const [row, column] = cursor;
    const movement = this.moveTo(row);
    this.cursorRow = row;
    this.write(movement + (column ? `\x1b[${column}C` : "") + CURSOR_SHOW);
    this.cursorVisible = true;
  }

  /**
   * Take the frame off the screen, leaving the terminal as it was found. The
   * erase stops at the top of the window: lines above it are scrollback, and
   * the only sequence that reaches them takes all of it. A session that ends
   * in fullscreen leaves the alternate screen here, and erases the frame it
   * parked on the normal screen.
   */
  close(): void {
    if (this.alt) {
      this.alt = false;
      this.terminal.setAltScreen(false);
      [this.previous, this.cursorRow] = this.parked;
      this.parked = [[], 0];
    }
    if (this.previous.length === 0) return;
    const height = this.terminal.size[1];
    const top = Math.max(0, this.previous.length - height);
    this.write(this.moveTo(top) + ERASE_BELOW + CURSOR_SHOW);
    this.cursorRow = top;
    this.cursorVisible = true;
    this.previous = [];
  }

  private checkWidths(lines: string[], width: number): void {
    lines.forEach((line, index) => {
      if (line.includes("\n") || line.includes("\r")) {
        // Worse than too wide: the row count every later diff relies on is off.
        const report = `line ${index} contains a line break: ${repr([...stripAnsi(line)].slice(0, 200).join(""))}`;
        this.onError?.(report);
        throw new LineHasBreak(report);
      }
      const measured = cellWidth(line);
      if (measured <= width) return;
      const report = widthReport(lines, index, measured, width);
      this.onError?.(report);
      throw new LineTooWide(report);
    });
  }

  private write(data: string): void {
    for (let start = 0; start < data.length; start += WRITE_CHUNK)
      this.terminal.write(data.slice(start, start + WRITE_CHUNK));
  }
}

function widthReport(lines: string[], index: number, measured: number, width: number): string {
  const from = Math.max(0, index - 2);
  const rows = lines
    .slice(from, index + 3)
    .map(
      (line, offset) =>
        `  ${String(from + offset).padStart(4)}  ${String(cellWidth(line)).padStart(4)}  ${repr(stripAnsi(line))}`,
    );
  return [
    `line ${index} is ${measured} cells wide at width ${width}`,
    `  ${"row".padStart(4)}  ${"cells".padStart(4)}  text`,
    ...rows,
  ].join("\n");
}

/** Index of the first line that changed, or `null` if none did. */
function firstDifference(before: string[], after: string[]): number | null {
  const shorter = Math.min(before.length, after.length);
  for (let index = 0; index < shorter; index++) if (before[index] !== after[index]) return index;
  return before.length !== after.length ? shorter : null;
}

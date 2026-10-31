/**
 * The multi-line prompt.
 *
 * The cursor is drawn as an **inverse-video cell** rather than left to the
 * terminal's own cursor, which cannot composite with a background fill. The
 * grapheme under it is inverted, or a space at end of line - and the width
 * bookkeeping differs between those cases, because appending costs a column.
 *
 * It also tells the terminal where the **hardware** cursor belongs, via a
 * zero-width marker the screen strips out, so an input method's candidate
 * window opens beside the text.
 *
 * The editor owns its frame - a rule above and a rule below, no verticals - and
 * draws any completion list *above* it, so the dock pinned to the bottom row
 * does not jump with every keystroke.
 */

import { fillLine, inverse } from "./ansi.ts";
import { TextBuffer } from "./buffer.ts";
import { Widget } from "./component.ts";
import { LabelledRule, Rule } from "./primitives.ts";
import { CURSOR_MARKER } from "./screen.ts";
import { UndoStack } from "./undo.ts";
import { cellWidth, graphemeClusters, truncateToWidth } from "./width.ts";

/** Rows the draft is always allowed, however short the terminal. */
const MIN_VISIBLE_LINES = 5;
/** Share of the terminal a draft may grow to before it scrolls internally. */
const VISIBLE_FRACTION = 0.3;

/** One visual row: its text, and where the cursor sits in it, if it does. */
export interface LayoutLine {
  readonly text: string;
  readonly cursorColumn: number | null;
}

/**
 * Map the buffer onto visual rows, wrapping to `width` between words. The rows
 * of a line are exactly its text, cut up, so a buffer offset maps onto a row
 * and a column by counting alone.
 */
export function layout(text: string, width: number, cursor: number): LayoutLine[] {
  const w = Math.max(1, width);
  const rows: LayoutLine[] = [];
  let offset = 0;
  for (const logical of text.split("\n")) {
    const chunks = wrapWords(logical, w);
    chunks.forEach((chunk, index) => {
      const length = chunk.length;
      let column: number | null = null;
      // A cursor at a break belongs to the row it continues onto.
      const last = index === chunks.length - 1;
      if ((cursor >= offset && cursor < offset + length) || (cursor === offset + length && last)) {
        column = cellWidth(chunk.slice(0, cursor - offset));
      }
      rows.push({ text: chunk, cursorColumn: column });
      offset += length;
    });
    offset += 1; // the newline itself
  }
  return rows;
}

/**
 * Break a logical line into rows of at most `width` cells, after the last space
 * that fits. The space a break falls on may hang one cell past `width`; a word
 * too long for any row breaks where it hits the edge.
 */
function wrapWords(line: string, width: number): string[] {
  if (!line) return [""];
  const clusters = [...graphemeClusters(line)];
  const rows: string[] = [];
  let start = 0;
  let used = 0;
  // Where the row may end: just past a space with a word before it.
  let fold: number | null = null;
  let worded = false;
  let index = 0;
  while (index < clusters.length) {
    const cluster = clusters[index] as string;
    const step = cellWidth(cluster);
    if (used + step > width && index > start) {
      let end: number;
      if (cluster === " " && worded && used === width)
        end = index + 1; // hang it
      else if (fold !== null) end = fold;
      else end = index;
      rows.push(clusters.slice(start, end).join(""));
      start = end;
      fold = null;
      const pending = clusters.slice(start, index);
      used = pending.reduce((total, c) => total + cellWidth(c), 0);
      worded = pending.some((c) => c !== " ");
      if (end > index) index = end;
      continue;
    }
    if (cluster === " ") {
      if (worded) fold = index + 1;
    } else worded = true;
    used += step;
    index++;
  }
  rows.push(clusters.slice(start).join(""));
  return rows;
}

/** An editable draft, framed by two rules. */
export class Editor extends Widget {
  readonly buffer = new TextBuffer();
  readonly undoStack = new UndoStack();
  placeholder = "";
  private scrollTop = 0;
  /** Labelled, because the top rule doubles as the working indicator: a turn
   * starting costs no layout. */
  readonly topRule: LabelledRule;
  readonly bottomRule: Rule;
  /** Extra rows drawn above the editor, inside its own line array. */
  completions: string[] = [];

  constructor(
    protected readonly paddingX = 1,
    ruleColor: ((text: string) => string) | null = null,
    private readonly rowsAvailable: () => number = () => 24,
  ) {
    super();
    this.topRule = new LabelledRule(ruleColor, "", "left");
    this.bottomRule = new Rule(ruleColor);
  }

  get text(): string {
    return this.buffer.text;
  }

  set text(value: string) {
    this.buffer.set(value);
    this.invalidate();
  }

  clear(): void {
    this.buffer.clear();
    this.undoStack.breakRun();
    this.invalidate();
  }

  insert(text: string, coalesce = false): void {
    this.undoStack.record(this.buffer.text, this.buffer.cursor, coalesce);
    this.buffer.insert(text);
    this.invalidate();
  }

  /** Put a status into the top rule, or clear it with an empty string. */
  setStatus(label: string): void {
    this.topRule.setLabel(label);
    this.invalidate();
  }

  /** Rows to draw above the frame - the completion list, or nothing. */
  setCompletions(lines: string[]): void {
    this.completions = lines;
    this.invalidate();
  }

  get maxVisible(): number {
    return Math.max(MIN_VISIBLE_LINES, Math.trunc(this.rowsAvailable() * VISIBLE_FRACTION));
  }

  draw(width: number): string[] {
    const inner = Math.max(1, width - 2 * this.paddingX);
    const rows = this.rows(inner);
    const visible = this.scrolled(rows);
    const pad = " ".repeat(this.paddingX);
    const body = visible.map((row) => fillLine(pad + this.paint(row, inner), width));
    const hiddenAbove = this.scrollTop;
    const hiddenBelow = rows.length - this.scrollTop - visible.length;
    return [
      ...this.completions.map((line) => fillLine(pad + line, width)),
      ...this.rule(this.topRule, width, hiddenAbove, "↑"),
      ...body,
      ...this.rule(this.bottomRule, width, hiddenBelow, "↓"),
    ];
  }

  private rows(inner: number): LayoutLine[] {
    if (!this.buffer.text && this.placeholder) return [{ text: this.placeholder, cursorColumn: 0 }];
    return layout(this.buffer.text, inner, this.buffer.cursor);
  }

  /** Keep the cursor's row on screen, then clamp. */
  private scrolled(rows: LayoutLine[]): LayoutLine[] {
    const limit = this.maxVisible;
    if (rows.length <= limit) {
      this.scrollTop = 0;
      return rows;
    }
    const found = rows.findIndex((row) => row.cursorColumn !== null);
    const cursorRow = Math.max(0, found);
    if (cursorRow < this.scrollTop) this.scrollTop = cursorRow;
    else if (cursorRow >= this.scrollTop + limit) this.scrollTop = cursorRow - limit + 1;
    this.scrollTop = Math.max(0, Math.min(this.scrollTop, rows.length - limit));
    return rows.slice(this.scrollTop, this.scrollTop + limit);
  }

  /** A rule, carrying an overflow count when the draft scrolls. */
  private rule(rule: Rule, width: number, hidden: number, arrow: string): string[] {
    if (hidden <= 0) return rule.render(width);
    return new LabelledRule(rule.paint, `${arrow} ${hidden} more`, "center").render(width);
  }

  /** One visual row, with the cursor drawn into it. */
  private paint(row: LayoutLine, inner: number): string {
    const placeholder = !this.buffer.text && this.placeholder;
    if (row.cursorColumn === null) return truncateToWidth(row.text, inner);
    // The cursor may sit one cell past the text column, in the right-hand
    // padding, rather than over the text.
    const room = inner + Math.min(1, this.paddingX);
    let consumed = 0;
    let head = "";
    let under = "";
    let after = "";
    for (const cluster of graphemeClusters(row.text)) {
      if (consumed < row.cursorColumn) head += cluster;
      else if (!under) under = cluster;
      else after += cluster;
      consumed += cellWidth(cluster);
    }
    // Nothing typed: the cursor sits before the hint.
    if (placeholder)
      return CURSOR_MARKER + inverse(" ") + truncateToWidth(row.text, Math.max(0, inner - 1));
    if (under) return truncateToWidth(head + CURSOR_MARKER + inverse(under) + after, room);
    // At end of line the cursor is an appended cell, which costs a column.
    return truncateToWidth(head, Math.max(0, room - 1)) + CURSOR_MARKER + inverse(" ");
  }

  killRange(start: number, end: number): string {
    this.undoStack.record(this.buffer.text, this.buffer.cursor);
    const killed = this.buffer.deleteRange(start, end);
    this.invalidate();
    return killed;
  }

  undo(): boolean {
    const snapshot = this.undoStack.undo(this.buffer.text, this.buffer.cursor);
    if (snapshot === null) return false;
    this.buffer.set(snapshot.text, snapshot.cursor);
    this.invalidate();
    return true;
  }

  redo(): boolean {
    const snapshot = this.undoStack.redo(this.buffer.text, this.buffer.cursor);
    if (snapshot === null) return false;
    this.buffer.set(snapshot.text, snapshot.cursor);
    this.invalidate();
    return true;
  }
}

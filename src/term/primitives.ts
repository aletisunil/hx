/**
 * The whole vocabulary the UI is built from: one way to draw a line of text,
 * one way to tint a block, one way to separate two things vertically, and one
 * way to draw a rule - and no box, anywhere.
 */

import { fillLine, SEGMENT_RESET, terminate, tokenize, wrap } from "./ansi.ts";
import { type Component, Widget } from "./component.ts";
import { cellWidth, stripAnsi, truncateToWidth } from "./width.ts";

/** Wraps a line in a background. */
export type Tint = (line: string) => string;

/** Where Enter will land. Two cells, so unselected rows align under it. */
export const CURSOR = "→ ";
/** The value already in force. Sits beside `CURSOR`, never instead of it. */
export const CURRENT = "✓ ";
/** An unselected row, occupying exactly what a marker would. */
export const GUTTER = "  ";

/** One run of text, wrapped to the width and padded out to it. */
export class Text extends Widget {
  constructor(
    private content = "",
    private readonly paddingX = 1,
    private readonly paddingY = 0,
    private readonly tint: Tint | null = null,
  ) {
    super();
  }

  get text(): string {
    return this.content;
  }

  setText(text: string): void {
    if (text !== this.content) {
      this.content = text;
      this.invalidate();
    }
  }

  draw(width: number): string[] {
    const inner = Math.max(1, width - 2 * this.paddingX);
    const pad = " ".repeat(this.paddingX);
    const body = wrap(this.content, inner).map((line) => `${pad}${line}`);
    const blank = Array<string>(this.paddingY).fill("");
    return [...blank, ...body, ...blank].map((line) =>
      this.tint === null ? fillLine(line, width) : terminate(this.tint(padTo(line, width))),
    );
  }
}

/** A row laid out in columns: a label, two or more spaces, then its text -
 * not a sentence ending in `.`, `!` or `?` followed by two spaces. */
const COLUMN = /^(\s*\S.*?[^\s.!?]\s{2,})\S/;

/** Wrap one line, continuing under its own last column or indent rather than
 * at the margin. */
function wrapAligned(paragraph: string, width: number): string[] {
  const lines = wrap(paragraph, width);
  if (lines.length < 2) return lines;
  const plain = stripAnsi(paragraph);
  const column = COLUMN.exec(plain);
  const lead = column
    ? cellWidth(column[1] as string)
    : plain.length - plain.replace(/^ +/, "").length;
  if (!(lead > 0 && lead <= Math.floor(width / 2))) return lines;
  const [head, body] = splitAtCell(paragraph, lead);
  const rest = wrap(body, width - lead);
  return [head + rest[0], ...rest.slice(1).map((line) => " ".repeat(lead) + line)];
}

/** `text` cut after its first `cells` visible cells; the body starts with
 * every escape the head held. */
function splitAtCell(text: string, cells: number): [string, string] {
  const tokens = tokenize(text);
  let seen = 0;
  for (const [index, [isEscape, chunk]] of tokens.entries()) {
    if (isEscape) continue;
    if (seen >= cells) {
      const head = tokens.slice(0, index);
      const style = head
        .filter(([isEscape]) => isEscape)
        .map(([, c]) => c)
        .join("");
      return [
        head.map(([, c]) => c).join(""),
        style +
          tokens
            .slice(index)
            .map(([, c]) => c)
            .join(""),
      ];
    }
    seen += cellWidth(chunk);
  }
  return [text, ""];
}

/** A prefixed block whose continuation lines indent under the text, for
 * wrapped lines and explicit newlines alike. */
export class HangingText extends Widget {
  private readonly prefixWidth: number;

  constructor(
    private readonly prefix: string,
    private body: string,
    private readonly paddingX = 1,
    private readonly tint: Tint | null = null,
  ) {
    super();
    this.prefixWidth = cellWidth(prefix);
  }

  setBody(body: string): void {
    if (body !== this.body) {
      this.body = body;
      this.invalidate();
    }
  }

  draw(width: number): string[] {
    const pad = " ".repeat(this.paddingX);
    const inner = Math.max(1, width - 2 * this.paddingX - this.prefixWidth);
    const hang = " ".repeat(this.prefixWidth);
    const out: string[] = [];
    for (const paragraph of this.body.split("\n")) {
      wrapAligned(paragraph, inner).forEach((line, index) => {
        out.push(`${pad}${out.length === 0 && index === 0 ? this.prefix : hang}${line}`);
      });
    }
    return out.map((line) =>
      this.tint === null ? fillLine(line, width) : terminate(this.tint(padTo(line, width))),
    );
  }
}

/** A tinted block of children, reaching the full width so it reads as a band. */
export class Box extends Widget {
  children: Component[];

  constructor(
    private readonly paddingX = 1,
    private readonly paddingY = 1,
    private readonly tint: Tint | null = null,
    ...children: Component[]
  ) {
    super();
    this.children = children;
  }

  add<T extends Component>(child: T): T {
    this.children.push(child);
    this.invalidate();
    return child;
  }

  clear(): void {
    this.children = [];
    this.invalidate();
  }

  override render(width: number): string[] {
    const inner = Math.max(1, width - 2 * this.paddingX);
    const pad = " ".repeat(this.paddingX);
    const body = this.children.flatMap((child) =>
      child.render(inner).map((line) => `${pad}${stripTerminator(line)}`),
    );
    const blank = Array<string>(this.paddingY).fill("");
    return [...blank, ...body, ...blank].map((line) =>
      this.tint === null ? fillLine(line, width) : terminate(this.tint(padTo(line, width))),
    );
  }

  draw(width: number): string[] {
    return this.render(width);
  }
}

/** Blank rows. The only vertical separator in the UI. */
export class Spacer extends Widget {
  constructor(private readonly lines = 1) {
    super();
  }

  draw(): string[] {
    return Array<string>(this.lines).fill("");
  }
}

/** A horizontal line, full width. The only border in the UI. */
export class Rule extends Widget {
  constructor(
    protected color: ((text: string) => string) | null = null,
    protected readonly char = "─",
  ) {
    super();
  }

  /** What the rule is painted with. */
  get paint(): ((text: string) => string) | null {
    return this.color;
  }

  setColor(color: ((text: string) => string) | null): void {
    this.color = color;
    this.invalidate();
  }

  draw(width: number): string[] {
    const line = this.char.repeat(Math.max(1, width));
    return [terminate(this.color ? this.color(line) : line)];
  }
}

/** A rule carrying a label, so a status line costs no row of its own. The
 * label is dropped rather than clipped when it will not fit. */
export class LabelledRule extends Rule {
  constructor(
    color: ((text: string) => string) | null = null,
    private label = "",
    private readonly align: "left" | "center" = "left",
    char = "─",
  ) {
    super(color, char);
  }

  setLabel(label: string): void {
    if (label !== this.label) {
      this.label = label;
      this.invalidate();
    }
  }

  override draw(width: number): string[] {
    const w = Math.max(1, width);
    if (!this.label) return super.draw(w);
    const label = ` ${this.label} `;
    const labelWidth = cellWidth(label);
    // Two cells of rule on the shorter side, so the label reads as set into the line.
    if (labelWidth + 4 > w) return super.draw(w);
    const left = this.align === "center" ? Math.floor((w - labelWidth) / 2) : 2;
    const right = w - labelWidth - left;
    const paint = this.color ?? ((text: string) => text);
    return [terminate(paint(this.char.repeat(left)) + label + paint(this.char.repeat(right)))];
  }
}

/** Already-rendered lines, passed through with a left pad: the seam between
 * string builders and components. */
export class Lines extends Widget {
  constructor(
    private lines: string[],
    private readonly paddingX = 1,
    private readonly tint: Tint | null = null,
  ) {
    super();
  }

  setLines(lines: string[]): void {
    this.lines = lines;
    this.invalidate();
  }

  draw(width: number): string[] {
    const pad = " ".repeat(this.paddingX);
    const inner = Math.max(1, width - 2 * this.paddingX);
    return this.lines
      .map((line) => `${pad}${truncateToWidth(stripTerminator(line), inner)}`)
      .map((line) =>
        this.tint === null ? fillLine(line, width) : terminate(this.tint(padTo(line, width))),
      );
  }
}

/** Drop a line's trailing reset so it can be embedded inside another. */
export const stripTerminator = (line: string) =>
  line.endsWith(SEGMENT_RESET) ? line.slice(0, -SEGMENT_RESET.length) : line;

function padTo(line: string, width: number): string {
  let fitted = line;
  let used = cellWidth(fitted);
  if (used > width) {
    fitted = truncateToWidth(fitted, width);
    used = cellWidth(fitted);
  }
  return fitted + " ".repeat(Math.max(0, width - used));
}

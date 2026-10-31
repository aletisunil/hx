/**
 * Rendering markdown to styled terminal lines.
 *
 * Parsing is markdown-it's job - the CommonMark parser markdown-it-py ports.
 * Rendering is ours, so every palette role a user can set reaches the screen.
 *
 * - **H1 and H2 drop their `#`.** H3 and below keep theirs, because by then the
 *   level is worth stating.
 * - **Code blocks lose their fences.** The block is a band of its own colour,
 *   with the language named at the top - or a rule above and below for a theme
 *   with no colour to tint with.
 * - **Tables are the one place box-drawing is allowed.**
 */

import MarkdownIt from "markdown-it";
import type Token from "markdown-it/lib/token.mjs";
import { rstrip, strip } from "../py.ts";
import { fillLine, hyperlink, openStyles, wrap } from "./ansi.ts";
import { Widget } from "./component.ts";
import { cellWidth, truncateToWidth } from "./width.ts";

const CODE_INDENT = "  ";
const CODE_RULE = "─";
const QUOTE_RAIL = "│ ";
/** A rule spanning a very wide terminal reads as a divider in a book. */
const HR_MAX = 80;
/** The narrowest a table column is squeezed to; below this a table is drawn as records. */
const MIN_COLUMN = 3;

export interface PaintStyle {
  readonly bold?: boolean;
  readonly italic?: boolean;
  readonly underline?: boolean;
}

/** How each markdown element is coloured. Injected so the terminal library
 * stays independent of HX's palette. */
export class Painter {
  paint(_role: string, text: string, _style: PaintStyle = {}): string {
    return text;
  }

  /** `text` on a background, or `null` if this role has none - the renderer
   * asks rather than assumes, and rules the block instead when the answer is no. */
  fill(_role: string, _text: string): string | null {
    return null;
  }

  code(source: string, _language: string | null): string[] {
    return source.split("\n");
  }
}

/** One node of the syntax tree markdown-it-py builds from the token stream. */
interface Node {
  readonly type: string;
  readonly tag: string;
  readonly content: string;
  readonly info: string;
  readonly attrs: Record<string, string>;
  readonly children: Node[];
}

function nodeOf(token: Token, children: Node[]): Node {
  return {
    type: token.nesting === 1 ? token.type.replace(/_open$/, "") : token.type,
    tag: token.tag,
    content: token.content,
    info: token.info,
    attrs: Object.fromEntries(token.attrs ?? []),
    children,
  };
}

/** Nest a flat token list on `nesting`, the way `SyntaxTreeNode` does. */
function tree(tokens: Token[]): Node[] {
  const out: Node[] = [];
  let index = 0;
  while (index < tokens.length) {
    const token = tokens[index] as Token;
    if (token.nesting === 1) {
      let depth = 1;
      let close = index + 1;
      while (close < tokens.length && depth > 0) {
        depth += (tokens[close] as Token).nesting;
        if (depth > 0) close++;
      }
      out.push(nodeOf(token, tree(tokens.slice(index + 1, close))));
      index = close + 1;
    } else {
      out.push(nodeOf(token, token.children ? tree(token.children) : []));
      index++;
    }
  }
  return out;
}

const PARSER = new MarkdownIt("commonmark", { breaks: false }).enable(["table", "strikethrough"]);

/**
 * Render `text` into lines at most `width` cells wide. `breaks` keeps a single
 * newline inside a paragraph as a line break - right for what a person typed,
 * wrong for a model's hard-wrapped prose.
 */
export function renderMarkdown(
  text: string,
  width: number,
  painter: Painter = new Painter(),
  breaks = false,
): string[] {
  const renderer = new Renderer(painter, width, breaks);
  renderer.block(tree(PARSER.parse(text, {})), 0);
  return renderer.trim();
}

class Renderer {
  lines: string[] = [];

  constructor(
    readonly painter: Painter,
    readonly width: number,
    readonly breaks: boolean,
  ) {}

  emit(line: string, indent: number): void {
    this.lines.push(line ? " ".repeat(indent) + line : "");
  }

  /** One blank line between blocks, never two. */
  blank(): void {
    if (this.lines.length && this.lines.at(-1) !== "") this.lines.push("");
  }

  trim(): string[] {
    while (this.lines.length && this.lines.at(-1) === "") this.lines.pop();
    while (this.lines.length && this.lines[0] === "") this.lines.shift();
    return this.lines;
  }

  wrapped(text: string, indent: number, hanging = 0): string[] {
    return wrap(text, Math.max(1, this.width - indent - hanging));
  }

  block(children: Node[], indent: number): void {
    for (const child of children) this.node(child, indent);
  }

  node(node: Node, indent: number): void {
    switch (node.type) {
      case "heading":
        this.heading(node, indent);
        break;
      case "paragraph":
        this.paragraph(node, indent);
        break;
      case "fence":
      case "code_block":
        this.fence(node, indent);
        break;
      case "hr":
        this.hr(indent);
        break;
      case "blockquote":
        this.blockquote(node, indent);
        break;
      case "bullet_list":
        this.list(node, indent, false);
        break;
      case "ordered_list":
        this.list(node, indent, true);
        break;
      case "table":
        this.table(node, indent);
        break;
      default:
        // An html_block has no children and renders nothing.
        this.block(node.children, indent);
    }
  }

  private heading(node: Node, indent: number): void {
    const level = Number(node.tag.slice(1));
    const body = this.inline(node);
    this.blank();
    let text: string;
    if (level === 1) text = this.painter.paint("md_heading", body, { bold: true, underline: true });
    else if (level === 2) text = this.painter.paint("md_heading", body, { bold: true });
    else text = this.painter.paint("md_heading", `${"#".repeat(level)} ${body}`, { bold: true });
    for (const line of this.wrapped(text, indent)) this.emit(line, indent);
    this.blank();
  }

  private paragraph(node: Node, indent: number): void {
    const body = this.inline(node);
    if (!body) return;
    this.blank();
    for (const line of this.wrapped(body, indent)) this.emit(line, indent);
    this.blank();
  }

  private fence(node: Node, indent: number): void {
    const language = strip(node.info || "").split(" ")[0] || null;
    const available = Math.max(1, this.width - indent);
    const room = Math.max(1, available - 2 * CODE_INDENT.length);
    const body = this.painter
      .code(rstrip(node.content, "\n"), language)
      .map((line) => truncateToWidth(line, room));
    this.blank();
    if (this.painter.fill("md_code_block_bg", "") === null)
      this.ruledBlock(body, language, available, indent);
    else this.tintedBlock(body, language, available, indent);
    this.blank();
  }

  /** Code on a band of its own colour spanning the column, the language named
   * at the top and a blank row below - inside the tint, so the band is a rectangle. */
  private tintedBlock(
    body: string[],
    language: string | null,
    available: number,
    indent: number,
  ): void {
    const span = available;
    const label = truncateToWidth(language ?? "", Math.max(0, span - 2 * CODE_INDENT.length));
    const head = label ? this.painter.paint("md_code_block", label) : "";
    for (const row of [head, ...body, ""]) {
      const padding = " ".repeat(Math.max(0, span - CODE_INDENT.length - cellWidth(row)));
      const filled = this.painter.fill("md_code_block_bg", CODE_INDENT + row + padding);
      this.emit(filled ?? CODE_INDENT + row, indent);
    }
  }

  /** The same block for a theme with no colour of its own to tint with, never
   * shorter than the code it encloses. */
  private ruledBlock(
    body: string[],
    language: string | null,
    available: number,
    indent: number,
  ): void {
    const widest = Math.max(0, ...body.map(cellWidth)) + CODE_INDENT.length;
    const span = Math.min(available, Math.max(HR_MAX, widest));
    this.emit(this.codeRule(span, language), indent);
    for (const line of body) this.emit(CODE_INDENT + line, indent);
    this.emit(this.painter.paint("md_code_block_border", CODE_RULE.repeat(span)), indent);
  }

  /** The rule opening a code block, carrying the language if there is one. */
  private codeRule(span: number, language: string | null): string {
    const paint = (role: string, text: string) => this.painter.paint(role, text);
    const label = truncateToWidth(language ?? "", Math.max(0, span - 4));
    if (!label) return paint("md_code_block_border", CODE_RULE.repeat(span));
    const trail = CODE_RULE.repeat(Math.max(0, span - 4 - cellWidth(label)));
    return `${paint("md_code_block_border", CODE_RULE.repeat(2))} ${paint("md_code_block", label)} ${paint("md_code_block_border", trail)}`;
  }

  private hr(indent: number): void {
    const span = Math.min(this.width - indent, HR_MAX);
    this.blank();
    this.emit(this.painter.paint("md_hr", "─".repeat(Math.max(1, span))), indent);
    this.blank();
  }

  private blockquote(node: Node, indent: number): void {
    const inner = new Renderer(
      this.painter,
      this.width - indent - cellWidth(QUOTE_RAIL),
      this.breaks,
    );
    inner.block(node.children, 0);
    const rail = this.painter.paint("md_quote_border", QUOTE_RAIL);
    this.blank();
    for (const line of inner.trim()) {
      this.emit(
        line ? rail + this.painter.paint("md_quote", line, { italic: true }) : rail,
        indent,
      );
    }
    this.blank();
  }

  private list(node: Node, indent: number, ordered: boolean): void {
    const start = Number(node.attrs.start ?? 1) || 1;
    this.blank();
    node.children.forEach((item, offset) => {
      this.listItem(item, indent, ordered ? `${start + offset}.` : "-");
    });
    this.blank();
  }

  private listItem(node: Node, indent: number, marker: string): void {
    const painted = this.painter.paint("md_bullet", marker);
    const gutter = cellWidth(marker) + 1;
    const inner = new Renderer(this.painter, this.width - indent - gutter, this.breaks);
    inner.block(node.children, 0);
    const body = inner.trim();
    if (body.length === 0) {
      this.emit(painted, indent);
      return;
    }
    body.forEach((line, position) => {
      if (position === 0) this.emit(`${painted} ${line}`, indent);
      // Continuation aligns under the item's text, not its marker.
      else if (line) this.emit(" ".repeat(gutter) + line, indent);
      else this.emit("", indent);
    });
  }

  private table(node: Node, indent: number): void {
    let rows: string[][] = [];
    let header = 0;
    for (const section of node.children) {
      for (const row of section.children) rows.push(row.children.map((cell) => this.inline(cell)));
      if (section.type === "thead") header = rows.length;
    }
    if (rows.length === 0) return;
    const count = Math.max(...rows.map((row) => row.length));
    rows = rows.map((row) => [...row, ...Array<string>(count - row.length).fill("")]);
    const widths = Array.from({ length: count }, (_, i) =>
      Math.max(...rows.map((row) => cellWidth(row[i] as string))),
    );
    // Narrowed to fit, widest column first, and the cells wrapped inside it.
    const budget = this.width - indent - (3 * count + 1);
    if (budget < MIN_COLUMN * count) {
      this.stackedTable(rows, header, indent);
      return;
    }
    while (widths.reduce((a, b) => a + b, 0) > budget) {
      const widest = widths.indexOf(Math.max(...widths));
      widths[widest] = (widths[widest] as number) - 1;
    }
    const body = rows.map((row) => row.map((cell, i) => cellLines(cell, widths[i] as number)));
    // Once a row takes more than one line, only a rule says where the next starts.
    const ruled = body.slice(header).some((row) => Math.max(...row.map((cell) => cell.length)) > 1);
    const divider = () =>
      this.painter.paint(
        "md_code_block_border",
        `├─${widths.map((w) => "─".repeat(w)).join("─┼─")}─┤`,
      );
    this.blank();
    body.forEach((row, index) => {
      if (index && (index === header || (index > header && ruled))) this.emit(divider(), indent);
      const depth = Math.max(...row.map((cell) => cell.length));
      for (let line = 0; line < depth; line++) {
        const cells = row.map((cell, i) => {
          const piece = cell[line] ?? "";
          return piece + " ".repeat((widths[i] as number) - cellWidth(piece));
        });
        this.emit(`│ ${cells.join(" │ ")} │`, indent);
      }
    });
    this.blank();
  }

  /** A table too wide for the terminal, one `header: value` record at a time. */
  private stackedTable(rows: string[][], header: number, indent: number): void {
    const names = header
      ? (rows[header - 1] as string[])
      : Array<string>((rows[0] as string[]).length).fill("");
    this.blank();
    rows.slice(header).forEach((row, index) => {
      if (index)
        this.emit(
          this.painter.paint(
            "md_code_block_border",
            "─".repeat(Math.min(this.width - indent, HR_MAX)),
          ),
          indent,
        );
      names.forEach((name, i) => {
        const cell = row[i] as string;
        const label = name ? this.painter.paint("md_heading", `${name}:`, { bold: true }) : "";
        this.wrapped(label ? `${label} ${cell}` : cell, indent, 2).forEach((line, position) => {
          // Continuation hangs under the value, clear of the name.
          this.emit(line, indent + (position ? 2 : 0));
        });
      });
    });
    this.blank();
  }

  inline(node: Node): string {
    return node.children.map((child) => this.inlineNode(child)).join("");
  }

  private inlineNode(node: Node): string {
    const paint = (role: string, text: string, style: PaintStyle = {}) =>
      this.painter.paint(role, text, style);
    switch (node.type) {
      case "text":
        return node.content;
      case "code_inline":
        return paint("md_code", node.content);
      case "softbreak":
        return this.breaks ? "\n" : " ";
      case "hardbreak":
        return "\n";
      case "strong":
        return paint("text", this.inline(node), { bold: true });
      case "em":
        return paint("text", this.inline(node), { italic: true });
      case "s":
        return this.inline(node);
      case "link": {
        const url = node.attrs.href ?? "";
        const painted = paint("md_link", this.inline(node) || url, { underline: true });
        return url ? hyperlink(url, painted) : painted;
      }
      case "image": {
        const alt = this.inline(node) || node.content || "image";
        return paint("md_link_url", `[${alt}]`);
      }
      case "inline":
        return this.inline(node);
      default:
        return node.content || this.inline(node);
    }
  }
}

/** One table cell wrapped to its column, each line closing what it opened so a
 * link or bold phrase stays inside its own cell. */
function cellLines(text: string, width: number): string[] {
  return wrap(text, width)
    .map((piece) => truncateToWidth(piece, width))
    .map((line) => line + openStyles(line)[1]);
}

/** A block of markdown as a component. */
export class Markdown extends Widget {
  constructor(
    private source = "",
    private readonly painter: Painter = new Painter(),
  ) {
    super();
  }

  setSource(source: string): void {
    if (source !== this.source) {
      this.source = source;
      this.invalidate();
    }
  }

  draw(width: number): string[] {
    return renderMarkdown(this.source, width, this.painter).map((line) => fillLine(line, width));
  }
}

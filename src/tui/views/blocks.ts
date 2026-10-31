/**
 * The things that appear in a transcript.
 *
 * Four kinds, and the difference between them is carried by weight rather than
 * by labels: the user's turn is a tinted block, the model's reply is bare text,
 * a tool call is a marker coloured by how it ended with its result hung under
 * it, and a notice is one line with a bullet.
 */

import { describe } from "../../core/images.ts";
import type { ImageBlock } from "../../core/messages.ts";
import { strip } from "../../py.ts";
import { wrap } from "../../term/ansi.ts";
import { Widget } from "../../term/component.ts";
import { renderMarkdown } from "../../term/markdown.ts";
import { Box, HangingText, Lines } from "../../term/primitives.ts";
import { plainText } from "../../term/sanitize.ts";
import { cellWidth, truncateToWidth } from "../../term/width.ts";
import {
  IMAGE,
  NOTICE,
  SPINNER,
  TOOL_AWAITING,
  TOOL_DONE,
  TOOL_FAILED,
  TOOL_RESULT,
} from "../glyphs.ts";
import { STREAM_TAIL } from "../limits.ts";
import { fg, ThemePainter, tint } from "../paint.ts";
import {
  formatDuration,
  toolCall as makeToolCall,
  type Params,
  rendererFor,
  renderTodos,
  sanitizedCall,
  sanitizedFields,
  type ToolCall,
} from "../renderers.ts";

const PAINTER = new ThemePainter();

/**
 * What the user said, tinted the width of the terminal. Markdown is rendered,
 * because a user pasting a code block or a list meant it as one; sanitized,
 * because a paste is whatever was on the clipboard.
 */
export class UserMessage extends Widget {
  readonly text: string;
  private readonly images: readonly ImageBlock[];

  constructor(text: string, images: readonly ImageBlock[] = []) {
    super();
    this.text = plainText(text);
    this.images = [...images];
  }

  draw(width: number): string[] {
    const inner = Math.max(1, width - 2);
    const lines = renderMarkdown(this.text, inner, undefined, true).map((line) =>
      fg("user_text", line),
    );
    // Named under the text rather than drawn: a terminal cannot show the
    // picture, but it can show which one went and how big it was.
    for (const image of this.images)
      lines.push(fg("muted", truncateToWidth(IMAGE + plainText(describe(image)), inner)));
    return new Box(1, 1, tint("user_bg"), new Lines(lines, 0)).render(width);
  }
}

/** What the model said. No background at all: the contrast with the user's
 * tinted block is what marks the turn. */
export class AssistantMessage extends Widget {
  private body: string;

  constructor(text = "") {
    super();
    this.body = plainText(text);
  }

  /** Per chunk rather than over the whole buffer, so a long reply is
   * sanitized once end to end. */
  append(chunk: string): void {
    this.body += plainText(chunk);
    this.invalidate();
  }

  setText(text: string): void {
    const clean = plainText(text);
    if (clean !== this.body) {
      this.body = clean;
      this.invalidate();
    }
  }

  get text(): string {
    return this.body;
  }

  draw(width: number): string[] {
    if (!strip(this.body)) return [];
    return new Lines(renderMarkdown(this.body, Math.max(1, width - 2), PAINTER)).render(width);
  }
}

/** The model's reasoning, in its own muted colour. Shown rather than hidden,
 * with `ctrl+o` to fold it. */
export class ThinkingMessage extends Widget {
  private body: string;

  constructor(
    text = "",
    private collapsed = false,
  ) {
    super();
    this.body = plainText(text);
  }

  append(chunk: string): void {
    this.body += plainText(chunk);
    this.invalidate();
  }

  setCollapsed(collapsed: boolean): void {
    if (collapsed !== this.collapsed) {
      this.collapsed = collapsed;
      this.invalidate();
    }
  }

  toggle(): void {
    this.setCollapsed(!this.collapsed);
  }

  get text(): string {
    return this.body;
  }

  draw(width: number): string[] {
    if (!strip(this.body)) return [];
    const lines = strip(this.body)
      .split("\n")
      .map((line) => fg("thinking", line, { italic: true }));
    if (!this.collapsed) return new Lines(lines).render(width);
    // Folded by the reader, with the key they just pressed - so the count is
    // what is worth saying here, not the key again.
    const note = lines.length > 1 ? fg("dim", ` (${lines.length} lines)`) : "";
    return new Lines([fg("thinking", "Thinking…", { italic: true }) + note]).render(width);
  }
}

/** The last `STREAM_TAIL` characters, from a line start so the cut cannot
 * land inside an escape sequence. */
function streamTail(text: string): string {
  if (text.length <= STREAM_TAIL) return text;
  const tail = text.slice(-STREAM_TAIL);
  return tail.slice(tail.indexOf("\n") + 1);
}

/** Cells the `⎿` sits in from the marker, under the header's text. */
export const RESULT_INDENT = 2;
/** Cells a tool call's result sits in: past the `⎿`, one left edge for every row. */
export const BODY_INDENT = RESULT_INDENT + cellWidth(TOOL_RESULT);

type State = "running" | "awaiting" | "done" | "error" | "interrupted";

const MARKERS: Record<Exclude<State, "running">, [string, string]> = {
  awaiting: [TOOL_AWAITING, "warning"],
  done: [TOOL_DONE, "success"],
  error: [TOOL_FAILED, "error"],
  // Stopped, not failed: the warning the "Interrupted" line is drawn in.
  interrupted: [TOOL_FAILED, "warning"],
};

/**
 * One tool call: a header behind a marker, and what came back hung under it.
 * The marker is the state machine - three glyphs in three colours, so a long
 * transcript can be skimmed for the red one.
 */
export class ToolBlock extends Widget {
  private current: ToolCall;
  private frame = 0;
  /** What the tool has streamed, raw; the output is its sanitized form. */
  private stream: string[] = [];
  private streamed = 0;
  private streamDirty = false;
  /** Held at an approval: not running yet, so it does not spin. */
  private awaiting = false;

  constructor(call: ToolCall) {
    super();
    this.current = sanitizedCall(call);
  }

  get call(): ToolCall {
    this.foldStream();
    return this.current;
  }

  /**
   * Replace fields on the call. Whole values only, and only what is being
   * replaced is sanitized. Streaming output goes through `appendOutput`.
   */
  update(changes: Partial<ToolCall>): void {
    if ("output" in changes) {
      this.stream = [];
      this.streamed = 0;
      this.streamDirty = false;
    } else this.foldStream();
    this.current = { ...this.current, ...sanitizedFields(changes) };
    this.invalidate();
  }

  /**
   * Add one streamed chunk. Held raw and folded into the output once per
   * frame, sanitized as a whole so an escape split across chunks goes as a
   * whole. Only the last `STREAM_TAIL` characters are kept.
   */
  appendOutput(chunk: string): void {
    if (!this.stream.length) {
      this.stream = [this.current.output];
      this.streamed = this.current.output.length;
    }
    this.stream.push(chunk);
    this.streamed += chunk.length;
    if (this.streamed > 2 * STREAM_TAIL) {
      const tail = streamTail(this.stream.join(""));
      this.stream = [tail];
      this.streamed = tail.length;
    }
    this.streamDirty = true;
    this.invalidate();
  }

  private foldStream(): void {
    if (!this.streamDirty) return;
    const raw = streamTail(this.stream.join(""));
    this.stream = [raw];
    this.streamed = raw.length;
    this.streamDirty = false;
    this.current = { ...this.current, output: plainText(raw) };
  }

  /** Advance the spinner while the call runs. Whether anything changed. */
  tick(): boolean {
    if (this.current.finished || this.awaiting) return false;
    this.frame = (this.frame + 1) % SPINNER.length;
    this.invalidate();
    return true;
  }

  /** Whether the call is held at an approval rather than running. */
  setAwaiting(awaiting: boolean): void {
    if (awaiting !== this.awaiting) {
      this.awaiting = awaiting;
      this.invalidate();
    }
  }

  toggle(): void {
    this.update({ expanded: !this.current.expanded });
  }

  private get state(): State {
    if (!this.current.finished) return this.awaiting ? "awaiting" : "running";
    if (this.current.interrupted) return "interrupted";
    return this.current.isError ? "error" : "done";
  }

  draw(width: number): string[] {
    this.foldStream();
    const state = this.state;
    const [marker, markerRole] =
      state === "running" ? [SPINNER[this.frame] as string, "accent"] : MARKERS[state];

    const renderer = rendererFor(this.current.name);
    // Wrapped, not cut at the edge: the end of a row is often the part that
    // matters, and the result starts 6 cells in.
    const room = Math.max(1, width - 2 - BODY_INDENT);
    const result = renderer.result(this.current).flatMap((line) => wrap(line, room));
    if (this.current.interrupted) {
      const took = this.current.durationMs;
      result.push(fg("warning", `Interrupted${took ? ` after ${formatDuration(took)}` : ""}`));
    }

    const lines = [fg(markerRole, `${marker} `) + renderer.header(this.current)];
    // Hung from the header's text rather than its marker, every row on the
    // same edge, so the result reads as one thing under one call.
    result.forEach((line, row) => {
      const gutter = row === 0 ? fg("dim", TOOL_RESULT) : " ".repeat(cellWidth(TOOL_RESULT));
      lines.push(" ".repeat(RESULT_INDENT) + gutter + line);
    });
    return new Lines(lines, 1).render(width);
  }
}

/** One line of housekeeping. The bullet hangs, so a multi-line notice indents
 * under its text. */
export class Notice extends Widget {
  private readonly text: string;

  constructor(
    text: string,
    readonly level = "info",
  ) {
    super();
    // An error message is very often a tool's stderr wearing a sentence.
    this.text = plainText(text);
  }

  draw(width: number): string[] {
    const role =
      ({ error: "error", warning: "warning", success: "success" } as Record<string, string>)[
        this.level
      ] ?? "muted";
    const bullet = NOTICE[this.level] ?? (NOTICE.info as string);
    return new HangingText(fg(role, bullet), fg(role, this.text)).render(width);
  }
}

/** The current plan, as a block in the transcript. */
export class TodoBlock extends Widget {
  constructor(private readonly todos: unknown) {
    super();
  }

  draw(width: number): string[] {
    const rows = renderTodos(this.todos);
    if (!rows.length) return [];
    const todos = this.todos as unknown[];
    const done = todos.filter(
      (todo) =>
        typeof todo === "object" &&
        todo !== null &&
        !Array.isArray(todo) &&
        (todo as Record<string, unknown>).status === "completed",
    ).length;
    const header = fg("accent", "Todos", { bold: true }) + fg("dim", `  ${done}/${todos.length}`);
    return new Lines([header, ...rows]).render(width);
  }
}

/** Build a `ToolCall`, defaults included. */
export function toolCall(
  name: string,
  params: Params,
  cwd: string,
  extra: Partial<ToolCall> = {},
): ToolCall {
  return makeToolCall({ name, params, cwd, ...extra });
}

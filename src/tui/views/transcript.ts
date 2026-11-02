/**
 * The conversation, and the dock beneath it.
 *
 * The transcript is not a scrolling widget - it is the document, and the
 * terminal scrolls it. Blocks are appended and never moved, so finished output
 * becomes real scrollback the terminal owns.
 *
 * The dock below it is the part that does change: the prompt, its two rules,
 * the hints and the status bar.
 */

import { keymap } from "../../keys.ts";
import { formatFixed } from "../../py.ts";
import { inverse } from "../../term/ansi.ts";
import { type Component, Container } from "../../term/component.ts";
import { Spacer, Text } from "../../term/primitives.ts";
import { CURSOR_MARKER } from "../../term/screen.ts";
import { SPINNER } from "../glyphs.ts";
import { fg } from "../paint.ts";
import { AssistantMessage, UserMessage } from "./blocks.ts";
import { Prompt as RealPrompt } from "./prompt.ts";
import { Header, HintsBar, StatusBar } from "./status.ts";

/** Appended to, never rearranged. Every block brings the blank line above it. */
export class Transcript extends Container {
  /** The block the keyboard is pointing at, for copy and expand. */
  cursor: Component | null = null;

  append<T extends Component>(block: T): T {
    if (this.children.length) super.add(new Spacer(1));
    return super.add(block);
  }

  /** The blocks, without the spacers between them. */
  get blocks(): Component[] {
    return this.children.filter((child) => !(child instanceof Spacer));
  }

  last(): Component | null {
    return this.blocks.at(-1) ?? null;
  }

  /** Blocks the cursor can land on: things somebody said. */
  navigable(): Component[] {
    return this.blocks.filter(
      (block) => block instanceof UserMessage || block instanceof AssistantMessage,
    );
  }

  /** Step to the next or previous message, starting from the bottom. */
  moveCursor(delta: number): Component | null {
    const blocks = this.navigable();
    if (!blocks.length) return null;
    const at = this.cursor === null ? -1 : blocks.indexOf(this.cursor);
    const index =
      at < 0
        ? delta < 0
          ? blocks.length - 1
          : 0
        : Math.max(0, Math.min(blocks.length - 1, at + delta));
    this.cursor = blocks[index] as Component;
    return this.cursor;
  }

  /** Text of the cursored block, falling back to the last answer. */
  cursoredText(): string {
    if (this.cursor !== null) {
      const text = (this.cursor as { text?: unknown }).text;
      return typeof text === "string" ? text : "";
    }
    for (const block of [...this.blocks].reverse())
      if (block instanceof AssistantMessage) return block.text;
    return "";
  }
}

/** A framed block that can be told the dock will close it. */
export interface Dockable {
  setDocked(docked: boolean): void;
}

export function isDockable(value: unknown): value is Dockable {
  return typeof (value as Partial<Dockable> | null)?.setDocked === "function";
}

/** What the dock needs from whatever is taking the user's typing. */
export interface PromptLike extends Component {
  readonly value: string;
  /** Rows drawn above the prompt's frame for a completion list. */
  readonly completions?: readonly string[];
  clear(): void;
  setStatus(label: string): void;
  handleInput(key: string, data: string): boolean;
}

/**
 * The working status, written into the prompt's own top rule. Not a
 * component: the status lives in a line that already exists, so a turn
 * starting costs no layout and the transcript above never jumps.
 */
export class WorkingIndicator {
  private frame = 0;
  private isRunning = false;
  private elapsed = 0;

  constructor(readonly target: PromptLike) {}

  get running(): boolean {
    return this.isRunning;
  }

  /** Show the status, `elapsed` seconds into the turn. */
  start(elapsed = 0): void {
    this.isRunning = true;
    this.elapsed = elapsed;
    this.paint();
  }

  stop(): void {
    this.isRunning = false;
    this.target.setStatus("");
  }

  /** Advance the spinner. Whether anything changed. */
  tick(elapsed: number): boolean {
    if (!this.isRunning) return false;
    this.frame = (this.frame + 1) % SPINNER.length;
    this.elapsed = elapsed;
    this.paint();
    return true;
  }

  private paint(): void {
    this.target.setStatus(
      fg("accent", SPINNER[this.frame] as string) +
        fg(
          "muted",
          ` Working… (${formatFixed(this.elapsed, 0)}s · ${keymap().primary("app.interrupt")} to interrupt)`,
        ),
    );
  }
}

/** A single line of typing, and nothing else. */
export class StubPrompt extends Text implements PromptLike {
  private buffer = "";

  constructor() {
    super("", 1, 0);
    this.refresh();
  }

  get value(): string {
    return this.buffer;
  }

  clear(): void {
    this.buffer = "";
    this.refresh();
  }

  setStatus(_label: string): void {}

  handleInput(key: string, data: string): boolean {
    if (key === "text" || key === "paste") this.buffer += data.replaceAll("\n", " ");
    else if (key === "backspace") this.buffer = Array.from(this.buffer).slice(0, -1).join("");
    else if (key === "ctrl+u") this.buffer = "";
    else return false;
    this.refresh();
    return true;
  }

  private refresh(): void {
    const placeholder = "Ask HX…  (/ for commands)";
    const shown = this.buffer ? fg("text", this.buffer) : fg("dim", placeholder);
    this.setText(shown + CURSOR_MARKER + inverse(" "));
  }
}

/** Everything below the transcript, in the order it is drawn. It draws no
 * frame of its own: the prompt is framed by its own two rules. */
export class Dock extends Container {
  readonly prompt: PromptLike;
  readonly working: WorkingIndicator;
  readonly hints = new HintsBar();
  readonly status = new StatusBar();

  constructor(prompt: PromptLike | null = null) {
    super();
    this.prompt = prompt ?? new RealPrompt(process.cwd());
    this.working = new WorkingIndicator(this.prompt);
    this.add(new Spacer(1));
    this.add(this.prompt);
    this.add(this.hints);
    this.add(this.status);
  }

  /** The completion list comes and goes as you type, so it floats. */
  floatingHeight(): number {
    return this.prompt.completions?.length ?? 0;
  }
}

/**
 * The whole document: a header, the transcript, an overlay, and the dock. The
 * overlay sits straight on the dock, and both stay on the bottom of the
 * screen. The overlay floats: it covers the conversation's last rows instead
 * of scrolling them away, and gives them back when it closes.
 */
export class Session extends Container {
  /** Blank rows `show` puts between the transcript and the overlay. */
  static readonly OVERLAY_GAP = 1;

  readonly header: Header;
  readonly transcript = new Transcript();
  readonly overlay = new Container();
  readonly dock: Dock;
  /** Whichever framed block is currently letting the dock close it. */
  private docked: Dockable | null = null;

  constructor(version = "", quiet = false, prompt: PromptLike | null = null) {
    super();
    this.header = new Header(version, quiet);
    this.dock = new Dock(prompt);
    this.add(this.header);
    this.add(this.transcript);
    this.add(this.overlay);
    this.add(this.dock);
  }

  show(component: Component): void {
    this.overlay.clear();
    this.overlay.add(new Spacer(Session.OVERLAY_GAP));
    this.overlay.add(component);
  }

  override render(width: number): string[] {
    this.seatFrames();
    return super.render(width);
  }

  /**
   * Tell whatever lands directly above the dock that the dock closes it, so
   * a framed block does not draw rule, blank, rule. Decided per frame, since
   * which block that is changes as the conversation grows.
   */
  private seatFrames(): void {
    const last = this.showing ?? this.transcript.blocks.at(-1) ?? null;
    const bottom = isDockable(last) ? last : null;
    if (bottom === this.docked) return;
    this.docked?.setDocked(false);
    this.docked = bottom;
    bottom?.setDocked(true);
  }

  dismiss(): void {
    this.overlay.clear();
  }

  /** Whatever currently owns the keyboard, if anything. */
  get showing(): Component | null {
    return this.overlay.children.filter((child) => !(child instanceof Spacer)).at(-1) ?? null;
  }

  /** Rows the renderer holds on the bottom of the screen: the overlay and
   * the dock. */
  footerHeight(width: number): number {
    return this.overlay.render(width).length + this.dock.render(width).length;
  }

  /** Rows of the footer laid over the conversation rather than after it. */
  floatingHeight(width: number): number {
    return this.overlay.render(width).length + this.dock.floatingHeight();
  }

  /** How tall a thing on the overlay may be, on a terminal this size. */
  overlayRows(width: number, rows: number): number {
    return Math.max(1, rows - this.dock.render(width).length - Session.OVERLAY_GAP);
  }

  override handleInput(key: string, data: string): boolean {
    const showing = this.showing;
    if (showing !== null) {
      const handler = (showing as { handleInput?: (key: string, data: string) => boolean })
        .handleInput;
      handler?.call(showing, key, data);
      // An overlay swallows everything while it is up: a key meant for it
      // must never fall through and be typed into the prompt.
      return true;
    }
    return this.dock.handleInput(key, data);
  }
}

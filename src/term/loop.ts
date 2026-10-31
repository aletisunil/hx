/**
 * When to draw, and where keys go.
 *
 * **Keystrokes redraw immediately.** A character that appears 30ms after it
 * was typed reads as lag. **Everything else is coalesced**: a model streaming
 * tokens invalidates the document hundreds of times a second, collapsed into
 * one frame at `MAX_FRAMES_PER_SECOND`.
 *
 * The other job here is the Escape key. A terminal sends `\x1b` both for the
 * key and as the first byte of every arrow, so the decoder holds a trailing
 * Escape back, and a timer decides it was really the key.
 */

import type { Component } from "./component.ts";
import { Decoder, type Key, REPORT_PREFIX } from "./keydecode.ts";
import { MainScreen } from "./screen.ts";
import { ProcessTerminal, type Terminal } from "./terminal.ts";

const MAX_FRAMES_PER_SECOND = 30;
const MIN_RENDER_INTERVAL = 1000 / MAX_FRAMES_PER_SECOND;

/** How long a lone Escape is held before being taken at face value. */
export const ESCAPE_TIMEOUT = 30;

export type KeyHandler = (key: Key) => void;

/** Owns the terminal, the render schedule, and key dispatch. */
export class TuiRunner {
  readonly terminal: Terminal;
  readonly screen: MainScreen;
  private readonly decoder = new Decoder();
  private running = false;
  private renderPending = false;
  private drawing = false;
  private redrawAfterDraw = false;
  private resizePending = false;
  private renderTimer: NodeJS.Timeout | null = null;
  private escapeTimer: NodeJS.Timeout | null = null;
  private lastRender = 0;
  private stopped: (() => void) | null = null;

  constructor(
    private readonly root: Component,
    terminal: Terminal | null = null,
    private readonly onKey: KeyHandler | null = null,
    /** Applied once the terminal is in raw mode - the alternate screen is
     * entered after the terminal is ours, not while it belongs to the shell. */
    private readonly startFullscreen = false,
  ) {
    this.terminal = terminal ?? new ProcessTerminal();
    this.screen = new MainScreen(this.terminal, root);
  }

  /** Draw until `stop` is called, then hand the terminal back. */
  async run(): Promise<void> {
    const done = new Promise<void>((resolve) => {
      this.stopped = resolve;
    });
    this.running = true;
    this.terminal.start(
      (data) => this.onInput(data),
      () => this.onResize(),
    );
    try {
      if (this.startFullscreen) this.screen.setFullscreen(true);
      this.requestImmediateRender();
      await done;
    } finally {
      this.running = false;
      this.cancelTimers();
      try {
        this.screen.close();
      } catch {
        // The terminal is handed back below whatever the screen managed.
      }
      this.terminal.stop();
    }
  }

  stop(): void {
    this.stopped?.();
  }

  get fullscreen(): boolean {
    return this.screen.fullscreen;
  }

  /**
   * Draw into the alternate screen, or back into the scrollback. Coming back,
   * the normal screen still holds this session's transcript up to the moment
   * it left, so the screen - not the scrollback, which is the user's - is
   * cleared before the document is drawn again.
   */
  setFullscreen(enabled: boolean): void {
    if (enabled === this.screen.fullscreen) return;
    this.screen.setFullscreen(enabled);
    if (!enabled) this.terminal.write("\x1b[2J\x1b[H");
    this.requestImmediateRender();
  }

  /** Scroll the fullscreen viewport. A no-op on the normal screen. */
  scroll(rows: number): void {
    if (this.screen.scrollBy(rows)) this.requestImmediateRender();
  }

  scrollToTop(): void {
    if (this.screen.scrollToTop()) this.requestImmediateRender();
  }

  scrollToBottom(): void {
    if (this.screen.scrollToBottom()) this.requestImmediateRender();
  }

  /** Wipe the screen and the scrollback, then draw the document again. */
  clearScreen(): void {
    this.screen.clear();
  }

  /** `ctrl+z`: give the terminal back until the shell resumes us, then repaint
   * from scratch - the screen belonged to the shell in between. */
  suspend(): void {
    this.terminal.suspend();
    this.screen.invalidate();
    this.requestImmediateRender();
  }

  private cancelTimers(): void {
    if (this.renderTimer) clearTimeout(this.renderTimer);
    if (this.escapeTimer) clearTimeout(this.escapeTimer);
    this.renderTimer = null;
    this.escapeTimer = null;
  }

  /** Draw soon, collapsing everything else asked for in the meantime. */
  requestRender(): void {
    if (!this.running || this.renderPending) return;
    this.renderPending = true;
    const elapsed = performance.now() - this.lastRender;
    this.renderTimer = setTimeout(
      () => {
        this.renderTimer = null;
        if (this.renderPending && this.running) this.draw();
      },
      Math.max(0, MIN_RENDER_INTERVAL - elapsed),
    );
  }

  /** Draw now, ahead of the throttle - for anything the user just did. */
  requestImmediateRender(): void {
    if (!this.running) return;
    this.renderPending = false;
    if (this.renderTimer) clearTimeout(this.renderTimer);
    this.renderTimer = null;
    this.draw();
  }

  /** Paint once, and never from inside another paint: the inner request is
   * remembered and drawn by the outer one on its way out. */
  private draw(): void {
    if (this.drawing) {
      this.redrawAfterDraw = true;
      return;
    }
    this.renderPending = false;
    this.lastRender = performance.now();
    this.drawing = true;
    try {
      this.screen.render();
    } finally {
      this.drawing = false;
    }
    if (this.redrawAfterDraw) {
      this.redrawAfterDraw = false;
      this.draw();
    }
  }

  private onInput(data: string): void {
    const keys = this.decoder.feed(data);
    for (const key of keys) this.dispatch(key);
    // Whatever is left is an unfinished sequence. If nothing follows it
    // shortly, it was the Escape key.
    this.armEscapeTimer();
    if (keys.length) this.requestImmediateRender();
  }

  private dispatch(key: Key): void {
    if (key.name.startsWith(REPORT_PREFIX)) {
      // The terminal answering a question, not the user pressing a key.
      this.terminal.report(key.name, key.data);
      return;
    }
    if (this.onKey !== null) {
      this.onKey(key);
      return;
    }
    (this.root as { handleInput?: (name: string, data: string) => boolean }).handleInput?.(
      key.name,
      key.data,
    );
  }

  private armEscapeTimer(): void {
    if (this.escapeTimer) clearTimeout(this.escapeTimer);
    this.escapeTimer = setTimeout(() => {
      this.escapeTimer = null;
      const pending = this.decoder.takePending();
      if (pending.length === 0) return;
      for (const key of pending) this.dispatch(key);
      this.requestImmediateRender();
    }, ESCAPE_TIMEOUT);
  }

  /** A burst of resize signals collapses into one redraw, at the size the
   * terminal has when it runs. */
  private onResize(): void {
    if (!this.running || this.resizePending) return;
    this.resizePending = true;
    setImmediate(() => {
      this.resizePending = false;
      if (this.running) this.requestImmediateRender();
    });
  }
}

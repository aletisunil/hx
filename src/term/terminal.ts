/**
 * Owning the terminal: raw mode, the modes we turn on, and putting them back.
 *
 * Turning a terminal mode on is a two-byte write that cannot fail. Turning it
 * off again has to happen on every path out of the process - a clean exit, an
 * uncaught error, a signal - and if it does not, the user is left in a shell
 * with no echo and an invisible cursor. So `restore` is idempotent and is
 * wired to process exit, to uncaught errors and to the fatal signals.
 *
 * `SIGKILL` runs no handler, so `start` begins by turning everything off,
 * whether or not this process turned it on: the fix for a terminal left broken
 * by a kill is to run `hx` again.
 *
 * The default is *not* the alternate screen. Output stays in the terminal's own
 * scrollback, where the terminal can scroll, select and copy it, and where it
 * survives the process exiting. `/fullscreen` turns the alternate screen on.
 */

import { writeSync } from "node:fs";
import { ATTRIBUTES_REPORT, KEYBOARD_REPORT } from "./keydecode.ts";

/** Used when the terminal will not say - a pipe, a CI log, a dead tty. */
export const DEFAULT_SIZE: [number, number] = [80, 24];

// Modes HX turns on, each paired with the sequence that turns it off.
const BRACKETED_PASTE_ON = "\x1b[?2004h";
const BRACKETED_PASTE_OFF = "\x1b[?2004l";
const MOUSE_ON = "\x1b[?1000h\x1b[?1002h\x1b[?1006h";
const MOUSE_OFF = "\x1b[?1006l\x1b[?1002l\x1b[?1000l";
const CURSOR_HIDE = "\x1b[?25l";
const CURSOR_SHOW = "\x1b[?25h";
const ALT_SCREEN_ON = "\x1b[?1049h";
const ALT_SCREEN_OFF = "\x1b[?1049l";
/**
 * Leave the alternate screen *without* restoring a cursor. `?1049l` restores a
 * cursor the matching `?1049h` saved; asked to clean up after somebody else it
 * homes the cursor, and the first frame would land on the user's scrollback.
 */
const ALT_SCREEN_LEAVE = "\x1b[?1047l";
const SGR_RESET = "\x1b[0m";

/** Disambiguate escape codes (1) and report alternate keys (4): shift+enter,
 * alt+enter and ctrl+shift+z get codes of their own, and ctrl+c still means
 * ctrl+c on a Cyrillic layout. Key releases (2) are only noise here. */
const KITTY_KEYBOARD_FLAGS = 1 | 4;
const KITTY_KEYBOARD_PUSH = `\x1b[>${KITTY_KEYBOARD_FLAGS}u`;
const KITTY_KEYBOARD_POP = "\x1b[<u";
const KITTY_KEYBOARD_QUERY = "\x1b[?u";
const DEVICE_ATTRIBUTES_QUERY = "\x1b[c";
/** xterm's modifyOtherKeys, level 2: the fallback for a terminal without the
 * kitty protocol, and how shift+enter survives tmux. */
const MODIFY_OTHER_KEYS_ON = "\x1b[>4;2m";
const MODIFY_OTHER_KEYS_OFF = "\x1b[>4;0m";

export type KeyboardProtocol = "kitty" | "modifyOtherKeys" | null;

/** What the renderer needs from the outside world. */
export interface Terminal {
  readonly size: [number, number];
  write(data: string): void;
  start(onInput: (data: string) => void, onResize: () => void): void;
  stop(): void;
  setAltScreen(enabled: boolean): void;
  readonly altScreen: boolean;
  suspend(): void;
  /** An answer to one of the terminal's own queries, from the decoder. */
  report(name: string, data: string): void;
  readonly keyboard: KeyboardProtocol;
  setMouse(enabled: boolean): void;
}

/** The real terminal this process is attached to. */
export class ProcessTerminal implements Terminal {
  private entered = false;
  private restored = false;
  private mouse = false;
  private alt = false;
  private protocol: KeyboardProtocol = null;
  private onInput: ((data: string) => void) | null = null;
  private onResize: (() => void) | null = null;
  private readonly decoder = new TextDecoder("utf-8");
  private readonly listeners: [string | symbol, (...args: never[]) => void][] = [];
  private readonly reader = (chunk: Buffer) => {
    const text = this.decoder.decode(chunk, { stream: true });
    if (text) this.onInput?.(text);
  };

  get size(): [number, number] {
    const { columns, rows } = process.stdout;
    // A zero is a terminal mid-resize or a pty not yet sized.
    return [columns || DEFAULT_SIZE[0], rows || DEFAULT_SIZE[1]];
  }

  /** Written synchronously, so a frame is on the screen before the next one
   * starts and `restore` can run from an exit handler. */
  write(data: string): void {
    if (!data) return;
    let buffer = Buffer.from(data);
    while (buffer.length) {
      try {
        const written = writeSync(1, buffer);
        buffer = buffer.subarray(written);
      } catch (error) {
        // The far end went away, or the tty is momentarily full.
        if ((error as NodeJS.ErrnoException).code === "EAGAIN") continue;
        return;
      }
    }
  }

  /** Enter raw mode, enable our modes, and arm every path back out. The
   * restore hooks go in *before* the modes are turned on. */
  start(onInput: (data: string) => void, onResize: () => void): void {
    this.onResize = onResize;
    this.installSafetyNet();
    this.sanitize();
    if (process.stdin.isTTY) process.stdin.setRawMode(true);
    this.entered = true;
    this.restored = false;
    this.write(BRACKETED_PASTE_ON + CURSOR_HIDE + this.keyboardNegotiate());
    this.on(process, "SIGWINCH", () => this.onResize?.());
    // A resize during a suspend is only discoverable on the way back.
    this.on(process, "SIGCONT", () => this.onResize?.());
    this.onInput = onInput;
    this.attachReader();
  }

  stop(): void {
    this.detachReader();
    this.restore();
  }

  /** Turn off every mode HX can turn on, before turning any of them on. The
   * keyboard protocols are left alone: their stack may hold the shell's entry. */
  private sanitize(): void {
    this.write(ALT_SCREEN_LEAVE + MOUSE_OFF + BRACKETED_PASTE_OFF + SGR_RESET + CURSOR_SHOW);
  }

  /** Put the terminal back exactly as it was found. Safe to call twice, and
   * from an exit handler: it must not throw. */
  restore(): void {
    if (this.restored || !this.entered) {
      this.restored = true;
      return;
    }
    this.restored = true;
    // Turn the modes off while still in raw mode, then hand the line discipline back.
    try {
      this.write(
        // The alternate screen has a keyboard stack of its own.
        (this.alt ? this.screenKeyboardPop() + ALT_SCREEN_OFF : "") +
          (this.mouse ? MOUSE_OFF : "") +
          BRACKETED_PASTE_OFF +
          this.keyboardDisable() +
          SGR_RESET +
          CURSOR_SHOW,
      );
    } catch {
      // Best effort by definition.
    }
    try {
      if (process.stdin.isTTY) process.stdin.setRawMode(false);
    } catch {
      // The tty is gone.
    }
    this.removeListeners();
  }

  /** Mouse reporting on or off. Off by default: while it is on, the
   * terminal's own click-to-select stops working. */
  setMouse(enabled: boolean): void {
    if (enabled === this.mouse) return;
    this.mouse = enabled;
    this.write(enabled ? MOUSE_ON : MOUSE_OFF);
  }

  /** The alternate screen on or off. Each screen keeps its own kitty keyboard
   * stack, so the mode is pushed again on the way in and popped on the way out. */
  setAltScreen(enabled: boolean): void {
    if (enabled === this.alt) return;
    this.alt = enabled;
    if (enabled) this.write(ALT_SCREEN_ON + this.screenKeyboardPush());
    else this.write(this.screenKeyboardPop() + ALT_SCREEN_OFF);
  }

  get altScreen(): boolean {
    return this.alt;
  }

  /**
   * Hand the terminal back, stop this process, and take it again. `ctrl+z` in
   * raw mode is a key like any other, so this does by hand what the shell would
   * otherwise have done: put the modes back, stop, and on the way back turn
   * them on again. The caller repaints.
   */
  suspend(): void {
    if (!this.entered) return;
    const alt = this.alt;
    this.detachReader();
    this.write(
      (alt ? this.screenKeyboardPop() + ALT_SCREEN_OFF : "") +
        (this.mouse ? MOUSE_OFF : "") +
        BRACKETED_PASTE_OFF +
        this.keyboardDisable() +
        SGR_RESET +
        CURSOR_SHOW,
    );
    if (process.stdin.isTTY) process.stdin.setRawMode(false);
    process.kill(process.pid, "SIGTSTP");
    // Continued. Everything turned off above comes back on.
    if (process.stdin.isTTY) process.stdin.setRawMode(true);
    this.write(
      this.keyboardEnable() +
        (alt ? ALT_SCREEN_ON + this.screenKeyboardPush() : "") +
        (this.mouse ? MOUSE_ON : "") +
        BRACKETED_PASTE_ON +
        CURSOR_HIDE,
    );
    this.attachReader();
  }

  /**
   * Ask for modified keys to be reported, and find out how they will be: push
   * the kitty flags, ask which took, then ask for the device attributes. Every
   * terminal answers the last, in order - so an attributes answer with no
   * keyboard answer ahead of it means the push was ignored. No timer is
   * involved, which matters over a slow ssh link.
   */
  private keyboardNegotiate(): string {
    return KITTY_KEYBOARD_PUSH + KITTY_KEYBOARD_QUERY + DEVICE_ATTRIBUTES_QUERY;
  }

  private keyboardEnable(): string {
    return this.protocol === "modifyOtherKeys" ? MODIFY_OTHER_KEYS_ON : KITTY_KEYBOARD_PUSH;
  }

  /** Before the terminal has answered the pop is sent anyway: to a terminal
   * that ignored the push, it is ignored too. */
  private keyboardDisable(): string {
    return this.protocol === "modifyOtherKeys" ? MODIFY_OTHER_KEYS_OFF : KITTY_KEYBOARD_POP;
  }

  private screenKeyboardPush(): string {
    return this.protocol === "modifyOtherKeys" ? "" : KITTY_KEYBOARD_PUSH;
  }

  private screenKeyboardPop(): string {
    return this.protocol === "modifyOtherKeys" ? "" : KITTY_KEYBOARD_POP;
  }

  /** `kitty`, `modifyOtherKeys`, or `null` for the legacy encoding. */
  get keyboard(): KeyboardProtocol {
    return this.protocol;
  }

  /** Take the terminal's answers to the keyboard negotiation. */
  report(name: string, data: string): void {
    if (this.protocol !== null || this.restored) return;
    if (name === KEYBOARD_REPORT) {
      // Without disambiguation (1) the push did not take, and the attributes
      // answer behind this one decides instead.
      const flags = /^\d+$/.test(data) ? Number(data) : 0;
      if (flags & 1) this.protocol = "kitty";
    } else if (name === ATTRIBUTES_REPORT) {
      this.protocol = "modifyOtherKeys";
      this.write(MODIFY_OTHER_KEYS_ON);
    }
  }

  private on(
    target: NodeJS.EventEmitter,
    event: string | symbol,
    listener: (...args: never[]) => void,
  ): void {
    target.on(event, listener as never);
    this.listeners.push([event, listener]);
  }

  private removeListeners(): void {
    for (const [event, listener] of this.listeners)
      process.removeListener(event, listener as never);
    this.listeners.length = 0;
  }

  private installSafetyNet(): void {
    this.on(process, "exit", () => this.restore());
    this.on(process, "uncaughtException", (error: Error) => {
      this.restore();
      process.stderr.write(`${error.stack ?? error}\n`);
      process.exit(1);
    });
    // SIGTERM and SIGHUP otherwise kill the process with the modes still on.
    // SIGINT is left alone: in raw mode ctrl+c is a key, not a signal.
    for (const signal of ["SIGTERM", "SIGHUP"] as const) {
      const handler = () => {
        this.restore();
        process.removeListener(signal, handler);
        process.kill(process.pid, signal);
      };
      this.on(process, signal, handler);
    }
  }

  private attachReader(): void {
    process.stdin.on("data", this.reader);
    process.stdin.resume();
  }

  private detachReader(): void {
    process.stdin.off("data", this.reader);
    process.stdin.pause();
  }
}

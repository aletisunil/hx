/**
 * Asking the operating system which modifier keys are held.
 *
 * For the one key a terminal cannot describe. A terminal built on xterm.js -
 * the Claude desktop app's panel, VS Code's - and Apple's Terminal speak
 * neither the kitty keyboard protocol nor modifyOtherKeys, so shift+enter
 * reaches HX as a bare carriage return, or as ESC then carriage return. Nothing
 * on the wire tells those apart from enter and alt+enter.
 *
 * The keyboard still knows: on macOS the window server tracks which modifiers
 * are down, and any process may ask. So an enter, or an alt+enter, that arrives
 * while shift is held and option is not was shift+enter.
 *
 * Only asked on macOS, and not over ssh, where the keyboard of the machine HX
 * runs on is not the one in use. `HX_MODIFIER_PROBE=0` turns it off - for
 * input that is not typed at all, from `tmux send-keys` or a script.
 */

import { createRequire } from "node:module";

/** `kCGEventSourceStateCombinedSessionState`. */
const COMBINED_SESSION_STATE = 0;
const MASKS: Record<string, bigint> = {
  shift: 0x00020000n, // kCGEventFlagMaskShift
  ctrl: 0x00040000n, // kCGEventFlagMaskControl
  alt: 0x00080000n, // kCGEventFlagMaskAlternate - option
  super: 0x00100000n, // kCGEventFlagMaskCommand
};

type FlagsState = (state: number) => bigint | number;
let probe: FlagsState | null | undefined;

/** `CGEventSourceFlagsState`, or null where it cannot be asked. */
function flagsState(): FlagsState | null {
  if (probe !== undefined) return probe;
  probe = null;
  if (process.platform !== "darwin") return probe;
  if (process.env.SSH_CONNECTION || process.env.SSH_TTY) return probe;
  if ((process.env.HX_MODIFIER_PROBE ?? "").trim() === "0") return probe;
  try {
    // Loaded on first use, synchronously: the answer is needed for this enter.
    const koffi = createRequire(import.meta.url)("koffi");
    const graphics = koffi.load("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics");
    probe = graphics.func("uint64_t CGEventSourceFlagsState(int32_t state)") as FlagsState;
  } catch {
    probe = null;
  }
  return probe;
}

/** The modifiers down right now, or null where that is unknowable - which is
 * not the same as none held. */
export function held(): Set<string> | null {
  const fn = flagsState();
  if (fn === null) return null;
  let flags: bigint;
  try {
    flags = BigInt(fn(COMBINED_SESSION_STATE));
  } catch {
    return null; // a window server gone away is not worth a crash
  }
  return new Set(
    Object.entries(MASKS)
      .filter(([, mask]) => flags & mask)
      .map(([name]) => name),
  );
}

/** Whether `key` - `enter` or `alt+enter` as decoded - was shift+enter: shift
 * held and option not, so a real option+enter still steers. */
export function shiftEnter(key: string): boolean {
  if (key !== "enter" && key !== "alt+enter") return false;
  const modifiers = held();
  return (modifiers?.has("shift") ?? false) && !modifiers?.has("alt");
}

/** How soon after a shifted character an enter is taken to be rollover. */
const ROLLOVER_MS = 200;
const SHIFTED_SYMBOLS = new Set('~!@#$%^&*()_+{}|:"<>?');

/**
 * `shiftEnter`, minus the enter that only follows a shifted character: fast
 * typists release shift late, so `does this work?` then enter arrives with
 * shift still down.
 */
export class ShiftEnter {
  private shiftedAt = Number.NEGATIVE_INFINITY;

  /** Note what was just typed, for the enter that may follow it. */
  typed(text: string): void {
    const last = [...text].at(-1) ?? "";
    const shifted =
      (last !== last.toLowerCase() && last === last.toUpperCase()) || SHIFTED_SYMBOLS.has(last);
    this.shiftedAt = shifted ? performance.now() : Number.NEGATIVE_INFINITY;
  }

  test(key: string): boolean {
    if (performance.now() - this.shiftedAt < ROLLOVER_MS) return false;
    return shiftEnter(key);
  }
}

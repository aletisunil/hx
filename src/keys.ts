/**
 * Keybindings as one registry, so a key is described in exactly one place:
 * the app dispatches from it, and `/help`, the hints bar and the expand
 * affordances render their key names through it.
 *
 * Users override defaults in `~/.hx/keybindings.json`:
 *
 *     {"app.tools.expand": "ctrl+r", "app.message.copy": ["ctrl+x", "alt+c"]}
 */

import { compareCodePoints } from "./json.ts";
import { isFile, readText } from "./pathlib.ts";
import { keybindingsFile } from "./paths.ts";
import { repr } from "./py.ts";

/** One named action and the keys that trigger it. */
export interface KeyBinding {
  readonly id: string;
  readonly defaultKeys: readonly string[];
  readonly description: string;
  /** Shown in the compact hints bar under the prompt. */
  readonly hint: string | null;
  /** Checked first, and skips when it has nothing to do. */
  readonly priority: boolean;
}

const binding = (
  id: string,
  defaultKeys: string[],
  description: string,
  options: { hint?: string; priority?: boolean } = {},
): KeyBinding => ({
  id,
  defaultKeys,
  description,
  hint: options.hint ?? null,
  priority: options.priority ?? false,
});

/** True under WSL, where several terminals swallow the ctrl+shift chords. */
const wslStyle = () => Boolean(process.env.WSL_DISTRO_NAME || process.env.WSL_INTEROP);

function defaults(): Map<string, KeyBinding> {
  const wsl = wslStyle();
  const bindings = [
    // Turn control.
    binding("app.interrupt", ["escape"], "Cancel or abort", { hint: "interrupt" }),
    binding("app.selection.copy", ["ctrl+c"], "Copy the selected text", { priority: true }),
    binding("app.clear", ["ctrl+c"], "Clear the prompt (twice to exit)"),
    binding("app.exit", ["ctrl+d"], "Exit when the prompt is empty"),
    // Unbound: ctrl+z is undo. A user who wants job control back binds this.
    binding("app.suspend", [], "Suspend to the background"),
    // Session and mode.
    binding("app.mode.cycle", ["shift+tab"], "Cycle permission mode"),
    binding("app.commands", ["ctrl+p"], "Open the command palette"),
    binding("app.model.select", ["ctrl+l"], "Open the model picker"),
    binding("app.todos.toggle", ["ctrl+t"], "Show the current plan"),
    // Transcript.
    binding("app.tools.expand", ["ctrl+o", "ctrl+r"], "Expand tool output", { hint: "expand" }),
    binding("app.message.copy", ["ctrl+x"], "Copy the selected message"),
    binding("app.transcript.pageUp", ["pageup"], "Scroll the transcript up"),
    binding("app.transcript.pageDown", ["pagedown"], "Scroll the transcript down"),
    binding("app.transcript.top", ["ctrl+home"], "Jump to the start"),
    binding("app.transcript.bottom", ["ctrl+end"], "Jump to the end"),
    binding(
      "app.transcript.previousPrompt",
      wsl ? ["ctrl+up"] : ["ctrl+up", "ctrl+shift+up"],
      "Previous user message",
    ),
    binding(
      "app.transcript.nextPrompt",
      wsl ? ["ctrl+down"] : ["ctrl+down", "ctrl+shift+down"],
      "Next user message",
    ),
    // Prompt editing.
    binding("tui.input.submit", ["enter"], "Send the message"),
    binding("tui.input.steer", ["alt+enter"], "Steer the running turn"),
    binding("tui.input.newLine", ["shift+enter", "ctrl+j"], "Insert a newline"),
    binding("tui.input.complete", ["tab"], "Accept or cycle a completion"),
    // ctrl+v, not the platform paste: a terminal's own paste carries text only.
    binding("tui.input.pasteImage", ["ctrl+v"], "Paste an image from the clipboard"),
    binding("tui.editor.cursorLeft", ["ctrl+b"], "Move left one character"),
    binding("tui.editor.cursorRight", ["ctrl+f"], "Move right one character"),
    binding("tui.editor.cursorWordLeft", ["alt+b"], "Move left one word"),
    binding("tui.editor.cursorWordRight", ["alt+f"], "Move right one word"),
    binding("tui.editor.deleteWordForward", ["alt+d"], "Delete the word ahead"),
    binding("tui.editor.yank", ["ctrl+y"], "Yank the last kill"),
    binding("tui.editor.yankPop", ["alt+y"], "Cycle back through kills"),
    // cmd reaches HX as super, from a terminal that passes it through.
    binding("tui.editor.undo", ["ctrl+z", "super+z", "ctrl+underscore"], "Undo"),
    binding("tui.editor.redo", ["ctrl+shift+z", "super+shift+z"], "Redo"),
  ];
  return new Map(bindings.map((b) => [b.id, b]));
}

/** Modifiers as a Mac user writes them, spelled the way the decoder names them. */
const ALIASES: Record<string, string> = { cmd: "super", command: "super", option: "alt" };

const canonical = (key: string) =>
  key
    .split("+")
    .map((part) => ALIASES[part] ?? part)
    .join("+");

function normalize(value: unknown): string[] | null {
  if (typeof value === "string") return [canonical(value)];
  if (Array.isArray(value) && value.every((item) => typeof item === "string"))
    return value.map(canonical);
  return null;
}

/** Key names spelled the way a terminal user writes them. */
const DISPLAY: Record<string, string> = {
  escape: "esc",
  pageup: "pgup",
  pagedown: "pgdn",
  delete: "del",
  underscore: "_",
  backslash: "\\",
  circumflex_accent: "^",
  right_square_bracket: "]",
};

/** `escape` -> `esc`, and `alt` -> `option` on macOS, as pi does. */
export function displayKey(key: string): string {
  return key
    .split("+")
    .map((raw) => {
      const part = DISPLAY[raw] ?? raw;
      if (part === "alt" && process.platform === "darwin") return "option";
      if (part === "super" && process.platform === "darwin") return "cmd";
      return part;
    })
    .join("+");
}

/** Resolved keys per action, plus whatever went wrong resolving them. */
export class Keymap {
  constructor(
    readonly bindings: Map<string, KeyBinding>,
    readonly keys: Map<string, readonly string[]>,
    readonly problems: string[] = [],
  ) {}

  keysFor(action: string): readonly string[] {
    return this.keys.get(action) ?? [];
  }

  /** `ctrl+o/ctrl+r` - every key for an action, for `/help`. */
  text(action: string): string {
    return this.keysFor(action).map(displayKey).join("/");
  }

  /** Just the first key: what a compact hint should say. */
  primary(action: string): string {
    const keys = this.keysFor(action);
    return keys.length ? displayKey(keys[0] as string) : "";
  }

  description(action: string): string {
    return this.bindings.get(action)?.description ?? action;
  }

  /** Whether `key` triggers `action`. */
  matches(action: string, key: string): boolean {
    return this.keysFor(action).includes(key);
  }
}

/**
 * Actions sharing a key. Reported, never silently resolved - except a
 * `priority` action, which is meant to share: it runs first and skips when it
 * has nothing to do. Two priority actions on one key is still a conflict.
 */
function conflicts(
  keys: Map<string, readonly string[]>,
  bindings: Map<string, KeyBinding>,
): string[] {
  const isPriority = (action: string) => bindings.get(action)?.priority ?? false;
  const owners = new Map<string, string[]>();
  for (const [action, actionKeys] of keys) {
    for (const key of actionKeys) owners.set(key, [...(owners.get(key) ?? []), action]);
  }
  const problems: string[] = [];
  for (const key of [...owners.keys()].sort(compareCodePoints)) {
    const actions = owners.get(key) as string[];
    const contenders = actions.filter((action) => !isPriority(action));
    const prioritised = actions.filter(isPriority);
    const clashing = prioritised.length <= 1 ? contenders : actions;
    if (clashing.length > 1) problems.push(`${key} is bound to ${clashing.join(" and ")}`);
  }
  return problems;
}

/** Defaults, with `~/.hx/keybindings.json` layered on top. A broken override
 * file costs the user their overrides, not their ability to start HX. */
export function loadKeymap(given?: Record<string, unknown>): Keymap {
  const bindings = defaults();
  const keys = new Map<string, readonly string[]>(
    [...bindings].map(([action, b]) => [action, b.defaultKeys]),
  );
  const problems: string[] = [];
  let overrides = given;
  if (overrides === undefined) {
    overrides = {};
    const path = keybindingsFile();
    if (isFile(path)) {
      let loaded: unknown = {};
      try {
        loaded = JSON.parse(readText(path));
      } catch (error) {
        problems.push(`${path}: ${(error as Error).message}`);
      }
      if (typeof loaded === "object" && loaded !== null && !Array.isArray(loaded))
        overrides = loaded as Record<string, unknown>;
      else if (loaded) problems.push(`${path}: expected a JSON object of action -> key`);
    }
  }
  for (const [action, value] of Object.entries(overrides)) {
    if (!bindings.has(action)) {
      problems.push(`unknown action ${repr(action)} in keybindings`);
      continue;
    }
    const normalized = normalize(value);
    if (normalized === null) {
      problems.push(`${action}: expected a key or list of keys`);
      continue;
    }
    keys.set(action, normalized);
  }
  problems.push(...conflicts(keys, bindings));
  return new Keymap(bindings, keys, problems);
}

let resolved: Keymap | null = null;

/** The keymap for this process, resolved on first use. */
export function keymap(): Keymap {
  resolved ??= loadKeymap();
  return resolved;
}

/** Every key bound to `action`, joined with `/`. */
export const keyText = (action: string) => keymap().text(action);
/** The first key bound to `action`, for compact hints. */
export const primaryKey = (action: string) => keymap().primary(action);
/** `[key, description]` for the hints bar. */
export const keyHint = (action: string, description?: string): [string, string] => [
  keyText(action),
  description ?? keymap().description(action),
];

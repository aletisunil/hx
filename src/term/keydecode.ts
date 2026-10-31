/**
 * Turning what the terminal sends into the key names the app already uses.
 *
 * A terminal delivers keys as bytes, and the encoding is a pile of conventions
 * rather than a standard: an arrow is three or six bytes depending on whether a
 * modifier is held, Escape is indistinguishable from the start of every escape
 * sequence, and `shift+enter` cannot be expressed at all unless the terminal
 * speaks a newer protocol. All of that is dealt with here, once.
 *
 * `decode` is a pure function from a string to key events. The names it
 * produces - `ctrl+c`, `shift+tab`, `alt+enter` - are the ones the keybinding
 * registry and `~/.hx/keybindings.json` use.
 */

export const ESC = "\x1b";
export const PASTE_START = "\x1b[200~";
export const PASTE_END = "\x1b[201~";

/** One key event. `name` is what bindings match on; `data` is the text the
 * key inserts, empty for anything that is not typing. */
export class Key {
  constructor(
    readonly name: string,
    readonly data = "",
  ) {}

  get isText(): boolean {
    return this.name === "text" || this.name === "paste";
  }
}

/** The final byte of a CSI sequence, and what it means. */
const CSI_FINAL: Record<string, string> = {
  A: "up",
  B: "down",
  C: "right",
  D: "left",
  H: "home",
  F: "end",
  E: "begin",
  P: "f1",
  Q: "f2",
  R: "f3",
  S: "f4",
  Z: "shift+tab", // back-tab, carrying its shift in the name
};

/** `CSI <number> ~` sequences. */
const CSI_TILDE: Record<number, string> = {
  1: "home",
  2: "insert",
  3: "delete",
  4: "end",
  5: "pageup",
  6: "pagedown",
  7: "home",
  8: "end",
  11: "f1",
  12: "f2",
  13: "f3",
  14: "f4",
  15: "f5",
  17: "f6",
  18: "f7",
  19: "f8",
  20: "f9",
  21: "f10",
  23: "f11",
  24: "f12",
};

/** Control characters with a name of their own rather than a `ctrl+` one. */
const NAMED_CONTROLS: Record<string, string> = {
  "\x00": "ctrl+space",
  "\x08": "backspace", // ctrl+h on some terminals
  "\x09": "tab",
  "\x0a": "ctrl+j", // LF: a real ctrl+j, since Enter sends CR in raw mode
  "\x0d": "enter",
  "\x1b": "escape",
  "\x7f": "backspace",
};

/** A CSI sequence per ECMA-48, admitting the private markers a terminal's
 * *answers* use - `CSI ? 1 u` is a reply about the keyboard protocol. */
const CSI_PATTERN = /\x1b\[([0-?]*)([ -/]*)([@-~])/y;
const SS3_PATTERN = /\x1bO([@-~])/y;
const MOUSE_PATTERN = /\x1b\[<([0-9]+);([0-9]+);([0-9]+)([Mm])/y;

/** Names under this prefix are the terminal answering a query, not keys. */
export const REPORT_PREFIX = "report:";
/** The answer to `CSI ? u`: which kitty keyboard flags are on. */
export const KEYBOARD_REPORT = "report:keyboard";
/** The answer to `CSI c`. Every terminal answers it, so it arriving *alone*
 * means the keyboard query went unanswered. */
export const ATTRIBUTES_REPORT = "report:attributes";

/** Kitty's numbers for keypad keys, which have to become characters again. */
const KITTY_KEYPAD_TEXT: Record<number, string> = {
  ...Object.fromEntries(Array.from({ length: 10 }, (_, digit) => [57399 + digit, String(digit)])),
  57409: ".",
  57410: "/",
  57411: "*",
  57412: "-",
  57413: "+",
  57415: "=",
  57416: ",",
};
const KITTY_KEYPAD_NAMED: Record<number, string> = {
  57414: "enter",
  57417: "left",
  57418: "right",
  57419: "up",
  57420: "down",
  57421: "pageup",
  57422: "pagedown",
  57423: "home",
  57424: "end",
  57425: "insert",
  57426: "delete",
  57427: "begin",
};

/** `ctrl` plus a punctuation key, named the way a legacy terminal's C0 byte
 * is: the same physical keys produce both encodings. */
const CTRL_PUNCTUATION: Record<string, string> = {
  " ": "space",
  "@": "space",
  _: "underscore",
  "-": "underscore",
  "/": "underscore",
  "\\": "backslash",
  "]": "right_square_bracket",
  "^": "circumflex_accent",
};

/** Kitty's event type for a key coming up: acted on as a press, every key
 * would be typed twice. */
const KEY_RELEASE = 3;

/** Decode the modifier parameter, a bitmask plus one. The locks are state
 * rather than part of a chord, so they are dropped. */
function modifiers(value: number): string[] {
  const bits = Math.max(0, value - 1);
  const out: string[] = [];
  if (bits & 8) out.push("super");
  if (bits & 16) out.push("hyper");
  if (bits & 32) out.push("meta");
  if (bits & 4) out.push("ctrl");
  if (bits & 2) out.push("alt");
  if (bits & 1) out.push("shift");
  return out;
}

function withModifiers(base: string, mods: string[]): string {
  if (mods.length === 0) return base;
  // shift+tab already says so.
  if (base === "shift+tab" && mods.length === 1 && mods[0] === "shift") return base;
  return [...mods, base].join("+");
}

/** `ctrl+<letter>` for a C0 control that has no name of its own. */
function controlName(char: string): string {
  const code = char.charCodeAt(0);
  if (code >= 1 && code <= 26) return `ctrl+${String.fromCharCode(code + 96)}`;
  if (code === 28) return "ctrl+backslash";
  if (code === 29) return "ctrl+right_square_bracket";
  if (code === 30) return "ctrl+circumflex_accent";
  if (code === 31) return "ctrl+underscore";
  return "unknown";
}

/**
 * Decode as much of `buffer` as is complete. Returns the keys found and
 * whatever trailing bytes were an escape sequence cut in half, to be carried
 * into the next call.
 */
export function decode(buffer: string): [Key[], string] {
  const keys: Key[] = [];
  let index = 0;
  while (index < buffer.length) {
    const char = buffer[index] as string;
    if (char !== ESC) {
      const [consumed, key] = decodePlain(buffer, index);
      if (key !== null) keys.push(key);
      index = consumed;
      continue;
    }
    // A lone Escape at the end of a read could be the key or the start of a
    // sequence still in flight.
    if (index + 1 >= buffer.length) return [keys, buffer.slice(index)];
    const following = buffer[index + 1] as string;
    if (following === "[") {
      const [consumed, produced, incomplete] = decodeCsi(buffer, index);
      if (incomplete) return [keys, buffer.slice(index)];
      keys.push(...produced);
      index = consumed;
      continue;
    }
    if (following === "O") {
      SS3_PATTERN.lastIndex = index;
      const match = SS3_PATTERN.exec(buffer);
      if (match === null) return [keys, buffer.slice(index)];
      keys.push(new Key(CSI_FINAL[match[1] as string] ?? "unknown"));
      index = SS3_PATTERN.lastIndex;
      continue;
    }
    if (following === ESC) {
      // Two escapes: the first is a real Escape press.
      keys.push(new Key("escape"));
      index++;
      continue;
    }
    // ESC then anything else is alt+that.
    const named = NAMED_CONTROLS[following];
    if (named !== undefined) keys.push(new Key(`alt+${named}`));
    else if (following.charCodeAt(0) < 0x20) keys.push(new Key(`alt+${controlName(following)}`));
    else {
      const cp = buffer.codePointAt(index + 1) as number;
      const glyph = String.fromCodePoint(cp);
      keys.push(new Key(`alt+${glyph}`));
      index += glyph.length - 1;
    }
    index += 2;
  }
  return [keys, ""];
}

/** A control character, or a run of printable text. */
function decodePlain(buffer: string, index: number): [number, Key | null] {
  const char = buffer[index] as string;
  const named = NAMED_CONTROLS[char];
  if (named !== undefined) return [index + 1, new Key(named)];
  if (char.charCodeAt(0) < 0x20) return [index + 1, new Key(controlName(char), char)];
  // Printable characters are batched, so a paste into a terminal without
  // bracketed paste is one event rather than hundreds.
  let end = index;
  while (end < buffer.length && (buffer[end] as string) >= " " && buffer[end] !== "\x7f") end++;
  return [end, new Key("text", buffer.slice(index, end))];
}

/** Decode one `CSI` sequence. The third result means "not all here yet". */
function decodeCsi(buffer: string, index: number): [number, Key[], boolean] {
  if (buffer.startsWith(PASTE_START, index)) {
    const end = buffer.indexOf(PASTE_END, index);
    if (end === -1) return [index, [], true]; // the paste is still arriving
    return [
      end + PASTE_END.length,
      [new Key("paste", buffer.slice(index + PASTE_START.length, end))],
      false,
    ];
  }
  MOUSE_PATTERN.lastIndex = index;
  const mouse = MOUSE_PATTERN.exec(buffer);
  if (mouse !== null) {
    const [, button, column, row, kind] = mouse;
    return [MOUSE_PATTERN.lastIndex, [new Key(`mouse:${button}:${column}:${row}:${kind}`)], false];
  }
  CSI_PATTERN.lastIndex = index;
  const match = CSI_PATTERN.exec(buffer);
  // Incomplete, or unrecognised: the safe reading costs one keystroke at worst.
  if (match === null) return [index, [], true];
  const [, params = "", intermediates = "", final = ""] = match;
  const end = CSI_PATTERN.lastIndex;

  if (params.startsWith("?") && !intermediates) {
    // An answer to one of HX's own queries, not a key.
    if (final === "u") return [end, [new Key(KEYBOARD_REPORT, params.slice(1))], false];
    if (final === "c") return [end, [new Key(ATTRIBUTES_REPORT, params.slice(1))], false];
  }
  if (intermediates || ["<", "=", ">", "?"].includes(params.slice(0, 1))) return [end, [], false];

  const fields = params
    .split(";")
    .map((field) => field.split(":").map((p) => (/^\d+$/.test(p) ? Number(p) : 0)));
  const second = fields[1];
  const modifier = second?.length ? (second[0] as number) : 1;
  const event = second && second.length > 1 ? (second[1] as number) : 1;
  if (event === KEY_RELEASE) return [end, [], false];

  const first = fields[0] as number[];
  if (final === "u") {
    // The kitty keyboard protocol: `CSI code[:shifted[:base]] ; mods u`.
    const [code = 0, ...alternates] = first;
    const shifted = alternates[0] ? alternates[0] : null;
    const base = alternates.length > 1 && alternates[1] ? alternates[1] : null;
    const key = unicodeKey(code, shifted, base, modifiers(modifier));
    return [end, key !== null ? [key] : [], false];
  }
  if (final === "~") {
    const number = first[0] as number;
    if (number === 27 && fields.length > 2) {
      // xterm's modifyOtherKeys: `CSI 27 ; mods ; code ~`, which tmux sends.
      const key = unicodeKey((fields[2] as number[])[0] as number, null, null, modifiers(modifier));
      return [end, key !== null ? [key] : [], false];
    }
    // Strictly shift+F3, but also what remapped terminals send for
    // shift+enter - and HX binds no function keys.
    if (number === 13 && modifier === 2) return [end, [new Key("shift+enter")], false];
    return [
      end,
      [new Key(withModifiers(CSI_TILDE[number] ?? "unknown", modifiers(modifier)))],
      false,
    ];
  }
  const finalName = CSI_FINAL[final];
  if (finalName !== undefined)
    return [end, [new Key(withModifiers(finalName, modifiers(modifier)))], false];
  return [end, [], false];
}

/**
 * A key reported by its codepoint, under either extended encoding, named the
 * way the legacy encoding names the same key wherever it can - except what the
 * legacy encoding throws away: `ctrl+shift+z` is itself here.
 */
function unicodeKey(
  code: number,
  shifted: number | null,
  base: number | null,
  mods: string[],
): Key | null {
  const keypad = KITTY_KEYPAD_NAMED[code];
  if (keypad !== undefined) return new Key(withModifiers(keypad, mods));
  const keypadText = KITTY_KEYPAD_TEXT[code];
  if (keypadText !== undefined)
    return mods.length ? new Key(withModifiers(keypadText, mods)) : new Key("text", keypadText);
  if (code < 0x20 || code === 0x7f) {
    const char = String.fromCharCode(code);
    return new Key(withModifiers(NAMED_CONTROLS[char] ?? controlName(char), mods));
  }
  // Kitty's other private-use keys - media keys, lone modifiers: never text.
  if (!(code > 0 && code <= 0x10ffff) || (code >= 0xe000 && code <= 0xf8ff)) return null;

  const chording = mods.filter((mod) => mod !== "shift");
  if (chording.length === 0) {
    // Shift alone, or nothing: this is typing.
    if (!mods.includes("shift")) return new Key("text", String.fromCodePoint(code));
    return new Key(
      "text",
      shifted ? String.fromCodePoint(shifted) : String.fromCodePoint(code).toUpperCase(),
    );
  }
  let char = String.fromCodePoint(code);
  // A non-Latin layout: shortcuts are meant by position, and `base` is that key.
  if (code > 0x7f && base) char = String.fromCodePoint(base);

  if (mods.includes("ctrl")) {
    if (mods.includes("shift") && shifted && String.fromCodePoint(shifted) in CTRL_PUNCTUATION) {
      char = String.fromCodePoint(shifted);
    }
    const punctuation = CTRL_PUNCTUATION[char];
    if (punctuation !== undefined) {
      // A legacy terminal cannot tell these apart, so neither may we.
      return new Key(
        withModifiers(
          punctuation,
          mods.filter((mod) => mod !== "shift"),
        ),
      );
    }
    return new Key(withModifiers(char === " " ? "space" : char.toLowerCase(), mods));
  }
  // super, hyper and meta have no legacy encoding: shift is named, as a user writes it.
  if (chording.some((mod) => mod !== "alt")) {
    return new Key(withModifiers(char === " " ? "space" : char.toLowerCase(), mods));
  }
  // alt alone. The legacy encoding sends ESC then the shifted character, so
  // alt+shift+b has always been alt+B.
  let rest = mods;
  if (mods.includes("shift")) {
    char = shifted ? String.fromCodePoint(shifted) : char.toUpperCase();
    rest = mods.filter((mod) => mod !== "shift");
  }
  return new Key(withModifiers(char === " " ? "space" : char, rest));
}

/** Stateful wrapper that carries an incomplete sequence between reads. */
export class Decoder {
  private pending = "";

  feed(data: string): Key[] {
    const [keys, pending] = decode(this.pending + data);
    this.pending = pending;
    return keys;
  }

  /** Flush a held-back sequence as-is. A lone Escape that no more bytes
   * followed was the Escape key. */
  takePending(): Key[] {
    if (!this.pending) return [];
    const pending = this.pending;
    this.pending = "";
    if (pending === ESC) return [new Key("escape")];
    return decode(`${pending} `)[0];
  }
}

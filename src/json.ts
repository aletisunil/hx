/**
 * JSON written byte-for-byte as Python's `json.dumps` writes it.
 *
 * Every file HX keeps on disk - settings, credentials, sessions, the model
 * cache - was first written by Python, and a user's files carry its spacing:
 * `", "` and `": "` between items, non-ASCII escaped as `\uXXXX`, `1.0` for a
 * whole float. `JSON.stringify` differs on all of these, so a settings file
 * rewritten by `JSON.stringify` would show up as changed on every line.
 */

import { floatRepr } from "./py.ts";

/**
 * A number Python holds as a float, so it is written as `1.0` rather than `1`.
 *
 * JavaScript has one number type; where HX's Python wrote a float - a
 * timestamp from `time.time()`, a cost in dollars - the value is wrapped in
 * this so the file reads the same.
 */
export class PyFloat {
  constructor(readonly value: number) {}
  valueOf(): number {
    return this.value;
  }
}

export interface DumpOptions {
  /** Spaces per level; `undefined` writes everything on one line. */
  readonly indent?: number;
  readonly sortKeys?: boolean;
  /** Escape every non-ASCII character as `\uXXXX`. Python's default. */
  readonly ensureAscii?: boolean;
}

/** `json.dumps(value, indent=..., sort_keys=..., ensure_ascii=...)`. */
export function dumps(value: unknown, options: DumpOptions = {}): string {
  const ensureAscii = options.ensureAscii ?? true;
  const indent = options.indent;
  const itemSeparator = indent === undefined ? ", " : ",";
  const sortKeys = options.sortKeys ?? false;

  const write = (node: unknown, depth: number): string => {
    if (node === null || node === undefined) return "null";
    if (node === true) return "true";
    if (node === false) return "false";
    if (node instanceof PyFloat) return number(node.value, true);
    if (typeof node === "number") return number(node, false);
    if (typeof node === "bigint") return node.toString();
    if (typeof node === "string") return string(node, ensureAscii);
    if (Array.isArray(node)) {
      if (node.length === 0) return "[]";
      const items = node.map((item) => write(item, depth + 1));
      return wrap("[", "]", items, depth);
    }
    if (typeof node === "object") {
      let keys = Object.keys(node).filter(
        (key) => (node as Record<string, unknown>)[key] !== undefined,
      );
      if (keys.length === 0) return "{}";
      if (sortKeys) keys = [...keys].sort(compareCodePoints);
      const items = keys.map(
        (key) =>
          `${string(key, ensureAscii)}: ${write((node as Record<string, unknown>)[key], depth + 1)}`,
      );
      return wrap("{", "}", items, depth);
    }
    throw new TypeError(`Object of type ${typeof node} is not JSON serializable`);
  };

  const wrap = (open: string, close: string, items: string[], depth: number): string => {
    if (indent === undefined) return open + items.join(itemSeparator) + close;
    const inner = `\n${" ".repeat(indent * (depth + 1))}`;
    const outer = `\n${" ".repeat(indent * depth)}`;
    return open + inner + items.join(itemSeparator + inner) + outer + close;
  };

  return write(value, 0);
}

function number(value: number, isFloat: boolean): string {
  if (Number.isNaN(value)) return "NaN";
  if (value === Infinity) return "Infinity";
  if (value === -Infinity) return "-Infinity";
  if (!isFloat && Number.isInteger(value)) return String(value);
  return floatRepr(value);
}

function string(text: string, ensureAscii: boolean): string {
  let out = '"';
  for (let index = 0; index < text.length; index++) {
    const char = text[index] as string;
    const code = text.charCodeAt(index);
    if (char === '"') out += '\\"';
    else if (char === "\\") out += "\\\\";
    else if (char === "\n") out += "\\n";
    else if (char === "\r") out += "\\r";
    else if (char === "\t") out += "\\t";
    else if (char === "\b") out += "\\b";
    else if (char === "\f") out += "\\f";
    else if (code < 0x20 || (ensureAscii && code > 0x7f)) {
      out += `\\u${code.toString(16).padStart(4, "0")}`;
    } else out += char;
  }
  return `${out}"`;
}

/** Python's `str` ordering: by code point, where JavaScript's default compares UTF-16 units. */
export function compareCodePoints(a: string, b: string): number {
  const left = [...a];
  const right = [...b];
  for (let index = 0; index < Math.min(left.length, right.length); index++) {
    const difference =
      ((left[index] as string).codePointAt(0) as number) -
      ((right[index] as string).codePointAt(0) as number);
    if (difference !== 0) return difference;
  }
  return left.length - right.length;
}

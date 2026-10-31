/**
 * Python's semantics, where HX's behaviour depends on them.
 *
 * HX was first written in Python, and what it prints, writes and accepts was
 * defined by Python's string, number and path rules. A user's settings files,
 * sessions and scripts were written against that behaviour, so where JavaScript
 * differs - which characters count as whitespace, how a path prints, what
 * `bool()` makes of a value - the difference is settled here, once.
 */

/** Characters `str.isspace()` accepts. JavaScript's `\s` adds U+FEFF and drops U+001C-U+001F and U+0085. */
export const SPACE =
  "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000";

export function isSpace(char: string): boolean {
  return char.length > 0 && SPACE.includes(char);
}

/** `str.strip()`, or `str.strip(chars)` when `chars` is given. */
export function strip(text: string, chars?: string): string {
  return lstrip(rstrip(text, chars), chars);
}

/** `str.lstrip()`, or `str.lstrip(chars)`. */
export function lstrip(text: string, chars?: string): string {
  const drop = (char: string) => (chars === undefined ? isSpace(char) : chars.includes(char));
  let start = 0;
  while (start < text.length && drop(text[start] as string)) start++;
  return text.slice(start);
}

/** `str.rstrip()`, or `str.rstrip(chars)`. */
export function rstrip(text: string, chars?: string): string {
  const drop = (char: string) => (chars === undefined ? isSpace(char) : chars.includes(char));
  let end = text.length;
  while (end > 0 && drop(text[end - 1] as string)) end--;
  return text.slice(0, end);
}

/** `str.split()` with no separator: runs of whitespace, no empty fields. */
export function splitWhitespace(text: string): string[] {
  const fields: string[] = [];
  let current = "";
  for (const char of text) {
    if (isSpace(char)) {
      if (current) fields.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  if (current) fields.push(current);
  return fields;
}

/** `str.splitlines()`: every line break Python knows, no trailing empty line. */
export function splitLines(text: string): string[] {
  const lines = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/);
  if (lines.at(-1) === "") lines.pop();
  return lines;
}

/** `str.isalnum()` for one character: a letter or a number of any script. */
export function isAlnum(char: string): boolean {
  return /^[\p{L}\p{N}]$/u.test(char);
}

/** `bool(value)` for a value read from JSON. */
export function truthy(value: unknown): boolean {
  if (value === null || value === undefined || value === false) return false;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return value.length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

/** `int(value)` for a value read from JSON or the environment. Throws where Python raises. */
export function toInt(value: unknown): number {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error(`cannot convert float ${value} to integer`);
    return Math.trunc(value);
  }
  if (typeof value === "string") {
    const cleaned = strip(value);
    if (/^[+-]?\d+(_\d+)*$/.test(cleaned)) return Number.parseInt(cleaned.replaceAll("_", ""), 10);
  }
  throw new Error(`invalid literal for int() with base 10: ${repr(value)}`);
}

/** `float(value)` for a value read from JSON or the environment. Throws where Python raises. */
export function toFloat(value: unknown): number {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const cleaned = strip(value).toLowerCase();
    if (/^[+-]?(inf|infinity)$/.test(cleaned))
      return cleaned.startsWith("-") ? -Infinity : Infinity;
    if (/^[+-]?nan$/.test(cleaned)) return Number.NaN;
    if (/^[+-]?(\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(e[+-]?\d+)?$/.test(cleaned)) {
      return Number.parseFloat(cleaned.replaceAll("_", ""));
    }
  }
  throw new Error(`could not convert string to float: ${repr(value)}`);
}

/** `repr(value)` for the values HX puts in messages: JSON values, as Python holds them. */
export function repr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : floatRepr(value);
  if (Array.isArray(value)) return `[${value.map(repr).join(", ")}]`;
  if (typeof value === "object") {
    const items = Object.entries(value as Record<string, unknown>).map(
      ([k, v]) => `${repr(k)}: ${repr(v)}`,
    );
    return `{${items.join(", ")}}`;
  }
  if (typeof value !== "string") return String(value);
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  let out = quote;
  for (const char of value) {
    const code = char.codePointAt(0) as number;
    if (char === quote || char === "\\") out += `\\${char}`;
    else if (char === "\n") out += "\\n";
    else if (char === "\r") out += "\\r";
    else if (char === "\t") out += "\\t";
    else if (code < 0x20 || code === 0x7f) out += `\\x${hex(code, 2)}`;
    else if (!isPrintable(char)) {
      if (code <= 0xff) out += `\\x${hex(code, 2)}`;
      else if (code <= 0xffff) out += `\\u${hex(code, 4)}`;
      else out += `\\U${hex(code, 8)}`;
    } else out += char;
  }
  return out + quote;
}

/** `str.isprintable()` for one character, which `repr` uses to decide what to escape. */
function isPrintable(char: string): boolean {
  if (char === " ") return true;
  return !/^[\p{C}\p{Z}]$/u.test(char);
}

function hex(code: number, width: number): string {
  return code.toString(16).padStart(width, "0");
}

/** Python's `repr(float)`: shortest round-trip digits, `1.0` for whole numbers, `1e-05` style exponents. */
export function floatRepr(value: number): string {
  if (Number.isNaN(value)) return "nan";
  if (value === Infinity) return "inf";
  if (value === -Infinity) return "-inf";
  if (Number.isInteger(value) && Math.abs(value) < 1e16) return `${value}.0`;
  const [mantissa, exponent] = value.toExponential().split("e") as [string, string];
  const power = Number(exponent);
  if (power < -4 || power >= 16) {
    const sign = power < 0 ? "-" : "+";
    return `${mantissa}e${sign}${String(Math.abs(power)).padStart(2, "0")}`;
  }
  return String(value);
}

/**
 * `str(PurePosixPath(text))`: repeated and trailing slashes collapsed, `.` parts
 * dropped, `..` kept. Paths print this way in every message HX writes.
 */
export function purePath(text: string): string {
  if (text === "") return ".";
  const root =
    text.startsWith("//") && !text.startsWith("///") ? "//" : text.startsWith("/") ? "/" : "";
  const parts = text.split("/").filter((part) => part !== "" && part !== ".");
  const joined = root + parts.join("/");
  return joined === "" ? "." : joined;
}

/** `strerror()` for the errors a file operation meets, as Python prints them. */
const STRERROR: Record<string, string> = {
  EPERM: "Operation not permitted",
  ENOENT: "No such file or directory",
  EIO: "Input/output error",
  EBADF: "Bad file descriptor",
  EACCES: "Permission denied",
  EBUSY: "Resource busy",
  EEXIST: "File exists",
  EXDEV: "Cross-device link",
  ENOTDIR: "Not a directory",
  EISDIR: "Is a directory",
  EINVAL: "Invalid argument",
  EMFILE: "Too many open files",
  EFBIG: "File too large",
  ENOSPC: "No space left on device",
  EROFS: "Read-only file system",
  EPIPE: "Broken pipe",
  ELOOP: "Too many levels of symbolic links",
  ENAMETOOLONG: "File name too long",
  ENOTEMPTY: "Directory not empty",
};

/**
 * `str(OSError)` for a Node file system error: `[Errno 2] No such file or
 * directory: 'path'`. Messages that name a failed file read the way they did
 * before the rewrite.
 */
export function osErrorText(error: unknown): string {
  const failure = error as NodeJS.ErrnoException;
  const code = failure.code ?? "";
  const number = failure.errno === undefined ? undefined : Math.abs(failure.errno);
  const message = STRERROR[code];
  if (number === undefined || message === undefined) return failure.message;
  const where = failure.path === undefined ? "" : `: ${repr(failure.path)}`;
  return `[Errno ${number}] ${message}${where}`;
}

/**
 * The exact decimal value of a double, as `[digits, scale]`: the value is
 * `digits / 10**scale`. Every double is a finite binary fraction, so this is exact.
 */
function exactDecimal(value: number): [bigint, number] {
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, Math.abs(value));
  const bits = view.getBigUint64(0);
  const exponentBits = Number((bits >> 52n) & 0x7ffn);
  let mantissa = bits & 0xfffffffffffffn;
  let exponent: number;
  if (exponentBits === 0) exponent = -1074;
  else {
    mantissa |= 1n << 52n;
    exponent = exponentBits - 1075;
  }
  if (exponent >= 0) return [mantissa << BigInt(exponent), 0];
  return [mantissa * 5n ** BigInt(-exponent), -exponent];
}

/** The integer nearest `digits / 10**shift`, ties to even. */
function roundHalfEven(digits: bigint, shift: number): bigint {
  if (shift <= 0) return digits * 10n ** BigInt(-shift);
  const divisor = 10n ** BigInt(shift);
  const quotient = digits / divisor;
  const twice = (digits % divisor) * 2n;
  if (twice > divisor || (twice === divisor && quotient % 2n === 1n)) return quotient + 1n;
  return quotient;
}

/**
 * `f"{value:.{places}f}"`. Rounds the exact binary value with ties to even,
 * where `toFixed` rounds ties up: `1.25` is `1.2` here and `1.3` there.
 */
export function formatFixed(value: number, places: number, grouping = false): string {
  if (!Number.isFinite(value)) return Number.isNaN(value) ? "nan" : value > 0 ? "inf" : "-inf";
  const [digits, scale] = exactDecimal(value);
  const rounded = roundHalfEven(digits, scale - places)
    .toString()
    .padStart(places + 1, "0");
  let whole = rounded.slice(0, rounded.length - places);
  const fraction = rounded.slice(rounded.length - places);
  if (grouping) whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const sign = value < 0 || Object.is(value, -0) ? "-" : "";
  return sign + whole + (places > 0 ? `.${fraction}` : "");
}

/** `round(value)`: the nearest integer, ties to even. */
export function pyRound(value: number): number {
  return Number(formatFixed(value, 0));
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** `time.strftime(format, time.localtime(seconds))` for the C locale's codes HX uses. */
export function strftime(format: string, seconds: number): string {
  const date = new Date(seconds * 1000);
  const two = (n: number) => String(n).padStart(2, "0");
  return format.replace(/%([abdHIMmpSYy%])/g, (_, code: string) => {
    switch (code) {
      case "a":
        return DAYS[date.getDay()] as string;
      case "b":
        return MONTHS[date.getMonth()] as string;
      case "d":
        return two(date.getDate());
      case "H":
        return two(date.getHours());
      case "I":
        return two(date.getHours() % 12 || 12);
      case "M":
        return two(date.getMinutes());
      case "m":
        return two(date.getMonth() + 1);
      case "p":
        return date.getHours() < 12 ? "AM" : "PM";
      case "S":
        return two(date.getSeconds());
      case "Y":
        return String(date.getFullYear());
      case "y":
        return two(date.getFullYear() % 100);
      default:
        return "%";
    }
  });
}

/** `urllib.parse.quote_plus`: what Python leaves unescaped differs from `URLSearchParams` at `*` and `~`. */
export function quotePlus(text: string): string {
  let out = "";
  for (const byte of Buffer.from(text, "utf8")) {
    const char = String.fromCharCode(byte);
    if (/[A-Za-z0-9_.\-~]/.test(char)) out += char;
    else if (char === " ") out += "+";
    else out += `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return out;
}

/** `urllib.parse.urlencode(params)`. */
export function urlencode(params: Record<string, string>): string {
  return Object.entries(params)
    .map(([key, value]) => `${quotePlus(key)}=${quotePlus(value)}`)
    .join("&");
}

/** `len(text)`: code points, where `.length` counts UTF-16 units. */
export function pyLen(text: string): number {
  let surrogates = 0;
  for (let index = 0; index < text.length; index++) {
    const code = text.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        surrogates++;
        index++;
      }
    }
  }
  return text.length - surrogates;
}

/** `items[start:stop]`, with Python's rules for negative and out-of-range bounds. */
export function pySlice<T>(items: readonly T[], start: number, stop: number): T[] {
  const bound = (index: number) =>
    index < 0 ? Math.max(items.length + index, 0) : Math.min(index, items.length);
  return items.slice(bound(start), Math.max(bound(start), bound(stop)));
}

/** `str.splitlines(keepends=True)`. */
export function splitLinesKeepEnds(text: string): string[] {
  return (
    text
      .match(/[^\n\r\v\f\x1c-\x1e\x85\u2028\u2029]*(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]|$)/g)
      ?.filter((line) => line !== "") ?? []
  );
}

/** `text.count(sub)`: non-overlapping occurrences; an empty `sub` counts every gap. */
export function pyCount(text: string, sub: string): number {
  if (sub === "") return pyLen(text) + 1;
  let found = 0;
  for (let index = text.indexOf(sub); index >= 0; index = text.indexOf(sub, index + sub.length))
    found++;
  return found;
}

/** `text.replace(old, new, count)`; `count` of -1 replaces every occurrence. */
export function pyReplace(text: string, old: string, replacement: string, count = -1): string {
  if (old === "") {
    const chars = [...text];
    const limit = count < 0 ? chars.length + 1 : count;
    let out = "";
    for (let index = 0; index <= chars.length; index++) {
      if (index < limit) out += replacement;
      if (index < chars.length) out += chars[index];
    }
    return out;
  }
  if (count < 0) return text.split(old).join(replacement);
  let out = text;
  let from = 0;
  for (let done = 0; done < count; done++) {
    const index = out.indexOf(old, from);
    if (index < 0) break;
    out = out.slice(0, index) + replacement + out.slice(index + old.length);
    from = index + replacement.length;
  }
  return out;
}

/** `str.expandtabs(size)`: tab stops every `size` columns, counted from each line start. */
export function expandTabs(text: string, size = 8): string {
  let out = "";
  let column = 0;
  for (const char of text) {
    if (char === "\t") {
      if (size > 0) {
        const pad = size - (column % size);
        out += " ".repeat(pad);
        column += pad;
      }
    } else if (char === "\n" || char === "\r") {
      out += char;
      column = 0;
    } else {
      out += char;
      column++;
    }
  }
  return out;
}

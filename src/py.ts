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
const SPACE =
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

/** `repr(value)` for the values HX puts in messages: strings, numbers, booleans, None. */
export function repr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : floatRepr(value);
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

/**
 * The file system operations HX needs, with the semantics of the Python
 * `pathlib` calls it was first written with: `resolve()` that tolerates a
 * missing path, `expanduser()`, and the `is_file()`/`is_dir()` checks that say
 * false rather than throw.
 */

import { lstatSync, readFileSync, readlinkSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { purePath } from "./py.ts";

/** `Path.home()`: `$HOME`, falling back to the account's home directory. */
export function home(): string {
  return process.env.HOME || homedir();
}

/** `Path.expanduser()` for `~` and `~/...`. Another user's `~name` is left as written. */
export function expandUser(path: string): string {
  if (path !== "~" && !path.startsWith("~/")) return path;
  const base = home().replace(/\/+$/, "") || "/";
  return path === "~" ? base : base === "/" ? path.slice(1) : base + path.slice(1);
}

/** `os.path.join(a, b)` for POSIX: an absolute `b` replaces `a`. */
export function join(base: string, ...parts: string[]): string {
  let out = base;
  for (const part of parts) {
    if (part.startsWith("/")) out = part;
    else if (out === "" || out.endsWith("/")) out += part;
    else out += `/${part}`;
  }
  return out;
}

/** `Path(path).parent`, as text. */
export function parent(path: string): string {
  const clean = purePath(path);
  if (clean === "/" || clean === "//") return clean;
  const index = clean.lastIndexOf("/");
  if (index < 0) return ".";
  return index === 0 ? "/" : clean.slice(0, index);
}

/** `Path(path).name`. */
export function basename(path: string): string {
  const clean = purePath(path);
  if (clean === "/" || clean === "." || clean === "//") return "";
  return clean.slice(clean.lastIndexOf("/") + 1);
}

/** `Path.absolute()`: joined to the working directory, not resolved. */
export function absolute(path: string): string {
  return purePath(path.startsWith("/") ? path : join(process.cwd(), path));
}

/**
 * `Path.resolve()`: absolute, symlinks followed, `..` applied. A part that does
 * not exist is kept as written rather than being an error, as in Python.
 */
export function resolve(path: string): string {
  return realpath(path.startsWith("/") ? path : join(process.cwd(), path), 0);
}

function realpath(path: string, depth: number): string {
  let resolved = "/";
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      resolved = parent(resolved);
      continue;
    }
    const next = resolved === "/" ? `/${part}` : `${resolved}/${part}`;
    let isLink = false;
    try {
      isLink = lstatSync(next).isSymbolicLink();
    } catch {
      isLink = false;
    }
    if (!isLink || depth > 40) {
      resolved = next;
      continue;
    }
    const target = readlinkSync(next);
    resolved = realpath(target.startsWith("/") ? target : join(resolved, target), depth + 1);
  }
  return resolved;
}

/** `Path.is_file()`: false for anything missing or unreadable. */
export function isFile(path: string): boolean {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

/** `Path.is_dir()`: false for anything missing or unreadable. */
export function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/** `Path.exists()`. */
export function exists(path: string): boolean {
  try {
    statSync(path);
    return true;
  } catch {
    return false;
  }
}

/**
 * `Path.read_text()`: UTF-8, strict, with universal newlines - `\r\n` and a lone
 * `\r` both read as `\n`. Throws on a missing file and on bytes that are not
 * UTF-8, which Python reports as `UnicodeDecodeError`.
 */
export function readText(path: string): string {
  const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
    readFileSync(path),
  );
  return text.replace(/\r\n?/g, "\n");
}

/**
 * `PurePath.relative_to(base)` for two normalised absolute paths, or `null`
 * where Python raises `ValueError`. The same path is `.`.
 */
export function relativeTo(path: string, base: string): string | null {
  if (path === base) return ".";
  if (base === "/") return path.startsWith("/") ? path.slice(1) : null;
  return path.startsWith(`${base}/`) ? path.slice(base.length + 1) : null;
}

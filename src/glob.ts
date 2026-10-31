/**
 * `pathlib` globbing, as Python 3.14 does it: `*` matches dotfiles, `**` walks
 * without following symlinked directories, and results come in the order the
 * walk finds them. The Glob and Grep tools list files this way, and the order
 * they list them in is part of what the model reads.
 */

import { type Dirent, opendirSync, statSync } from "node:fs";
import { join } from "./pathlib.ts";
import { purePath } from "./py.ts";

/** `fnmatch.translate` for one path component, as an anchored RegExp. */
export function translate(pattern: string): RegExp {
  let out = "";
  const chars = [...pattern];
  for (let i = 0; i < chars.length; i++) {
    const char = chars[i] as string;
    if (char === "*") {
      while (chars[i + 1] === "*") i++;
      out += "[\\s\\S]*";
    } else if (char === "?") out += "[\\s\\S]";
    else if (char === "[") {
      let j = i + 1;
      if (chars[j] === "!") j++;
      if (chars[j] === "]") j++;
      while (j < chars.length && chars[j] !== "]") j++;
      if (j >= chars.length) out += "\\[";
      else {
        let body = chars.slice(i + 1, j).join("");
        i = j;
        if (body.startsWith("!")) body = `^${body.slice(1)}`;
        else if (body.startsWith("^")) body = `\\${body}`;
        out += `[${body.replaceAll("\\", "\\\\")}]`;
      }
    } else out += char.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
  }
  return new RegExp(`^${out}$`, "u");
}

const isWild = (part: string) => /[*?[]/.test(part);

/** `PurePath(text).parts`. */
export function parts(text: string): string[] {
  const clean = purePath(text);
  if (clean === ".") return [];
  const rest = clean.split("/").filter(Boolean);
  return clean.startsWith("/") ? ["/", ...rest] : rest;
}

/** A directory's entries in the order the file system returns them, as
 * `os.scandir` reads them. `readdirSync` sorts them, which changes the order
 * every search reports. */
export function entries(directory: string): Dirent[] {
  const found: Dirent[] = [];
  try {
    const dir = opendirSync(directory);
    try {
      for (let entry = dir.readSync(); entry !== null; entry = dir.readSync()) found.push(entry);
    } finally {
      dir.closeSync();
    }
  } catch {
    return found;
  }
  return found;
}

/** Whether a directory entry is a directory, following a symlink to one. */
function isDirEntry(directory: string, entry: Dirent): boolean {
  if (entry.isDirectory()) return true;
  if (!entry.isSymbolicLink()) return false;
  try {
    return statSync(join(directory, entry.name)).isDirectory();
  } catch {
    return false;
  }
}

/** `Path(root).glob(pattern)`, as a list of `root`-joined paths. */
export function glob(root: string, pattern: string): string[] {
  const out: string[] = [];
  select(root, parts(pattern), out);
  return out;
}

function select(path: string, rest: string[], out: string[]): void {
  if (rest.length === 0) {
    out.push(path);
    return;
  }
  const [part, ...tail] = rest as [string, ...string[]];
  if (part === "**") {
    // `**` at the end matches every file and directory below; before more
    // parts, every directory - the root included - each as soon as it is found.
    const last = tail.length === 0;
    if (last) out.push(path);
    else select(path, tail, out);
    const stack = [path];
    while (stack.length) {
      const directory = stack.pop() as string;
      for (const entry of entries(directory)) {
        const child = join(directory, entry.name);
        const dir = entry.isDirectory();
        if (last) out.push(child);
        else if (dir) select(child, tail, out);
        if (dir) stack.push(child);
      }
    }
    return;
  }
  if (!isWild(part)) {
    const child = join(path, part);
    if (tail.length === 0) {
      try {
        statSync(child);
        out.push(child);
      } catch {
        // Not there.
      }
    } else select(child, tail, out);
    return;
  }
  const regex = translate(part);
  for (const entry of entries(path)) {
    if (!regex.test(entry.name)) continue;
    const child = join(path, entry.name);
    if (tail.length === 0) out.push(child);
    else if (isDirEntry(path, entry)) select(child, tail, out);
  }
}

/** `PurePath(path).match(pattern)`: matched from the right, one component per part. */
export function match(path: string, pattern: string): boolean {
  const pathParts = parts(path);
  const patternParts = parts(pattern);
  if (patternParts.length === 0) throw new Error("empty pattern");
  if (patternParts[0] === "/" && pathParts.length !== patternParts.length) return false;
  if (patternParts.length > pathParts.length) return false;
  const offset = pathParts.length - patternParts.length;
  return patternParts.every((part, index) =>
    translate(part).test(pathParts[offset + index] as string),
  );
}

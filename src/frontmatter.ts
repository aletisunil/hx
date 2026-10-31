/**
 * YAML frontmatter parsing, shared by skills and agent definitions.
 *
 * Both are Markdown files whose first block is `---`-delimited YAML. Keeping
 * one parser means a malformed file fails the same way in both places. The
 * YAML is read as YAML 1.1, as PyYAML read it: `yes` and `no` are booleans.
 */

import { parse as parseYaml } from "yaml";
import { readText } from "./pathlib.ts";
import { osErrorText, splitLines, strip } from "./py.ts";

const DELIMITER = "---";

export interface Document {
  readonly metadata: Record<string, unknown>;
  readonly body: string;
  readonly path: string;
}

export class FrontmatterError extends Error {
  override name = "FrontmatterError";
}

/**
 * Split frontmatter from body. Throws `FrontmatterError` when the block is
 * unterminated or is not a mapping: a file that silently parses as "no
 * metadata" would be discovered and then quietly ignored.
 */
export function parse(text: string, path: string): Document {
  const stripped = text.replace(/^﻿+/, "");
  if (!stripped.startsWith(DELIMITER)) return { metadata: {}, body: strip(stripped), path };
  const lines = splitLines(stripped);
  const closing = lines.findIndex((line, index) => index > 0 && strip(line) === DELIMITER);
  if (closing < 0)
    throw new FrontmatterError(`${path}: frontmatter opened with --- but never closed`);
  let metadata: unknown;
  try {
    metadata =
      parseYaml(lines.slice(1, closing).join("\n"), { version: "1.1", schema: "yaml-1.1" }) ?? {};
  } catch (error) {
    throw new FrontmatterError(`${path}: invalid YAML frontmatter (${(error as Error).message})`);
  }
  if (
    typeof metadata !== "object" ||
    metadata === null ||
    Array.isArray(metadata) ||
    metadata instanceof Date
  ) {
    throw new FrontmatterError(`${path}: frontmatter must be a mapping`);
  }
  return {
    metadata: metadata as Record<string, unknown>,
    body: strip(lines.slice(closing + 1).join("\n")),
    path,
  };
}

export function read(path: string): Document {
  let text: string;
  try {
    text = readText(path);
  } catch (error) {
    throw new FrontmatterError(`${path}: ${osErrorText(error)}`);
  }
  return parse(text, path);
}

/** `str(value)` for a YAML scalar, as Python prints it. */
export function pyStr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  return String(value);
}

export function require(document: Document, ...keys: string[]): void {
  const missing = keys.filter((key) => !strip(pyStr(document.metadata[key] ?? "")));
  if (missing.length) {
    throw new FrontmatterError(
      `${document.path}: missing required field(s): ${missing.join(", ")}`,
    );
  }
}

/** Accept either a YAML list or a comma-separated string. */
export function stringTuple(value: unknown): string[] {
  if (value === null || value === undefined) return [];
  if (typeof value === "string")
    return value
      .split(",")
      .map((part) => strip(part))
      .filter(Boolean);
  if (Array.isArray(value)) return value.map((item) => strip(pyStr(item))).filter(Boolean);
  return [];
}

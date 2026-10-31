/** Glob tool: fast path matching, sorted by modification time. */

import { statSync } from "node:fs";
import { glob, parts } from "../glob.ts";
import { expandUser, isDir, isFile, join } from "../pathlib.ts";
import { purePath, repr, toInt } from "../py.ts";
import { count } from "../text.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";

const DESCRIPTION = `Find files by glob pattern (e.g. \`**/*.py\`, \`src/**/*.ts\`).

Returns paths sorted by modification time, newest first.`;

export const IGNORED_DIRS = new Set([
  ".git",
  "node_modules",
  "__pycache__",
  ".venv",
  "venv",
  ".mypy_cache",
  ".ruff_cache",
  "dist",
]);
const DEFAULT_LIMIT = 200;

export class GlobTool extends Tool {
  readonly name = "Glob";
  readonly description = DESCRIPTION;

  schema() {
    return {
      type: "object",
      properties: {
        pattern: { type: "string" },
        path: { type: "string", description: "Directory to search (default cwd)" },
        limit: { type: "integer", default: DEFAULT_LIMIT },
      },
      required: ["pattern"],
    };
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const root = searchRoot(params.path, ctx.cwd);
    if (!isDir(root)) throw new ToolError(`${root}: not a directory`);
    const limit = toInt(params.limit || DEFAULT_LIMIT);
    const pattern = String(params.pattern);
    checkPattern(pattern);
    const matches = glob(root, pattern).filter((path) => isFile(path) && !ignored(path));
    // Stable, so files of one age keep the order the walk found them in.
    matches.sort((a, b) => mtime(b) - mtime(a));
    const shown = matches.slice(0, limit);
    let body = shown.join("\n") || "(no matches)";
    if (matches.length > limit) {
      body += `\n\n... ${count(matches.length - limit, "more match", "more matches")} (raise limit to see them)`;
    }
    return toolResult(body, { summary: count(matches.length, "match", "matches") });
  }
}

/** The directory a search starts from: `path`, against the cwd when relative. */
export function searchRoot(raw: unknown, cwd: string): string {
  const root = purePath(expandUser(String(raw || cwd)));
  return root.startsWith("/") ? root : join(cwd, root);
}

/** Refuse what cannot be globbed, with what to send instead. */
function checkPattern(pattern: string): void {
  const pieces = parts(pattern);
  if (pieces.length === 0) {
    // ".", "./", "": no parts left once normalised.
    throw new ToolError(
      `bad pattern ${repr(pattern)}: it names a directory, not files. Use '*' or '**/*'.`,
    );
  }
  if (pieces[0] === "/") {
    let literal = 0;
    while (literal < pieces.length - 1 && !/[*?[]/.test(pieces[literal] as string)) literal++;
    const directory = purePath(pieces.slice(0, literal).join("/").replace(/^\/\//, "/"));
    const rest = purePath(pieces.slice(literal).join("/"));
    throw new ToolError(
      `bad pattern ${repr(pattern)}: it is an absolute path. Pass the directory as 'path' ` +
        `and a relative pattern, such as path=${repr(directory)}, pattern=${repr(rest)}.`,
    );
  }
}

const ignored = (path: string) => parts(path).some((part) => IGNORED_DIRS.has(part));

function mtime(path: string): number {
  try {
    return statSync(path).mtimeMs;
  } catch {
    return 0;
  }
}

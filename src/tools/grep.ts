/**
 * Grep tool: content search.
 *
 * Shells out to `ripgrep` when present and falls back to a built-in walker
 * otherwise, so the tool works on a machine without rg rather than failing.
 */

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { glob, match, parts } from "../glob.ts";
import { isFile } from "../pathlib.ts";
import { splitLines, strip, toInt, truthy } from "../py.ts";
import { compileError, compile as compileRegex } from "../pyre.ts";
import { count } from "../text.ts";
import { which } from "../which.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";
import { IGNORED_DIRS, searchRoot } from "./glob.ts";

const DESCRIPTION = `Search file contents with a regular expression.

Modes: \`files_with_matches\` (default), \`content\` (with -A/-B/-C context), or
\`count\`. Filter with \`glob\`. Case-insensitive with \`-i\`.`;

const DEFAULT_LIMIT = 200;
const SEARCH_TIMEOUT = 60;

export class GrepTool extends Tool {
  readonly name = "Grep";
  readonly description = DESCRIPTION;

  schema() {
    return {
      type: "object",
      properties: {
        pattern: { type: "string", description: "Regular expression" },
        path: { type: "string", description: "File or directory (default cwd)" },
        glob: { type: "string", description: "Filter files, e.g. '*.py'" },
        output_mode: {
          type: "string",
          enum: ["files_with_matches", "content", "count"],
          default: "files_with_matches",
        },
        "-i": { type: "boolean", description: "Case insensitive" },
        "-A": { type: "integer", description: "Lines of trailing context" },
        "-B": { type: "integer", description: "Lines of leading context" },
        "-C": { type: "integer", description: "Lines of context either side" },
        limit: { type: "integer", default: DEFAULT_LIMIT },
      },
      required: ["pattern"],
    };
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const pattern = String(params.pattern);
    const ripgrep = which("rg");
    // ripgrep reports a bad pattern itself; the fallback compiles it here.
    const regex = ripgrep === null ? compilePattern(pattern, truthy(params["-i"])) : null;

    const root = searchRoot(params.path, ctx.cwd);
    if (!existsSync(root)) throw new ToolError(`${root}: no such file or directory`);
    const mode = String(params.output_mode || "files_with_matches");
    const limit = toInt(params.limit || DEFAULT_LIMIT);

    const lines =
      ripgrep !== null
        ? runRipgrep(ripgrep, pattern, root, mode, params)
        : search(regex as RegExp, root, mode, params);
    const shown = lines.slice(0, limit);
    let body = shown.join("\n") || "(no matches)";
    if (lines.length > limit)
      body += `\n\n... ${count(lines.length - limit, "more result")} (raise limit to see them)`;
    return toolResult(body, { summary: count(lines.length, "result") });
  }
}

/** The pattern as a RegExp for the built-in search, or the reason it is not one. */
function compilePattern(pattern: string, ignoreCase: boolean): RegExp {
  try {
    return compileRegex(pattern, ignoreCase ? "i" : "");
  } catch (error) {
    throw new ToolError(`invalid regular expression: ${compileError(error)}`);
  }
}

function runRipgrep(
  rg: string,
  pattern: string,
  root: string,
  mode: string,
  params: Params,
): string[] {
  const argv = ["--no-heading", "--color", "never"];
  if (mode === "files_with_matches") argv.push("--files-with-matches");
  else if (mode === "count") argv.push("--count");
  else {
    argv.push("--line-number");
    for (const flag of ["-A", "-B", "-C"]) {
      if (params[flag] !== null && params[flag] !== undefined)
        argv.push(flag, String(toInt(params[flag])));
    }
  }
  if (truthy(params["-i"])) argv.push("--ignore-case");
  if (truthy(params.glob)) argv.push("--glob", String(params.glob));
  argv.push("--regexp", pattern, root);

  const result = spawnSync(rg, argv, {
    encoding: "utf8",
    timeout: SEARCH_TIMEOUT * 1000,
    maxBuffer: 1 << 30,
  });
  if (result.error && (result.error as NodeJS.ErrnoException).code === "ETIMEDOUT") {
    throw new ToolError(`search timed out after ${SEARCH_TIMEOUT}s`);
  }
  // rg exits 1 for "no matches", which is not an error condition here.
  if (result.status !== 0 && result.status !== 1) {
    const stderr = strip(result.stderr ?? "");
    if (stderr.includes("regex parse error")) {
      throw new ToolError(`invalid regular expression: ${stderr.replace(/\s+/g, " ")}`);
    }
    throw new ToolError(stderr || `ripgrep exited ${result.status}`);
  }
  return splitLines(result.stdout ?? "").filter(Boolean);
}

function search(regex: RegExp, root: string, mode: string, params: Params): string[] {
  const globFilter = String(params.glob || "");
  const context = toInt(params["-C"] || 0);
  const before = toInt(params["-B"] || context);
  const after = toInt(params["-A"] || context);

  const results: string[] = [];
  for (const path of walk(root)) {
    if (globFilter && !match(path, globFilter)) continue;
    let lines: string[];
    try {
      lines = splitLines(new TextDecoder().decode(readFileSync(path)).replace(/\r\n?/g, "\n"));
    } catch {
      continue;
    }
    const hits = lines.flatMap((line, index) => (regex.test(line) ? [index] : []));
    if (hits.length === 0) continue;
    if (mode === "files_with_matches") results.push(path);
    else if (mode === "count") results.push(`${path}:${hits.length}`);
    else results.push(...renderContext(path, lines, hits, before, after));
  }
  return results;
}

function walk(root: string): string[] {
  if (isFile(root)) return [root];
  return glob(root, "**/*").filter(
    (path) => isFile(path) && !parts(path).some((part) => IGNORED_DIRS.has(part)),
  );
}

function renderContext(
  path: string,
  lines: string[],
  hits: number[],
  before: number,
  after: number,
): string[] {
  const wanted = new Set<number>();
  for (const hit of hits) {
    for (
      let index = Math.max(0, hit - before);
      index < Math.min(lines.length, hit + after + 1);
      index++
    )
      wanted.add(index);
  }
  return [...wanted].sort((a, b) => a - b).map((index) => `${path}:${index + 1}:${lines[index]}`);
}

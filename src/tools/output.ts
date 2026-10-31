/**
 * Tool-output capping.
 *
 * Large outputs are the fastest way to burn a context window. Anything over the
 * cap keeps its head and tail, elides the middle with an explicit marker, and
 * spills the full text to `~/.hx/sessions/<id>/outputs/<tool_use_id>.txt` -
 * whose path is handed to the model so it can grep or read the rest
 * deliberately.
 *
 * The TUI always renders the uncapped stream; capping is a context concern only.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "../pathlib.ts";
import { sessionOutputsDir } from "../paths.ts";
import { pyLen, splitLines, strip } from "../py.ts";
import { count } from "../text.ts";

/** Floor on kept output, so the elision notice can never eat it whole. */
const MIN_BODY_CHARS = 200;

function elision(omittedLines: number, omittedChars: number, path: string): string {
  return (
    `\n\n... [${omittedLines} lines / ${omittedChars} chars elided] ...\n` +
    `Full output: ${path}\n` +
    "Use Read or Grep on that path to inspect the rest.\n\n"
  );
}

export interface CappedOutput {
  readonly text: string;
  readonly truncated: boolean;
  readonly spilledPath: string | null;
  readonly originalChars: number;
  readonly originalLines: number;
}

/**
 * Apply char and line caps, spilling to disk when either is exceeded.
 * `headRatio` favours the head, where errors and headers usually live, while
 * keeping the tail that holds exit status and final failures.
 */
export function capOutput(
  text: string,
  options: {
    sessionId: string;
    toolUseId: string;
    charCap?: number;
    lineCap?: number;
    headRatio?: number;
  },
): CappedOutput {
  const charCap = options.charCap ?? 25_000;
  const lineCap = options.lineCap ?? 2_000;
  const headRatio = options.headRatio ?? 0.6;
  const lines = splitLines(text);
  const originalChars = pyLen(text);
  const originalLines = lines.length;

  if (originalChars <= charCap && originalLines <= lineCap) {
    return { text, truncated: false, spilledPath: null, originalChars, originalLines };
  }

  const path = spill(text, options.sessionId, options.toolUseId);
  // The elision notice counts against the cap: the caller asked for a bound on
  // what enters the context, not on the body alone.
  const reserve = pyLen(elision(originalLines, originalChars, path));
  const bodyCap = Math.max(charCap - reserve, MIN_BODY_CHARS);
  const [head, tail] = split(lines, bodyCap, lineCap, headRatio);
  const omittedLines = originalLines - head.length - tail.length;
  const keptChars = [...head, ...tail].reduce((total, line) => total + pyLen(line) + 1, 0);
  const body =
    head.join("\n") + elision(omittedLines, originalChars - keptChars, path) + tail.join("\n");
  return { text: body, truncated: true, spilledPath: path, originalChars, originalLines };
}

/** Choose the head and tail slices that fit inside both caps. */
function split(
  lines: string[],
  charCap: number,
  lineCap: number,
  headRatio: number,
): [string[], string[]] {
  const budgetLines = Math.min(lineCap, lines.length);
  const headTarget = Math.max(1, Math.trunc(budgetLines * headRatio));
  const tailTarget = Math.max(1, budgetLines - headTarget);

  const head: string[] = [];
  let used = 0;
  const headBudget = Math.trunc(charCap * headRatio);
  for (const line of lines.slice(0, headTarget)) {
    if (used + pyLen(line) + 1 > headBudget) break;
    head.push(line);
    used += pyLen(line) + 1;
  }

  const tail: string[] = [];
  const tailBudget = charCap - used;
  let tailUsed = 0;
  const rest = lines.slice(head.length);
  for (const line of rest.slice(Math.max(0, rest.length - tailTarget)).reverse()) {
    if (tailUsed + pyLen(line) + 1 > tailBudget) break;
    tail.unshift(line);
    tailUsed += pyLen(line) + 1;
  }
  return [head, tail];
}

/** Write the full output and return its path. */
export function spill(text: string, sessionId: string, toolUseId: string): string {
  const directory = sessionOutputsDir(sessionId);
  mkdirSync(directory, { recursive: true });
  // Tool ids come from the model; keep them from escaping the outputs dir.
  const safe = toolUseId.replace(/[^A-Za-z0-9_.-]/g, "_") || "output";
  const path = join(directory, `${safe}.txt`);
  writeFileSync(path, text, "utf8");
  return path;
}

/** One-line summary for a collapsed tool block in the transcript. */
export function summarizeForUi(text: string, maxLen = 120): string {
  const stripped = strip(text);
  if (!stripped) return "no output";
  const all = splitLines(stripped);
  let first = strip(all[0] as string);
  const extra = all.length - 1;
  if (pyLen(first) > maxLen) first = `${[...first].slice(0, maxLen - 1).join("")}…`;
  return extra > 0 ? `${first} (+${count(extra, "line")})` : first;
}

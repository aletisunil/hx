/**
 * Session traces: one self-contained HTML page holding everything that happened.
 *
 * A trace is the whole session made readable - the system prompt, the tool
 * schemas, every prompt, reply, thinking block and tool call with its result,
 * the per-turn token ledger and the files HX changed.
 *
 * Nothing is summarised: the messages are serialised with `toDict`, the same
 * function that writes the session JSONL, and ephemeral and compacted messages
 * are included. Nothing is fetched: the page carries its payload, stylesheet
 * and script inline, so it opens from a file:// URL with no network and keeps
 * a trace of a private repository private.
 */

import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { checkpointToDict } from "./core/checkpoints.ts";
import { toDict } from "./core/messages.ts";
import { metaToDict, type Session, usageToDict } from "./core/session.ts";
import { dumps, PyFloat } from "./json.ts";
import { join, parent, resolve } from "./pathlib.ts";
import { sessionDir } from "./paths.ts";
import TEMPLATE from "./trace-template.html";
import { VERSION } from "./version.ts";

/** Default name for a trace written into its own session directory. */
export const TRACE_FILENAME = "trace.html";

export interface TraceOverrides {
  readonly systemPrompt?: string | null;
  readonly tools?: Record<string, unknown>[] | null;
  readonly projectContext?: string | null;
  readonly skillsIndex?: string | null;
  /** The session is still running: the page says it is a snapshot. */
  readonly live?: boolean;
}

/**
 * Assemble the JSON payload for a trace. The prompt and the tool schemas come
 * from the session's recorded environment unless they are passed in, which
 * is for a live session traced before its first provider call.
 *
 * The page embeds the payload verbatim, so anything it shows can be pulled
 * back out of the file with a JSON parser.
 */
export function buildTrace(
  session: Session,
  overrides: TraceOverrides = {},
): Record<string, unknown> {
  const recorded = session.environment;
  const pick = <T>(given: T | null | undefined, fallback: T | null) =>
    (given ?? null) !== null ? (given as T) : fallback;
  return {
    hx_version: VERSION,
    generated_at: new PyFloat(Date.now() / 1000),
    live: overrides.live ?? false,
    session: metaToDict(session.meta),
    system_prompt: pick(overrides.systemPrompt, recorded ? recorded.systemPrompt : null),
    project_context: pick(overrides.projectContext, recorded ? recorded.projectContext : null),
    skills_index: pick(overrides.skillsIndex, recorded ? recorded.skillsIndex : null),
    tools: [...(overrides.tools ?? (recorded ? recorded.tools : []))],
    messages: session.messages.map((message, index) => ({ index, ...toDict(message) })),
    usage: {
      turns: session.usage.turns.map(usageToDict),
      context_tokens: session.usage.contextTokens,
      context_window: session.usage.contextWindow,
    },
    checkpoints: session.checkpoints.map(checkpointToDict),
  };
}

/** `html.escape(text)`. */
const escapeHtml = (text: string) =>
  text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#x27;");

/**
 * Render a trace payload as one self-contained HTML document, in one pass:
 * chained replacements would let a session titled `__HX_TRACE_DATA__` have
 * the payload spliced into its `<title>`.
 */
export function renderHtml(trace: Record<string, unknown>): string {
  const meta = (trace.session ?? {}) as Record<string, unknown>;
  const title = meta.title || meta.session_id || "session";
  const filled: Record<string, string> = {
    __HX_TRACE_TITLE__: escapeHtml(`hx trace - ${String(title)}`),
    __HX_TRACE_DATA__: embed(trace),
  };
  return TEMPLATE.replace(/__HX_TRACE_(?:TITLE|DATA)__/g, (match) => filled[match] as string);
}

/** Render and write a trace. Returns the path written. */
export function writeTrace(trace: Record<string, unknown>, path: string): string {
  mkdirSync(parent(path), { recursive: true });
  writeFileSync(path, renderHtml(trace), "utf8");
  return path;
}

/**
 * Where a trace lands when the user names no path: inside the session's own
 * directory, next to the transcript - never somebody's checkout.
 */
export const defaultTracePath = (session: Session) =>
  join(sessionDir(session.meta.sessionId), TRACE_FILENAME);

/**
 * Best-effort open in a *graphical* browser, as Python's `webbrowser` did it.
 * False when there is none, and never throws.
 *
 * `$BROWSER`, and `xdg-open` on Linux, are generic command browsers there,
 * which this refuses: one of them can be a terminal browser that would launch
 * into the terminal HX is drawing on. On a headless box the answer is the
 * path, which the caller prints either way.
 */
export async function openInBrowser(path: string): Promise<boolean> {
  if (process.env.BROWSER) return false;
  const url = pathToFileURL(resolve(path)).href;
  let command: string[];
  if (process.platform === "darwin")
    command = ["osascript", "-e", `open location "${url.replaceAll('"', "%22")}"`];
  else if (process.platform === "win32") command = ["cmd", "/c", "start", "", url];
  else return false;
  return new Promise((done) => {
    try {
      const child = spawn(command[0] as string, command.slice(1), { stdio: "ignore" });
      child.on("error", () => done(false));
      child.on("exit", (code) => done(code === 0));
    } catch {
      done(false);
    }
  });
}

/**
 * Serialise the payload for embedding inside a `<script>` element. Every `<`
 * is escaped so `</script` in a tool result cannot end the element early;
 * `>` and `&` go with it, and U+2028/U+2029 because they end a line in
 * JavaScript but not in JSON.
 */
function embed(trace: Record<string, unknown>): string {
  return dumps(trace, { ensureAscii: false })
    .replaceAll("<", "\\u003c")
    .replaceAll(">", "\\u003e")
    .replaceAll("&", "\\u0026")
    .replaceAll("\u2028", "\\u2028")
    .replaceAll("\u2029", "\\u2029");
}

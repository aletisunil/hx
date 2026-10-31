/**
 * Context assembly: the system prompt and the project preamble.
 *
 * This text sits at the very top of every request. Providers key their KV
 * cache on an exact prefix match, so nothing here may carry a timestamp or a
 * per-turn counter: it is read once per session and then frozen.
 */

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readdirSync } from "node:fs";
import type { PromptSettings } from "../config.ts";
import { compareCodePoints, dumps } from "../json.ts";
import { isDir, isFile, join, readText, resolve } from "../pathlib.ts";
import {
  projectInstructionsFiles,
  projectRoot,
  projectSystemPromptAppendFile,
  projectSystemPromptFile,
  tilde,
  userInstructionsFile,
  userSystemPromptAppendFile,
  userSystemPromptFile,
} from "../paths.ts";
import { pyLen, rstrip, strip } from "../py.ts";
import { estimateTokens as imageTokens } from "./images.ts";
import type { Message } from "./messages.ts";

/** One AGENTS.md in force: what it is, where it lives, what it says. */
export interface Instructions {
  readonly heading: string;
  readonly path: string;
  readonly text: string;
}

/**
 * Every AGENTS.md in force, least specific first.
 *
 * `~/.hx/AGENTS.md` follows the user into every project; then each `AGENTS.md`
 * from the repository root down to `cwd`. Where two disagree the more specific
 * one comes later and has the last word. A blank or unreadable file is
 * skipped, and a path reached twice - running from inside `$HX_HOME`, say - is
 * loaded once.
 */
export function loadInstructions(cwd: string): Instructions[] {
  const user = userInstructionsFile();
  const here = resolve(cwd);
  const root = projectRoot(here);
  const base = root ?? here;
  const candidates: [string, string][] = [[`User instructions (${tilde(user)})`, user]];
  for (const path of projectInstructionsFiles(here, root)) {
    const where = path.slice(base === "/" ? 1 : base.length + 1);
    candidates.push([`Project instructions (${where})`, path]);
  }

  const found: Instructions[] = [];
  const seen = new Set<string>();
  for (const [heading, path] of candidates) {
    const body = readPromptFile(path);
    if (!body || seen.has(resolve(path))) continue;
    seen.add(resolve(path));
    found.push({ heading, path, text: body });
  }
  return found;
}

/**
 * Said once, ahead of the files. Without it a rule like "no unit tests" reads
 * as background next to a request to make something "well covered", and the
 * model re-reads the file it was already given to find out whether it counts.
 */
export const INSTRUCTIONS_PREAMBLE =
  "The sections below are AGENTS.md files: standing instructions from the user " +
  "and this project, already loaded in full - there is no need to read them " +
  "again. Follow them over your own defaults and habits. Where two disagree, " +
  "the later, more specific one wins.";

/**
 * Static per-session project preamble: cwd, git branch, listing, AGENTS.md contents.
 *
 * Computed once at startup and then frozen - refreshing it mid-session would
 * invalidate the prefix. Pass `instructions` to fold in the AGENTS.md files
 * already loaded, so the caller can remember exactly which ones are in force.
 */
export function buildProjectContext(cwd: string, instructions?: Instructions[]): string {
  const lines = [`Working directory: ${cwd}`];

  const branch = gitBranch(cwd);
  if (branch) lines.push(`Git branch: ${branch}`);

  const entries = readdirSync(cwd)
    .filter((name) => !name.startsWith("."))
    .map((name) => name + (isDir(join(cwd, name)) ? "/" : ""))
    .sort(compareCodePoints);
  if (entries.length) lines.push(`Top level: ${entries.slice(0, 60).join(", ")}`);

  const loaded = instructions ?? loadInstructions(cwd);
  if (loaded.length) lines.push(`\n${INSTRUCTIONS_PREAMBLE}`);
  for (const entry of loaded) lines.push(`\n# ${entry.heading}\n\n${entry.text}`);

  return lines.join("\n");
}

export function gitBranch(cwd: string): string | null {
  const result = spawnSync("git", ["rev-parse", "--abbrev-ref", "HEAD"], {
    cwd,
    encoding: "utf8",
    timeout: 2000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error || result.status !== 0) return null;
  return strip(result.stdout) || null;
}

export const SYSTEM_PROMPT = `\
You are HX, an agentic coding assistant running in the user's terminal.

Work directly on the user's codebase using the tools available to you. Prefer
reading the relevant code over guessing at it. Make the change that was asked
for - do not narrow it, widen it, or substitute a different one.

Be concise. The user is reading your output in a terminal, so favour short
direct answers and code over prose. Reference files as \`path/to/file.py:42\`.

When a task takes several steps, track them so the user can see the plan and
what remains. Report what actually happened: if a command failed, say so and
show the output.

Questions about HX itself - a command, a setting, how to add an API key, what
changed in a release - are answered from the documentation shipped with this
build, not from memory: \`hx docs\` lists the manual's sections, \`hx docs
<section>\` prints one, and \`hx changelog [version]\` prints what each version
added. Read the relevant one before answering.
`;

/** The system prompt in force, and where each part of it came from. */
export interface ResolvedPrompt {
  readonly text: string;
  /** What supplied the base prompt: `built-in`, `--system-prompt`, or a path. */
  readonly source: string;
  /** Sources of the appended blocks, in the order they were appended. */
  readonly appends: readonly string[];
}

/**
 * Layer the system prompt overrides.
 *
 * The base prompt is replaced by the first of these that exists:
 *
 *     --system-prompt / prompt.system   (settings layer, highest)
 *     <cwd>/.hx/system-prompt.md
 *     ~/.hx/system-prompt.md
 *     the built-in SYSTEM_PROMPT
 *
 * Appended text is then added in a fixed order - user file, project file, then
 * each `--append-system-prompt` value - so two layers can both contribute
 * without either winning.
 */
export function resolveSystemPrompt(cwd: string, prompt?: PromptSettings | null): ResolvedPrompt {
  let text = SYSTEM_PROMPT;
  let source = "built-in";
  if (prompt?.system) {
    text = prompt.system;
    source = "--system-prompt";
  } else {
    for (const path of [projectSystemPromptFile(cwd), userSystemPromptFile()]) {
      const body = readPromptFile(path);
      if (body) {
        text = body;
        source = path;
        break;
      }
    }
  }

  const blocks: string[] = [];
  const appends: string[] = [];
  for (const path of [userSystemPromptAppendFile(), projectSystemPromptAppendFile(cwd)]) {
    const body = readPromptFile(path);
    if (body) {
      blocks.push(body);
      appends.push(path);
    }
  }
  for (const extra of prompt?.append ?? []) {
    if (strip(extra)) {
      blocks.push(strip(extra));
      appends.push("--append-system-prompt");
    }
  }

  if (blocks.length) text = [rstrip(text, "\n"), ...blocks].join("\n\n");
  return { text, source, appends };
}

/**
 * Contents of a prompt override file, or `""` when it is absent or empty.
 *
 * An unreadable file - no permission, or not UTF-8 - is treated as absent: a
 * problem with an optional override must not stop the session from starting.
 */
function readPromptFile(path: string): string {
  try {
    return isFile(path) ? strip(readText(path)) : "";
  } catch {
    return "";
  }
}

/** The system prompt in force. Contains no volatile values (no clock, no counters). */
export function loadSystemPrompt(cwd: string, prompt?: PromptSettings | null): string {
  return resolveSystemPrompt(cwd, prompt).text;
}

/** Anthropic's per-request limit on explicit `cache_control` markers. */
export const MAX_CACHE_BREAKPOINTS = 4;

/** One addressable chunk of the prefix, tracked so `/context` can show a breakdown. */
export interface PromptSection {
  readonly name: string;
  readonly text: string;
  tokens: number;
}

/**
 * The exact payload handed to a provider, plus the accounting behind it.
 *
 * `messages` holds the conversation only. The provider prepends the system
 * message when serialising, so `breakpoints` index the *payload* list where
 * position 0 is that system message.
 */
export interface AssembledContext {
  readonly system: PromptSection[];
  messages: Message[];
  readonly tools: Record<string, unknown>[];
  /** Indices into the payload message list that carry an explicit cache
   * breakpoint. Empty for implicit-caching models. */
  readonly breakpoints: readonly number[];
  readonly totalTokens: number;
  readonly sections: PromptSection[];
}

/** Concatenated system sections, in declared order. */
export function systemText(context: AssembledContext): string {
  return context.system
    .filter((section) => section.text)
    .map((section) => section.text)
    .join("\n\n");
}

/** Stable hash of everything above the first breakpoint; a change means the
 * cache was invalidated. */
export function prefixFingerprint(context: AssembledContext): string {
  return createHash("sha256")
    .update(systemText(context))
    .update("\x00")
    .update(dumps(context.tools, { sortKeys: true, separators: [",", ":"] }))
    .digest("hex");
}

/** Assembles an `AssembledContext` from session state. */
export class ContextBuilder {
  /** Estimated tokens that must accumulate below the rolling breakpoint before
   * it is allowed to move. Advancing it every turn rewrites the cache
   * constantly and costs more than the hit it buys. */
  static readonly BREAKPOINT_HYSTERESIS_TOKENS = 4_096;
  static readonly CHARS_PER_TOKEN = 3.7;

  private breakpointB: number | null = null;

  constructor(
    public systemPrompt: string,
    public cwd: string,
    public keepRecentTurns = 6,
  ) {}

  /** Assemble the request. `cacheMode` is `explicit` (place `cache_control`
   * markers), `implicit` (rely on prefix stability alone), or `none`. */
  build(
    messages: Message[],
    tools: Record<string, unknown>[],
    options: {
      skillsIndex?: string | null;
      projectContext?: string | null;
      cacheMode?: string;
    } = {},
  ): AssembledContext {
    const system: PromptSection[] = [{ name: "system", text: this.systemPrompt, tokens: 0 }];
    if (options.skillsIndex) system.push({ name: "skills", text: options.skillsIndex, tokens: 0 });
    if (options.projectContext) {
      system.push({ name: "project", text: options.projectContext, tokens: 0 });
    }
    for (const section of system) section.tokens = this.estimateTokens(section.text);

    const active = messages.filter((m) => !m.compacted);
    const sorted = sortTools(tools);
    const explicit = (options.cacheMode ?? "explicit") === "explicit";
    const breakpoints = explicit ? this.placeBreakpoints(active) : [];
    const toolText = dumps(sorted, { sortKeys: true });
    const sections: PromptSection[] = [
      ...system,
      { name: "tools", text: toolText, tokens: this.estimateTokens(toolText) },
      {
        name: "history",
        text: "",
        tokens: active.reduce((total, m) => total + this.estimateMessageTokens(m), 0),
      },
    ];
    return {
      system,
      messages: active,
      tools: sorted,
      breakpoints,
      totalTokens: sections.reduce((total, s) => total + s.tokens, 0),
      sections,
    };
  }

  /**
   * Choose breakpoint indices into the payload list (0 = system message).
   *
   * Breakpoint A sits on the system message, at the end of the static prefix.
   * Breakpoint B rolls forward to just before the last `keepRecentTurns`
   * messages, and only moves once enough tokens have accumulated below it.
   */
  placeBreakpoints(messages: Message[]): number[] {
    const breakpoints = [0];
    if (messages.length <= this.keepRecentTurns) {
      this.breakpointB = null;
      return breakpoints;
    }
    const candidate = messages.length - this.keepRecentTurns;
    if (this.breakpointB === null || this.breakpointB > candidate) {
      // A shorter history - a rewind, /clear, a resumed session - brings B back with it.
      this.breakpointB = candidate;
    } else {
      const below = messages
        .slice(this.breakpointB, candidate)
        .reduce((total, m) => total + this.estimateMessageTokens(m), 0);
      if (below >= ContextBuilder.BREAKPOINT_HYSTERESIS_TOKENS) this.breakpointB = candidate;
    }
    if (this.breakpointB > 0) breakpoints.push(this.breakpointB);
    return breakpoints.slice(0, MAX_CACHE_BREAKPOINTS);
  }

  /** Cheap local token estimate for gauges and the compaction trigger.
   * Deliberately approximate; the authoritative counts come back from the provider. */
  estimateTokens(text: string): number {
    return text ? Math.trunc(pyLen(text) / ContextBuilder.CHARS_PER_TOKEN) + 1 : 0;
  }

  /** `estimateTokens` for a whole message, images charged by pixel count. */
  estimateMessageTokens(message: Message): number {
    return this.estimateTokens(messageText(message)) + messageImageTokens(message);
  }
}

/** Canonical tool ordering: builtins first, then `mcp__*`, each by name. The
 * serialised tool block sits in the cached prefix, so the order must not
 * depend on registration order. */
export function sortTools(tools: Record<string, unknown>[]): Record<string, unknown>[] {
  const key = (tool: Record<string, unknown>) => String(tool.name ?? "");
  return [...tools].sort((a, b) => {
    const mcp = Number(key(a).startsWith("mcp__")) - Number(key(b).startsWith("mcp__"));
    return mcp || compareCodePoints(key(a), key(b));
  });
}

/** Estimated tokens for every image in a message, tool results' included. */
export function messageImageTokens(message: Message): number {
  let total = 0;
  for (const block of message.content) {
    if (block.type === "image") total += imageTokens(block);
    else if (block.type === "tool_result") {
      total += block.images.reduce((sum, image) => sum + imageTokens(image), 0);
    }
  }
  return total;
}

function messageText(message: Message): string {
  const parts: string[] = [];
  for (const block of message.content) {
    const text = ("text" in block && block.text) || ("content" in block && block.content) || null;
    if (typeof text === "string") parts.push(text);
  }
  return parts.join("\n");
}

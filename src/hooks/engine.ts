/**
 * Hook loading and execution.
 *
 * **Where hooks may be declared is a security boundary, not a convenience.** A
 * hook is an arbitrary shell command, so HX loads hooks only from files the
 * user owns:
 *
 * - `~/.hx/settings.json`
 * - `~/.hx/projects/<slug>/settings.local.json`
 *
 * Hooks in a project's checked-in `./.hx/settings.json` are **ignored**, and the
 * session says so at startup. That file arrives with a clone, and cloning a
 * repository must never be enough to run commands on the machine that cloned it.
 */

import { spawn } from "node:child_process";
import { readSettingsFile } from "../config.ts";
import { dumps } from "../json.ts";
import { projectLocalSettingsFile, projectSettingsFile, userSettingsFile } from "../paths.ts";
import { strip, toInt } from "../py.ts";
import {
  BLOCK_EXIT_CODE,
  DEFAULT_TIMEOUT_SECONDS,
  HOOK_EVENTS,
  type HookCommand,
  type HookEvent,
  HookOutcome,
  hookMatches,
} from "./spec.ts";

/** A hook that prints a megabyte is a bug; truncate rather than feed it forward. */
const MAX_OUTPUT_BYTES = 64_000;

type Hooks = Map<HookEvent, HookCommand[]>;

/** Holds the loaded hooks and runs the ones that match. */
export class HookEngine {
  constructor(
    private readonly hooks: Hooks = new Map(),
    public cwd: string = process.cwd(),
    public sessionId = "",
    /** Hooks refused because of where they were declared. Shown at startup. */
    readonly ignored: string[] = [],
  ) {}

  static load(cwd: string, sessionId = ""): HookEngine {
    const trusted: Hooks = new Map();
    for (const path of [userSettingsFile(), projectLocalSettingsFile(cwd)]) {
      collect(readSettingsFile(path).hooks, path, trusted);
    }
    const untrusted: Hooks = new Map();
    const shared = projectSettingsFile(cwd);
    collect(readSettingsFile(shared).hooks, shared, untrusted);
    const ignored = [...untrusted].flatMap(([event, commands]) =>
      commands.map((c) => `${event}: ${c.command}`),
    );
    return new HookEngine(trusted, cwd, sessionId, ignored);
  }

  /** Whether any hook is loaded at all. */
  get any(): boolean {
    return [...this.hooks.values()].some((commands) => commands.length > 0);
  }

  commands(event: HookEvent): HookCommand[] {
    return [...(this.hooks.get(event) ?? [])];
  }

  allCommands(): Hooks {
    return new Map([...this.hooks].filter(([, commands]) => commands.length > 0));
  }

  preToolUse(toolName: string, toolInput: Record<string, unknown>): Promise<HookOutcome> {
    return this.fire("PreToolUse", toolName, { tool_name: toolName, tool_input: toolInput });
  }

  postToolUse(
    toolName: string,
    toolInput: Record<string, unknown>,
    response: string,
    isError: boolean,
  ): Promise<HookOutcome> {
    return this.fire("PostToolUse", toolName, {
      tool_name: toolName,
      tool_input: toolInput,
      tool_response: { content: response, is_error: isError },
    });
  }

  userPromptSubmit(prompt: string): Promise<HookOutcome> {
    return this.fire("UserPromptSubmit", "", { prompt });
  }

  stop(): Promise<HookOutcome> {
    return this.fire("Stop", "", {});
  }

  private async fire(
    event: HookEvent,
    toolName: string,
    payload: Record<string, unknown>,
  ): Promise<HookOutcome> {
    const outcome = new HookOutcome();
    const matching = (this.hooks.get(event) ?? []).filter((hook) => hookMatches(hook, toolName));
    if (matching.length === 0) return outcome;
    const document = dumps({
      session_id: this.sessionId,
      cwd: this.cwd,
      hook_event_name: event,
      ...payload,
    });
    for (const hook of matching) {
      await this.runOne(hook, document, outcome);
      // First refusal wins: the rest cannot change the answer.
      if (outcome.blocked) break;
    }
    return outcome;
  }

  private runOne(hook: HookCommand, document: string, outcome: HookOutcome): Promise<void> {
    return new Promise((resolve) => {
      const child = spawn("/bin/sh", ["-c", hook.command], {
        cwd: this.cwd,
        stdio: ["pipe", "pipe", "pipe"],
      });
      const stdout: Buffer[] = [];
      const stderr: Buffer[] = [];
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        child.kill("SIGKILL");
      }, hook.timeout * 1000);
      child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
      child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
      child.stdin.on("error", () => {});
      child.on("error", (error) => {
        clearTimeout(timer);
        outcome.errors.push(`${hook.command}: ${error.message}`);
        resolve();
      });
      child.on("close", (code) => {
        clearTimeout(timer);
        if (timedOut) outcome.errors.push(`${hook.command}: timed out after ${hook.timeout}s`);
        else
          apply(hook, code ?? 0, clip(Buffer.concat(stdout)), clip(Buffer.concat(stderr)), outcome);
        resolve();
      });
      child.stdin.end(document);
    });
  }
}

/** Fold one hook's result into the merged outcome. */
function apply(
  hook: HookCommand,
  code: number,
  stdout: string,
  stderr: string,
  outcome: HookOutcome,
): void {
  const parsed = decision(stdout);
  if (code === BLOCK_EXIT_CODE) {
    outcome.blocked = true;
    outcome.reason = strip(String(parsed.reason || stderr || "blocked by hook"));
    return;
  }
  if (code !== 0) {
    outcome.errors.push(`${hook.command}: exit ${code}${stderr ? ` - ${strip(stderr)}` : ""}`);
    return;
  }
  if (parsed.decision === "block") {
    outcome.blocked = true;
    outcome.reason = strip(String(parsed.reason || "blocked by hook"));
    return;
  }
  const updated = parsed.updatedInput;
  if (typeof updated === "object" && updated !== null && !Array.isArray(updated)) {
    outcome.updatedInput = updated as Record<string, unknown>;
  }
  const extra = parsed.additionalContext;
  if (typeof extra === "string" && strip(extra)) outcome.context.push(strip(extra));
}

/** Parse a hook's stdout as JSON, tolerating a hook that just prints. */
function decision(stdout: string): Record<string, unknown> {
  const text = strip(stdout);
  if (!text.startsWith("{")) return {};
  try {
    const parsed = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Read one layer's `hooks` block. Malformed entries are skipped, not fatal. */
function collect(raw: unknown, source: string, into: Hooks): void {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return;
  for (const [eventName, groups] of Object.entries(raw)) {
    if (!HOOK_EVENTS.includes(eventName as HookEvent) || !Array.isArray(groups)) continue;
    const event = eventName as HookEvent;
    for (const group of groups) {
      if (typeof group !== "object" || group === null || Array.isArray(group)) continue;
      const matcher = String(group.matcher ?? "");
      for (const entry of group.hooks || []) {
        if (typeof entry !== "object" || entry === null || Array.isArray(entry)) continue;
        const command = strip(String(entry.command ?? ""));
        if (!command || (entry.type ?? "command") !== "command") continue;
        const list = into.get(event) ?? [];
        list.push({
          command,
          matcher,
          timeout: toInt(entry.timeout ?? DEFAULT_TIMEOUT_SECONDS),
          source,
        });
        into.set(event, list);
      }
    }
  }
}

function clip(raw: Buffer): string {
  return new TextDecoder().decode(raw.subarray(0, MAX_OUTPUT_BYTES));
}

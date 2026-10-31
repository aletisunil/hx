/**
 * Hook types.
 *
 * A hook is a shell command HX runs at a named point in the turn. It reads a
 * JSON event on stdin and answers with an exit code, optionally with JSON on
 * stdout. The wire format deliberately matches Claude Code's, so a team that
 * already has `PreToolUse` hooks on disk can point HX at them without a
 * rewrite. What does not match is where hooks may be declared - see the engine.
 */

import { fullmatch } from "../pyre.ts";

export const DEFAULT_TIMEOUT_SECONDS = 60;

/**
 * Exit 2 blocks the action and hands stderr to the model as the reason. Any
 * other non-zero exit is a broken hook, not a decision: it is surfaced to the
 * user as a notice and the action proceeds.
 */
export const BLOCK_EXIT_CODE = 2;

export const HOOK_EVENTS = ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"] as const;
export type HookEvent = (typeof HOOK_EVENTS)[number];

export interface HookCommand {
  readonly command: string;
  /** Regex matched against the tool name in full. Empty or `*` matches all. */
  readonly matcher: string;
  readonly timeout: number;
  /** The settings file it came from, so `/hooks` can show it. */
  readonly source: string;
}

export function hookMatches(hook: HookCommand, toolName: string): boolean {
  if (!hook.matcher || hook.matcher === "*") return true;
  try {
    return fullmatch(hook.matcher, toolName);
  } catch {
    // A malformed matcher matches nothing rather than everything: failing open
    // is the wrong way round for something whose job is to say no.
    return false;
  }
}

/** The merged verdict of every hook that ran for one event. */
export class HookOutcome {
  blocked = false;
  reason = "";
  /** `PreToolUse` only: input the hook rewrote, applied before the tool runs. */
  updatedInput: Record<string, unknown> | null = null;
  /** `additionalContext` strings, injected for the model to read. */
  context: string[] = [];
  /** Hooks that failed to run or crashed. Reported, never blocking. */
  errors: string[] = [];

  get contextText(): string {
    return this.context.join("\n");
  }
}

/**
 * Tool interface.
 *
 * A tool declares a JSON schema, a permission specifier, and whether it mutates
 * state. The loop uses `mutating` to decide what may run concurrently, and the
 * permission engine uses `permissionSpecifier` to match rules like
 * `Bash(git commit:*)` or `Edit(src/**)`.
 */

import type { Settings } from "../config.ts";
import type { ImageBlock } from "../core/messages.ts";
import type { FileTracker } from "../core/reads.ts";

/** Everything a tool is allowed to reach. Tools never touch global state. */
export interface ToolContext {
  readonly cwd: string;
  readonly sessionId: string;
  readonly toolUseId: string;
  /** The files this session has read. Edit and Write refuse any other. */
  readonly reads: FileTracker;
  readonly settings: Settings;
  /** Stream live output to the TUI. Not subject to context capping. */
  readonly emitProgress: (chunk: string) => void;
  /** Aborted when the user interrupts the turn: a tool stops what it is doing
   * and lets the abort propagate. */
  readonly signal: AbortSignal;
}

export interface ToolResult {
  content: string;
  isError: boolean;
  spilledPath: string | null;
  /** One-line description for the collapsed TUI block, e.g. `Read 340 lines`. */
  summary: string;
  metadata: Record<string, unknown>;
  /** Images for the model to see alongside `content`. */
  images: ImageBlock[];
}

export function toolResult(
  content: string,
  fields: Partial<Omit<ToolResult, "content">> = {},
): ToolResult {
  return {
    content,
    isError: false,
    spilledPath: null,
    summary: "",
    metadata: {},
    images: [],
    ...fields,
  };
}

export type Params = Record<string, unknown>;

/** Base class for every tool, builtin or MCP-backed. */
export abstract class Tool {
  abstract readonly name: string;
  abstract readonly description: string;
  /** Mutating tools run serially and require permission in non-bypass modes. */
  readonly mutating: boolean = false;
  /** Set on a mutating tool whose calls are independent of each other. */
  readonly parallelSafe: boolean = false;

  /** JSON Schema for the tool input. Must be deterministic - the serialised
   * schema block sits in the cached prefix. */
  abstract schema(): Record<string, unknown>;

  /** Execute. Throw `ToolError` for user-facing failures. */
  abstract run(params: Params, ctx: ToolContext): Promise<ToolResult>;

  /** The specifier matched against permission rules, e.g. the command string
   * for Bash or the target path for Edit. `null` means the tool name alone. */
  permissionSpecifier(_params: Params): string | null {
    return null;
  }

  /** Every specifier one call covers, for a tool that acts on a list. */
  permissionSpecifiers(params: Params): string[] {
    const specifier = this.permissionSpecifier(params);
    return specifier ? [specifier] : [];
  }

  /**
   * Check input against `schema` and drop unknown keys. Deliberately shallow:
   * it catches the common failure (a missing required argument) without a
   * JSON Schema engine. Tools still validate their own semantics.
   */
  validate(params: Params): Params {
    const schema = this.schema();
    const properties = (schema.properties as Record<string, unknown>) || {};
    const required = (schema.required as string[]) ?? [];
    const missing = required.filter((key) => !(key in params));
    if (missing.length) {
      throw new ToolError(`${this.name}: missing required argument(s): ${missing.join(", ")}`);
    }
    if (Object.keys(properties).length === 0) return { ...params };
    return Object.fromEntries(Object.entries(params).filter(([key]) => key in properties));
  }
}

/** Failure that should be reported to the model as a tool result, not crash the turn. */
export class ToolError extends Error {
  override name = "ToolError";
}

/** The turn was interrupted. Tools let this pass; the loop answers the call. */
export function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return (signal?.aborted ?? false) || (error as Error)?.name === "AbortError";
}

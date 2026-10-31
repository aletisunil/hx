/**
 * Tool registry.
 *
 * Holds builtins, MCP-backed tools and skill-provided tools in one place. The
 * ordering rule matters: schemas are rendered into the cached prefix, so the
 * sort must be stable across runs and across MCP server restarts.
 */

import type { AuthResolver } from "../auth/resolve.ts";
import { TAVILY } from "../auth/store.ts";
import type { CheckpointStore } from "../core/checkpoints.ts";
import type { EventBus } from "../core/events.ts";
import { compareCodePoints } from "../json.ts";
import { repr } from "../py.ts";
import {
  isAbort,
  type Params,
  type Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";
import {
  BackgroundJobs,
  BashOutputTool,
  BashTool,
  KillShellTool,
  type PersistentShell,
} from "./bash.ts";
import { EditTool } from "./edit.ts";
import { GlobTool } from "./glob.ts";
import { GrepTool } from "./grep.ts";
import { ReadTool } from "./read.ts";
import { type TodoList, TodoWriteTool } from "./todo.ts";
import { CreditLedger, WebFetchTool, WebSearchTool } from "./websearch.ts";
import { WriteTool } from "./write.ts";

export class DuplicateTool extends Error {
  override name = "DuplicateTool";
}

export class UnknownTool extends Error {
  override name = "UnknownTool";
}

/** Builtins first, then `mcp__*`, each by name. */
function byCanonicalOrder(a: string, b: string): number {
  return Number(a.startsWith("mcp__")) - Number(b.startsWith("mcp__")) || compareCodePoints(a, b);
}

export class ToolRegistry {
  private readonly tools = new Map<string, Tool>();

  /** Throws `DuplicateTool` on a name collision. */
  register(tool: Tool): void {
    if (this.tools.has(tool.name)) throw new DuplicateTool(tool.name);
    this.tools.set(tool.name, tool);
  }

  /** Remove a tool, if present. For an MCP server whose tools changed. */
  unregister(name: string): void {
    this.tools.delete(name);
  }

  get(name: string): Tool {
    const tool = this.tools.get(name);
    if (tool === undefined) throw new UnknownTool(name);
    return tool;
  }

  has(name: string): boolean {
    return this.tools.has(name);
  }

  names(): string[] {
    return [...this.tools.keys()].sort(byCanonicalOrder);
  }

  /** Rendered schemas in canonical order, restricted to `allowed` when given
   * (subagent allowlists, plan mode hiding mutating tools). */
  schemas(allowed: Set<string> | null = null): Record<string, unknown>[] {
    return this.names()
      .filter((name) => allowed === null || allowed.has(name))
      .map((name) => {
        const tool = this.tools.get(name) as Tool;
        return { name, description: tool.description, input_schema: tool.schema() };
      });
  }

  /** A registry exposing only `names`, sharing the same tool instances - the
   * persistent shell must be the same object for a subagent. */
  subset(names: Set<string>): ToolRegistry {
    const restricted = new ToolRegistry();
    for (const name of names) {
      const tool = this.tools.get(name);
      if (tool !== undefined) restricted.tools.set(name, tool);
    }
    return restricted;
  }

  /**
   * Validate, dispatch, and convert exceptions into error results, so the model
   * sees what went wrong and can adapt instead of the turn dying. An interrupt
   * is not a failure of the tool, and passes through.
   */
  async call(name: string, params: Params, ctx: ToolContext): Promise<ToolResult> {
    let tool: Tool;
    try {
      tool = this.get(name);
    } catch {
      const available = this.names().join(", ") || "none";
      return toolResult(`Unknown tool ${repr(name)}. Available tools: ${available}`, {
        isError: true,
        summary: "unknown tool",
      });
    }
    try {
      return await tool.run(tool.validate(params), ctx);
    } catch (error) {
      if (isAbort(error, ctx.signal)) throw error;
      if (error instanceof ToolError)
        return toolResult(error.message, { isError: true, summary: "error" });
      const failure = error as Error;
      return toolResult(`${failure?.name ?? "Error"}: ${failure?.message ?? String(error)}`, {
        isError: true,
        summary: "error",
      });
    }
  }
}

/**
 * Registry with the builtin tools registered. Bash is registered only when a
 * shell is supplied; the web tools only when a Tavily key exists, because their
 * schemas sit in the cached prefix and a model told "no key" simply tries again.
 */
export function buildDefaultRegistry(
  options: {
    shell?: PersistentShell | null;
    jobs?: BackgroundJobs | null;
    todos?: TodoList | null;
    bus?: EventBus | null;
    auth?: AuthResolver | null;
    checkpoints?: CheckpointStore | null;
  } = {},
): ToolRegistry {
  const registry = new ToolRegistry();
  if (options.shell && options.jobs) {
    registry.register(new BashTool(options.shell, options.jobs));
    registry.register(new BashOutputTool(options.jobs));
    registry.register(new KillShellTool(options.jobs));
  }
  registry.register(new ReadTool());
  registry.register(new WriteTool(options.checkpoints ?? null));
  registry.register(new EditTool(options.checkpoints ?? null));
  registry.register(new GlobTool());
  registry.register(new GrepTool());
  if (options.todos) registry.register(new TodoWriteTool(options.todos, options.bus ?? null));
  if (options.auth?.hasCredential(TAVILY)) {
    // One ledger between them: the bill is per key, not per tool.
    const ledger = new CreditLedger();
    registry.register(new WebSearchTool(options.auth, ledger));
    registry.register(new WebFetchTool(options.auth, ledger));
  }
  return registry;
}

export { BackgroundJobs };

/**
 * Task tool: spawn a subagent.
 *
 * The subagent runs its own agent loop with an isolated transcript, so a long
 * exploratory search costs the parent only the final report instead of every
 * intermediate tool result.
 */

import { compareCodePoints } from "../json.ts";
import { count } from "../text.ts";
import { type Params, Tool, type ToolContext, type ToolResult, toolResult } from "./base.ts";

const DESCRIPTION = `Launch a subagent to handle a multi-step task in its own context.

The subagent cannot ask follow-up questions, so the prompt must be
self-contained. Only its final report returns to this conversation - use this
when the intermediate tool output would not be worth its space here.`;

/** What a subagent runner hands back. */
export interface SubagentResult {
  readonly subagentId: string;
  readonly agentType: string;
  readonly report: string;
  readonly isError: boolean;
  readonly turns: number;
  readonly costUsd: number;
}

/** The part of the subagent runner the tool needs. */
export interface SubagentRunnerLike {
  readonly definitions: ReadonlyMap<string, { readonly description: string }>;
  run(
    agentType: string,
    prompt: string,
    description: string,
    signal: AbortSignal,
  ): Promise<SubagentResult>;
}

export class TaskTool extends Tool {
  readonly name = "Task";
  readonly description = DESCRIPTION;
  /** Conservatively mutating: a subagent may run tools that write. */
  override readonly mutating = true;
  /** Subagents are independent by construction - separate contexts, separate
   * transcripts - so several Task calls in one turn run concurrently. */
  override readonly parallelSafe = true;

  constructor(private readonly runner: SubagentRunnerLike) {
    super();
  }

  /** The `subagent_type` enum is filled from the loaded agent definitions. */
  schema() {
    const names = [...this.runner.definitions.keys()].sort(compareCodePoints);
    const described = names
      .map((name) => `- ${name}: ${this.runner.definitions.get(name)?.description}`)
      .join("\n");
    return {
      type: "object",
      properties: {
        subagent_type: {
          type: "string",
          enum: names,
          description: `Which agent to run.\n${described}`,
        },
        prompt: {
          type: "string",
          description: "Self-contained task. The subagent sees nothing else.",
        },
        description: {
          type: "string",
          description: "3-5 word label shown to the user while it runs",
        },
      },
      required: ["subagent_type", "prompt"],
    };
  }

  override permissionSpecifier(params: Params): string {
    return String(params.subagent_type ?? "");
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const result = await this.runner.run(
      String(params.subagent_type),
      String(params.prompt),
      String(params.description || params.subagent_type),
      ctx.signal,
    );
    return toolResult(result.report, {
      isError: result.isError,
      summary: `${result.agentType}: ${count(result.turns, "turn")}`,
      metadata: { subagent_id: result.subagentId, cost_usd: result.costUsd },
    });
  }
}

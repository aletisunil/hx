/**
 * Subagent execution.
 *
 * Each subagent gets its own agent loop, transcript, tool allowlist and model.
 * `Task` is never in a subagent's allowlist, so recursion is impossible by
 * construction rather than by a depth counter.
 *
 * Only the final assistant text returns to the parent as the tool result - the
 * subagent's intermediate tool output never enters the parent's context, which
 * is the entire reason to spawn one.
 */

import { randomBytes } from "node:crypto";
import type { Settings } from "../config.ts";
import { Compactor } from "../core/compaction.ts";
import { ContextBuilder } from "../core/context.ts";
import type { EventBus } from "../core/events.ts";
import { InjectionRegistry } from "../core/lateinject.ts";
import { AgentLoop, type TurnResult } from "../core/loop.ts";
import { messageText } from "../core/messages.ts";
import { newSession } from "../core/session.ts";
import type { UsageLedger } from "../core/usage.ts";
import type { HookEngine } from "../hooks/engine.ts";
import { compareCodePoints } from "../json.ts";
import type { PermissionEngine } from "../permissions/engine.ts";
import type { Provider } from "../providers/base.ts";
import type { ModelRegistry } from "../providers/models.ts";
import { repr, strip } from "../py.ts";
import type { ToolRegistry } from "../tools/registry.ts";
import type { SubagentResult, SubagentRunnerLike } from "../tools/task.ts";
import type { AgentDefinition } from "./definitions.ts";

/** Recursion is prevented by omission, not by a depth counter that someone
 * will later raise 'just for this case'. */
const NEVER_AVAILABLE_TO_SUBAGENTS = new Set(["Task"]);

export interface SubagentRunnerOptions {
  definitions: Map<string, AgentDefinition>;
  provider: Provider;
  tools: ToolRegistry;
  permissions: PermissionEngine | null;
  bus: EventBus;
  settings: Settings;
  models?: ModelRegistry | null;
  parentSessionId?: string | null;
  parentUsage?: UsageLedger | null;
  /** Shared with the parent: a hook that guards Bash must guard a subagent's
   * Bash too, or the guard is one delegation away from being bypassed. */
  hooks?: HookEngine | null;
  /** The parent's project preamble, AGENTS.md included: a subagent edits the
   * same project under the same rules. */
  projectContext?: string | null;
}

/** Spawns and supervises subagents. */
export class SubagentRunner implements SubagentRunnerLike {
  readonly definitions: Map<string, AgentDefinition>;
  provider: Provider;
  tools: ToolRegistry;
  permissions: PermissionEngine | null;
  bus: EventBus;
  settings: Settings;
  models: ModelRegistry | null;
  parentSessionId: string | null;
  parentUsage: UsageLedger | null;
  hooks: HookEngine | null;
  projectContext: string | null;
  private readonly running = new Map<string, string>();

  constructor(options: SubagentRunnerOptions) {
    this.definitions = options.definitions;
    this.provider = options.provider;
    this.tools = options.tools;
    this.permissions = options.permissions;
    this.bus = options.bus;
    this.settings = options.settings;
    this.models = options.models ?? null;
    this.parentSessionId = options.parentSessionId ?? null;
    this.parentUsage = options.parentUsage ?? null;
    this.hooks = options.hooks ?? null;
    this.projectContext = options.projectContext ?? null;
  }

  /**
   * Run one subagent to completion. Permission prompts from a subagent surface
   * in the parent attributed to that subagent - an approval with no visible
   * origin is not an informed approval.
   */
  async run(
    agentType: string,
    prompt: string,
    description: string,
    signal: AbortSignal,
  ): Promise<SubagentResult> {
    const definition = this.definitions.get(agentType);
    if (definition === undefined) {
      const available = [...this.definitions.keys()].sort(compareCodePoints).join(", ") || "none";
      return {
        subagentId: "",
        agentType,
        report: `Unknown agent type ${repr(agentType)}. Available: ${available}`,
        isError: true,
        turns: 0,
        costUsd: 0,
      };
    }
    const subagentId = `sub_${randomBytes(4).toString("hex")}`;
    this.running.set(subagentId, agentType);
    this.bus.publish({ kind: "SubagentStarted", subagentId, agentType, description });
    try {
      const loop = this.buildLoop(definition);
      const result = await loop.run(prompt, [], signal);
      let report = finalText(result);
      if (result.error !== null) {
        // Without the reason the parent cannot tell a broken run from one with
        // nothing to say.
        const failure = `The subagent failed: ${result.error}`;
        report = report ? `${report}\n\n${failure}` : failure;
      }
      report ||= "(the subagent returned no text)";
      const cost = loop.session.usage.totalCostUsd;
      this.rollUpCost(loop);
      return {
        subagentId,
        agentType,
        report,
        isError: result.error !== null,
        turns: loop.session.usage.turns.length,
        costUsd: cost,
      };
    } catch (error) {
      if (signal.aborted) throw error;
      const failure = error as Error;
      return {
        subagentId,
        agentType,
        report: `The subagent failed: ${failure.name}: ${failure.message}`,
        isError: true,
        turns: 0,
        costUsd: 0,
      };
    } finally {
      this.running.delete(subagentId);
      this.bus.publish({ kind: "SubagentFinished", subagentId, isError: false });
    }
  }

  private buildLoop(definition: AgentDefinition): AgentLoop {
    const model =
      definition.model || this.settings.models.subagentModel || this.settings.models.model;
    const tools = this.tools.subset(this.allowedTools(definition));
    const context = new ContextBuilder(
      definition.systemPrompt,
      this.settings.cwd,
      this.settings.context.keepRecentTurns,
    );
    const session = newSession(this.settings.cwd, model, this.parentSessionId);
    const loop = new AgentLoop({
      provider: this.provider,
      session,
      tools,
      permissions: this.permissions,
      context,
      compactor: new Compactor(
        this.provider,
        model,
        this.settings.context.keepRecentTurns,
        context,
      ),
      injections: new InjectionRegistry(),
      bus: this.bus,
      settings: this.settings,
      modelInfo: this.models?.getOrDefault(model) ?? null,
      projectContext: this.projectContext,
      hooks: this.hooks,
      models: this.models,
    });
    loop.origin = `${definition.name} subagent`;
    return loop;
  }

  private allowedTools(definition: AgentDefinition): Set<string> {
    const available = new Set(
      this.tools.names().filter((name) => !NEVER_AVAILABLE_TO_SUBAGENTS.has(name)),
    );
    if (definition.tools.length === 0) return available;
    return new Set(definition.tools.filter((name) => available.has(name)));
  }

  /** Fold the subagent's usage into the parent ledger: a subagent that spends
   * real money without moving the parent's cost display makes it a lie. */
  private rollUpCost(loop: AgentLoop): void {
    if (this.parentUsage === null) return;
    for (const turn of loop.session.usage.turns) this.parentUsage.record(turn);
  }

  /** Ids of running subagents, for the TUI progress rows. */
  active(): string[] {
    return [...this.running.keys()].sort(compareCodePoints);
  }
}

function finalText(result: TurnResult): string {
  for (const m of [...result.messages].reverse()) {
    if (m.role === "assistant" && strip(messageText(m))) return strip(messageText(m));
  }
  return "";
}

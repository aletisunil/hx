/**
 * The agent turn engine.
 *
 * One iteration: assemble context -> stream from the provider -> collect
 * tool_use blocks -> execute them -> append results -> repeat, until the model
 * returns a turn with no tool calls or the user cancels.
 *
 * Read-only tools in the same assistant turn run concurrently; anything that
 * mutates state runs serially in the order the model emitted it, so side
 * effects stay predictable.
 *
 * Cancellation is an `AbortSignal` per turn. Every await point that can wait -
 * a provider stream, a running tool - stops when it aborts, and the cleanup a
 * turn owes (answering every call it started) runs on the way out.
 */

import type { Settings } from "../config.ts";
import type { HookEngine } from "../hooks/engine.ts";
import { HookOutcome } from "../hooks/spec.ts";
import { isFile } from "../pathlib.ts";
import type { PermissionEngine, PermissionRequest } from "../permissions/engine.ts";
import type { Provider, ProviderRequest, StreamEnd } from "../providers/base.ts";
import { ProviderError } from "../providers/base.ts";
import type { ModelInfo, ModelRegistry } from "../providers/models.ts";
import { formatFixed, pyLen, repr, strip } from "../py.ts";
import type { ActiveSkills } from "../skills/runtime.ts";
import { count } from "../text.ts";
import type { ToolContext, ToolResult } from "../tools/base.ts";
import {
  applyEdits,
  applyHashline,
  parseEdits,
  parseHashline,
  unifiedDiffOf,
} from "../tools/edit.ts";
import { readExact, resolvePath } from "../tools/read.ts";
import type { ToolRegistry } from "../tools/registry.ts";
import { linkTarget } from "../tools/write.ts";
import type { Compactor } from "./compaction.ts";
import type { AssembledContext, ContextBuilder, Instructions } from "./context.ts";
import type { Event, EventBus } from "./events.ts";
import { withoutImages } from "./images.ts";
import type { InjectionRegistry } from "./lateinject.ts";
import {
  answerUnansweredCalls,
  assistantMessage,
  type ContentBlock,
  type ImageBlock,
  interruptedResult,
  type Message,
  type StopReason,
  type ToolResultBlock,
  type ToolUseBlock,
  textBlock,
  thinkingBlock,
  toolResultBlock,
  toolResultMessage,
  toolUseBlock,
  toolUses,
  type UserTurn,
  userMessage,
} from "./messages.ts";
import { FileTracker } from "./reads.ts";
import type { Session } from "./session.ts";
import { fallbackTitle, generateTitle } from "./title.ts";
import { computeCost, type PlanWindow, type TurnUsage, turnUsage } from "./usage.ts";

/** The user stopped the turn. Thrown at whatever await point the turn was on. */
export class Cancelled extends Error {
  override name = "AbortError";
  constructor() {
    super("cancelled");
  }
}

/** This stream was cut off to deliver a steer, not to stop. */
class Steered extends Error {
  override name = "AbortError";
}

const STEER = Symbol("steer");

/**
 * How long one tool call has run, for its "Took". Started once the call is
 * cleared to run: the time a person spent on an approval is not the tool's.
 */
class Stopwatch {
  private started: number | null = null;

  start(): void {
    this.started = performance.now();
  }

  get ms(): number {
    return this.started === null ? 0 : performance.now() - this.started;
  }
}

export interface TurnResult {
  readonly stopReason: StopReason;
  readonly messages: Message[];
  readonly error: string | null;
}

const turnResult = (
  stopReason: StopReason,
  messages: Message[],
  error: string | null = null,
): TurnResult => ({
  stopReason,
  messages,
  error,
});

export interface AgentLoopOptions {
  provider: Provider;
  session: Session;
  tools: ToolRegistry;
  permissions: PermissionEngine | null;
  context: ContextBuilder;
  compactor: Compactor | null;
  injections: InjectionRegistry;
  bus: EventBus;
  settings: Settings;
  modelInfo?: ModelInfo | null;
  activeSkills?: ActiveSkills | null;
  skillsIndex?: string | null;
  projectContext?: string | null;
  instructions?: Instructions[];
  hooks?: HookEngine | null;
  models?: ModelRegistry | null;
}

/** Drives one conversation. Subagents each get their own instance. */
export class AgentLoop {
  /** Backstop against a model that calls tools forever. */
  static readonly MAX_TURNS = 100;

  provider: Provider;
  session: Session;
  tools: ToolRegistry;
  permissions: PermissionEngine | null;
  context: ContextBuilder;
  compactor: Compactor | null;
  injections: InjectionRegistry;
  bus: EventBus;
  settings: Settings;
  modelInfo: ModelInfo | null;
  activeSkills: ActiveSkills | null;
  skillsIndex: string | null;
  projectContext: string | null;
  /** The AGENTS.md files folded into `projectContext`, least specific first. */
  instructions: Instructions[];
  hooks: HookEngine | null;
  /** Where a window the backend reports is kept. */
  models: ModelRegistry | null;
  lastContext: AssembledContext | null = null;
  /** Set for subagents so approval prompts name who is asking. */
  origin: string | null = null;

  private cancelled = false;
  private turnIndex = 0;
  /** Messages the user pushed into a running turn, not yet delivered. */
  private steers: UserTurn[] = [];
  private stream: AbortController | null = null;
  private turn: AbortController | null = null;

  constructor(options: AgentLoopOptions) {
    this.provider = options.provider;
    this.session = options.session;
    this.tools = options.tools;
    this.permissions = options.permissions;
    this.context = options.context;
    this.compactor = options.compactor;
    this.injections = options.injections;
    this.bus = options.bus;
    this.settings = options.settings;
    this.modelInfo = options.modelInfo ?? null;
    this.activeSkills = options.activeSkills ?? null;
    this.skillsIndex = options.skillsIndex ?? null;
    this.projectContext = options.projectContext ?? null;
    this.instructions = options.instructions ?? [];
    this.hooks = options.hooks ?? null;
    this.models = options.models ?? null;
  }

  get model(): string {
    return this.session.meta.model;
  }

  /** The files this session has read - whichever session is current now. */
  get reads(): FileTracker {
    return new FileTracker(this.session);
  }

  /** The turn's signal; aborted when the user cancels. */
  private get signal(): AbortSignal {
    if (this.turn === null) this.turn = new AbortController();
    return this.turn.signal;
  }

  /** Swap the provider mid-session, returning the one replaced. The caller owns closing it. */
  setProvider(provider: Provider): Provider {
    const previous = this.provider;
    this.provider = provider;
    if (this.compactor !== null) this.compactor.provider = provider;
    return previous;
  }

  /** Re-read the model in force from a catalogue that has just changed. */
  catalogueRefreshed(): void {
    if (this.models === null) return;
    this.modelInfo = this.models.getOrDefault(this.model);
    this.session.usage.contextWindow = this.modelInfo.contextWindow;
  }

  setModel(modelId: string, modelInfo: ModelInfo | null = null): void {
    this.session.meta.model = modelId;
    this.modelInfo = modelInfo;
  }

  /**
   * Run turns until the model stops calling tools. An empty `userInput`
   * continues the conversation without a new user turn - unless there are
   * images, which are a turn on their own. Cancelled - by `cancel`, or by
   * `parent` aborting - it throws after leaving the transcript honest.
   */
  async run(
    userInput: string,
    images: readonly ImageBlock[] = [],
    parent?: AbortSignal,
  ): Promise<TurnResult> {
    this.cancelled = false;
    this.turn = new AbortController();
    // A subagent stops with the turn that started it.
    const stop = () => this.cancel();
    if (parent?.aborted) stop();
    parent?.addEventListener("abort", stop, { once: true });
    try {
      if (userInput || images.length) {
        const gate = await this.fireHooks((h) => h.userPromptSubmit(userInput));
        if (gate.blocked) {
          const reason = gate.reason || "blocked by hook";
          this.publish({ kind: "ErrorRaised", message: reason, recoverable: true });
          return turnResult("error", [], reason);
        }
        let input = userInput;
        if (gate.contextText) input = `${input}\n\n${gate.contextText}`;
        this.session.append(userMessage(input, images));
      }
      return await this.turns([]);
    } finally {
      parent?.removeEventListener("abort", stop);
      await this.fireStop();
    }
  }

  /** Request cancellation of the in-flight turn. */
  cancel(): void {
    this.cancelled = true;
    this.turn?.abort(new Cancelled());
  }

  private throwIfCancelled(): void {
    if (this.signal.aborted) throw this.signal.reason;
  }

  private publish(event: Event): void {
    this.bus.publish(event);
  }

  private async turns(produced: Message[]): Promise<TurnResult> {
    let stopReason: StopReason = "end_turn";
    for (let turn = 0; turn < AgentLoop.MAX_TURNS; turn++) {
      if (this.cancelled) return turnResult("cancelled", produced);
      produced.push(...this.deliverSteer());
      await this.maybeCompact();
      this.turnIndex++;
      if (this.origin === null)
        this.publish({ kind: "TurnStarted", turnIndex: this.turnIndex, model: this.model });

      let result: [Message, StopReason];
      try {
        result = await this.awaitStream();
      } catch (error) {
        if (error instanceof Steered) {
          // The stream was cut off to make room for what the user just said.
          // The next iteration delivers the message and asks again.
          if (this.origin === null)
            this.publish({
              kind: "TurnFinished",
              turnIndex: this.turnIndex,
              stopReason: "cancelled",
            });
          continue;
        }
        if (this.signal.aborted) {
          this.publish({
            kind: "TurnFinished",
            turnIndex: this.turnIndex,
            stopReason: "cancelled",
          });
          throw error;
        }
        if (error instanceof ProviderError) {
          this.reportPlan(error.planWindows);
          this.publish({ kind: "ErrorRaised", message: error.message, recoverable: true });
          this.publish({ kind: "TurnFinished", turnIndex: this.turnIndex, stopReason: "error" });
          return turnResult("error", produced, error.message);
        }
        throw error;
      }
      const [message, reason] = result;
      stopReason = reason;
      this.session.append(message);
      produced.push(message);
      if (this.origin === null)
        this.publish({ kind: "TurnFinished", turnIndex: this.turnIndex, stopReason });

      const calls = toolUses(message);
      if (calls.length && this.cancelled) {
        // Stopped between the answer and its tools: none of them ran, and the
        // transcript has to say so before the turn ends - as does the screen.
        for (const call of calls) {
          this.publish({
            kind: "ToolCallStarted",
            toolUseId: call.id,
            name: this.displayName(call.name),
            input: call.input,
          });
          this.publish(
            finished(call.id, { isError: true, summary: "interrupted", interrupted: true }),
          );
        }
        produced.push(this.answerCalls(calls, new Map()));
      }
      if (calls.length === 0 || this.cancelled) {
        // A steer that arrived during the last stretch is the next thing the
        // user said, so the conversation continues.
        if (this.steers.length && !this.cancelled) continue;
        await this.nameSession();
        return turnResult(stopReason, produced);
      }

      const results = new Map<string, ToolResultBlock>();
      try {
        await this.executeTools(calls, results);
      } finally {
        // Also on interrupt: a call left without a result is rejected by every
        // route on the next request. What finished keeps its real result.
        produced.push(this.answerCalls(calls, results));
      }
    }
    this.publish({
      kind: "ErrorRaised",
      message: `Stopped after ${AgentLoop.MAX_TURNS} turns`,
      recoverable: true,
    });
    return turnResult(stopReason, produced, "turn limit reached");
  }

  /**
   * Put `text` into the turn that is running now. Returns whether it will land
   * mid-turn. While the model is streaming, that stream is cut off; while
   * *tools* are running they are left to finish, and the message lands at the
   * next model call instead.
   */
  steer(text: string, images: readonly ImageBlock[] = []): boolean {
    const stripped = strip(text);
    if ((!stripped && images.length === 0) || this.origin !== null) return false;
    this.steers.push({ text: stripped, images });
    const stream = this.stream;
    if (stream === null || stream.signal.aborted) return false;
    stream.abort(STEER);
    return true;
  }

  /** Hand undelivered steers back to the caller, clearing them. */
  takePendingSteer(): UserTurn[] {
    const pending = this.steers;
    this.steers = [];
    return pending;
  }

  /** Append pending steers to the transcript as what they are: user turns. */
  private deliverSteer(): Message[] {
    return this.takePendingSteer().map((turn) => {
      const m = userMessage(turn.text, turn.images);
      this.session.append(m);
      return m;
    });
  }

  /** Run one stream under a signal of its own, so a steer can end that
   * provider call and no more. */
  private async awaitStream(): Promise<[Message, StopReason]> {
    const controller = new AbortController();
    const turnSignal = this.signal;
    const relay = () => controller.abort(turnSignal.reason);
    if (turnSignal.aborted) relay();
    else turnSignal.addEventListener("abort", relay, { once: true });
    this.stream = controller;
    try {
      return await this.streamTurn(controller.signal);
    } catch (error) {
      if (controller.signal.reason === STEER && !this.cancelled) throw new Steered("steered");
      throw error;
    } finally {
      turnSignal.removeEventListener("abort", relay);
      this.stream = null;
    }
  }

  /**
   * Give the session a title once, after its first completed exchange.
   * Awaited rather than detached: in print mode the process exits the moment
   * `run` returns. Naming must never cost the user a turn, so every failure
   * falls back to the first thing they said.
   */
  private async nameSession(): Promise<void> {
    if (this.origin !== null || this.session.meta.title || this.cancelled) return;
    const messages = this.nameableMessages(this.session);
    if (messages === null) return;
    let title: string | null;
    try {
      title = await this.askForTitle(messages, this.session);
    } catch (error) {
      if (!this.session.meta.title) this.session.setTitle(fallbackTitle(messages));
      throw error;
    }
    // The user may have named it with /title while the call was out.
    if (this.session.meta.title) return;
    this.session.setTitle(title || fallbackTitle(messages));
  }

  /**
   * Rename a session for what it turned into, as the user leaves it - only
   * when the conversation changed since the name was written, never over a
   * name the user chose. Detached from any turn, so it takes a signal of its
   * own: an interrupted turn must not cancel the rename that follows it.
   */
  async retitleSession(target: Session, signal: AbortSignal = this.signal): Promise<void> {
    if (this.origin !== null || target.meta.titlePinned) return;
    const messages = this.nameableMessages(target);
    if (messages === null) return;
    const promptCount = target.meta.promptCount;
    if (promptCount === target.meta.titlePromptCount) return;
    const title = await this.askForTitle(messages, target, signal);
    // The user may have named it while the call was out; their name stands.
    if (title && !target.meta.titlePinned) target.setTitle(title, { promptCount });
  }

  private nameableMessages(session: Session): Message[] | null {
    const messages = session.messages.filter((m) => !m.ephemeral);
    return messages.some((m) => m.role === "user") ? messages : null;
  }

  /** One tiny provider call. `null` if it produced nothing usable. Billed to
   * the session being named, which is not always the live one. */
  private async askForTitle(
    messages: Message[],
    session: Session,
    signal: AbortSignal = this.signal,
  ): Promise<string | null> {
    let title: string | null;
    let usage: TurnUsage;
    try {
      const model = this.settings.models.titleModel || this.model;
      [title, usage] = await generateTitle(this.provider, model, messages, signal);
    } catch (error) {
      if (signal.aborted) throw error;
      return null;
    }
    if (usage.inputTokens + usage.cacheReadTokens || usage.outputTokens)
      this.recordUsage(usage, null, session);
    return title;
  }

  /** One provider call. Publishes deltas and the usage update. */
  private async streamTurn(signal: AbortSignal): Promise<[Message, StopReason]> {
    const request = await this.buildRequest();
    const textParts: string[] = [];
    const thinkingParts: string[] = [];
    let thinkingSignature: string | null = null;
    const toolCalls: ToolUseBlock[] = [];
    let stopReason: StopReason = "end_turn";
    let usage = turnUsage();
    const started = performance.now();

    try {
      for await (const item of this.provider.astream(request, signal)) {
        if (item.kind === "end") {
          stopReason = item.stopReason;
          usage = item.usage;
          this.adoptLimits(item);
          this.reportPlan(item.planWindows);
          continue;
        }
        if (item.text) {
          textParts.push(item.text);
          // A subagent's prose is an intermediate result that reaches the user
          // as the Task tool's output, not the assistant speaking.
          if (this.origin === null) this.publish({ kind: "TextDelta", text: item.text });
        }
        if (item.thinking) {
          thinkingParts.push(item.thinking);
          if (this.origin === null) this.publish({ kind: "ThinkingDelta", text: item.thinking });
        }
        if (item.thinkingSignature) thinkingSignature = item.thinkingSignature;
        if (item.toolUseId && item.toolName) {
          toolCalls.push(
            toolUseBlock(item.toolUseId, item.toolName, parseToolInput(item.toolInputJson ?? null)),
          );
        }
      }
    } catch (error) {
      if (signal.aborted) {
        // Keep whatever prose streamed before the interrupt: the next turn must
        // reflect what the user saw. The tool calls are dropped - they never
        // ran - and a stream cut off before any text carries nothing a route
        // can encode.
        if (textParts.length)
          this.session.append(this.assemble(textParts, thinkingParts, [], thinkingSignature));
      }
      throw error;
    }
    if (!usage.latencyMs) usage.latencyMs = performance.now() - started;
    this.recordUsage(usage, request);
    return [this.assemble(textParts, thinkingParts, toolCalls, thinkingSignature), stopReason];
  }

  private async buildRequest(): Promise<ProviderRequest> {
    let messages = answerUnansweredCalls(
      await this.injections.apply(this.session.activeMessages()),
    );
    if (this.modelInfo === null || !this.modelInfo.supportsImages) {
      messages = withoutImages(messages, this.modelInfo?.name ?? this.model);
    }
    const cacheMode = this.modelInfo ? this.modelInfo.cacheMode : "none";
    const assembled = this.context.build(messages, this.tools.schemas(this.allowedTools()), {
      skillsIndex: this.skillsIndex,
      projectContext: this.projectContext,
      cacheMode,
    });
    this.lastContext = assembled;
    // What the model was told, recorded alongside what it said: the full
    // registry, not this turn's allowed subset.
    if (this.session.environment === null) {
      this.session.recordEnvironment({
        systemPrompt: this.context.systemPrompt,
        tools: this.tools.schemas(),
        projectContext: this.projectContext,
        skillsIndex: this.skillsIndex,
      });
    }
    this.session.usage.contextTokens = assembled.totalTokens;
    this.session.usage.contextWindow = this.modelInfo ? this.modelInfo.contextWindow : 0;
    return {
      context: assembled,
      model: this.model,
      maxTokens: this.settings.models.maxTokens,
      temperature: this.settings.models.temperature,
      stopSequences: [],
      extra: {},
    };
  }

  /**
   * Which tools the model may see this turn. A loaded skill's `allowed-tools`
   * narrows the set - intersected, never unioned - so a skill can only restrict.
   */
  private allowedTools(): Set<string> | null {
    let allowed: Set<string> | null = null;
    if (this.permissions !== null) {
      const names = this.tools.names();
      const mutating = new Set(names.filter((name) => this.tools.get(name).mutating));
      allowed = this.permissions.allowedTools(names, mutating);
    }
    const skillAllowed = this.activeSkills?.toolAllowlist();
    if (skillAllowed) {
      // Skill and Task stay reachable so the model can switch skills or delegate.
      const keep = new Set([...skillAllowed, "Skill", "Task"]);
      allowed = allowed === null ? keep : new Set([...allowed].filter((name) => keep.has(name)));
    }
    return allowed;
  }

  private assemble(
    textParts: string[],
    thinkingParts: string[],
    toolCalls: ToolUseBlock[],
    signature: string | null,
  ): Message {
    const blocks: ContentBlock[] = [];
    if (thinkingParts.length || signature)
      blocks.push(thinkingBlock(thinkingParts.join(""), signature));
    if (textParts.length) blocks.push(textBlock(textParts.join("")));
    blocks.push(...toolCalls);
    return assistantMessage(blocks, this.model);
  }

  /** Take the window the backend reported over the catalogue's figure, and
   * keep it in the catalogue too. */
  private adoptLimits(end: StreamEnd): void {
    const info = this.modelInfo;
    if (info === null || info.id !== this.model) return;
    const window = end.contextWindow || info.contextWindow;
    const output = end.maxOutputTokens || info.maxOutputTokens;
    if (window === info.contextWindow && output === info.maxOutputTokens) return;
    this.modelInfo = { ...info, contextWindow: window, maxOutputTokens: output };
    this.session.usage.contextWindow = window;
    this.models?.adoptLimits(info.id, window, output);
  }

  /** Pass on what the route said about its usage windows, if anything. */
  private reportPlan(windows: readonly PlanWindow[]): void {
    if (windows.length)
      this.publish({ kind: "PlanUsageUpdated", route: this.provider.name, windows });
  }

  /** Bill a call to a session's ledger. Spend that belongs to another session
   * is not published: the status bar shows the live one. */
  private recordUsage(
    usage: TurnUsage,
    _request: ProviderRequest | null,
    session: Session | null = null,
  ): void {
    if (usage.costUsd === null && this.modelInfo !== null)
      usage.costUsd = computeCost(usage, this.modelInfo.pricing);
    const target = session ?? this.session;
    target.recordUsage(usage);
    target.flush();
    if (target !== this.session) return;
    const ledger = this.session.usage;
    this.publish({
      kind: "UsageUpdated",
      inputTokens: ledger.totalInput,
      outputTokens: ledger.totalOutput,
      cacheReadTokens: ledger.totalCacheRead,
      cacheWriteTokens: ledger.totalCacheWrite,
      contextTokens: ledger.contextTokens,
      contextWindow: ledger.contextWindow,
      costUsd: ledger.totalCostUsd,
    });
  }

  /**
   * Permission-check, then run. Read-only calls are gathered concurrently;
   * mutating calls run in emission order. Each result lands in `results` the
   * moment its call finishes, so an interrupt keeps what had already run.
   */
  private async executeTools(
    calls: ToolUseBlock[],
    results: Map<string, ToolResultBlock>,
  ): Promise<void> {
    let concurrent: ToolUseBlock[] = [];
    const run = async (call: ToolUseBlock) => {
      results.set(call.id, await this.runOne(call, results));
    };
    const gather = async (batch: ToolUseBlock[]) => {
      const settled = await Promise.allSettled(batch.map(run));
      const failure = settled.find((outcome) => outcome.status === "rejected");
      if (failure) throw (failure as PromiseRejectedResult).reason;
    };
    for (const call of calls) {
      if (this.runsSerially(call)) {
        if (concurrent.length) await gather(concurrent);
        concurrent = [];
        await run(call);
      } else concurrent.push(call);
    }
    if (concurrent.length) await gather(concurrent);
  }

  /** Append the results for `calls`, in the order the model asked for them. A
   * call with no result was cut off by the user, and is answered as that. */
  private answerCalls(calls: ToolUseBlock[], results: Map<string, ToolResultBlock>): Message {
    const m = toolResultMessage(
      calls.map((call) => results.get(call.id) ?? interruptedResult(call.id)),
    );
    this.session.append(m);
    return m;
  }

  private runsSerially(call: ToolUseBlock): boolean {
    if (!this.tools.has(call.name)) return true; // about to be an error result; never concurrent
    const tool = this.tools.get(call.name);
    return tool.mutating && !tool.parallelSafe;
  }

  /** Attribute a subagent's tool calls so they do not read as the parent's. */
  private displayName(name: string): string {
    return this.origin === null ? name : `${this.origin} > ${name}`;
  }

  /** Run one call. Interrupted, it leaves its answer in `results`. */
  private async runOne(
    original: ToolUseBlock,
    results: Map<string, ToolResultBlock>,
  ): Promise<ToolResultBlock> {
    const clock = new Stopwatch();
    let call = original;
    // PreToolUse runs before the call is announced, so a rewritten input never
    // leaves the transcript showing a command that never ran.
    const pre = await this.fireHooks((h) => h.preToolUse(call.name, call.input));
    if (pre.updatedInput !== null) call = { ...call, input: { ...pre.updatedInput } };
    this.throwIfCancelled();

    this.publish({
      kind: "ToolCallStarted",
      toolUseId: call.id,
      name: this.displayName(call.name),
      input: call.input,
    });
    const streamed: string[] = [];
    try {
      return await this.runAnnounced(call, pre, clock, streamed, results);
    } catch (error) {
      if (!this.signal.aborted) throw error;
      // The tool had finished and only its hooks were cut off.
      if (results.has(call.id)) throw error;
      const cap = this.settings.context.toolOutputCharCap;
      const output = [...streamed.join("")];
      results.set(
        call.id,
        interruptedResult(call.id, output.slice(Math.max(0, output.length - cap)).join("")),
      );
      // The call is on screen as running; left alone it would spin on.
      this.publish(
        finished(call.id, {
          isError: true,
          durationMs: clock.ms,
          summary: "interrupted",
          interrupted: true,
        }),
      );
      throw error;
    }
  }

  /** The part of `runOne` after the call is on screen. */
  private async runAnnounced(
    call: ToolUseBlock,
    pre: HookOutcome,
    clock: Stopwatch,
    streamed: string[],
    results: Map<string, ToolResultBlock>,
  ): Promise<ToolResultBlock> {
    if (pre.blocked) {
      const refusal = toolResultBlock(
        call.id,
        `${call.name} was blocked by a hook: ${pre.reason}`,
        { isError: true },
      );
      this.publish(
        finished(call.id, {
          isError: true,
          durationMs: clock.ms,
          summary: "blocked by hook",
          detail: refusal.content,
        }),
      );
      return refusal;
    }
    const denied = await this.checkPermission(call);
    this.throwIfCancelled();
    if (denied !== null) {
      this.publish(
        finished(call.id, {
          isError: true,
          durationMs: clock.ms,
          summary: "denied",
          detail: denied.content,
        }),
      );
      return denied;
    }
    clock.start();
    const ctx: ToolContext = {
      cwd: this.settings.cwd,
      sessionId: this.session.meta.sessionId,
      toolUseId: call.id,
      reads: this.reads,
      settings: this.settings,
      emitProgress: (chunk) => {
        streamed.push(chunk);
        this.publish({ kind: "ToolCallProgress", toolUseId: call.id, chunk });
      },
      signal: this.signal,
    };
    const result = await this.tools.call(call.name, call.input, ctx);
    const post = await this.fireHooks((h) =>
      h.postToolUse(call.name, call.input, result.content, result.isError),
    );
    if (this.signal.aborted) {
      // The tool finished and its effects are real: its own result stands.
      results.set(call.id, this.finish(call, result, new HookOutcome(), clock));
      throw this.signal.reason;
    }
    return this.finish(call, result, post, clock);
  }

  /** Announce a call the tool finished, and its result for the model. */
  private finish(
    call: ToolUseBlock,
    result: ToolResult,
    post: HookOutcome,
    clock: Stopwatch,
  ): ToolResultBlock {
    let content = result.content;
    if (post.blocked) content = `${content}\n\nA hook rejected this result: ${post.reason}`;
    else if (post.contextText) content = `${content}\n\n${post.contextText}`;
    const isError = result.isError || post.blocked;
    this.publish(
      finished(call.id, {
        isError,
        durationMs: clock.ms,
        summary: result.summary || (result.isError ? "error" : "done"),
        detail: isError ? content : "",
        metadata: { ...result.metadata },
      }),
    );
    return toolResultBlock(call.id, content, {
      isError,
      spilledPath: result.spilledPath,
      images: [...result.images],
    });
  }

  /** A denial result, or `null` when the call may proceed. */
  private async checkPermission(call: ToolUseBlock): Promise<ToolResultBlock | null> {
    if (this.permissions === null || !this.tools.has(call.name)) return null;
    const tool = this.tools.get(call.name);
    const [detail, detailKind] = permissionDetail(call, this.settings.cwd);
    const request: PermissionRequest = {
      toolName: call.name,
      specifier: tool.permissionSpecifier(call.input),
      specifiers: tool.permissionSpecifiers(call.input),
      params: call.input,
      mutating: tool.mutating,
      description: `${call.name}(${brief(call.input)})`,
      detail,
      detailKind,
      linkTarget: linkTargetOf(call, this.settings.cwd),
      origin: this.origin,
      toolUseId: call.id,
    };
    const [allowed, reason] = await this.permissions.request(request, () =>
      this.announceAsk(call.id, request),
    );
    if (allowed) return null;
    return toolResultBlock(call.id, `${call.name} was not permitted: ${reason}`, { isError: true });
  }

  /** Announce only the calls that actually stop for approval. */
  private announceAsk(callId: string, request: PermissionRequest): void {
    this.publish({
      kind: "PermissionRequested",
      requestId: callId,
      toolName: request.toolName,
      description: request.description,
      detail: request.detail ?? "",
    });
  }

  /** Fire `Stop` on every way out of a turn, not only the tidy one. */
  private async fireStop(): Promise<void> {
    try {
      await this.fireHooks((h) => h.stop());
    } catch {
      // A stop hook that fails must not hide the turn's own outcome.
    }
  }

  /** Run one hook event, reporting anything that broke. Only an explicit
   * refusal - exit 2, or `{"decision": "block"}` - stops anything. */
  private async fireHooks(call: (hooks: HookEngine) => Promise<HookOutcome>): Promise<HookOutcome> {
    if (this.hooks === null) return new HookOutcome();
    const outcome = await call(this.hooks);
    for (const problem of outcome.errors)
      this.publish({ kind: "ErrorRaised", message: `hook: ${problem}`, recoverable: true });
    return outcome;
  }

  /**
   * Compact if the context fraction crossed the configured threshold - checked
   * against what there is to summarise before anything is announced, so a
   * crossed threshold with nothing behind it does not announce a compaction
   * that does nothing, once per turn.
   */
  private async maybeCompact(): Promise<void> {
    if (this.compactor === null) return;
    const fraction = this.session.usage.contextFraction;
    if (!this.compactor.shouldCompact(fraction, this.settings.context.compactAt)) return;
    if (!this.compactor.canCompact(this.session.activeMessages())) return;
    await this.compact(null, `context at ${formatFixed(fraction * 100, 0)}% of the window`);
  }

  /**
   * Replace older turns with a summary. Returns false when there was nothing
   * worth compacting. The cached conversation is discarded by definition, and
   * that is announced rather than done quietly.
   */
  async compact(instructions: string | null = null, reason = "requested"): Promise<boolean> {
    if (this.compactor === null) return false;
    if (!this.compactor.canCompact(this.session.activeMessages())) return false;
    this.publish({ kind: "CompactionStarted", reason });
    let result: Awaited<ReturnType<Compactor["compact"]>>;
    try {
      result = await this.compactor.compact(
        this.session.activeMessages(),
        instructions,
        this.signal,
      );
    } catch (error) {
      if (!(error instanceof ProviderError)) throw error;
      this.reportPlan(error.planWindows);
      this.publish({
        kind: "ErrorRaised",
        message: `Compaction failed: ${error.message}`,
        recoverable: true,
      });
      return false;
    }
    this.session.recordCompaction(result.dropped, result.kept, result.summary);
    // The estimates cover the conversation only; the system prompt and tools
    // ahead of it are still sent, so they stay in the gauge.
    const prefix = (this.lastContext?.sections ?? [])
      .filter((section) => section.name !== "history")
      .reduce((total, section) => total + section.tokens, 0);
    this.session.usage.contextTokens = prefix + result.tokensAfter;
    this.publish({
      kind: "CompactionFinished",
      tokensBefore: result.tokensBefore,
      tokensAfter: result.tokensAfter,
    });
    return true;
  }
}

function finished(
  toolUseId: string,
  fields: {
    isError: boolean;
    durationMs?: number;
    summary: string;
    detail?: string;
    metadata?: Record<string, unknown>;
    interrupted?: boolean;
  },
): Event {
  return {
    kind: "ToolCallFinished",
    toolUseId,
    isError: fields.isError,
    durationMs: fields.durationMs ?? 0,
    summary: fields.summary,
    detail: fields.detail ?? "",
    metadata: fields.metadata ?? {},
    interrupted: fields.interrupted ?? false,
  };
}

/** Tool arguments arrive as a JSON string assembled from stream fragments.
 * Malformed JSON becomes `{}`; the tool's own validation then reports the
 * missing arguments to the model. */
function parseToolInput(raw: string | null): Record<string, unknown> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * What the approval shows, and what kind of thing it is. For an edit this
 * previews the change against the file on disk, resolved against the session's
 * directory the way the tool resolves it, so the user approves a diff.
 */
function permissionDetail(call: ToolUseBlock, cwd: string): [string, "text" | "diff" | "command"] {
  if (call.name === "Bash") return [String(call.input.command ?? ""), "command"];
  const rawPath = call.input.file_path;
  if ((call.name === "Edit" || call.name === "Write") && rawPath) {
    try {
      const path = resolvePath(String(rawPath), cwd);
      const before = isFile(path) ? readExact(path) : "";
      let after: string;
      if (call.name === "Write") after = String(call.input.content ?? "");
      // An anchored edit is previewed by resolving it, the same way the tool will.
      else if (call.input.hashline) after = applyHashline(before, parseHashline(call.input));
      else after = applyEdits(before, parseEdits(call.input));
      const diff = unifiedDiffOf(before, after, path);
      return diff ? [diff, "diff"] : [`${path} (no change)`, "text"];
    } catch {
      // Previewing is best effort; never block the prompt on it.
      return [String(rawPath), "text"];
    }
  }
  return [detailed(call.input), "text"];
}

/** Where an Edit or Write lands when the path it names is a symlink. */
function linkTargetOf(call: ToolUseBlock, cwd: string): string | null {
  const rawPath = call.input.file_path;
  if ((call.name !== "Edit" && call.name !== "Write") || !rawPath) return null;
  try {
    return linkTarget(resolvePath(String(rawPath), cwd));
  } catch {
    return null;
  }
}

/** Elements shown in full before the rest are counted. */
const MAX_LISTED = 20;

/**
 * What the approval prompt shows for a tool with no preview of its own. Lists
 * are spelled out one element per line: the whole question for `WebFetch` is
 * *which* URLs are about to leave the machine.
 */
function detailed(params: Record<string, unknown>, limit = 400): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
      let listed = value
        .slice(0, MAX_LISTED)
        .map((item) => `  ${item}`)
        .join("\n");
      if (value.length > MAX_LISTED) listed += `\n  … and ${value.length - MAX_LISTED} more`;
      parts.push(value.length ? `${key}:\n${listed}` : `${key}: (empty)`);
    } else parts.push(`${key}=${oneValue(value, limit)}`);
  }
  return parts.join("\n");
}

function oneValue(value: unknown, limit: number): string {
  if (Array.isArray(value)) return `[${count(value.length, "item")}]`;
  if (typeof value === "object" && value !== null) return "{…}";
  if (typeof value === "string" && pyLen(value) > limit)
    return repr(`${[...value].slice(0, limit - 1).join("")}…`);
  return repr(value);
}

/** One-line parameter preview for a tool-call header. Nested structures
 * collapse to a placeholder rather than being sliced mid-literal. */
export function brief(params: Record<string, unknown>, limit = 80): string {
  const joined = Object.entries(params)
    .map(([key, value]) => `${key}=${oneValue(value, limit)}`)
    .join(", ");
  return pyLen(joined) <= limit ? joined : `${[...joined].slice(0, limit - 1).join("")}…`;
}

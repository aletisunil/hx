/**
 * Claude Code provider - a Claude Pro/Max subscription, through the `claude` CLI.
 *
 * HX does not call the Anthropic API here. It runs Claude Code - the official
 * CLI, signed in to the user's Claude account - and lets it make the calls:
 *
 * - the model sees HX's tools, served to the CLI as one in-process MCP server,
 *   with every built-in Claude Code tool off - so every call is still run,
 *   permission-checked and drawn by HX;
 * - the system prompt is Claude Code's own, with HX's project context, skills
 *   index and user additions appended;
 * - thinking, text and tool calls stream back as raw Anthropic events.
 *
 * HX calls `astream` once per model turn; the CLI wants one prompt and then
 * runs tools until the model is done. They meet at the tool call: the CLI's
 * `tools/call` is held open and `astream` ends with `tool_use`. HX runs the
 * tool and calls `astream` again with the result, which answers the held
 * call. One process serves a conversation while the two transcripts agree;
 * when they part, a new process starts on a session rebuilt from HX's.
 */

import { createHash, randomBytes } from "node:crypto";
import { closeSync, openSync, rmdirSync, unlinkSync, writeSync } from "node:fs";
import { tmpdir } from "node:os";
import * as ccAuth from "../auth/claude_code.ts";
import type { AuthResolver } from "../auth/resolve.ts";
import { SYSTEM_PROMPT, systemText } from "../core/context.ts";
import {
  type Message,
  message,
  messageText,
  type StopReason,
  toolResults,
  toolUses,
} from "../core/messages.ts";
import { formatRenewal, PlanWindow, type TurnUsage, turnUsage } from "../core/usage.ts";
import { dumps } from "../json.ts";
import { withTlsHint } from "../net.ts";
import { join } from "../pathlib.ts";
import { splitWhitespace, strip, toInt } from "../py.ts";
import {
  delta,
  type Provider,
  ProviderError,
  type ProviderRequest,
  type StreamDelta,
  type StreamItem,
  streamEnd,
} from "./base.ts";
import { DATED } from "./claude_code_catalogue.ts";
import { CallCancelled, CliProcess, Exit, ToolServer } from "./claude_code_cli.ts";
import * as transcript from "./claude_code_transcript.ts";
import type { ModelRegistry } from "./models.ts";
import { BUILDERS } from "./registry.ts";

export const PROVIDER_ID = ccAuth.PROVIDER_ID;
export const MODEL_PREFIX = `${PROVIDER_ID}/`;

export type EffortSource = (modelId: string) => string | null;

/** Most processes kept between turns: the main conversation and subagents. */
const POOL_SIZE = 4;
/** Idle processes kept for a next prompt: the main conversation's. */
const IDLE_KEPT = 1;

export const CHILD_ENV: Record<string, string> = {
  // HX's tools must all be in front of the model, not behind tool search.
  ENABLE_TOOL_SEARCH: "false",
  // HX compacts its own transcript; the CLI compacting its copy would leave
  // the two describing different conversations.
  DISABLE_AUTO_COMPACT: "1",
  CLAUDE_CODE_DISABLE_AUTO_MEMORY: "1",
  // HX caps and spills tool output itself.
  MAX_MCP_OUTPUT_TOKENS: "1000000",
  // No auto-update, telemetry or error reports from a process HX started.
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
};

const CHILD_SETTINGS = { autoMemoryEnabled: false, autoCompactEnabled: false };

type State = "idle" | "streaming" | "parked" | "dead";
type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const objectOf = (value: unknown): Json => (isObject(value) ? value : {});

/** Everything fixed when a process starts. A request needing anything else
 * needs another process. */
interface SpawnKey {
  readonly model: string;
  readonly effort: string | null;
  /** Digest of the tool schemas: plan mode and skills narrow the set. */
  readonly tools: string;
  /** Digest of what HX appends to the system prompt. */
  readonly append: string;
  readonly cwd: string;
}

const sameKey = (a: SpawnKey, b: SpawnKey) =>
  a.model === b.model &&
  a.effort === b.effort &&
  a.tools === b.tools &&
  a.append === b.append &&
  a.cwd === b.cwd;

const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");

class Conversation {
  /** Fingerprints of the HX messages the CLI holds, in order. */
  mirror: string[];
  state: State;
  /** What the last streamed turn said and called, to recognise it in HX's
   * transcript when the next request arrives. */
  producedText = "";
  producedCalls: string[] = [];
  /** Held `tools/call` requests, by tool_use id. */
  private readonly waiting = new Map<
    string,
    { resolve(result: Json): void; reject(error: Error): void }
  >();
  /** Results HX delivered before the CLI asked for them. */
  private readonly answers = new Map<string, Json>();
  readonly answered = new Set<string>();
  /** The CLI's copy changed under HX - it answered a call itself, or compacted. */
  diverged = false;
  cleanup: string[];
  lastUsed = performance.now();

  constructor(
    readonly key: SpawnKey,
    readonly cli: CliProcess,
    readonly names: transcript.ToolNames,
    readonly oneShot: boolean,
    mirror: string[],
    state: State,
    cleanup: string[],
  ) {
    this.mirror = mirror;
    this.state = state;
    this.cleanup = cleanup;
  }

  handleCall = async (toolUseId: string, _name: string, _args: Json): Promise<Json> => {
    this.answered.add(toolUseId);
    const ready = this.answers.get(toolUseId);
    if (ready !== undefined) {
      this.answers.delete(toolUseId);
      return ready;
    }
    try {
      return await new Promise<Json>((resolve, reject) =>
        this.waiting.set(toolUseId, { resolve, reject }),
      );
    } finally {
      this.waiting.delete(toolUseId);
    }
  };

  deliver(toolUseId: string, result: Json): void {
    const waiter = this.waiting.get(toolUseId);
    if (waiter !== undefined) waiter.resolve(result);
    else this.answers.set(toolUseId, result);
  }

  async close(): Promise<void> {
    this.state = "dead";
    for (const waiter of this.waiting.values()) waiter.reject(new CallCancelled("closed"));
    await this.cli.close();
    remove(this.cleanup);
    this.cleanup = [];
  }
}

export class ClaudeCodeProvider implements Provider {
  readonly name = PROVIDER_ID;
  readonly cwd: string;
  readonly effort: EffortSource;
  private readonly cliPath: string | null;
  private pool: Conversation[] = [];
  /** Processes started so far. One per conversation while transcripts agree. */
  spawned = 0;

  constructor(
    options: { cwd?: string | null; effort?: EffortSource | null; cliPath?: string | null } = {},
  ) {
    this.cwd = options.cwd ?? process.cwd();
    this.effort = options.effort ?? (() => null);
    this.cliPath = options.cliPath ?? null;
  }

  // -- the Provider protocol -------------------------------------------------------

  async *astream(request: ProviderRequest, signal: AbortSignal): AsyncGenerator<StreamItem> {
    const messages = request.context.messages;
    if (!messages.length) throw new ProviderError("Nothing to send: the conversation is empty.");
    checkPrompt(request);
    const key = this.spawnKey(request);
    const prints = messages.map(transcript.fingerprint);

    let conversation: Conversation;
    const found = this.continueWith(key, messages, prints);
    if (found !== null) {
      conversation = found[0];
      conversation.state = "streaming";
      await this.resume(conversation, messages.at(-1) as Message, found[1]);
    } else {
      await this.retire(prints);
      conversation = await this.start(key, request, prints);
    }
    let finished = false;
    try {
      yield* this.stream(conversation, request, signal);
      finished = true;
    } finally {
      conversation.lastUsed = performance.now();
      // Cancelled mid-stream (Esc, a steer), or the stream failed: the CLI's
      // copy now holds a turn HX's transcript will not.
      if (!finished) await this.drop(conversation);
    }
    if (conversation.oneShot && conversation.state === "idle") await this.drop(conversation);
    else await this.trim();
  }

  async aclose(): Promise<void> {
    const pool = this.pool;
    this.pool = [];
    for (const conversation of pool) await conversation.close();
  }

  // -- matching a request to a running conversation --------------------------------

  /**
   * A running conversation that `messages` extends by one step - the turn it
   * streamed last, plus the results it waits on or one new user turn - and
   * whether it was holding tool calls. Anything else means the transcripts
   * parted, and a rebuild is the only safe answer.
   */
  private continueWith(
    key: SpawnKey,
    messages: readonly Message[],
    prints: string[],
  ): [Conversation, boolean] | null {
    for (const conversation of [...this.pool].sort((a, b) => b.lastUsed - a.lastUsed)) {
      if (
        (conversation.state !== "idle" && conversation.state !== "parked") ||
        conversation.diverged
      )
        continue;
      if (!sameKey(conversation.key, key) || !conversation.cli.alive) continue;
      const size = conversation.mirror.length;
      if (
        prints.slice(0, size).join("\n") !== conversation.mirror.join("\n") ||
        messages.length !== size + 2
      )
        continue;
      const produced = messages[size] as Message;
      const tail = messages[size + 1] as Message;
      if (!this.isProduced(conversation, produced) || tail.role !== "user") continue;
      const results = toolResults(tail);
      const held = conversation.state === "parked";
      if (held) {
        if (results.map((r) => r.toolUseId).join("\n") !== conversation.producedCalls.join("\n"))
          continue;
        // A steer folded into the results' message has to reach the model as
        // the user's own turn, which a held call cannot do.
        if (tail.content.some((b) => b.type !== "tool_result" && !transcript.isReminder(b)))
          continue;
      } else if (results.length || !transcript.promptContent(tail).length) continue;
      conversation.mirror = prints;
      return [conversation, held];
    }
    return null;
  }

  private isProduced(conversation: Conversation, produced: Message): boolean {
    return (
      produced.role === "assistant" &&
      messageText(produced) === conversation.producedText &&
      toolUses(produced)
        .map((call) => call.id)
        .join("\n") === conversation.producedCalls.join("\n")
    );
  }

  /** Hand the running CLI the step HX took: tool results, or a new prompt. */
  private async resume(conversation: Conversation, tail: Message, held: boolean): Promise<void> {
    if (!held) {
      try {
        await conversation.cli.sendUser(transcript.promptContent(tail));
      } catch (error) {
        await this.drop(conversation);
        throw error;
      }
      return;
    }
    const reminders = tail.content.filter(transcript.isReminder).map((b) => strip(b.text));
    const results = toolResults(tail);
    results.forEach((result, index) => {
      // Per-turn reminders ride on the last result: a held call is the only
      // way anything reaches the model before its next reply.
      const extra = index === results.length - 1 ? reminders : null;
      conversation.deliver(result.toolUseId, transcript.mcpResult(result, extra));
    });
  }

  // -- starting a process ----------------------------------------------------------

  private async start(
    key: SpawnKey,
    request: ProviderRequest,
    prints: string[],
  ): Promise<Conversation> {
    const messages = request.context.messages;
    const tools = request.context.tools;
    const names = transcript.ToolNames.forTools(tools.map((t) => String(t.name)));
    const [history, prompt] = splitPrompt(messages);
    const cleanup: string[] = [];
    const argv = this.argv(key, tools.length > 0);
    // A working session: Claude Code's own prompt, with HX's context. A
    // one-off with no tools - naming a session, summarising one - has its own
    // short prompt and no tool guidance.
    const [flag, system] = tools.length
      ? ["--append-system-prompt-file", appendedPrompt(request)]
      : ["--system-prompt-file", systemText(request.context)];
    if (system) {
      const promptFile = privateFile(system);
      cleanup.push(promptFile);
      argv.push(flag, promptFile);
    }
    const turns = transcript.historyTurns(history, names, wireModel(key.model));
    if (turns.length) {
      const [sessionId, path] = transcript.writeSession(turns, key.cwd);
      cleanup.push(path);
      argv.push("--resume", sessionId);
    }
    let server: ToolServer | null = null;
    if (tools.length)
      server = new ToolServer(
        tools.map((tool) => ({
          name: names.add(String(tool.name)),
          description: String(tool.description || ""),
          inputSchema: objectSchema(tool.input_schema || tool.parameters),
        })),
        noTools,
      );
    let cli: CliProcess;
    try {
      cli = await CliProcess.spawn(argv, { cwd: key.cwd, env: ccAuth.childEnv(CHILD_ENV), server });
    } catch (error) {
      remove(cleanup);
      throw error;
    }
    this.spawned++;
    const conversation = new Conversation(
      key,
      cli,
      names,
      !tools.length,
      prints,
      "streaming",
      cleanup,
    );
    // No call can arrive before the prompt is sent below.
    if (server !== null) server.handler = conversation.handleCall;
    this.pool.push(conversation);
    try {
      await cli.initialize();
      // Read by now: the CLI loads its session and prompt file before it
      // answers initialize, and nothing else should ever see them.
      remove(conversation.cleanup);
      conversation.cleanup = [];
      await cli.sendUser(prompt);
    } catch (error) {
      await this.drop(conversation);
      if (error instanceof ProviderError)
        throw new ProviderError(startupFailure(error.message, cli));
      throw error;
    }
    return conversation;
  }

  private argv(key: SpawnKey, withTools: boolean): string[] {
    const cli = this.cliPath ?? ccAuth.findCli();
    if (cli === null) throw new ProviderError(ccAuth.missingMessage());
    const argv = [
      cli,
      "-p",
      "--output-format",
      "stream-json",
      "--input-format",
      "stream-json",
      "--verbose",
      "--include-partial-messages",
      "--model",
      wireModel(key.model),
      // No built-in tools: every tool the model can call is HX's.
      "--tools",
      "",
      // No user or project settings: their hooks, rules and CLAUDE.md belong
      // to Claude Code sessions, not HX's.
      "--setting-sources",
      "",
      "--disable-slash-commands",
      "--strict-mcp-config",
      "--no-session-persistence",
      "--settings",
      dumps(CHILD_SETTINGS),
    ];
    if (withTools)
      argv.push(
        "--mcp-config",
        dumps({ mcpServers: { hx: { type: "sdk", name: "hx" } } }),
        // HX asks for permission before it runs anything; the CLI must not
        // ask again, or refuse what HX already allowed.
        "--allowedTools",
        "mcp__hx",
      );
    if (key.effort) argv.push("--effort", key.effort);
    return argv;
  }

  private spawnKey(request: ProviderRequest): SpawnKey {
    return {
      model: request.model,
      effort: this.effort(request.model),
      tools: sha256(dumps(request.context.tools, { sortKeys: true })),
      append: sha256(appendedPrompt(request)),
      cwd: this.cwd,
    };
  }

  // -- reading a turn --------------------------------------------------------------

  private async *stream(
    conversation: Conversation,
    request: ProviderRequest,
    signal: AbortSignal,
  ): AsyncGenerator<StreamItem> {
    const turn = new TurnReader(conversation.names);
    const started = performance.now();
    const frames = conversation.cli.frames;
    while (true) {
      const frame = await frames.get(signal);
      if (frame instanceof Exit) {
        conversation.state = "dead";
        throw new ProviderError(exitMessage(frame, turn.rateLimit, request.model), {
          planWindows: [...turn.planWindows],
        });
      }
      const kind = frame.type;
      if (kind === "stream_event" && (frame.parent_tool_use_id ?? null) === null) {
        yield* turn.consume(objectOf(frame.event));
        if (turn.parked && turn.calls.length) {
          conversation.state = "parked";
          remember(conversation, turn);
          const payload = fromPayload(turn);
          if (payload !== null) yield payload;
          yield streamEnd("tool_use", {
            usage: turn.finishUsage(started),
            planWindows: [...turn.planWindows],
          });
          return;
        }
      } else if (kind === "user") noteResults(conversation, frame);
      else if (kind === "system" && frame.subtype === "compact_boundary")
        conversation.diverged = true;
      else if (kind === "rate_limit_event") {
        const info = objectOf(frame.rate_limit_info);
        if (info.status === "rejected") turn.rateLimit = info;
        turn.planWindows.push(...planWindows(info));
      } else if (kind === "result") {
        if (frame.is_error) {
          conversation.diverged = true;
          throw new ProviderError(resultError(frame, turn.rateLimit, request), {
            planWindows: [...turn.planWindows],
          });
        }
        conversation.state = "idle";
        remember(conversation, turn);
        const payload = fromPayload(turn);
        if (payload !== null) yield payload;
        const [window, output] = reportedLimits(frame, request.model);
        yield streamEnd(turn.stopReason(), {
          usage: turn.finishUsage(started),
          contextWindow: window,
          maxOutputTokens: output,
          planWindows: [...turn.planWindows],
        });
        return;
      }
    }
  }

  // -- the pool --------------------------------------------------------------------

  /**
   * Stop the processes a rebuild of this transcript replaces: one that holds
   * any of the same messages and is not streaming is this transcript's
   * earlier copy. Retiring one wrongly costs a rebuild later, never a wrong
   * answer.
   */
  private async retire(prints: string[]): Promise<void> {
    const current = new Set(prints);
    const stale = this.pool.filter(
      (c) => c.state !== "streaming" && c.mirror.some((p) => current.has(p)),
    );
    this.pool = this.pool.filter((c) => !stale.includes(c));
    for (const conversation of stale) await conversation.close();
  }

  private async drop(conversation: Conversation): Promise<void> {
    this.pool = this.pool.filter((c) => c !== conversation);
    await conversation.close();
  }

  /**
   * Stop processes that cannot be used again, or are not worth their memory -
   * each is a full Claude Code. A parked one is mid-turn and kept; of the idle
   * ones only the most recently used is.
   */
  private async trim(): Promise<void> {
    const unusable = this.pool.filter(
      (c) => c.state === "dead" || !c.cli.alive || (c.diverged && c.state !== "streaming"),
    );
    const live = this.pool.filter((c) => !unusable.includes(c));
    const idle = live.filter((c) => c.state === "idle").sort((a, b) => a.lastUsed - b.lastUsed);
    const surplus = idle.length > IDLE_KEPT ? idle.slice(0, idle.length - IDLE_KEPT) : [];
    const keep = live.filter((c) => !surplus.includes(c));
    const parked = keep.filter((c) => c.state === "parked").sort((a, b) => a.lastUsed - b.lastUsed);
    surplus.push(...parked.slice(0, Math.max(0, keep.length - POOL_SIZE)));
    const stale = [...unusable, ...surplus];
    this.pool = this.pool.filter((c) => !stale.includes(c));
    for (const conversation of stale) await conversation.close();
  }
}

function remember(conversation: Conversation, turn: TurnReader): void {
  conversation.producedText = turn.text;
  conversation.producedCalls = [...turn.calls];
}

/** A tool result the CLI recorded. One HX did not supply means the CLI
 * answered a call itself, and its copy no longer matches HX's. */
function noteResults(conversation: Conversation, frame: Json): void {
  const content = objectOf(frame.message).content;
  if (!Array.isArray(content)) return;
  for (const block of content) {
    if (!isObject(block) || block.type !== "tool_result") continue;
    const callId = String(block.tool_use_id);
    if (conversation.answered.has(callId)) conversation.answered.delete(callId);
    else conversation.diverged = true;
  }
}

/**
 * One model turn, assembled from raw Anthropic stream events. Usually one API
 * message; the CLI can make more inside a turn - retrying a broken stream, or
 * continuing after an output limit - and all of them are this turn's.
 */
class TurnReader {
  readonly messages: Json[] = [];
  private blocks = new Map<number, Json>();
  private json = new Map<number, string[]>();
  text = "";
  readonly calls: string[] = [];
  parked = false;
  private stop = "";
  private readonly usage: TurnUsage = turnUsage();
  /** Output tokens per API message; `message_delta` reports a running total. */
  private readonly outputs = new Map<number, number>();
  rateLimit: Json | null = null;
  /** Each `rate_limit_event` of the turn, as a usage window. */
  readonly planWindows: PlanWindow[] = [];

  constructor(private readonly names: transcript.ToolNames) {}

  consume(event: Json): StreamItem[] {
    const kind = event.type;
    if (kind === "message_start") {
      const started = objectOf(event.message);
      this.messages.push({ id: started.id ?? null, model: started.model ?? null, content: [] });
      this.blocks = new Map();
      this.json = new Map();
      this.addUsage(objectOf(started.usage), true);
      return [];
    }
    if (kind === "content_block_start") {
      const index = toInt(event.index || 0);
      const block = { ...objectOf(event.content_block) };
      delete block.caller;
      this.blocks.set(index, block);
      if (block.type === "tool_use") this.json.set(index, []);
      return [];
    }
    if (kind === "content_block_delta")
      return this.delta(toInt(event.index || 0), objectOf(event.delta));
    if (kind === "content_block_stop") return this.stopBlock(toInt(event.index || 0));
    if (kind === "message_delta") {
      const change = objectOf(event.delta);
      this.stop = String(change.stop_reason || this.stop);
      this.addUsage(objectOf(event.usage), false);
      return [];
    }
    // Parked only when HX has calls to run: a `tool_use` stop with none of
    // HX's tools in it is the CLI's to resolve.
    if (kind === "message_stop") this.parked = this.stop === "tool_use";
    return [];
  }

  private delta(index: number, change: Json): StreamItem[] {
    const block = this.blocks.get(index);
    if (block === undefined) return [];
    const kind = change.type;
    if (kind === "text_delta") {
      const text = String(change.text || "");
      block.text = String(block.text ?? "") + text;
      this.text += text;
      return text ? [delta({ text })] : [];
    }
    if (kind === "thinking_delta") {
      const text = String(change.thinking || "");
      block.thinking = String(block.thinking ?? "") + text;
      return text ? [delta({ thinking: text })] : [];
    }
    if (kind === "signature_delta") {
      block.signature = String(block.signature ?? "") + String(change.signature || "");
      return [];
    }
    if (kind === "input_json_delta") this.json.get(index)?.push(String(change.partial_json || ""));
    return [];
  }

  private stopBlock(index: number): StreamItem[] {
    const block = this.blocks.get(index);
    const current = this.messages.at(-1);
    if (block === undefined || current === undefined) return [];
    if (block.type === "tool_use") {
      const raw = (this.json.get(index) ?? []).join("");
      this.json.delete(index);
      try {
        block.input = raw ? JSON.parse(raw) : {};
      } catch {
        block.input = {};
      }
    }
    (current.content as Json[]).push(block);
    if (block.type !== "tool_use") return [];
    const name = this.names.fromModel(String(block.name || ""));
    if (name === null) return [];
    const callId = String(block.id);
    this.calls.push(callId);
    return [delta({ toolUseId: callId, toolName: name, toolInputJson: dumps(block.input) })];
  }

  private addUsage(usage: Json, prompt: boolean): void {
    if (prompt) {
      const created = toInt(usage.cache_creation_input_tokens || 0);
      this.usage.inputTokens += toInt(usage.input_tokens || 0) + created;
      this.usage.cacheWriteTokens += created;
      this.usage.cacheReadTokens += toInt(usage.cache_read_input_tokens || 0);
    }
    const output = toInt(usage.output_tokens || 0);
    if (!prompt && output) {
      this.outputs.set(this.messages.length, output);
      this.usage.outputTokens = [...this.outputs.values()].reduce((sum, value) => sum + value, 0);
    }
  }

  stopReason(): StopReason {
    if (this.stop === "max_tokens") return "max_tokens";
    if (this.stop === "stop_sequence") return "stop_sequence";
    return "end_turn";
  }

  finishUsage(started: number): TurnUsage {
    this.usage.latencyMs = performance.now() - started;
    // A subscription: nothing is charged per token.
    this.usage.costUsd = 0;
    return this.usage;
  }
}

/** The turn's raw messages, carried on HX's thinking block for a rebuild. */
function fromPayload(turn: TurnReader): StreamDelta | null {
  if (!turn.messages.length) return null;
  return delta({ thinkingSignature: transcript.encodePayload(turn.messages) });
}

// -- helpers ------------------------------------------------------------------------

/**
 * `[history for the session file, content of the turn to send]`. The last
 * user message is sent as the prompt when it can be; tool results cannot
 * travel that way, so they stay in the history and Claude Code's own resume
 * prompt starts the next request.
 */
function splitPrompt(messages: readonly Message[]): [Message[], Json[]] {
  const last = messages.at(-1) as Message;
  if (last.role !== "user")
    return [[...messages], [transcript.textBlock(transcript.RESUME_PROMPT)]];
  if (!toolResults(last).length) return [messages.slice(0, -1), transcript.promptContent(last)];
  const reminders = last.content.filter(transcript.isReminder);
  const resultsOnly = message(
    "user",
    last.content.filter((b) => b.type === "tool_result"),
  );
  const rest = message(
    "user",
    last.content.filter((b) => b.type !== "tool_result" && !(reminders as unknown[]).includes(b)),
  );
  const prompt = transcript.promptContent(rest);
  const content = prompt.length ? prompt : [transcript.textBlock(transcript.RESUME_PROMPT)];
  content.push(...reminders.map((b) => transcript.textBlock(b.text)));
  return [[...messages.slice(0, -1), resultsOnly], content];
}

/** What HX adds to Claude Code's system prompt. */
const appendedPrompt = (request: ProviderRequest) =>
  appendedSections(request)
    .map(([, text]) => text)
    .join("\n\n");

/** The project context and skills index always; the base prompt only where
 * the user wrote their own or appended to HX's. */
function appendedSections(request: ProviderRequest): [string, string][] {
  const parts: [string, string][] = [];
  const builtin = strip(SYSTEM_PROMPT);
  for (const section of request.context.system) {
    let text = strip(section.text);
    if (section.name === "system" && text.startsWith(builtin))
      text = strip(text.slice(builtin.length));
    if (text) parts.push([section.name, text]);
  }
  return parts;
}

/** HX's part of the system prompt Claude Code will send. */
function sentSections(request: ProviderRequest): [string, string][] {
  if (request.context.tools.length) return appendedSections(request);
  return request.context.system.filter((s) => strip(s.text)).map((s) => [s.name, strip(s.text)]);
}

/** HX's base prompt a paragraph at a time, whitespace flattened, so a copy
 * that was rewrapped is still recognised. */
const HX_PROMPT_PARAGRAPHS = SYSTEM_PROMPT.split("\n\n")
  .filter((paragraph) => strip(paragraph))
  .map((paragraph) => splitWhitespace(paragraph).join(" "));

const SECTION_NAMES: Record<string, string> = {
  system: "the system prompt",
  skills: "the skills index",
  project: "the project context",
};

const SECTION_SOURCES: Record<string, string> = {
  system:
    "`hx prompt` shows where yours comes from; a subagent's is its definition in an agents folder",
  skills: "it comes from a skill's description",
  project: "it comes from an AGENTS.md file, which `hx prompt` lists",
};

/**
 * Refuse a turn whose system prompt carries HX's own. Anthropic decides from a
 * request's content whether a subscription covers it, and another harness's
 * prompt beside Claude Code's can get the turn billed as Extra Usage.
 */
function checkPrompt(request: ProviderRequest): void {
  for (const [name, text] of sentSections(request)) {
    const flat = splitWhitespace(text).join(" ");
    for (const paragraph of HX_PROMPT_PARAGRAPHS) {
      if (!flat.includes(paragraph)) continue;
      const where = SECTION_NAMES[name] ?? `the ${name} section`;
      const source = SECTION_SOURCES[name] ?? "";
      const snippet = splitWhitespace(paragraph).slice(0, 6).join(" ");
      throw new ProviderError(
        `HX's own system prompt is in ${where} ("${snippet}…"), so this turn was not sent. On the Claude Code route the model gets Claude Code's prompt instead, and another harness's prompt beside it can get the turn billed as Extra Usage rather than your plan. Remove that text${source ? ` - ${source}.` : "."}`,
      );
    }
  }
}

function objectSchema(raw: unknown): Json {
  if (isObject(raw) && raw.type === "object") return raw;
  return { type: "object", properties: {} };
}

const wireModel = (modelId: string) =>
  modelId.startsWith(MODEL_PREFIX) ? modelId.slice(MODEL_PREFIX.length) : modelId;

/**
 * `[contextWindow, maxOutputTokens]` Claude Code reports for the model run.
 * Only the entry for the model asked for is taken; started by alias, it
 * cannot be told apart and nothing is.
 */
function reportedLimits(frame: Json, modelId: string): [number | null, number | null] {
  const wanted = wireModel(modelId);
  const usage = frame.modelUsage;
  if (!isObject(usage)) return [null, null];
  for (const [name, entry] of Object.entries(usage)) {
    if (!isObject(entry)) continue;
    const names = new Set(
      [name, entry.canonicalModel].filter((n) => n).map((n) => String(n).replace(DATED, "")),
    );
    if (!names.has(wanted)) continue;
    const window = entry.contextWindow;
    const output = entry.maxOutputTokens;
    return [
      typeof window === "number" && Number.isInteger(window) && window > 0 ? window : null,
      typeof output === "number" && Number.isInteger(output) && output > 0 ? output : null,
    ];
  }
  return [null, null];
}

/** A file only this user can read, in the temporary directory. */
function privateFile(text: string): string {
  const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_";
  for (;;) {
    const name = [...randomBytes(8)].map((byte) => alphabet[byte % alphabet.length]).join("");
    const path = join(tmpdir(), `hx-claude-${name}.md`);
    let fd: number;
    try {
      fd = openSync(path, "wx", 0o600);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") continue;
      throw error;
    }
    try {
      writeSync(fd, text);
    } finally {
      closeSync(fd);
    }
    return path;
  }
}

function remove(paths: readonly string[]): void {
  for (const path of paths) {
    try {
      unlinkSync(path);
    } catch {
      // Already gone.
    }
    // The project folder HX may have created for a session: gone too when
    // nothing else is in it.
    if (path.endsWith(".jsonl")) {
      try {
        rmdirSync(path.slice(0, path.lastIndexOf("/")));
      } catch {
        // Claude Code's own sessions are there too.
      }
    }
  }
}

const noTools = async (_toolUseId: string, name: string, _args: Json): Promise<Json> => ({
  content: [{ type: "text", text: `${name} is not available.` }],
  isError: true,
});

function startupFailure(text: string, cli: CliProcess): string {
  const stderr = cli.stderrTail();
  if (stderr.includes("No conversation found") || text.includes("No conversation found"))
    return "Claude Code could not load the conversation HX rebuilt for it. Start a new session with /clear, and report this with `hx --version`.";
  return withTlsHint(`Claude Code failed to start: ${text}`);
}

const tail = (text: string, limit: number) => [...text].slice(-limit).join("");

function exitMessage(frame: Exit, rateLimit: Json | null, modelId: string): string {
  if (needsCredits(rateLimit, frame.stderr)) return creditsMessage(modelId, strip(frame.stderr));
  if (rateLimit) return limitMessage(rateLimit);
  const detail = strip(frame.stderr);
  if (detail.toLowerCase().includes("not logged in") || detail.includes("/login"))
    return ccAuth.missingMessage();
  const suffix = detail ? `: ${tail(detail, 1200)}` : "";
  return withTlsHint(
    `Claude Code exited unexpectedly (code ${frame.code === null ? "None" : frame.code})${suffix}`,
  );
}

function resultError(frame: Json, rateLimit: Json | null, request: ProviderRequest): string {
  const errors = frame.errors;
  let text = Array.isArray(errors) && errors.length ? errors.map(String).join("; ") : "";
  text = text || String(frame.result || frame.subtype || "unknown error");
  if (needsCredits(rateLimit, text)) return creditsMessage(request.model, text);
  if (billedAsExtraUsage(frame, text, rateLimit)) return extraUsageMessage(request, text);
  const lowered = text.toLowerCase();
  if (rateLimit || lowered.includes("usage limit") || lowered.includes("out of extra usage"))
    return limitMessage(rateLimit ?? {}, text);
  if (lowered.includes("authentication") || text.includes("/login"))
    return `${strip(text)}. ${ccAuth.missingMessage()}`;
  return withTlsHint(`Claude Code: ${strip(text)}`);
}

/** A turn refused because the model, or its long context, needs usage
 * credits - no usage window is reached. */
function needsCredits(rateLimit: Json | null, text: string): boolean {
  if (rateLimit?.errorCode === "credits_required") return true;
  const lowered = text.toLowerCase();
  return lowered.includes("requires usage credits") || lowered.includes("usage credits required");
}

/** Said in HX's words alone: Claude Code's own adds advice for flags HX sets. */
function creditsMessage(modelId: string, detail: string): string {
  const model = wireModel(modelId);
  const base = model.endsWith("[1m]") ? model.slice(0, -4) : model;
  const lowered = detail.toLowerCase();
  // Asked for at 1M, it is the long context that needs them - unless what
  // was said names the model itself.
  const namesModel = lowered.includes("requires usage credits");
  let what: string;
  let instead: string;
  if (
    lowered.includes("1m context") ||
    lowered.includes("long context") ||
    (model !== base && !namesModel)
  ) {
    what = `The 1M context of ${base} needs`;
    instead = `pick ${MODEL_PREFIX}${base} for its standard context`;
  } else {
    what = `${base} needs`;
    instead = "pick another model";
  }
  return `${what} usage credits, which your Claude plan has off - its usage limits are not reached. Turn them on at claude.ai/settings/usage and send again, or ${instead}.`;
}

/** A turn refused as Extra Usage while the plan itself had room: a 400 before
 * the model runs, with no rejected rate limit. */
function billedAsExtraUsage(frame: Json, text: string, rateLimit: Json | null): boolean {
  if (rateLimit) return false;
  const lowered = text.toLowerCase();
  const status = frame.api_error_status;
  return (
    lowered.includes("out of extra usage") && (status === 400 || lowered.includes("api error: 400"))
  );
}

function extraUsageMessage(request: ProviderRequest, detail: string): string {
  const names = sentSections(request)
    .map(([name]) => name)
    .filter((name) => name in SECTION_NAMES)
    .map((name) => SECTION_NAMES[name]);
  const ours = names.length ? ` HX's part of the system prompt: ${names.join(", ")}.` : "";
  return `Claude billed this turn as Extra Usage instead of your plan, and none is left - your plan's own limits are not reached. Anthropic decides this from what the request carries, so sending it again fails the same way.${ours} Text there that describes another coding agent is a known cause; remove it, or add Extra Usage. (${strip(detail)})`;
}

/** Claude Code's `rateLimitType` as a window's length and scope. `overage`
 * is Extra Usage, billed past the plan, not a share of it. */
const LIMIT_WINDOWS: Record<string, [number, string]> = {
  five_hour: [300, ""],
  seven_day: [10_080, ""],
  seven_day_opus: [10_080, "opus"],
  seven_day_sonnet: [10_080, "sonnet"],
};

/**
 * The usage windows a `rate_limit_event` reports. Current Claude Code puts
 * every window's figure in `unifiedWindows`, as a fraction; an older one
 * reports only that window, with a figure only past its warning. A rejected
 * window is full whatever either says.
 */
export function planWindows(info: Json): PlanWindow[] {
  const windows = new Map<string, PlanWindow>();
  const unified = info.unifiedWindows;
  for (const [name, entry] of Object.entries(isObject(unified) ? unified : {})) {
    if (!isObject(entry)) continue;
    const window = windowOf(name, entry);
    if (window !== null) windows.set(window.key, window);
  }
  const own = windowOf(String(info.rateLimitType || ""), info);
  if (own !== null && (!windows.has(own.key) || own.usedPercent === 100)) windows.set(own.key, own);
  return [...windows.values()];
}

function windowOf(name: string, entry: Json): PlanWindow | null {
  const length = LIMIT_WINDOWS[name];
  if (length === undefined) return null;
  let used: number | null = null;
  if (entry.status === "rejected") used = 100;
  else if (typeof entry.utilization === "number") used = entry.utilization * 100;
  const resets = entry.resetsAt;
  return new PlanWindow(
    length[0],
    used,
    typeof resets === "number" && resets > 0 ? resets : null,
    length[1],
  );
}

function limitMessage(info: Json, detail = ""): string {
  const resets = info.resetsAt;
  const when =
    typeof resets === "number" && resets > 0 ? ` It resets at ${formatRenewal(resets)}.` : "";
  const window = String(info.rateLimitType || "").replaceAll("_", " ");
  const base = window
    ? `Your Claude subscription's ${window} usage limit is reached.`
    : "Your Claude subscription's usage limit is reached.";
  const extra = strip(detail) ? ` (${strip(detail)})` : "";
  return base + when + extra;
}

/** Asked of the CLI, not of HX's store: Claude Code keeps its own login. */
BUILDERS[PROVIDER_ID] = (
  _spec,
  _resolver: AuthResolver,
  _sessionId: string | null,
  models: ModelRegistry | null,
) =>
  new ClaudeCodeProvider({
    effort: models !== null ? (modelId: string) => models.reasoningEffort(modelId) : null,
  });

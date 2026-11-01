/**
 * OpenRouter provider.
 *
 * Speaks the OpenAI-compatible chat-completions API over SSE. Two details
 * matter more than the rest:
 *
 * 1. `usage: {"include": true}` on the request body makes OpenRouter return
 *    `usage.cost`, `usage.cache_discount` and
 *    `usage.prompt_tokens_details.cached_tokens`. Without it the status bar
 *    cache and cost fields are guesses.
 * 2. Anthropic and Gemini models need explicit `cache_control` markers (max 4
 *    per request); OpenAI/DeepSeek/Grok cache implicitly off a stable prefix.
 */

import { MissingCredential } from "../auth/resolve.ts";
import { AuthStore, apiKey, mask, OPENROUTER } from "../auth/store.ts";
import { systemText } from "../core/context.ts";
import {
  caption,
  dataUrl,
  type ImageBlock,
  type Message,
  messageImages,
  type StopReason,
  type ToolResultBlock,
} from "../core/messages.ts";
import { type TurnUsage, turnUsage } from "../core/usage.ts";
import { dumps } from "../json.ts";
import { lines, request } from "../net.ts";
import { rstrip, strip, toFloat, toInt } from "../py.ts";
import {
  delta,
  type Provider,
  ProviderError,
  type ProviderRequest,
  type StreamItem,
  streamEnd,
  unreached,
} from "./base.ts";

export const API_BASE = "https://openrouter.ai/api/v1";

/** Points the OpenRouter route at another OpenAI-compatible endpoint: a
 * gateway, a recording proxy, or the local stand-in the end-to-end suite runs. */
export const BASE_URL_VAR = "HX_OPENROUTER_BASE_URL";

/** The endpoint the OpenRouter route talks to. */
export function apiBase(): string {
  return rstrip(strip(process.env[BASE_URL_VAR] ?? ""), "/") || API_BASE;
}

export const DEFAULT_HEADERS = {
  "HTTP-Referer": "https://github.com/aletisunil/hx",
  "X-Title": "HX",
};

/** Anthropic rejects a request carrying more than four. Not a soft limit. */
const MAX_CACHE_CONTROL_MARKERS = 4;

const STOP_REASONS: Record<string, StopReason> = {
  stop: "end_turn",
  end_turn: "end_turn",
  tool_calls: "tool_use",
  length: "max_tokens",
  content_filter: "stop_sequence",
};

const RETRY_STATUSES = new Set([408, 409, 429, 500, 502, 503, 504]);

/** A request body, as httpx wrote it: compact, non-ASCII as it is. */
export const encodeBody = (payload: unknown) =>
  dumps(payload, { separators: [",", ":"], ensureAscii: false });

type WireMessage = Record<string, unknown>;

export class OpenRouterProvider implements Provider {
  readonly name = "openrouter";
  readonly baseUrl: string;

  constructor(
    public apiKey: string,
    options: { baseUrl?: string; timeout?: number; maxRetries?: number } = {},
  ) {
    this.baseUrl = rstrip(options.baseUrl ?? apiBase(), "/");
    this.timeout = options.timeout ?? 600;
    this.maxRetries = options.maxRetries ?? 3;
  }

  private readonly timeout: number;
  private readonly maxRetries: number;

  /** Stream one completion, retrying retryable failures with jittered backoff. */
  async *astream(request: ProviderRequest, signal: AbortSignal): AsyncGenerator<StreamItem> {
    const body = encodeBody(this.buildPayload(request));
    const url = `${this.baseUrl}/chat/completions`;
    for (let attempt = 0; ; attempt++) {
      const started = performance.now();
      let yielded = false;
      try {
        for await (const item of this.streamOnce(url, body, started, signal)) {
          yielded = true;
          yield item;
        }
        return;
      } catch (error) {
        // Only retry before anything was yielded; a mid-stream restart would
        // duplicate text the consumer has already rendered.
        if (
          !(error instanceof ProviderError) ||
          !error.retryable ||
          attempt === this.maxRetries ||
          yielded
        ) {
          throw error;
        }
        await sleep(backoff(attempt), signal);
      }
    }
  }

  private async *streamOnce(
    url: string,
    body: string,
    started: number,
    signal: AbortSignal,
  ): AsyncGenerator<StreamItem> {
    const state = new StreamState();
    let response: Awaited<ReturnType<typeof request>>;
    try {
      response = await request(url, {
        method: "POST",
        body,
        headers: this.headers(),
        timeout: this.timeout,
        signal,
      });
    } catch (error) {
      if (signal.aborted) throw error;
      throw unreached(error);
    }
    try {
      if (response.status >= 400) {
        const text = await response.text();
        throw new ProviderError(errorMessage(response.status, text), {
          status: response.status,
          retryable: RETRY_STATUSES.has(response.status),
        });
      }
      try {
        for await (const line of lines(response)) {
          if (!line.startsWith("data:")) continue;
          const data = strip(line.slice(5));
          if (!data || data === "[DONE]") continue;
          let chunk: Record<string, unknown>;
          try {
            chunk = JSON.parse(data);
          } catch {
            continue;
          }
          yield* state.consume(chunk);
        }
        // An aborted response ends its body rather than failing it, and a
        // stream cut off is not one that finished.
        if (signal.aborted) throw signal.reason;
      } catch (error) {
        if (signal.aborted || error instanceof ProviderError) throw error;
        throw new ProviderError((error as Error).message, { retryable: true });
      }
    } finally {
      response.close();
    }

    // A stream that ends without a `finish_reason` still owes whatever tool
    // calls it had buffered: dropping a complete call would report `end_turn`
    // and leave the user paying for tokens that did nothing.
    yield* state.flush();
    const usage = { ...state.usage, latencyMs: performance.now() - started };
    yield streamEnd(state.stopReason, { usage });
  }

  private headers(): Record<string, string> {
    return {
      Authorization: `Bearer ${this.apiKey}`,
      "Content-Type": "application/json",
      ...DEFAULT_HEADERS,
    };
  }

  /** Swap the credential on a live provider. */
  setApiKey(key: string): void {
    this.apiKey = key;
  }

  async aclose(): Promise<void> {}

  /**
   * Serialise the assembled context into the request body. Applies cache
   * markers according to the model's cache mode and sets `usage.include`. Key
   * ordering is deterministic so the serialised prefix is byte-identical
   * between turns.
   */
  buildPayload(request: ProviderRequest): Record<string, unknown> {
    const context = request.context;
    const explicitCache = context.breakpoints.length > 0;
    const messages: WireMessage[] = [textMessage("system", systemText(context), explicitCache)];
    for (const m of context.messages) messages.push(...encodeMessage(m, explicitCache));
    if (explicitCache) applyCacheControl(messages, context.breakpoints);

    const payload: Record<string, unknown> = {
      model: request.model,
      messages,
      max_tokens: request.maxTokens,
      stream: true,
      usage: { include: true },
    };
    if (context.tools.length) payload.tools = context.tools.map(encodeTool);
    if (request.temperature !== null) payload.temperature = request.temperature;
    if (request.stopSequences.length) payload.stop = [...request.stopSequences];
    Object.assign(payload, request.extra);
    return payload;
  }
}

/** Attach `cache_control: {"type": "ephemeral"}` at the given message indices,
 * in place. Silently caps at four markers - exceeding the limit is a hard API error. */
function applyCacheControl(messages: WireMessage[], breakpoints: readonly number[]): void {
  let applied = 0;
  for (const index of [...new Set(breakpoints)].sort((a, b) => a - b)) {
    if (applied >= MAX_CACHE_CONTROL_MARKERS) return;
    if (!(index >= 0 && index < messages.length)) continue;
    const content = (messages[index] as WireMessage).content;
    if (!Array.isArray(content) || content.length === 0) continue;
    (content.at(-1) as Record<string, unknown>).cache_control = { type: "ephemeral" };
    applied++;
  }
}

/**
 * Accumulates SSE chunks into stream items. Tool-call arguments arrive as JSON
 * fragments spread over many chunks, so they are buffered per index and only
 * emitted once the stream ends.
 */
class StreamState {
  stopReason: StopReason = "end_turn";
  usage: TurnUsage = turnUsage();
  private tools = new Map<number, { id: string; name: string; arguments: string }>();
  private finishedTools = false;

  *consume(chunk: Record<string, unknown>): Generator<StreamItem> {
    if (chunk.usage) this.usage = parseUsage(chunk.usage as Record<string, unknown>);
    const choices = (chunk.choices as Record<string, unknown>[]) || [];
    if (choices.length === 0) return;
    const choice = choices[0] as Record<string, unknown>;
    const d = (choice.delta as Record<string, unknown>) || {};

    if (d.content) yield delta({ text: d.content as string });
    const reasoning = d.reasoning || d.reasoning_content;
    if (reasoning) yield delta({ thinking: reasoning as string });

    for (const call of (d.tool_calls as Record<string, unknown>[]) || []) {
      const index = toInt(call.index ?? 0);
      let slot = this.tools.get(index);
      if (slot === undefined) {
        slot = { id: "", name: "", arguments: "" };
        this.tools.set(index, slot);
      }
      if (call.id) slot.id = call.id as string;
      const fn = (call.function as Record<string, unknown>) || {};
      if (fn.name) slot.name = fn.name as string;
      if (fn.arguments) slot.arguments += fn.arguments as string;
    }

    const finish = choice.finish_reason;
    if (finish) {
      this.stopReason = STOP_REASONS[finish as string] ?? "end_turn";
      yield* this.flush();
    }
  }

  /** Emit the buffered tool calls, once: from the `finish_reason` branch and
   * again when the stream ends, so a stream that never sends one does not
   * swallow its own tool calls. */
  *flush(): Generator<StreamItem> {
    if (this.finishedTools) return;
    this.finishedTools = true;
    const indices = [...this.tools.keys()].sort((a, b) => a - b);
    for (const index of indices) {
      const slot = this.tools.get(index) as { id: string; name: string; arguments: string };
      yield delta({
        toolUseId: slot.id || `call_${index}`,
        toolName: slot.name,
        toolInputJson: slot.arguments || "{}",
      });
    }
    if (indices.length) this.stopReason = "tool_use";
  }
}

/** Map an OpenRouter usage object onto `TurnUsage`. `prompt_tokens` already
 * includes `cached_tokens`, so the cached portion is subtracted out. */
export function parseUsage(raw: Record<string, unknown>): TurnUsage {
  const prompt = toInt(raw.prompt_tokens || 0);
  const details = (raw.prompt_tokens_details as Record<string, unknown>) || {};
  const cached = toInt(details.cached_tokens || 0);
  const completion = (raw.completion_tokens_details as Record<string, unknown>) || {};
  const cost = raw.cost;
  return turnUsage({
    // prompt_tokens counts cached tokens too; bill them once, at the cache rate.
    inputTokens: Math.max(prompt - cached, 0),
    outputTokens: toInt(raw.completion_tokens || 0),
    cacheReadTokens: cached,
    cacheWriteTokens: toInt(details.cache_creation_tokens || 0),
    reasoningTokens: toInt(completion.reasoning_tokens || 0),
    costUsd: cost === null || cost === undefined ? null : toFloat(cost),
  });
}

/** One plain-text wire message, in whichever content shape the route wants. */
function textMessage(role: string, text: string, structured: boolean): WireMessage {
  return structured ? { role, content: [{ type: "text", text }] } : { role, content: text };
}

const blockText = (m: Message) =>
  m.content
    .filter((b) => b.type === "text")
    .map((b) => (b as { text: string }).text)
    .join("");

/**
 * Convert one transcript message into wire messages.
 *
 * Tool results become `role: "tool"` entries, which is why one transcript
 * message can expand into several wire messages. Text in the same message
 * follows them as a user turn rather than being discarded: a late-injected
 * reminder rides the newest user message, and that is often this one. Images a
 * tool returned ride that same trailing user turn.
 */
function encodeMessage(message: Message, structured: boolean): WireMessage[] {
  const results = message.content.filter((b): b is ToolResultBlock => b.type === "tool_result");
  if (results.length) {
    const entries: WireMessage[] = results.map((block) => ({
      role: "tool",
      tool_call_id: block.toolUseId,
      content: block.content,
    }));
    const trailing = blockText(message);
    const pictures: Record<string, unknown>[] = [];
    for (const block of results) {
      if (block.images.length) {
        pictures.push({ type: "text", text: `Images returned by tool call ${block.toolUseId}:` });
        pictures.push(...imageParts(block.images));
      }
    }
    pictures.push(...imageParts(messageImages(message)));
    if (pictures.length) {
      const textPart = trailing ? [{ type: "text", text: trailing }] : [];
      entries.push({ role: "user", content: [...textPart, ...pictures] });
    } else if (trailing) {
      entries.push(textMessage("user", trailing, structured));
    }
    return entries;
  }

  const textParts = message.content
    .filter((b) => b.type === "text" || b.type === "thinking")
    .map((b) => (b as { text: string }).text);
  const text = blockText(message);
  const toolUses = message.content.filter((b) => b.type === "tool_use");

  if (message.role === "assistant") {
    const entry: WireMessage = { role: "assistant", content: text || null };
    if (toolUses.length) {
      entry.tool_calls = toolUses.map((block) => ({
        id: block.id,
        type: "function",
        function: { name: block.name, arguments: dumps(block.input, { sortKeys: true }) },
      }));
    }
    return [entry];
  }

  const body = text || textParts.join("");
  const images = messageImages(message);
  if (images.length) {
    // An image needs the list form whichever shape the route otherwise takes,
    // and a pasted screenshot alone needs no empty text part.
    const textPart = body ? [{ type: "text", text: body }] : [];
    return [{ role: message.role, content: [...textPart, ...imageParts(images)] }];
  }
  if (structured) return [{ role: message.role, content: [{ type: "text", text: body }] }];
  return [{ role: message.role, content: body }];
}

/** Each image as an `image_url` part, named first so text that refers to
 * `[Image #2]` can be matched to the picture it means. */
function imageParts(images: readonly ImageBlock[]): Record<string, unknown>[] {
  const parts: Record<string, unknown>[] = [];
  for (const image of images) {
    const name = caption(image);
    if (name) parts.push({ type: "text", text: `[${name}]` });
    parts.push({ type: "image_url", image_url: { url: dataUrl(image) } });
  }
  return parts;
}

/** Wrap a bare tool schema in the OpenAI function-calling envelope. */
function encodeTool(tool: Record<string, unknown>): Record<string, unknown> {
  if (tool.type === "function") return tool;
  return {
    type: "function",
    function: {
      name: tool.name ?? "",
      description: tool.description ?? "",
      parameters: tool.input_schema || tool.parameters || {},
    },
  };
}

/** Exponential backoff with jitter, so retries from concurrent subagents do
 * not synchronise into a thundering herd. */
function backoff(attempt: number): number {
  return Math.min(2 ** attempt, 8) * (0.5 + Math.random());
}

/** Wait `seconds`, or less if `signal` aborts first. */
export function sleep(seconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", stop);
      resolve();
    }, seconds * 1000);
    const stop = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    signal?.addEventListener("abort", stop, { once: true });
  });
}

function errorMessage(status: number, body: string): string {
  let message: unknown = null;
  try {
    const parsed = JSON.parse(body);
    message = parsed.error?.message || parsed.message;
  } catch {
    message = null;
  }
  return `OpenRouter ${status}: ${message || [...body].slice(0, 400).join("")}`;
}

/** GET `/models`. Used by the model registry. */
export async function fetchModels(key: string, baseUrl?: string): Promise<unknown[]> {
  const response = await request(`${rstrip(baseUrl ?? apiBase(), "/")}/models`, {
    headers: { Authorization: `Bearer ${key}`, ...DEFAULT_HEADERS },
    timeout: 30,
  });
  const text = await response.text();
  if (response.status >= 400) throw new ProviderError(errorMessage(response.status, text));
  const data = JSON.parse(text);
  const models = data.data ?? [];
  return Array.isArray(models) ? models : [];
}

/** A key fragment safe to display. Never the whole thing. */
export const maskApiKey = mask;

/** Persist to `~/.hx/auth.json` with mode 0600. */
export function saveApiKey(key: string): void {
  new AuthStore().save(OPENROUTER, apiKey(key));
}

/** The CLI and TUI catch this name; the credential layer throws the
 * provider-agnostic error underneath. */
export const MissingAPIKey = MissingCredential;

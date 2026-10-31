/**
 * OpenAI Responses wire format, kept apart from the transport.
 *
 * The Responses API differs from chat-completions in three ways that matter:
 *
 * 1. The system prompt is a top-level `instructions` string, not a message.
 * 2. `input` is a flat list of *items*: a tool call and its result are
 *    separate items, not fields on an assistant message.
 * 3. Reasoning is opaque. `reasoning` items carry `encrypted_content` and must
 *    be echoed back verbatim on the next turn, or the server drops the prompt
 *    cache. They are stashed in the thinking block's signature.
 */

import { systemText } from "../core/context.ts";
import {
  caption,
  dataUrl,
  type ImageBlock,
  type Message,
  type StopReason,
} from "../core/messages.ts";
import { type TurnUsage, turnUsage } from "../core/usage.ts";
import { dumps } from "../json.ts";
import {
  delta,
  ProviderError,
  type ProviderRequest,
  type StreamEnd,
  type StreamItem,
  streamEnd,
} from "./base.ts";

type Json = Record<string, unknown>;

export const DEFAULT_INSTRUCTIONS = "You are a helpful assistant.";

/** Asking for encrypted reasoning is what makes multi-turn cache hits possible
 * with `store: false`. */
const INCLUDE = ["reasoning.encrypted_content"];

const INCOMPLETE_REASONS: Record<string, StopReason> = {
  max_output_tokens: "max_tokens",
  max_tokens: "max_tokens",
  content_filter: "stop_sequence",
};

const isObject = (value: unknown): value is Json =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** `value or {}`, for a field that should hold an object. */
const objectOf = (value: unknown): Json => (isObject(value) ? value : {});

/**
 * Serialise the assembled context into a Responses request body. `store` is
 * false: HX keeps its own transcript. `prompt_cache_key` carries the session
 * id instead, which is what the implicit cache keys off.
 */
export function buildBody(
  request: ProviderRequest,
  sessionId: string | null = null,
  reasoningEffort: string | null = null,
): Json {
  const context = request.context;
  const body: Json = {
    model: request.model,
    store: false,
    stream: true,
    instructions: systemText(context) || DEFAULT_INSTRUCTIONS,
    input: encodeInput(context.messages),
    include: [...INCLUDE],
    tool_choice: "auto",
    parallel_tool_calls: true,
  };
  if (context.tools.length) body.tools = context.tools.map(encodeTool);
  if (sessionId) body.prompt_cache_key = sessionId;
  if (request.temperature !== null) body.temperature = request.temperature;
  if (reasoningEffort) body.reasoning = { effort: reasoningEffort, summary: "auto" };
  Object.assign(body, request.extra);
  return body;
}

/** Responses puts the tool fields at the top level; a tool that arrived
 * wrapped in chat-completions' `function` is unwrapped. */
export function encodeTool(given: Json): Json {
  const tool = isObject(given.function) ? { ...given.function, type: "function" } : given;
  return {
    type: "function",
    name: tool.name ?? "",
    description: tool.description ?? "",
    parameters: tool.input_schema || tool.parameters || {},
  };
}

/** Flatten the transcript into Responses input items. */
export const encodeInput = (messages: Iterable<Message>): Json[] =>
  [...messages].flatMap(encodeMessage);

function encodeMessage(message: Message): Json[] {
  const items: Json[] = [];
  const textParts: string[] = [];
  const images: ImageBlock[] = [];
  for (const block of message.content) {
    if (block.type === "text") textParts.push(block.text);
    else if (block.type === "image") images.push(block);
    else if (block.type === "thinking") items.push(...decodeReasoning(block.signature));
    else if (block.type === "tool_use")
      items.push({
        type: "function_call",
        call_id: block.id,
        name: block.name,
        arguments: dumps(block.input, { sortKeys: true }),
      });
    else if (block.type === "tool_result")
      items.push({
        type: "function_call_output",
        call_id: block.toolUseId,
        // A list only when there is an image to carry.
        output: block.images.length
          ? [{ type: "input_text", text: block.content }, ...encodeImages(block.images)]
          : block.content,
      });
  }
  const text = textParts.join("");
  if (text || images.length) {
    const role = message.role === "assistant" ? "assistant" : "user";
    const contentType = role === "assistant" ? "output_text" : "input_text";
    const content: Json[] = text ? [{ type: contentType, text }] : [];
    content.push(...encodeImages(images));
    // Reasoning precedes the text it produced: after the reasoning items,
    // ahead of any tool items.
    const insertAt = items.filter((item) => item.type === "reasoning").length;
    items.splice(insertAt, 0, { type: "message", role, content });
  }
  return items;
}

/** Each image as `input_image` content, named first so text that refers to
 * `[Image #2]` can be matched to the picture it means. */
export function encodeImages(images: readonly ImageBlock[]): Json[] {
  const parts: Json[] = [];
  for (const image of images) {
    const name = caption(image);
    if (name) parts.push({ type: "input_text", text: `[${name}]` });
    parts.push({ type: "input_image", image_url: dataUrl(image) });
  }
  return parts;
}

/** Rebuild the opaque reasoning items stashed on a thinking block. A block
 * with no stored payload yields nothing rather than plain text the API rejects. */
export function decodeReasoning(signature: string | null): Json[] {
  if (!signature) return [];
  let payload: unknown;
  try {
    payload = JSON.parse(signature);
  } catch {
    return [];
  }
  if (!Array.isArray(payload)) return [];
  // `summary` is required on the way back in, so an older session replays.
  return payload
    .filter((item): item is Json => isObject(item) && item.type === "reasoning")
    .map((item) => ({ ...item, summary: truthy(item.summary) ? item.summary : [] }));
}

const truthy = (value: unknown) =>
  !(value === null || value === undefined || value === false || value === 0 || value === "") &&
  !(Array.isArray(value) && value.length === 0) &&
  !(isObject(value) && Object.keys(value).length === 0);

/** Serialise a turn's reasoning items for storage on a thinking block,
 * keeping only the fields the API needs back - `summary` among them. */
export function reasoningSignature(items: readonly Json[]): string {
  const keep = ["type", "id", "encrypted_content", "summary"];
  return dumps(
    items.map((item) =>
      Object.fromEntries(keep.filter((key) => key in item).map((key) => [key, item[key]])),
    ),
    { sortKeys: true },
  );
}

/** Accumulates one streamed response. Tool-call arguments are flushed only
 * when the item is done, so the consumer never sees half a JSON object. */
export class StreamState {
  stopReason: StopReason = "end_turn";
  usage: TurnUsage = turnUsage();
  private readonly calls = new Map<string, { id: string; name: string; arguments: string }>();
  private readonly reasoning: Json[] = [];

  *consume(event: Json): Generator<StreamItem> {
    const kind = event.type;
    if (typeof kind !== "string") return;
    if (kind === "response.output_text.delta") {
      if (truthy(event.delta)) yield delta({ text: String(event.delta) });
    } else if (
      kind === "response.reasoning_summary_text.delta" ||
      kind === "response.reasoning_text.delta"
    ) {
      if (truthy(event.delta)) yield delta({ thinking: String(event.delta) });
    } else if (kind === "response.output_item.added") this.itemAdded(event);
    else if (kind === "response.function_call_arguments.delta") this.accumulateArguments(event);
    else if (kind === "response.output_item.done") yield* this.itemDone(event);
    else if (kind === "response.completed" || kind === "response.done") yield* this.finish(event);
    else if (kind === "response.incomplete") {
      const reason = objectOf(objectOf(event.response).incomplete_details).reason;
      yield* this.finish(event);
      this.stopReason = INCOMPLETE_REASONS[String(reason)] ?? "max_tokens";
    } else if (kind === "response.failed" || kind === "error")
      throw new ProviderError(errorMessage(event));
  }

  /** Open a slot for a tool call; nothing is emitted while arguments stream. */
  private itemAdded(event: Json): void {
    const item = objectOf(event.item);
    if (item.type !== "function_call") return;
    const callId = String(item.call_id || item.id || "");
    if (!callId) return;
    this.calls.set(key(event, item), {
      id: callId,
      name: String(item.name || ""),
      arguments: String(item.arguments || ""),
    });
  }

  private accumulateArguments(event: Json): void {
    const index = event.output_index;
    const slot =
      index !== null && index !== undefined ? String(index) : String(event.item_id || "");
    const call = this.calls.get(slot);
    if (call !== undefined) call.arguments += String(event.delta || "");
  }

  private *itemDone(event: Json): Generator<StreamItem> {
    const item = objectOf(event.item);
    if (item.type === "reasoning") {
      this.reasoning.push(item);
      return;
    }
    if (item.type !== "function_call") return;
    const slot = key(event, item);
    const call = this.calls.get(slot) ?? { id: "", name: "", arguments: "" };
    this.calls.delete(slot);
    const callId = String(item.call_id || call.id || "");
    if (!callId) return;
    this.stopReason = "tool_use";
    yield delta({
      toolUseId: callId,
      toolName: String(item.name || call.name || ""),
      toolInputJson: String(item.arguments || call.arguments || "") || "{}",
    });
  }

  private *finish(event: Json): Generator<StreamItem> {
    const response = objectOf(event.response);
    this.usage = parseUsage(objectOf(response.usage));
    const output = Array.isArray(response.output) ? (response.output as Json[]) : [];
    if (output.some((item) => item.type === "function_call")) this.stopReason = "tool_use";
    // A terminal event may carry reasoning items that never streamed.
    const seen = new Set(this.reasoning.map((item) => dumps(item, { sortKeys: true })));
    for (const item of output)
      if (item.type === "reasoning" && !seen.has(dumps(item, { sortKeys: true })))
        this.reasoning.push(item);
    // One combined payload: the next turn replays every reasoning item.
    if (this.reasoning.length)
      yield delta({ thinkingSignature: reasoningSignature(this.reasoning) });
  }
}

/** Slot key: `output_index`, else the item id. */
function key(event: Json, item: Json): string {
  const index = event.output_index;
  return index !== null && index !== undefined ? String(index) : String(item.id || "");
}

/** Map a Responses usage object. `input_tokens` includes the cached part,
 * which is subtracted to keep the cost maths honest. */
export function parseUsage(raw: Json): TurnUsage {
  const inputTokens = int(raw.input_tokens);
  const cached = int(objectOf(raw.input_tokens_details).cached_tokens);
  return turnUsage({
    inputTokens: Math.max(inputTokens - cached, 0),
    outputTokens: int(raw.output_tokens),
    cacheReadTokens: cached,
    reasoningTokens: int(objectOf(raw.output_tokens_details).reasoning_tokens),
  });
}

function errorMessage(event: Json): string {
  const raw = event.error || objectOf(event.response).error || {};
  const message = isObject(raw) ? raw.message || raw.code : String(raw);
  return `Codex: ${message ? String(message) : "the response failed"}`;
}

/** Python's `int(value)`, or 0. */
function int(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "string" && /^\s*[+-]?\d+\s*$/.test(value))
    return Number.parseInt(value, 10);
  return 0;
}

export function endOf(state: StreamState, latencyMs: number): StreamEnd {
  return streamEnd(state.stopReason, { usage: { ...state.usage, latencyMs } });
}

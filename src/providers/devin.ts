/**
 * Devin provider - a Devin subscription over Codeium's Cascade backend.
 *
 * One turn is up to three calls:
 *
 * 1. `GetUserJwt` trades the stored session token for a short-lived user JWT,
 *    cached until shortly before it expires.
 * 2. `AssignModel`, only for a router model: the router id is not a model the
 *    chat call accepts, so the server picks one and signs the choice.
 * 3. `GetChatMessage`, a Connect server stream.
 *
 * Wire encoding lives in `devin_wire.ts`; this module maps HX's transcript
 * onto Cascade's three history channels and the stream back onto deltas.
 */

import { createHash, randomUUID } from "node:crypto";
import { claimsOf } from "../auth/oauth/devin.ts";
import type { AuthResolver, ResolvedAuth } from "../auth/resolve.ts";
import { systemText } from "../core/context.ts";
import {
  caption,
  type ImageBlock,
  type Message,
  messageImages,
  messageText,
  type StopReason,
} from "../core/messages.ts";
import { type TurnUsage, turnUsage } from "../core/usage.ts";
import { dumps } from "../json.ts";
import { request } from "../net.ts";
import { splitWhitespace } from "../py.ts";
import {
  delta,
  type Provider,
  ProviderError,
  type ProviderRequest,
  type StreamItem,
  streamEnd,
} from "./base.ts";
import { wireModel } from "./devin_catalogue.ts";
import * as wire from "./devin_wire.ts";
import { type ModelInfo, type ModelRegistry, modelInfo } from "./models.ts";
import { sleep } from "./openrouter.ts";
import { WireError } from "./protowire.ts";
import { BUILDERS } from "./registry.ts";
import { encodeTool } from "./responses_codec.ts";

export type TokenSource = () => Promise<ResolvedAuth>;
export type EffortSource = (modelId: string) => string | null;
export type InfoSource = (modelId: string) => ModelInfo;

/** Seconds before its expiry that a cached user JWT is replaced. */
const JWT_LEEWAY = 60;
/** How long to trust a user JWT whose expiry cannot be read. */
const JWT_FALLBACK_TTL = 300;
/** What the Devin CLI sends. Cascade models are tuned against it. */
const DEFAULT_TEMPERATURE = 0.4;
/** Seeds the ids derived for history entries, so they are stable across turns. */
const CASCADE_NAMESPACE = "5b1f0c3e-8a4d-4f0e-9a51-0d3c6e1d7a42";
const ERRORS_TO_RETRY = new Set(["unavailable", "deadline_exceeded", "aborted"]);
const RETRY_STATUSES = new Set([408, 429, 500, 502, 503, 504]);

type Json = Record<string, unknown>;

interface CachedJwt {
  readonly sessionToken: string;
  readonly jwt: string;
  readonly baseUrl: string;
  readonly expires: number;
}

/** `uuid.uuid5(namespace, name)`. */
function uuid5(namespace: string, name: string): string {
  const hash = createHash("sha1")
    .update(Buffer.from(namespace.replaceAll("-", ""), "hex"))
    .update(name, "utf8")
    .digest();
  hash[6] = ((hash[6] as number) & 0x0f) | 0x50;
  hash[8] = ((hash[8] as number) & 0x3f) | 0x80;
  const hex = hash.subarray(0, 16).toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** POST `body`; the status and the whole body back. */
export async function post(
  url: string,
  body: Buffer,
  headers: Record<string, string>,
  timeout: number,
  signal?: AbortSignal,
): Promise<[number, Buffer]> {
  const response = await request(url, {
    method: "POST",
    body,
    headers,
    timeout: 15,
    readTimeout: timeout,
    ...(signal ? { signal } : {}),
  });
  try {
    const parts: Buffer[] = [];
    for await (const chunk of response.chunks()) parts.push(chunk);
    return [response.status, Buffer.concat(parts)];
  } finally {
    response.close();
  }
}

export class DevinProvider implements Provider {
  readonly name = "devin";
  readonly baseUrl: string;
  sessionId: string | null;
  readonly effort: EffortSource;
  private readonly modelInfo: InfoSource | null;
  private readonly timeout: number;
  private readonly maxRetries: number;
  private jwt: CachedJwt | null = null;

  constructor(
    private readonly auth: TokenSource,
    options: {
      baseUrl?: string;
      sessionId?: string | null;
      effort?: EffortSource | null;
      modelInfo?: InfoSource | null;
      timeout?: number;
      maxRetries?: number;
    } = {},
  ) {
    this.baseUrl = (options.baseUrl ?? wire.DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.sessionId = options.sessionId ?? null;
    this.effort = options.effort ?? (() => null);
    this.modelInfo = options.modelInfo ?? null;
    this.timeout = options.timeout ?? 600;
    this.maxRetries = options.maxRetries ?? 3;
  }

  /** The session id names the Cascade thread, so it must follow /clear. */
  setSessionId(sessionId: string): void {
    this.sessionId = sessionId;
  }

  async aclose(): Promise<void> {}

  // -- the turn ------------------------------------------------------------------

  async *astream(given: ProviderRequest, signal: AbortSignal): AsyncGenerator<StreamItem> {
    let reminted = false;
    let attempt = 0;
    for (;;) {
      const started = performance.now();
      let yielded = false;
      try {
        for await (const item of this.turn(given, started, signal)) {
          yielded = true;
          yield item;
        }
        return;
      } catch (error) {
        // Only retry before anything was yielded.
        if (!(error instanceof ProviderError) || yielded) throw error;
        if (error.status === 401 && this.jwt !== null && !reminted) {
          // The cached user JWT may have been revoked before its expiry.
          // Minting another settles it; a second 401 is real.
          this.jwt = null;
          reminted = true;
          continue;
        }
        if (!error.retryable || attempt >= this.maxRetries) throw error;
        await sleep(backoff(attempt), signal);
        attempt++;
      }
    }
  }

  private async *turn(
    given: ProviderRequest,
    started: number,
    signal: AbortSignal,
  ): AsyncGenerator<StreamItem> {
    const auth = await this.auth();
    const jwt = await this.userJwt(auth.token);
    const info = this.info(given.model);
    const cascadeId = cascadeIdFor(this.sessionId);
    const prompts = encodeHistory(given.context.messages, cascadeId, given.model);
    let modelUid = wireModel(info, this.effort(given.model));
    let assignmentJwt = "";
    if (info.modelRouter) {
      const assignment = await this.assign(
        auth.token,
        jwt.baseUrl,
        modelUid,
        cascadeId,
        prompts,
        signal,
      );
      modelUid = assignment.modelUid;
      assignmentJwt = assignment.jwt;
    }
    const chat: wire.ChatRequest = {
      token: auth.token,
      userJwt: jwt.jwt,
      systemPrompt: systemText(given.context),
      prompts,
      modelUid,
      cascadeId,
      executionId: randomUUID(),
      maxTokens: given.maxTokens,
      tools: encodeTools(given.context.tools, isGemini(modelUid)),
      modelAssignmentJwt: assignmentJwt,
      temperature: given.temperature ?? DEFAULT_TEMPERATURE,
      stopPatterns: [...wire.DEFAULT_STOP_PATTERNS, ...given.stopSequences],
      parallelToolCalls: true,
    };
    yield* this.stream(jwt.baseUrl, chat, started, signal);
  }

  private async *stream(
    baseUrl: string,
    chat: wire.ChatRequest,
    started: number,
    signal: AbortSignal,
  ): AsyncGenerator<StreamItem> {
    const state = new StreamState();
    const reader = new wire.FrameReader();
    const body = wire.frame(wire.encodeChat(chat));
    let response: Awaited<ReturnType<typeof request>>;
    try {
      response = await request(`${baseUrl}${wire.CHAT_PATH}`, {
        method: "POST",
        body,
        headers: wire.STREAM_HEADERS,
        timeout: 15,
        readTimeout: this.timeout,
        signal,
      });
    } catch (error) {
      if (signal.aborted) throw error;
      throw new ProviderError((error as Error).message, { retryable: true });
    }
    try {
      if (response.status >= 400) {
        const parts: Buffer[] = [];
        for await (const chunk of response.chunks()) parts.push(chunk);
        throw httpError("chat", response.status, Buffer.concat(parts));
      }
      for await (const chunk of response.chunks()) {
        for (const frame of reader.feed(chunk)) {
          if (frame.endStream) {
            const error = wire.trailerError(frame.payload);
            if (error !== null) throw streamError(error);
            continue;
          }
          yield* state.consume(wire.parseChatChunk(frame.payload));
        }
      }
      if (signal.aborted) throw signal.reason;
    } catch (error) {
      if (signal.aborted || error instanceof ProviderError) throw error;
      if (error instanceof WireError)
        throw new ProviderError(`Devin sent an unreadable response: ${error.message}`);
      throw new ProviderError((error as Error).message, { retryable: true });
    } finally {
      response.close();
    }
    if (reader.leftover)
      throw new ProviderError("Devin's response ended mid-frame.", { retryable: true });
    yield* state.finish();
    yield streamEnd(state.stopReason(), {
      usage: { ...state.usage, latencyMs: performance.now() - started },
    });
  }

  // -- supporting calls ------------------------------------------------------------

  private async userJwt(token: string): Promise<CachedJwt> {
    const cached = this.jwt;
    const now = Date.now() / 1000;
    if (cached !== null && cached.sessionToken === token && now < cached.expires) return cached;
    const answer = await requestUserJwt(this.baseUrl, token, this.timeout);
    const exp = claimsOf(answer.jwt).exp;
    const expires = typeof exp === "number" && exp > 0 ? exp - JWT_LEEWAY : now + JWT_FALLBACK_TTL;
    this.jwt = {
      sessionToken: token,
      jwt: answer.jwt,
      baseUrl: answer.baseUrl || this.baseUrl,
      expires,
    };
    return this.jwt;
  }

  /** Resolve a router into a concrete model for this turn. The router scores
   * the current request alone, sent without an id. */
  private async assign(
    token: string,
    baseUrl: string,
    routerUid: string,
    cascadeId: string,
    prompts: readonly wire.ChatPrompt[],
    signal: AbortSignal,
  ): Promise<wire.Assignment> {
    const latest = [...prompts].reverse().find((p) => p.source === wire.SOURCE_USER);
    const current =
      latest !== undefined
        ? wire.chatPrompt({ messageId: "", source: wire.SOURCE_USER, prompt: latest.prompt })
        : null;
    const payload = await unaryCall(
      baseUrl,
      wire.ASSIGN_MODEL_PATH,
      wire.assignModelRequest(token, routerUid, cascadeId, current),
      "model assignment",
      this.timeout,
      signal,
    );
    let assignment: wire.Assignment;
    try {
      assignment = wire.parseAssignment(payload);
    } catch (error) {
      if (!(error instanceof WireError)) throw error;
      throw new ProviderError(`Devin sent an unreadable model assignment: ${error.message}`);
    }
    // The router id is never a legal chat model: nothing to fall back to.
    if (!assignment.jwt || !assignment.modelUid)
      throw new ProviderError(`Devin could not assign a model for ${routerUid}.`, {
        retryable: true,
      });
    return assignment;
  }

  private info(modelId: string): ModelInfo {
    if (this.modelInfo !== null) return this.modelInfo(modelId);
    return modelInfo({
      id: modelId,
      name: modelId,
      contextWindow: 0,
      maxOutputTokens: 0,
      providerId: "devin",
      isSubscription: true,
    });
  }
}

/** One unary Connect call; failures become `ProviderError`. */
export async function unaryCall(
  baseUrl: string,
  path: string,
  body: Buffer,
  operation: string,
  timeout = 600,
  signal?: AbortSignal,
): Promise<Buffer> {
  let status: number;
  let payload: Buffer;
  try {
    [status, payload] = await post(`${baseUrl}${path}`, body, wire.UNARY_HEADERS, timeout, signal);
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ProviderError((error as Error).message, { retryable: true });
  }
  if (status >= 400) throw httpError(operation, status, payload);
  return payload;
}

/**
 * Trade the stored session token for a user JWT, and learn where to send
 * turns: an enterprise tenant can be served from its own API server.
 */
export async function requestUserJwt(
  baseUrl: string,
  token: string,
  timeout = 600,
): Promise<wire.UserJwt> {
  const payload = await unaryCall(
    baseUrl,
    wire.AUTH_PATH,
    wire.userJwtRequest(token),
    "sign-in",
    timeout,
  );
  let answer: wire.UserJwt;
  try {
    answer = wire.parseUserJwt(payload);
  } catch (error) {
    if (!(error instanceof WireError)) throw error;
    throw new ProviderError(`Devin sent an unreadable sign-in response: ${error.message}`);
  }
  if (!answer.jwt)
    throw new ProviderError(
      "Devin accepted the login but issued no user token. Run `hx auth login devin` to sign in again.",
    );
  return answer;
}

/**
 * Accumulates one streamed turn. Tool calls arrive as argument fragments under
 * an id later fragments may omit; they are flushed whole at the end.
 */
class StreamState {
  readonly usage: TurnUsage = turnUsage();
  private readonly calls = new Map<string, { name: string; arguments: string }>();
  private active: string | null = null;
  private signature = "";
  private stop = 0;

  consume(chunk: wire.ChatChunk): StreamItem[] {
    const items: StreamItem[] = [];
    if (chunk.deltaThinking) items.push(delta({ thinking: chunk.deltaThinking }));
    if (chunk.deltaSignature) this.signature = chunk.deltaSignature;
    if (chunk.deltaText) items.push(delta({ text: chunk.deltaText }));
    for (const call of chunk.toolCalls) this.accumulate(call);
    if (chunk.stopReason) this.stop = chunk.stopReason;
    if (chunk.usage !== null) {
      this.usage.inputTokens = chunk.usage.inputTokens;
      this.usage.outputTokens = chunk.usage.outputTokens;
      this.usage.cacheReadTokens = chunk.usage.cacheReadTokens;
      this.usage.cacheWriteTokens = chunk.usage.cacheWriteTokens;
    }
    return items;
  }

  private accumulate(call: wire.ToolCall): void {
    const callId = call.id || this.active;
    if (!callId) return;
    let slot = this.calls.get(callId);
    if (slot === undefined) {
      slot = { name: "", arguments: "" };
      this.calls.set(callId, slot);
    }
    if (call.name) slot.name = call.name;
    this.active = callId;
    if (call.argumentsJson) {
      const previous = slot.arguments;
      // Some models resend the whole buffer, others only the new part.
      slot.arguments = call.argumentsJson.startsWith(previous)
        ? call.argumentsJson
        : previous + call.argumentsJson;
    }
  }

  finish(): StreamItem[] {
    const items: StreamItem[] = [];
    if (this.signature) items.push(delta({ thinkingSignature: this.signature }));
    for (const [callId, call] of this.calls)
      items.push(
        delta({ toolUseId: callId, toolName: call.name, toolInputJson: call.arguments || "{}" }),
      );
    return items;
  }

  stopReason(): StopReason {
    if (this.calls.size) return "tool_use";
    if (this.stop === wire.STOP_MAX_TOKENS) return "max_tokens";
    return "end_turn";
  }
}

// -- history --------------------------------------------------------------------

/** A UUID naming the Cascade thread, derived from the session id; without a
 * session every turn is its own thread. */
export function cascadeIdFor(sessionId: string | null): string {
  if (!sessionId) return randomUUID();
  return uuid5(CASCADE_NAMESPACE, sessionId);
}

const entryId = (cascadeId: string, ...parts: unknown[]) =>
  uuid5(CASCADE_NAMESPACE, [cascadeId, ...parts.map(String)].join("\u0000"));

/**
 * Map the transcript onto Cascade's USER, SYSTEM (assistant) and TOOL
 * channels. Ids are derived from position, never content. A thinking
 * signature is replayed only on turns this same model produced.
 */
export function encodeHistory(
  messages: readonly Message[],
  cascadeId: string,
  modelId: string,
): wire.ChatPrompt[] {
  const prompts: wire.ChatPrompt[] = [];
  messages.forEach((message, index) => {
    if (message.role === "assistant") {
      const prompt = assistantPrompt(message, cascadeId, index, message.model === modelId);
      if (prompt !== null) prompts.push(prompt);
      return;
    }
    if (message.role !== "user") return;
    // Results answer the assistant turn above them, so they come first.
    for (const block of message.content) {
      if (block.type !== "tool_result") continue;
      prompts.push(
        wire.chatPrompt({
          messageId: entryId(cascadeId, index, "tool", block.toolUseId),
          source: wire.SOURCE_TOOL,
          prompt: block.content,
          toolCallId: block.toolUseId,
          toolResultIsError: block.isError,
          images: images(block.images),
        }),
      );
    }
    const text = messageText(message);
    const attached = images(messageImages(message));
    // An image with no words is a whole message.
    if (text || attached.length)
      prompts.push(
        wire.chatPrompt({
          messageId: entryId(cascadeId, index, "user"),
          source: wire.SOURCE_USER,
          prompt: text,
          images: attached,
        }),
      );
  });
  return prompts;
}

const images = (blocks: readonly ImageBlock[]): wire.ImageData[] =>
  blocks.map((block) => ({
    base64Data: block.data,
    mimeType: block.mediaType,
    caption: caption(block),
  }));

function assistantPrompt(
  message: Message,
  cascadeId: string,
  index: number,
  native: boolean,
): wire.ChatPrompt | null {
  const text: string[] = [];
  const thinking: string[] = [];
  let signature = "";
  const calls: wire.ToolCall[] = [];
  for (const block of message.content) {
    if (block.type === "text") text.push(block.text);
    else if (block.type === "thinking") {
      thinking.push(block.text);
      if (native && !signature && block.signature) signature = block.signature;
    } else if (block.type === "tool_use")
      calls.push({ id: block.id, name: block.name, argumentsJson: dumps(block.input) });
  }
  if (!text.length && !thinking.some(Boolean) && !signature && !calls.length) return null;
  return wire.chatPrompt({
    messageId: `bot-${entryId(cascadeId, index, "assistant")}`,
    source: wire.SOURCE_SYSTEM,
    prompt: text.join(""),
    thinking: thinking.join(""),
    signature,
    toolCalls: calls,
  });
}

// -- tools ----------------------------------------------------------------------

export function encodeTools(tools: readonly Json[], gemini: boolean): wire.ToolDefinition[] {
  return tools.map((tool) => {
    const encoded = encodeTool(tool);
    const parameters = encoded.parameters as Json;
    let schema: unknown =
      parameters && Object.keys(parameters).length
        ? parameters
        : { type: "object", properties: {} };
    if (gemini) schema = geminiSchema(schema);
    return {
      name: String(encoded.name),
      description: String(encoded.description),
      jsonSchema: dumps(schema, { separators: [",", ":"] }),
    };
  });
}

const isGemini = (modelUid: string) => modelUid.toLowerCase().includes("gemini");

const GEMINI_DROPPED = new Set([
  "$schema",
  "$id",
  "additionalProperties",
  "patternProperties",
  "unevaluatedProperties",
]);

/**
 * Reduce a JSON Schema to what Gemini's tool validator accepts. Cascade
 * forwards schemas untouched, and Gemini rejects a type array such as
 * `["string", "null"]` with an opaque error that fails the whole turn.
 */
export function geminiSchema(schema: unknown): unknown {
  if (Array.isArray(schema)) return schema.map(geminiSchema);
  if (typeof schema !== "object" || schema === null) return schema;
  const out: Json = {};
  for (const [key, value] of Object.entries(schema as Json)) {
    if (GEMINI_DROPPED.has(key)) continue;
    if (key === "type" && Array.isArray(value)) {
      const kinds = value.filter((kind) => kind !== "null");
      out.type = kinds.length ? kinds[0] : "string";
      if (kinds.length < value.length) out.nullable = true;
      continue;
    }
    if (key === "const") {
      out.enum = [value];
      continue;
    }
    if (
      (key === "properties" || key === "$defs" || key === "definitions") &&
      typeof value === "object" &&
      value !== null &&
      !Array.isArray(value)
    ) {
      out[key] = Object.fromEntries(
        Object.entries(value).map(([name, sub]) => [name, geminiSchema(sub)]),
      );
      continue;
    }
    out[key] = geminiSchema(value);
  }
  return out;
}

// -- errors ---------------------------------------------------------------------

function httpError(operation: string, status: number, body: Buffer): ProviderError {
  const error = wire.unaryError(body);
  if (status === 401 || status === 403)
    return new ProviderError(
      `Devin rejected the credential (HTTP ${status}). Run \`hx auth login devin\` to sign in again.`,
      { status, retryable: false },
    );
  const detail = error?.message ? error.message : plain(body);
  return new ProviderError(`Devin ${operation} failed (${status})${detail ? `: ${detail}` : ""}`, {
    status,
    retryable: RETRY_STATUSES.has(status),
  });
}

function streamError(error: wire.ConnectError): ProviderError {
  const code = error.code.toLowerCase();
  if (code === "unauthenticated")
    return new ProviderError(
      `Devin rejected the credential: ${error.message}. Run \`hx auth login devin\` to sign in again.`,
      { status: 401 },
    );
  if (code === "resource_exhausted")
    return new ProviderError(`Devin usage limit reached: ${error.message}`, { status: 429 });
  return new ProviderError(`Devin stream error ${error.code}: ${error.message}`, {
    retryable: ERRORS_TO_RETRY.has(code),
  });
}

/** Characters `str.isprintable()` refuses, once whitespace is collapsed. */
const UNPRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]|(?! )\p{Zs}/u;

/** A short, printable excerpt of an error body - never HTML or binary. */
function plain(body: Buffer): string {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    return "";
  }
  text = splitWhitespace(text).join(" ");
  const lowered = text.toLowerCase();
  if (lowered.startsWith("<!doctype") || lowered.startsWith("<html") || UNPRINTABLE.test(text))
    return "";
  return [...text].slice(0, 400).join("");
}

/** Exponential with jitter, matching the other providers. */
const backoff = (attempt: number) => Math.min(2 ** attempt, 8) * (0.5 + Math.random() / 2);

/** Devin serves each reasoning depth as its own backend model, so the
 * catalogue entry turns "opus at high" into an id the wire accepts. */
BUILDERS.devin = (
  spec,
  resolver: AuthResolver,
  sessionId: string | null,
  models: ModelRegistry | null,
) =>
  new DevinProvider(() => resolver.resolve(spec.id), {
    sessionId,
    effort: models !== null ? (modelId: string) => models.reasoningEffort(modelId) : null,
    modelInfo: models !== null ? (modelId: string) => models.getOrDefault(modelId) : null,
  });

/**
 * Devin wire format: Cascade protobuf messages over the Connect protocol.
 *
 * Devin serves chat from Codeium's Cascade backend using Connect over
 * HTTP/1.1. Unary calls (sign-in exchange, model list, router assignment) are
 * a bare protobuf body; the chat call is a server stream of 5-byte-framed
 * messages ending in a JSON trailer that carries any error. Only the fields HX
 * reads or writes are named here, with their numbers from the `exa.*` schemas
 * the Devin CLI ships.
 */

import { gunzipSync, gzipSync } from "node:zlib";
import { strip } from "../py.ts";
import { type Fields, parse, WireError, Writer } from "./protowire.ts";

export const DEFAULT_BASE_URL = "https://server.codeium.com";

export const AUTH_PATH = "/exa.auth_pb.AuthService/GetUserJwt";
export const MODEL_CONFIGS_PATH = "/exa.api_server_pb.ApiServerService/GetCliModelConfigs";
export const ASSIGN_MODEL_PATH = "/exa.api_server_pb.ApiServerService/AssignModel";
export const CHAT_PATH = "/exa.api_server_pb.ApiServerService/GetChatMessage";

const SESSION_TOKEN_PREFIX = "devin-session-token$";

const OS =
  process.platform === "darwin" ? "darwin" : process.platform === "win32" ? "windows" : "linux";

/** The released Devin CLI, as chat requests present it. `chisel` is what
 * unlocks router assignment and the CLI model surface. */
const CLI_IDENTITY = { ideName: "devin-cli", ideType: "chisel", version: "3000.6.2" };

/** The identity the CLI lists models under: the one the server answers with
 * the full per-account catalogue. */
const DISCOVERY_IDENTITY = { ideName: "chisel", ideType: "", version: "0.0.0-dev" };

export const UNARY_HEADERS = {
  "content-type": "application/proto",
  "connect-protocol-version": "1",
  accept: "*/*",
};

export const STREAM_HEADERS = {
  "content-type": "application/connect+proto",
  "connect-protocol-version": "1",
  "connect-content-encoding": "gzip",
  "connect-accept-encoding": "gzip",
  // Frames are gzipped individually; a gzipped body around them would have to
  // be fully buffered before a single frame could be read.
  "accept-encoding": "identity",
};

export const DEFAULT_STOP_PATTERNS = [
  "<|user|>",
  "<|bot|>",
  "<|context_request|>",
  "<|endoftext|>",
  "<|end_of_turn|>",
];

// ChatMessageSource
export const SOURCE_USER = 1;
export const SOURCE_SYSTEM = 2;
export const SOURCE_TOOL = 4;

// ChatMessageRequestType / ConversationalPlannerMode / CacheControlType
const REQUEST_TYPE_CASCADE = 5;
const PLANNER_MODE_DEFAULT = 1;
const CACHE_CONTROL_EPHEMERAL = 1;

// StopReason
export const STOP_MAX_TOKENS = 3;

// DisplayOption. The CLI schema stops at 4; 6-8 are plain int32s on the wire.
export const DISPLAY_MODEL_ROUTER = 3;
export const DISPLAY_QUICK_REVIEW = 4;
export const DISPLAY_INTERNAL_DEFAULT = 6;
const DISPLAY_UNCLASSIFIED = 7;
const DISPLAY_NORMAL = 8;

/** Asking for the internal slots is what makes the server send its whole
 * catalogue; the internal entries are then filtered out, as the CLI does. */
const SUPPORTED_MODEL_DISPLAYS = [
  DISPLAY_MODEL_ROUTER,
  DISPLAY_QUICK_REVIEW,
  DISPLAY_INTERNAL_DEFAULT,
  DISPLAY_UNCLASSIFIED,
  DISPLAY_NORMAL,
];

const CONNECT_COMPRESSED = 0x01;
const CONNECT_END_STREAM = 0x02;

/** Cap on one frame: the length prefix is four bytes the peer controls. */
const MAX_FRAME_BYTES = 16 * 1024 * 1024;

/** The token as the wire carries it: the scheme prefix is required. */
export function sessionToken(token: string): string {
  if (!token || token.startsWith(SESSION_TOKEN_PREFIX)) return token;
  return `${SESSION_TOKEN_PREFIX}${token}`;
}

export function metadata(
  token: string,
  options: { userJwt?: string; discovery?: boolean } = {},
): Writer {
  const identity = options.discovery ? DISCOVERY_IDENTITY : CLI_IDENTITY;
  const md = new Writer();
  md.putStr(1, identity.ideName);
  md.putStr(2, identity.version); // extension_version
  md.putStr(3, sessionToken(token));
  md.putStr(4, "en");
  md.putStr(5, OS);
  md.putStr(7, identity.version); // ide_version
  md.putStr(12, "chisel"); // extension_name
  md.putStr(21, options.userJwt ?? "");
  md.putStr(28, identity.ideType);
  if (options.discovery) md.putPackedUints(30, SUPPORTED_MODEL_DISPLAYS);
  return md;
}

// -- unary calls ---------------------------------------------------------------

export function userJwtRequest(token: string): Buffer {
  const request = new Writer();
  request.putMessage(1, metadata(token));
  return request.finish();
}

export interface UserJwt {
  readonly jwt: string;
  /** An enterprise account's own API server, when it has one. */
  readonly baseUrl: string | null;
}

export function parseUserJwt(payload: Buffer): UserJwt {
  const fields = decodeUnary(payload);
  const custom = strip(fields.text(2)).replace(/\/+$/, "");
  return { jwt: fields.text(1), baseUrl: custom || null };
}

export function modelConfigsRequest(token: string): Buffer {
  const request = new Writer();
  request.putMessage(1, metadata(token, { discovery: true }));
  return request.finish();
}

export function assignModelRequest(
  token: string,
  routerUid: string,
  cascadeId: string,
  prompt: ChatPrompt | null,
): Buffer {
  const request = new Writer();
  request.putMessage(1, metadata(token));
  request.putStr(2, routerUid);
  request.putStr(3, cascadeId);
  if (prompt !== null) request.putMessage(5, encodePrompt(prompt));
  return request.finish();
}

export interface Assignment {
  readonly jwt: string;
  readonly modelUid: string;
}

export function parseAssignment(payload: Buffer): Assignment {
  const assignment = decodeUnary(payload).message(1);
  if (assignment === null) return { jwt: "", modelUid: "" };
  return { jwt: assignment.text(1), modelUid: assignment.text(2) };
}

/** A unary response body, which some edges gzip and some do not. */
export function decodeUnary(payload: Buffer): Fields {
  try {
    return parse(payload);
  } catch (error) {
    if (!(error instanceof WireError)) throw error;
    let inflated: Buffer;
    try {
      inflated = gunzipSync(payload);
    } catch {
      throw new WireError("response is neither protobuf nor gzipped protobuf");
    }
    return parse(inflated);
  }
}

// -- model catalogue -------------------------------------------------------------

export interface FamilyEntry {
  readonly key: string;
  readonly name: string;
  readonly order: number;
}

/** One `ClientModelConfig`, reduced to what the catalogue reads. */
export interface ModelConfig {
  readonly uid: string;
  readonly label: string;
  readonly disabled: boolean;
  readonly maxTokens: number;
  readonly maxOutputTokens: number;
  readonly supportsImages: boolean;
  readonly hasFeatures: boolean;
  readonly supportsTools: boolean;
  readonly supportsThinking: boolean;
  readonly displayOption: number;
  readonly isModelRouter: boolean;
  readonly harnessUids: readonly string[];
  readonly familyLabel: string;
  readonly familyEntries: readonly FamilyEntry[];
  readonly isDefaultInFamily: boolean;
}

export const parseModelConfigs = (payload: Buffer): ModelConfig[] =>
  decodeUnary(payload).messages(1).map(modelConfig);

function modelConfig(fields: Fields): ModelConfig {
  const info = fields.message(23);
  const features = info !== null ? info.message(6) : null;
  const family = fields.message(30);
  const entries: FamilyEntry[] = [];
  if (family !== null) {
    for (const entry of family.messages(2)) {
      const value = entry.message(2);
      if (value === null) continue;
      entries.push({ key: entry.text(1), name: value.text(2), order: value.int32(1) });
    }
  }
  return {
    uid: strip(fields.text(22)),
    label: strip(fields.text(1)),
    disabled: fields.flag(4),
    maxTokens: fields.int32(18),
    supportsImages: fields.flag(5),
    maxOutputTokens: info !== null ? info.int32(13) : 0,
    hasFeatures: features !== null,
    supportsTools: features !== null ? features.flag(12) : false,
    supportsThinking: features !== null ? features.flag(15) : false,
    displayOption: info !== null ? info.int32(22) : 0,
    isModelRouter: info !== null ? info.flag(25) : false,
    harnessUids: info !== null ? info.texts(20) : [],
    familyLabel: family !== null ? strip(family.text(1)) : "",
    familyEntries: entries,
    isDefaultInFamily: fields.flag(31) || (family?.flag(3) ?? false),
  };
}

// -- chat ------------------------------------------------------------------------

export interface ToolCall {
  readonly id: string;
  readonly name: string;
  readonly argumentsJson: string;
}

/** One `ImageData`: an image riding a user or tool prompt. */
export interface ImageData {
  readonly base64Data: string;
  readonly mimeType: string;
  readonly caption: string;
}

/** One `ChatMessagePrompt`: a turn of history on one of three channels. */
export interface ChatPrompt {
  readonly messageId: string;
  readonly source: number;
  readonly prompt: string;
  readonly toolCalls: readonly ToolCall[];
  readonly toolCallId: string;
  readonly toolResultIsError: boolean;
  readonly images: readonly ImageData[];
  readonly thinking: string;
  readonly signature: string;
}

export function chatPrompt(
  fields: Pick<ChatPrompt, "messageId" | "source"> & Partial<ChatPrompt>,
): ChatPrompt {
  return {
    prompt: "",
    toolCalls: [],
    toolCallId: "",
    toolResultIsError: false,
    images: [],
    thinking: "",
    signature: "",
    ...fields,
  };
}

function encodeCall(call: ToolCall): Writer {
  const writer = new Writer();
  writer.putStr(1, call.id);
  writer.putStr(2, call.name);
  writer.putStr(3, call.argumentsJson);
  return writer;
}

function encodeImage(image: ImageData): Writer {
  const writer = new Writer();
  writer.putStr(1, image.base64Data);
  writer.putStr(2, image.mimeType);
  writer.putStr(3, image.caption);
  return writer;
}

export function encodePrompt(prompt: ChatPrompt): Writer {
  const writer = new Writer();
  writer.putStr(1, prompt.messageId);
  writer.putUint(2, prompt.source);
  writer.putStr(3, prompt.prompt);
  for (const call of prompt.toolCalls) writer.putMessage(6, encodeCall(call));
  writer.putStr(7, prompt.toolCallId);
  writer.putBool(9, prompt.toolResultIsError);
  for (const image of prompt.images) writer.putMessage(10, encodeImage(image));
  writer.putStr(11, prompt.thinking);
  writer.putStr(12, prompt.signature);
  return writer;
}

export interface ToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly jsonSchema: string;
}

export interface ChatRequest {
  readonly token: string;
  readonly userJwt: string;
  readonly systemPrompt: string;
  readonly prompts: readonly ChatPrompt[];
  readonly modelUid: string;
  readonly cascadeId: string;
  readonly executionId: string;
  readonly maxTokens: number;
  readonly tools: readonly ToolDefinition[];
  readonly modelAssignmentJwt: string;
  readonly temperature: number;
  readonly stopPatterns: readonly string[];
  readonly parallelToolCalls: boolean;
}

export function encodeChat(chat: ChatRequest): Buffer {
  const config = new Writer();
  config.putUint(1, 1); // num_completions
  config.putUint(2, chat.maxTokens);
  config.putUint(3, 200); // max_newlines
  config.putDouble(5, chat.temperature);
  config.putDouble(6, chat.temperature); // first_temperature
  config.putUint(7, 50); // top_k
  config.putDouble(8, 1.0); // top_p
  config.putStrs(9, chat.stopPatterns);
  config.putDouble(11, 1.0); // fim_eot_prob_threshold

  const choice = new Writer();
  choice.putStr(1, "auto"); // option_name

  const cache = new Writer();
  cache.putUint(1, CACHE_CONTROL_EPHEMERAL);

  const request = new Writer();
  request.putMessage(1, metadata(chat.token, { userJwt: chat.userJwt }));
  request.putStr(2, chat.systemPrompt);
  for (const prompt of chat.prompts) request.putMessage(3, encodePrompt(prompt));
  request.putUint(7, REQUEST_TYPE_CASCADE);
  request.putMessage(8, config);
  for (const tool of chat.tools) {
    const definition = new Writer();
    definition.putStr(1, tool.name);
    definition.putStr(2, tool.description);
    definition.putStr(3, tool.jsonSchema);
    request.putMessage(10, definition);
  }
  request.putBool(11, !chat.parallelToolCalls);
  request.putMessage(12, choice);
  request.putMessage(13, cache);
  request.putStr(16, chat.cascadeId);
  request.putUint(20, PLANNER_MODE_DEFAULT);
  request.putStr(21, chat.modelUid);
  request.putStr(22, chat.executionId);
  request.putStr(26, chat.modelAssignmentJwt);
  return request.finish();
}

export interface Usage {
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly cacheWriteTokens: number;
  readonly cacheReadTokens: number;
}

/** One `GetChatMessageResponse` off the stream. */
export interface ChatChunk {
  readonly deltaText: string;
  readonly deltaThinking: string;
  readonly deltaSignature: string;
  readonly toolCalls: readonly ToolCall[];
  readonly stopReason: number;
  readonly usage: Usage | null;
  readonly actualModelUid: string;
}

export function parseChatChunk(payload: Buffer): ChatChunk {
  const fields = parse(payload);
  const usage = fields.message(7);
  return {
    deltaText: fields.text(3),
    deltaThinking: fields.text(9),
    deltaSignature: fields.text(10),
    toolCalls: fields
      .messages(6)
      .map((call) => ({ id: call.text(1), name: call.text(2), argumentsJson: call.text(3) })),
    stopReason: fields.int32(5),
    usage:
      usage !== null
        ? {
            inputTokens: usage.uint(2),
            outputTokens: usage.uint(3),
            cacheWriteTokens: usage.uint(4),
            cacheReadTokens: usage.uint(5),
          }
        : null,
    actualModelUid: fields.text(23),
  };
}

// -- Connect framing -------------------------------------------------------------

export function frame(payload: Buffer, compress = true): Buffer {
  const body = compress ? gzipSync(payload) : payload;
  const head = Buffer.alloc(5);
  head.writeUInt8(compress ? CONNECT_COMPRESSED : 0, 0);
  head.writeUInt32BE(body.length, 1);
  return Buffer.concat([head, body]);
}

export interface Frame {
  readonly endStream: boolean;
  /** Decompressed. */
  readonly payload: Buffer;
}

/** Splits a byte stream into Connect frames as bytes arrive. */
export class FrameReader {
  private pending: Buffer = Buffer.alloc(0);

  /** Every frame `chunk` completes. Throws `WireError` on a frame over the
   * cap or a payload that claims gzip and is not. */
  *feed(chunk: Buffer): Generator<Frame> {
    this.pending = Buffer.concat([this.pending, chunk]);
    while (this.pending.length >= 5) {
      const flags = this.pending.readUInt8(0);
      const length = this.pending.readUInt32BE(1);
      if (length > MAX_FRAME_BYTES)
        throw new WireError(
          `Connect frame of ${length} bytes exceeds the ${MAX_FRAME_BYTES}-byte cap`,
        );
      if (this.pending.length < 5 + length) return;
      let payload = Buffer.from(this.pending.subarray(5, 5 + length));
      this.pending = this.pending.subarray(5 + length);
      if (flags & CONNECT_COMPRESSED) {
        try {
          payload = gunzipSync(payload);
        } catch {
          throw new WireError("a compressed Connect frame did not decompress");
        }
      }
      yield { endStream: Boolean(flags & CONNECT_END_STREAM), payload };
    }
  }

  /** Bytes of an unfinished frame. Non-zero at end of body means truncation. */
  get leftover(): number {
    return this.pending.length;
  }
}

export interface ConnectError {
  readonly code: string;
  readonly message: string;
}

/** The error in an end-of-stream trailer, or `null` for a clean end. */
export const trailerError = (payload: Buffer) => connectError(payload, true);

/** The error body of a failed unary call: `{"code": ..., "message": ...}`. */
export const unaryError = (payload: Buffer) => connectError(payload, false);

function connectError(payload: Buffer, nested: boolean): ConnectError | null {
  const text = strip(new TextDecoder("utf-8").decode(payload));
  if (!text) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const error = nested ? (parsed as Record<string, unknown>).error : parsed;
  if (typeof error !== "object" || error === null || Array.isArray(error)) return null;
  const raw = error as Record<string, unknown>;
  const code = typeof raw.code === "string" ? raw.code : "";
  const message = typeof raw.message === "string" ? raw.message : "";
  if (!code && !message) return null;
  return { code, message };
}

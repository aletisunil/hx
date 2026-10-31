/**
 * HX's transcript in Claude Code's shapes.
 *
 * Three translations live here: tool names (`mcp__hx__<name>`, within the
 * API's 64-character limit); content, HX blocks to Anthropic content blocks;
 * and session files - the JSONL a `claude --resume` loads, rebuilt from HX's
 * transcript whenever the running CLI no longer holds the conversation HX has.
 *
 * HX's transcript is the one that counts. A Claude Code session is a
 * disposable copy of it: written when a process starts, deleted once read.
 */

import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  mkdirSync,
  openSync,
  readdirSync,
  realpathSync,
  statSync,
  writeSync,
} from "node:fs";
import { REMINDER_OPEN, stripInjections } from "../core/lateinject.ts";
import {
  blockToDict,
  type ContentBlock,
  type ImageBlock,
  type Message,
  messageText,
  type TextBlock,
  type ToolResultBlock,
  toolUses,
} from "../core/messages.ts";
import { dumps } from "../json.ts";
import { home, join } from "../pathlib.ts";
import { pyLen } from "../py.ts";

export const SERVER_PREFIX = "mcp__hx__";
/** The API's limit on a tool name, prefix included. */
const NAME_LIMIT = 64;

const PAYLOAD_VERSION = 1;
/** Marks the opaque payload on a thinking block as this route's: thinking
 * signatures are only valid for the API that issued them. */
const PAYLOAD_KIND = "claude-code";

/** What Claude Code itself says to a model resumed between a tool result and
 * the reply to it. */
export const RESUME_PROMPT = "Continue from where you left off.";

const VERSION_TAG = "hx";

type Json = Record<string, unknown>;

const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");

// -- tool names ------------------------------------------------------------------

/**
 * HX tool name <-> the name it is served under. A name that would push
 * `mcp__hx__<name>` past the API's limit is shortened, with a hash of the
 * full name so two long names sharing a prefix stay apart.
 */
export class ToolNames {
  readonly served = new Map<string, string>();
  readonly hx = new Map<string, string>();

  static forTools(names: readonly string[]): ToolNames {
    const table = new ToolNames();
    for (const name of names) table.add(name);
    return table;
  }

  add(name: string): string {
    const known = this.served.get(name);
    if (known !== undefined) return known;
    const served = servedName(name);
    this.served.set(name, served);
    this.hx.set(served, name);
    return served;
  }

  /** What the model calls an HX tool. */
  modelName(name: string): string {
    return SERVER_PREFIX + this.add(name);
  }

  /** The HX tool behind a name the model used, or `null` if not one of ours. */
  fromModel(name: string): string | null {
    if (!name.startsWith(SERVER_PREFIX)) return null;
    const served = name.slice(SERVER_PREFIX.length);
    return this.hx.get(served) ?? served;
  }
}

export function servedName(name: string): string {
  const clean = name.replace(/[^A-Za-z0-9_-]/gu, "_") || "tool";
  const room = NAME_LIMIT - SERVER_PREFIX.length;
  if (pyLen(clean) <= room && clean === name) return clean;
  return `${[...clean].slice(0, room - 9).join("")}_${sha256(name).slice(0, 8)}`;
}

/** A tool call id the API accepts; a foreign one is rewritten the same way
 * wherever it appears, keeping the pair together. */
export function toolId(raw: string): string {
  if (raw && /^[A-Za-z0-9_-]+$/.test(raw)) return raw;
  return `toolu_hx_${sha256(raw).slice(0, 24)}`;
}

// -- matching --------------------------------------------------------------------

/** Identity of a message as far as the conversation is concerned, without
 * the reminders HX regenerates every turn. */
export function fingerprint(message: Message): string {
  const clean = message.role === "user" ? stripInjections(message) : message;
  const payload = dumps([clean.role, clean.content.map(blockToDict)], {
    sortKeys: true,
    ensureAscii: false,
  });
  return sha256(payload);
}

export const isReminder = (block: ContentBlock): block is TextBlock =>
  block.type === "text" && block.text.includes(REMINDER_OPEN);

// -- content ---------------------------------------------------------------------

export const imageBlock = (image: ImageBlock): Json => ({
  type: "image",
  source: { type: "base64", media_type: image.mediaType, data: image.data },
});

export const textBlock = (text: string): Json => ({ type: "text", text });

/** A tool result's content as Claude Code records an MCP tool's answer. */
export function resultContent(result: ToolResultBlock, extraText: readonly string[] = []): Json[] {
  const content: Json[] = [];
  if (result.content || !result.images.length) content.push(textBlock(result.content));
  content.push(...result.images.map(imageBlock));
  content.push(...extraText.map(textBlock));
  return content;
}

/** The MCP `CallToolResult` that delivers `result` to the CLI. */
export function mcpResult(
  result: ToolResultBlock,
  extraText: readonly string[] | null = null,
): Json {
  const content: Json[] = [];
  if (result.content || !result.images.length) content.push({ type: "text", text: result.content });
  content.push(
    ...result.images.map((image) => ({
      type: "image",
      data: image.data,
      mimeType: image.mediaType,
    })),
  );
  content.push(...(extraText ?? []).map((text) => ({ type: "text", text })));
  return { content, isError: Boolean(result.isError) };
}

/** A user message as Anthropic content, tool results first. */
export function userContent(message: Message): Json[] {
  const results: Json[] = [];
  const rest: Json[] = [];
  for (const block of message.content) {
    if (block.type === "tool_result") {
      const entry: Json = {
        tool_use_id: toolId(block.toolUseId),
        type: "tool_result",
        content: resultContent(block),
      };
      if (block.isError) entry.is_error = true;
      results.push(entry);
    } else if (block.type === "text") {
      if (block.text) rest.push(textBlock(block.text));
    } else if (block.type === "image") rest.push(imageBlock(block));
  }
  return [...results, ...rest];
}

/** The parts of a user message that go on stdin as a new turn: its text and
 * images. Tool results cannot - the CLI drops them from a typed turn. */
export function promptContent(message: Message): Json[] {
  const content: Json[] = [];
  for (const block of message.content) {
    if (block.type === "text" && block.text) content.push(textBlock(block.text));
    else if (block.type === "image") content.push(imageBlock(block));
  }
  return content;
}

/** The raw assistant message(s) behind one HX turn, kept for a rebuild:
 * thinking has to be replayed exactly or the API rejects it. */
export const encodePayload = (messages: readonly Json[]) =>
  dumps({ kind: PAYLOAD_KIND, v: PAYLOAD_VERSION, messages });

export function decodePayload(raw: string | null): Json[] | null {
  if (!raw?.startsWith("{")) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null || (payload as Json).kind !== PAYLOAD_KIND)
    return null;
  const messages = (payload as Json).messages;
  if (
    !Array.isArray(messages) ||
    !messages.every((m) => typeof m === "object" && m !== null && !Array.isArray(m))
  )
    return null;
  return messages as Json[];
}

/**
 * An HX assistant turn as one or more Anthropic assistant messages. The raw
 * messages this route recorded are used while they still describe the turn,
 * so a rebuilt session replays the thinking; otherwise the turn is rebuilt
 * from HX's blocks, without thinking.
 */
export function assistantMessages(message: Message, names: ToolNames): Json[] {
  let raw: Json[] | null = null;
  for (const block of message.content)
    if (block.type === "thinking") raw = decodePayload(block.signature);
  if (raw && payloadMatches(raw, message, names)) return raw;
  const content: Json[] = [];
  for (const block of message.content) {
    if (block.type === "text" && block.text) content.push(textBlock(block.text));
    else if (block.type === "tool_use")
      content.push({
        type: "tool_use",
        id: toolId(block.id),
        name: names.modelName(block.name),
        input: block.input,
      });
  }
  if (!content.length) return [];
  return [{ id: `msg_hx_${hex(24)}`, content }];
}

const hex = (length: number) => randomUUID().replaceAll("-", "").slice(0, length);

function payloadMatches(raw: Json[], message: Message, names: ToolNames): boolean {
  const text: string[] = [];
  const calls: string[] = [];
  for (const entry of raw) {
    for (const block of Array.isArray(entry.content) ? entry.content : []) {
      if (typeof block !== "object" || block === null || Array.isArray(block)) return false;
      if (block.type === "text") text.push(String(block.text || ""));
      else if (block.type === "tool_use")
        calls.push(`${String(block.id)}\u0000${String(block.name)}`);
    }
  }
  const expected = toolUses(message).map((c) => `${toolId(c.id)}\u0000${names.modelName(c.name)}`);
  return (
    text.join("") === messageText(message) &&
    calls.join("\u0001") === expected.join("\u0001") &&
    calls.length === expected.length
  );
}

// -- session files ---------------------------------------------------------------

export function configDir(): string {
  const override = process.env.CLAUDE_CONFIG_DIR;
  return (override ? override : join(home(), ".claude")).normalize("NFC");
}

function realpath(path: string): string {
  try {
    return realpathSync(path);
  } catch {
    return path;
  }
}

/** Where Claude Code looks for the sessions of a working directory. */
export function projectDir(cwd: string): string {
  const resolved = realpath(cwd);
  const sanitized = resolved.normalize("NFC").replace(/[^a-zA-Z0-9]/gu, "-");
  const projects = join(configDir(), "projects");
  if (sanitized.length <= 200) return join(projects, sanitized);
  // Long paths get a hash suffix whose function has changed between CLI
  // builds; a directory that already exists for this prefix is the CLI's own.
  const prefix = sanitized.slice(0, 200);
  try {
    for (const entry of readdirSync(projects))
      if (entry.startsWith(`${prefix}-`) && statSync(join(projects, entry)).isDirectory())
        return join(projects, entry);
  } catch {
    // No projects folder yet.
  }
  return join(projects, `${prefix}-${jsHash(resolved)}`);
}

function jsHash(text: string): string {
  let value = 0;
  for (const char of text)
    value = (((value << 5) - value + (char.codePointAt(0) as number)) | 0) >>> 0;
  let signed = value >= 0x80000000 ? value - 0x100000000 : value;
  signed = Math.abs(signed);
  const digits = "0123456789abcdefghijklmnopqrstuvwxyz";
  let out = "";
  while (signed) {
    out = digits[signed % 36] + out;
    signed = Math.floor(signed / 36);
  }
  return out || "0";
}

/** One API message for a session file. */
export interface Turn {
  readonly role: string;
  readonly content: Json[];
  readonly messageId: string | null;
  readonly model: string | null;
}

const turn = (
  role: string,
  content: Json[],
  messageId: string | null = null,
  model: string | null = null,
): Turn => ({
  role,
  content,
  messageId,
  model,
});

/** HX's transcript as the alternating user/assistant turns of a session.
 * Adjacent turns of one role are merged, as the API would have them. */
export function historyTurns(
  messages: readonly Message[],
  names: ToolNames,
  model: string,
): Turn[] {
  const turns: Turn[] = [];
  for (const message of messages) {
    if (message.role === "assistant") {
      for (const raw of assistantMessages(message, names)) {
        const content = (Array.isArray(raw.content) ? raw.content : []).map((block: Json) => ({
          ...block,
        }));
        if (!content.length) continue;
        const last = turns.at(-1);
        if (last?.role === "assistant") last.content.push(...withoutThinking(content));
        else
          turns.push(
            turn("assistant", content, String(raw.id || ""), (raw.model as string) || model),
          );
      }
    } else if (message.role === "user") {
      const content = userContent(stripInjections(message));
      if (!content.length) continue;
      const last = turns.at(-1);
      if (last?.role === "user") last.content.push(...content);
      else turns.push(turn("user", content));
    }
  }
  // A transcript cut by compaction can open on the assistant; the API wants
  // a user turn first.
  if (turns.length && (turns[0] as Turn).role !== "user")
    turns.unshift(turn("user", [textBlock("(earlier conversation summarised)")]));
  return turns;
}

/** Thinking is only valid at the start of the message that produced it. */
const withoutThinking = (content: Json[]) =>
  content.filter((block) => block.type !== "thinking" && block.type !== "redacted_thinking");

/** Write `turns` as a session Claude Code can resume. Returns its id and path. */
export function writeSession(
  turns: readonly Turn[],
  cwd: string,
  cliVersion = VERSION_TAG,
): [string, string] {
  const sessionId = randomUUID();
  const folder = projectDir(cwd);
  mkdirSync(folder, { recursive: true });
  const path = join(folder, `${sessionId}.jsonl`);
  const now = new Date().toISOString();
  let parent: string | null = null;
  const lines: string[] = [];
  for (const entry of turns) {
    const recordId = randomUUID();
    const record: Json = {
      parentUuid: parent,
      isSidechain: false,
      userType: "external",
      cwd: realpath(cwd),
      sessionId,
      version: cliVersion,
      type: entry.role,
      uuid: recordId,
      timestamp: now,
    };
    record.message =
      entry.role === "user"
        ? { role: "user", content: entry.content }
        : {
            model: entry.model || "",
            id: entry.messageId || `msg_hx_${hex(24)}`,
            type: "message",
            role: "assistant",
            content: entry.content,
            stop_reason: entry.content.some((b) => b.type === "tool_use") ? "tool_use" : "end_turn",
            stop_sequence: null,
            usage: { input_tokens: 0, output_tokens: 0 },
          };
    lines.push(dumps(record, { ensureAscii: false }));
    parent = recordId;
  }
  const fd = openSync(path, "w", 0o600);
  try {
    writeSync(fd, `${lines.join("\n")}\n`);
  } finally {
    closeSync(fd);
  }
  return [sessionId, path];
}

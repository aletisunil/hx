/**
 * Canonical transcript model.
 *
 * Every layer - provider, tools, session persistence, TUI - speaks these types.
 * Provider-specific wire formats are converted at the provider boundary only, so
 * the transcript on disk stays stable across model switches.
 */

import { PyFloat } from "../json.ts";
import { repr, strip } from "../py.ts";

export type Role = "system" | "user" | "assistant" | "tool";

export type StopReason =
  | "end_turn"
  | "tool_use"
  | "max_tokens"
  | "stop_sequence"
  | "cancelled"
  | "error";

export interface TextBlock {
  readonly type: "text";
  text: string;
}

/** Reasoning content. Kept in the transcript but never re-sent as cacheable
 * prefix unless the provider requires it. */
export interface ThinkingBlock {
  readonly type: "thinking";
  text: string;
  signature: string | null;
}

export interface ToolUseBlock {
  readonly type: "tool_use";
  id: string;
  name: string;
  input: Record<string, unknown>;
}

/**
 * An image the model is shown: pasted by the user or returned by a tool.
 *
 * The bytes live in the block, base64-encoded, rather than behind a path. A
 * transcript that points at files breaks the day one of them moves, and a
 * resumed, forked or traced session has to be able to send every image it
 * holds.
 */
export interface ImageBlock {
  readonly type: "image";
  mediaType: string;
  /** Base64 of the encoded image, without a `data:` prefix. */
  data: string;
  width: number;
  height: number;
  /** What the user and the model call it: `Image #2`, `screenshot.png`. */
  label: string;
  /** The file it came from, when that is not already its label. */
  source: string;
}

export interface ToolResultBlock {
  readonly type: "tool_result";
  toolUseId: string;
  content: string;
  isError: boolean;
  /** Set when the full output was capped and written to disk. */
  spilledPath: string | null;
  /** Images the tool returned alongside its text - a Read of a PNG, an MCP screenshot. */
  images: ImageBlock[];
}

export type ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | ImageBlock;

/**
 * One transcript entry.
 *
 * `ephemeral` marks late-injected content that is regenerated every turn and
 * must never be treated as part of a stable cache prefix. `compacted` marks
 * messages superseded by a compaction summary: they stay in the session file
 * for resume and undo but are excluded from context.
 */
export interface Message {
  role: Role;
  content: ContentBlock[];
  /** Seconds since the epoch, as Python's `time.time()`. */
  timestamp: number;
  model: string | null;
  ephemeral: boolean;
  compacted: boolean;
  metadata: Record<string, unknown>;
}

export const textBlock = (text: string): TextBlock => ({ type: "text", text });
export const thinkingBlock = (text: string, signature: string | null = null): ThinkingBlock => ({
  type: "thinking",
  text,
  signature,
});
export const toolUseBlock = (
  id: string,
  name: string,
  input: Record<string, unknown>,
): ToolUseBlock => ({ type: "tool_use", id, name, input });

export function toolResultBlock(
  toolUseId: string,
  content: string,
  options: { isError?: boolean; spilledPath?: string | null; images?: ImageBlock[] } = {},
): ToolResultBlock {
  return {
    type: "tool_result",
    toolUseId,
    content,
    isError: options.isError ?? false,
    spilledPath: options.spilledPath ?? null,
    images: options.images ?? [],
  };
}

export function imageBlock(
  mediaType: string,
  data: string,
  options: { width?: number; height?: number; label?: string; source?: string } = {},
): ImageBlock {
  return {
    type: "image",
    mediaType,
    data,
    width: options.width ?? 0,
    height: options.height ?? 0,
    label: options.label ?? "",
    source: options.source ?? "",
  };
}

export const dataUrl = (image: ImageBlock) => `data:${image.mediaType};base64,${image.data}`;

/** `Image #2: mockup.png` - the name, and where it came from. */
export function caption(image: ImageBlock): string {
  if (image.source && image.source !== image.label) {
    return image.label ? `${image.label}: ${image.source}` : image.source;
  }
  return image.label;
}

/** `time.time()`. */
export const now = () => Date.now() / 1000;

export function message(
  role: Role,
  content: ContentBlock[],
  fields: Partial<Omit<Message, "role" | "content">> = {},
): Message {
  return {
    role,
    content,
    timestamp: fields.timestamp ?? now(),
    model: fields.model ?? null,
    ephemeral: fields.ephemeral ?? false,
    compacted: fields.compacted ?? false,
    metadata: fields.metadata ?? {},
  };
}

/** Concatenated text blocks. Ignores thinking and tool blocks. */
export const messageText = (m: Message) =>
  m.content
    .filter((b): b is TextBlock => b.type === "text")
    .map((b) => b.text)
    .join("");
export const toolUses = (m: Message) =>
  m.content.filter((b): b is ToolUseBlock => b.type === "tool_use");
export const toolResults = (m: Message) =>
  m.content.filter((b): b is ToolResultBlock => b.type === "tool_result");
/** Images attached to this message itself, not those inside tool results. */
export const messageImages = (m: Message) =>
  m.content.filter((b): b is ImageBlock => b.type === "image");

export function blockToDict(block: ContentBlock): Record<string, unknown> {
  switch (block.type) {
    case "text":
      return { type: "text", text: block.text };
    case "thinking":
      return { type: "thinking", text: block.text, signature: block.signature };
    case "tool_use":
      return { type: "tool_use", id: block.id, name: block.name, input: block.input };
    case "image":
      return {
        type: "image",
        media_type: block.mediaType,
        data: block.data,
        width: block.width,
        height: block.height,
        label: block.label,
        source: block.source,
      };
    case "tool_result": {
      const record: Record<string, unknown> = {
        type: "tool_result",
        tool_use_id: block.toolUseId,
        content: block.content,
        is_error: block.isError,
        spilled_path: block.spilledPath,
      };
      // Only when there are some, so a text-only result is written exactly as
      // it was before images existed.
      if (block.images.length) record.images = block.images.map(blockToDict);
      return record;
    }
  }
}

export function blockFromDict(data: Record<string, unknown>): ContentBlock {
  const kind = data.type;
  switch (kind) {
    case "text":
      return textBlock(data.text as string);
    case "thinking":
      return thinkingBlock(data.text as string, (data.signature as string | null) ?? null);
    case "tool_use":
      return toolUseBlock(
        data.id as string,
        data.name as string,
        data.input as Record<string, unknown>,
      );
    case "image":
      return imageBlock(data.media_type as string, data.data as string, {
        width: (data.width as number) ?? 0,
        height: (data.height as number) ?? 0,
        label: (data.label as string) ?? "",
        source: (data.source as string) ?? "",
      });
    case "tool_result":
      return toolResultBlock(data.tool_use_id as string, data.content as string, {
        isError: (data.is_error as boolean) ?? false,
        spilledPath: (data.spilled_path as string | null) ?? null,
        images: ((data.images as Record<string, unknown>[] | null) ?? [])
          .map(blockFromDict)
          .filter((b): b is ImageBlock => b.type === "image"),
      });
    default:
      throw new Error(`unknown content block type: ${repr(kind ?? null)}`);
  }
}

/** Serialise for the session JSONL. */
export function toDict(m: Message): Record<string, unknown> {
  return {
    role: m.role,
    content: m.content.map(blockToDict),
    timestamp: new PyFloat(m.timestamp),
    model: m.model,
    ephemeral: m.ephemeral,
    compacted: m.compacted,
    metadata: m.metadata,
  };
}

/** Inverse of `toDict`. Must round-trip exactly. */
export function fromDict(data: Record<string, unknown>): Message {
  return {
    role: data.role as Role,
    content: ((data.content as Record<string, unknown>[]) ?? []).map(blockFromDict),
    timestamp: (data.timestamp as number) ?? 0,
    model: (data.model as string | null) ?? null,
    ephemeral: (data.ephemeral as boolean) ?? false,
    compacted: (data.compacted as boolean) ?? false,
    metadata: (data.metadata as Record<string, unknown>) ?? {},
  };
}

/** Something the user sent that is not in the transcript yet: queued behind a
 * running turn, or steered into one. */
export interface UserTurn {
  readonly text: string;
  readonly images: readonly ImageBlock[];
}

/** A user turn. Images follow the text, in the order they were attached. */
export function userMessage(text: string, images: readonly ImageBlock[] = []): Message {
  const content: ContentBlock[] = text || images.length === 0 ? [textBlock(text)] : [];
  content.push(...images);
  return message("user", content);
}

export const assistantMessage = (blocks: ContentBlock[], model: string | null = null) =>
  message("assistant", [...blocks], { model });

/** Tool results are carried on a `user`-role message, matching the provider wire format. */
export const toolResultMessage = (results: ToolResultBlock[]) => message("user", [...results]);

export const INTERRUPTED = "Interrupted by the user before this call finished.";
const OUTPUT_SO_FAR = "\n\nOutput before the interrupt:\n";

/**
 * The answer for a call the user cut off, before it started or while it ran.
 *
 * What it printed before then goes with it: how far a command got is what the
 * model needs to decide whether to run it again.
 */
export function interruptedResult(toolUseId: string, output = ""): ToolResultBlock {
  const content = strip(output) ? `${INTERRUPTED}${OUTPUT_SO_FAR}${output}` : INTERRUPTED;
  return toolResultBlock(toolUseId, content, { isError: true });
}

/** What an interrupted call printed, or `null` when `result` is not one. */
export function interruptedOutput(result: ToolResultBlock): string | null {
  if (!(result.isError && result.content.startsWith(INTERRUPTED))) return null;
  const rest = result.content.slice(INTERRUPTED.length);
  return rest.startsWith(OUTPUT_SO_FAR) ? rest.slice(OUTPUT_SO_FAR.length) : rest;
}

/**
 * `messages` with every tool call answered by the message after it.
 *
 * The loop answers every call it starts, interrupted or not, so a request is
 * never built while a call waits. A transcript can still hold a call with no
 * result: written before the loop did that, or by a process killed mid-tool.
 * Every route rejects such a call, so the missing results are filled in as
 * interrupted. Only the request is repaired; the transcript on disk keeps what
 * happened.
 */
export function answerUnansweredCalls(messages: readonly Message[]): Message[] {
  const repaired: Message[] = [];
  let waiting: ToolUseBlock[] = [];
  for (let current of [...messages, null]) {
    if (waiting.length) {
      const results = new Map(
        current ? toolResults(current).map((r) => [r.toolUseId, r] as const) : [],
      );
      if (waiting.some((call) => !results.has(call.id))) {
        const answers = waiting.map((c) => results.get(c.id) ?? interruptedResult(c.id));
        const ids = new Set(waiting.map((call) => call.id));
        if (current !== null && results.size) {
          // Into the results already there: the calls stay answered by one
          // message, in the order they were made.
          const rest = current.content.filter(
            (b) => !(b.type === "tool_result" && ids.has(b.toolUseId)),
          );
          current = { ...current, content: [...answers, ...rest] };
        } else {
          repaired.push(toolResultMessage(answers));
        }
      }
    }
    if (current === null) break;
    repaired.push(current);
    waiting = current.role === "assistant" ? toolUses(current) : [];
  }
  return repaired;
}

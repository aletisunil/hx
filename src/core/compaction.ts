/**
 * Conversation compaction.
 *
 * Fires when the assembled context crosses `context.compact_at` of the model's
 * window, or on an explicit `/compact [instructions]`. Older turns are replaced
 * by one structured summary; the last N turns and the full todo list survive
 * verbatim. The pre-compaction messages stay in the session JSONL (flagged
 * `compacted`) so resume and undo still work.
 *
 * Compaction reseeds the cache prefix by definition - that cost is accepted and
 * surfaced in the status bar rather than hidden.
 */

import type { Provider } from "../providers/base.ts";
import { pyLen, repr, strip } from "../py.ts";
import { type AssembledContext, type ContextBuilder, messageImageTokens } from "./context.ts";
import { describe } from "./images.ts";
import {
  type ImageBlock,
  type Message,
  message,
  messageText,
  textBlock,
  toolUses,
} from "./messages.ts";

export const SUMMARY_SECTIONS = [
  "Goal",
  "Decisions made",
  "Files touched",
  "Current state",
  "Next steps",
  "Open questions",
];
export const SUMMARY_MARKER = "<hx-compacted-summary>";
export const SUMMARY_END = "</hx-compacted-summary>";

function summaryPrompt(sections: string, extra: string, transcript: string): string {
  return `Summarise the conversation below so that work can continue without it.

Write these sections, in order, using the exact headings:

${sections}

Be specific and concrete. Name files by path, record decisions with their
reasons, and state exactly where the work stopped. Preserve anything the next
turn would otherwise have to re-derive: chosen approaches, rejected approaches
and why, error messages still unresolved, and commands that must be re-run.

Do not summarise the summary instructions. Do not add commentary.
${extra}
--- conversation ---

${transcript}
`;
}

export interface CompactionResult {
  readonly summary: Message;
  readonly kept: Message[];
  readonly dropped: Message[];
  readonly tokensBefore: number;
  readonly tokensAfter: number;
}

export class Compactor {
  /** Cap on the summary itself. A summary that can grow without bound just
   * moves the context problem one turn later. */
  static readonly SUMMARY_MAX_TOKENS = 2048;
  static readonly MIN_MESSAGES_TO_COMPACT = 4;

  constructor(
    public provider: Provider | null = null,
    public model = "",
    public keepRecentTurns = 6,
    public context: ContextBuilder | null = null,
  ) {}

  shouldCompact(contextFraction: number, threshold: number): boolean {
    return contextFraction >= threshold && threshold > 0;
  }

  /** Partition into (to summarise, to keep verbatim). The boundary snaps to a
   * turn edge: never split an assistant tool-use message from its results. */
  split(messages: Message[]): [Message[], Message[]] {
    if (messages.length <= this.keepRecentTurns) return [[], [...messages]];
    let boundary = messages.length - this.keepRecentTurns;
    while (boundary > 0 && !isTurnEdge(messages, boundary)) boundary--;
    return [messages.slice(0, boundary), messages.slice(boundary)];
  }

  /** Whether `compact` would summarise anything - checked before a compaction
   * is announced, so one is never started only to do nothing. */
  canCompact(messages: Message[]): boolean {
    return this.split(messages)[0].length >= Compactor.MIN_MESSAGES_TO_COMPACT;
  }

  /** Summarise via a provider call and assemble the replacement transcript. */
  async compact(
    messages: Message[],
    instructions: string | null = null,
    signal?: AbortSignal,
  ): Promise<CompactionResult> {
    const [dropped, kept] = this.split(messages);
    if (dropped.length < Compactor.MIN_MESSAGES_TO_COMPACT) {
      return {
        summary: summaryMessage("(nothing to compact)"),
        kept: [...messages],
        dropped: [],
        tokensBefore: this.estimate(messages),
        tokensAfter: this.estimate(messages),
      };
    }
    const summary = summaryMessage(await this.summarise(dropped, instructions, signal));
    return {
      summary,
      kept,
      dropped,
      tokensBefore: this.estimate(messages),
      tokensAfter: this.estimate([summary, ...kept]),
    };
  }

  private async summarise(
    messages: Message[],
    instructions: string | null,
    signal?: AbortSignal,
  ): Promise<string> {
    // Without a provider there is nothing to call; a mechanical digest is
    // better than silently dropping the history.
    if (this.provider === null) return mechanicalSummary(messages);
    const context: AssembledContext = {
      system: [{ name: "system", text: "You write precise handover summaries.", tokens: 0 }],
      messages: [message("user", [textBlock(this.buildSummaryPrompt(messages, instructions))])],
      tools: [],
      breakpoints: [],
      totalTokens: 0,
      sections: [],
    };
    let text = "";
    const request = {
      context,
      model: this.model,
      maxTokens: Compactor.SUMMARY_MAX_TOKENS,
      temperature: null,
      stopSequences: [],
      extra: {},
    };
    for await (const item of this.provider.astream(
      request,
      signal ?? new AbortController().signal,
    )) {
      if (item.kind === "delta" && item.text) text += item.text;
    }
    return strip(text) || mechanicalSummary(messages);
  }

  /** Prompt requesting the sections in `SUMMARY_SECTIONS`. */
  buildSummaryPrompt(messages: Message[], instructions: string | null): string {
    const extra = instructions ? `\nAlso: ${strip(instructions)}\n` : "";
    return summaryPrompt(
      SUMMARY_SECTIONS.map((s) => `## ${s}`).join("\n"),
      extra,
      renderTranscript(messages),
    );
  }

  private estimate(messages: Message[]): number {
    if (this.context !== null) {
      const context = this.context;
      return messages.reduce((total, m) => total + context.estimateMessageTokens(m), 0);
    }
    return messages.reduce(
      (total, m) => total + Math.floor(pyLen(flatText(m)) / 4) + messageImageTokens(m),
      0,
    );
  }
}

/** True when a split before `index` leaves every tool call with its results. */
function isTurnEdge(messages: Message[], index: number): boolean {
  if (index <= 0 || index >= messages.length) return true;
  if (toolUses(messages[index - 1] as Message).length) return false;
  return !(messages[index] as Message).content.some((block) => block.type === "tool_result");
}

/**
 * Flatten messages to text for the summary prompt. Rendering to text rather
 * than replaying the structured messages keeps the summary call free of
 * tool-call pairing constraints, which a partial history would violate.
 */
export function renderTranscript(messages: Message[]): string {
  const lines: string[] = [];
  for (const m of messages) {
    for (const block of m.content) {
      if (block.type === "text") lines.push(`[${m.role}] ${block.text}`);
      else if (block.type === "tool_use")
        lines.push(`[${m.role}] calls ${block.name}(${repr(block.input)})`);
      else if (block.type === "tool_result") {
        const marker = block.isError ? "error" : "result";
        lines.push(`[tool ${marker}] ${clip(block.content)}`);
        for (const image of block.images) lines.push(`[tool ${marker}] ${imageLine(image)}`);
      } else if (block.type === "image") lines.push(`[${m.role}] ${imageLine(block)}`);
    }
  }
  return lines.join("\n");
}

/** The summariser cannot see an image, but it can record that there was one. */
const imageLine = (image: ImageBlock) => `(attached an image: ${describe(image)})`;

/** Last-resort digest when no model is available to write one. */
function mechanicalSummary(messages: Message[]): string {
  const users = messages.filter((m) => m.role === "user" && messageText(m)).map(messageText);
  const tools = [...new Set(messages.flatMap((m) => toolUses(m).map((b) => b.name)))].sort();
  const first = (text: string, n: number) => [...text].slice(0, n).join("");
  const lines = [
    "## Goal",
    users.length ? first(users[0] as string, 500) : "(unknown)",
    "",
    "## Current state",
  ];
  lines.push(`${messages.length} earlier messages were compacted without a model summary.`);
  if (tools.length) lines.push(`Tools used: ${tools.join(", ")}.`);
  if (users.length > 1)
    lines.push(
      "",
      "## Open questions",
      ...users.slice(1, 6).map((text) => `- ${first(text, 200)}`),
    );
  return lines.join("\n");
}

function summaryMessage(text: string): Message {
  const body = `${SUMMARY_MARKER}\nThis replaces the earlier conversation.\n\n${text}\n${SUMMARY_END}`;
  return message("user", [textBlock(body)], { metadata: { compaction_summary: true } });
}

function flatText(m: Message): string {
  return m.content
    .map(
      (block) => ("text" in block && block.text) || ("content" in block && block.content) || null,
    )
    .filter((text): text is string => typeof text === "string")
    .join("\n");
}

function clip(text: string, limit = 2000): string {
  return pyLen(text) <= limit ? text : `${[...text].slice(0, limit).join("")} … [clipped]`;
}

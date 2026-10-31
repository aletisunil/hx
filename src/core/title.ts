/**
 * Session naming.
 *
 * `/resume` lists sessions by title. Without one it lists ids, which are
 * timestamps - accurate and useless for picking the session that fixed the
 * parser. So after the first exchange the model is asked to name the session
 * in a few words, and the name is written to the session's `meta.json`.
 *
 * The call is deliberately tiny: one short prompt, no tools, a 32-token cap.
 * If it fails for any reason the session still gets a name, taken from the
 * first thing the user said.
 */

import type { Provider, ProviderRequest } from "../providers/base.ts";
import { lstrip, pyLen, rstrip, splitLines, splitWhitespace, strip } from "../py.ts";
import { renderTranscript } from "./compaction.ts";
import { type Message, message, messageText, textBlock } from "./messages.ts";
import { type TurnUsage, turnUsage } from "./usage.ts";

/** A title is a handful of words; anything longer is a summary nobody asked for. */
export const TITLE_MAX_TOKENS = 32;
/** A session row is read at a glance. Longer names stop being names. */
export const MAX_TITLE_CHARS = 50;
/** How much of the conversation the naming call sees. */
const TRANSCRIPT_CHARS = 2_000;
/** Of that, how much is the opening; the rest is the latest stretch. */
const OPENING_CHARS = 500;

export const TITLE_SYSTEM = "You name things briefly and precisely.";

const titlePrompt = (
  transcript: string,
) => `Name this coding session so it can be recognised in a list of sessions.

Rules:
- One line, at most 6 words.
- Name the concrete subject: the file, feature, bug or command in play.
- Name what the session did overall, not only how it opened.
- No quotes, no punctuation at the end, no prefix like "Session:" or "Title:".
- Reply with the name and nothing else.

--- conversation ---

${transcript}
`;

/** The provider call that names a session: no tools, no project context. */
export function buildTitleRequest(messages: Message[], model: string): ProviderRequest {
  const transcript = excerpt(renderTranscript(messages));
  return {
    context: {
      system: [{ name: "system", text: TITLE_SYSTEM, tokens: 0 }],
      messages: [message("user", [textBlock(titlePrompt(transcript))])],
      tools: [],
      breakpoints: [],
      totalTokens: 0,
      sections: [],
    },
    model,
    maxTokens: TITLE_MAX_TOKENS,
    temperature: null,
    stopSequences: [],
    extra: {},
  };
}

/** Ask the model for a title. `[null, usage]` when it produced nothing. */
export async function generateTitle(
  provider: Provider,
  model: string,
  messages: Message[],
  signal: AbortSignal,
): Promise<[string | null, TurnUsage]> {
  let text = "";
  let usage = turnUsage();
  for await (const item of provider.astream(buildTitleRequest(messages, model), signal)) {
    if (item.kind === "end") usage = item.usage;
    else if (item.text) text += item.text;
  }
  return [cleanTitle(text) || null, usage];
}

/** The opening and the latest stretch of a transcript too long to send whole. */
function excerpt(transcript: string): string {
  if (pyLen(transcript) <= TRANSCRIPT_CHARS) return transcript;
  const chars = [...transcript];
  const opening = chars.slice(0, OPENING_CHARS).join("");
  const latest = chars.slice(-(TRANSCRIPT_CHARS - OPENING_CHARS)).join("");
  return `${opening}\n[... earlier conversation omitted ...]\n${latest}`;
}

/** Strip the decoration models add to a name, and cap its length. Whitespace
 * is collapsed first, so a model that answers in two lines still yields one. */
export function cleanTitle(raw: string): string {
  let text = splitWhitespace(raw).join(" ");
  for (const prefix of ["Title:", "Session:", "Name:"]) {
    if (text.toLowerCase().startsWith(prefix.toLowerCase()))
      text = strip(text.slice(prefix.length));
  }
  text = strip(lstrip(text, "-*• "));
  text = strip(rstrip(strip(text, "\"'`"), ".:"));
  if (pyLen(text) > MAX_TITLE_CHARS)
    text = `${rstrip([...text].slice(0, MAX_TITLE_CHARS).join(""))}…`;
  return text;
}

/** A name taken from the first user message, for when the model call cannot run. */
export function fallbackTitle(messages: Message[]): string {
  for (const m of messages) {
    if (m.role !== "user") continue;
    const firstLine = splitLines(messageText(m)).find((line) => strip(line)) ?? "";
    const cleaned = cleanTitle(firstLine);
    if (cleaned) return cleaned;
  }
  return "Untitled session";
}

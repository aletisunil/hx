/**
 * Late injection of per-turn context.
 *
 * Anything that changes every turn - todo state, files modified since last
 * read, plan-mode notices - must NOT live in the system prompt. Mutating the
 * prefix invalidates the provider KV cache on every single turn, which is the
 * single most expensive mistake this harness can make.
 *
 * Instead, injectors emit `<hx-reminder>` blocks that are appended to the tail
 * of the newest user message and flagged `ephemeral`. Before each turn the
 * previous ephemeral blocks are stripped and regenerated. Nothing above the
 * rolling cache breakpoint is ever rewritten.
 */

import { compareCodePoints } from "../json.ts";
import { rstrip, strip } from "../py.ts";
import { type ContentBlock, type Message, message, textBlock } from "./messages.ts";

export const REMINDER_OPEN = "<hx-reminder>";
export const REMINDER_CLOSE = "</hx-reminder>";

/** One block of late-injected context. */
export interface Injection {
  /** Injector name, for debugging via `/context`. */
  readonly source: string;
  readonly text: string;
  /** Higher priority renders closer to the end of the message. */
  readonly priority: number;
}

/** Returns the current injection, or `null` to contribute nothing this turn. */
export type Injector = () => Injection | null;

/** Holds the active injectors and applies them to a message list. */
export class InjectionRegistry {
  private injectors = new Map<string, Injector>();

  register(name: string, injector: Injector): void {
    this.injectors.set(name, injector);
  }

  unregister(name: string): void {
    this.injectors.delete(name);
  }

  /** Run every injector, drop `null` results, sort by priority then name so
   * output is deterministic (a reordered prefix is a cache miss). */
  collect(): Injection[] {
    const collected: Injection[] = [];
    for (const name of [...this.injectors.keys()].sort(compareCodePoints)) {
      let injection: Injection | null;
      try {
        injection = (this.injectors.get(name) as Injector)();
      } catch {
        // A broken injector must not kill the turn.
        continue;
      }
      if (injection !== null && strip(injection.text)) collected.push(injection);
    }
    return collected.sort(
      (a, b) => a.priority - b.priority || compareCodePoints(a.source, b.source),
    );
  }

  /**
   * A copy of `messages` with stale ephemeral content stripped and fresh
   * injections appended to the final user message. The input list is never
   * mutated: the caller keeps the clean transcript and only the assembled
   * request carries injections.
   */
  async apply(messages: Message[]): Promise<Message[]> {
    const cleaned = messages.map((m) => (m.role === "user" ? stripInjections(m) : m));
    const injections = this.collect();
    if (injections.length === 0) return cleaned;
    const block = render(injections);
    const tail = cleaned.at(-1);
    if (tail?.role === "user") {
      cleaned[cleaned.length - 1] = {
        ...tail,
        content: [...tail.content, textBlock(`\n\n${block}`)],
        ephemeral: true,
      };
    } else {
      // No trailing user turn to ride on - carry the reminders on their own
      // ephemeral message rather than touching anything already cached.
      cleaned.push(message("user", [textBlock(block)], { ephemeral: true }));
    }
    return cleaned;
  }
}

const REMINDER = new RegExp(
  `${escapeRegex(REMINDER_OPEN)}[\\s\\S]*?${escapeRegex(REMINDER_CLOSE)}`,
  "g",
);

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
}

/** Remove `<hx-reminder>` blocks from a message's text content. */
export function stripInjections(m: Message): Message {
  const text = m.content.map((b) => (b.type === "text" ? b.text : "")).join("");
  if (!text.includes(REMINDER_OPEN)) return m;
  const content: ContentBlock[] = [];
  for (const block of m.content) {
    if (block.type === "text") {
      const stripped = rstrip(block.text.replace(REMINDER, ""));
      if (stripped) content.push(textBlock(stripped));
    } else content.push(block);
  }
  return { ...m, content, ephemeral: false };
}

/** Render injections into a single reminder-wrapped string. */
export function render(injections: Injection[]): string {
  const body = injections.map((i) => strip(i.text)).join("\n\n");
  return `${REMINDER_OPEN}\n${body}\n${REMINDER_CLOSE}`;
}

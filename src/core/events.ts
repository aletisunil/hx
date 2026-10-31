/**
 * Async event bus.
 *
 * The agent core is a producer; the TUI and `hx -p` print mode are consumers.
 * Keeping this one-way means the core never imports the TUI and can be driven
 * headlessly.
 */

import type { PlanWindow } from "./usage.ts";

export type Event =
  | { kind: "TurnStarted"; turnIndex: number; model: string }
  /** Streaming assistant text. */
  | { kind: "TextDelta"; text: string }
  | { kind: "ThinkingDelta"; text: string }
  | { kind: "ToolCallStarted"; toolUseId: string; name: string; input: Record<string, unknown> }
  /** Incremental output (e.g. Bash stdout) shown live in the TUI, uncapped. */
  | { kind: "ToolCallProgress"; toolUseId: string; chunk: string }
  | {
      kind: "ToolCallFinished";
      toolUseId: string;
      isError: boolean;
      durationMs: number;
      summary: string;
      /** Why it failed, in the tool's own words. Empty on success. */
      detail: string;
      /** Structured detail the renderer draws with - an edit's diff, a
       * command's exit code. Presentation only: the model never sees it. */
      metadata: Record<string, unknown>;
      /** The user stopped the turn while this call ran. */
      interrupted: boolean;
    }
  /** Core is blocked until a consumer answers. */
  | {
      kind: "PermissionRequested";
      requestId: string;
      toolName: string;
      description: string;
      detail: string;
    }
  | {
      kind: "UsageUpdated";
      inputTokens: number;
      outputTokens: number;
      cacheReadTokens: number;
      cacheWriteTokens: number;
      contextTokens: number;
      contextWindow: number;
      costUsd: number;
    }
  /** A subscription route reported how much of its usage windows is spent.
   * Tagged with the route because the figures belong to an account. */
  | { kind: "PlanUsageUpdated"; route: string; windows: readonly PlanWindow[] }
  | { kind: "TodosUpdated"; todos: Record<string, unknown>[] }
  | { kind: "SubagentStarted"; subagentId: string; agentType: string; description: string }
  | { kind: "SubagentFinished"; subagentId: string; isError: boolean }
  | { kind: "CompactionStarted"; reason: string }
  | { kind: "CompactionFinished"; tokensBefore: number; tokensAfter: number }
  | { kind: "TurnFinished"; turnIndex: number; stopReason: string }
  | { kind: "ErrorRaised"; message: string; recoverable: boolean };

export type EventOf<K extends Event["kind"]> = Extract<Event, { kind: K }>;

/** Event kinds safe to drop when a subscriber falls behind. Losing a text
 * delta degrades the render; losing a ToolCallFinished corrupts it. */
const COALESCIBLE = new Set<Event["kind"]>([
  "TextDelta",
  "ThinkingDelta",
  "ToolCallProgress",
  "UsageUpdated",
]);

/** One subscriber's bounded queue. */
class Subscription {
  readonly items: (Event | null)[] = [];
  waiter: (() => void) | null = null;

  push(item: Event | null): void {
    this.items.push(item);
    this.waiter?.();
    this.waiter = null;
  }
}

/**
 * Fan-out pub/sub.
 *
 * Slow consumers must not stall the agent loop: each subscriber gets its own
 * bounded queue and drops coalescible deltas rather than applying backpressure.
 */
export class EventBus {
  private readonly subscriptions: Subscription[] = [];
  private closed = false;

  constructor(private readonly maxQueue = 1024) {}

  /** Non-blocking. Safe to call from inside the agent loop. */
  publish(event: Event): void {
    if (this.closed) return;
    for (const subscription of this.subscriptions) {
      if (subscription.items.length >= this.maxQueue) {
        if (COALESCIBLE.has(event.kind)) continue;
        // Structural events must survive: evict the oldest item instead.
        subscription.items.shift();
      }
      subscription.push(event);
    }
  }

  /** Yield events until `close` is called. */
  async *subscribe(): AsyncGenerator<Event> {
    const subscription = new Subscription();
    this.subscriptions.push(subscription);
    try {
      while (true) {
        if (subscription.items.length === 0) {
          await new Promise<void>((resolve) => {
            subscription.waiter = resolve;
          });
          continue;
        }
        const event = subscription.items.shift() as Event | null;
        if (event === null) return;
        yield event;
      }
    } finally {
      const index = this.subscriptions.indexOf(subscription);
      if (index >= 0) this.subscriptions.splice(index, 1);
    }
  }

  close(): void {
    this.closed = true;
    for (const subscription of this.subscriptions) subscription.push(null);
  }
}

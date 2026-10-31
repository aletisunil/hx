/**
 * Provider protocol.
 *
 * A provider converts an `AssembledContext` into wire format, streams the
 * response, and converts it back into core message types. All provider-specific
 * quirks - cache markers, reasoning fields, tool-call encodings - stay behind
 * this boundary.
 */

import type { AssembledContext } from "../core/context.ts";
import type { StopReason } from "../core/messages.ts";
import { type PlanWindow, type TurnUsage, turnUsage } from "../core/usage.ts";

/** A chunk off the wire. */
export interface StreamDelta {
  readonly kind: "delta";
  readonly text?: string | null;
  readonly thinking?: string | null;
  /** Opaque provider payload for the turn's reasoning, emitted once at the end
   * of the stream. Stored on the thinking block and never shown to the user. */
  readonly thinkingSignature?: string | null;
  readonly toolUseId?: string | null;
  readonly toolName?: string | null;
  /** Partial JSON for a tool call; accumulated by the consumer. */
  readonly toolInputJson?: string | null;
}

export interface StreamEnd {
  readonly kind: "end";
  readonly stopReason: StopReason;
  readonly usage: TurnUsage;
  /** The model's window as the backend reported it running this turn. */
  readonly contextWindow: number | null;
  readonly maxOutputTokens: number | null;
  /** The subscription's usage windows as the backend reported them with this turn. */
  readonly planWindows: readonly PlanWindow[];
}

export type StreamItem = StreamDelta | StreamEnd;

export const delta = (fields: Omit<StreamDelta, "kind">): StreamDelta => ({
  kind: "delta",
  ...fields,
});

export function streamEnd(
  stopReason: StopReason,
  fields: Partial<Omit<StreamEnd, "kind" | "stopReason">> = {},
): StreamEnd {
  return {
    kind: "end",
    stopReason,
    usage: fields.usage ?? turnUsage(),
    contextWindow: fields.contextWindow ?? null,
    maxOutputTokens: fields.maxOutputTokens ?? null,
    planWindows: fields.planWindows ?? [],
  };
}

export interface ProviderRequest {
  readonly context: AssembledContext;
  readonly model: string;
  readonly maxTokens: number;
  readonly temperature: number | null;
  readonly stopSequences: readonly string[];
  readonly extra: Readonly<Record<string, unknown>>;
}

export interface Provider {
  readonly name: string;
  /** Stream a completion. Throws `ProviderError` on failure; stops when `signal` aborts. */
  astream(request: ProviderRequest, signal: AbortSignal): AsyncGenerator<StreamItem>;
  aclose(): Promise<void>;
}

/**
 * Transport or API failure. `retryable` says whether the loop should retry with
 * backoff (429, 5xx, connection resets) rather than surfacing to the user;
 * `planWindows` carries the subscription's usage windows when the refusal
 * reported them - a limit reached is the moment they matter most.
 */
export class ProviderError extends Error {
  override name = "ProviderError";
  readonly status: number | null;
  readonly retryable: boolean;
  readonly planWindows: readonly PlanWindow[];

  constructor(
    message: string,
    options: {
      status?: number | null;
      retryable?: boolean;
      planWindows?: readonly PlanWindow[];
    } = {},
  ) {
    super(message);
    this.status = options.status ?? null;
    this.retryable = options.retryable ?? false;
    this.planWindows = options.planWindows ?? [];
  }
}

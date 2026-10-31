/**
 * Token and cost ledger.
 *
 * Feeds the status bar fields: context used, tokens used, cache-read/cache-write
 * tokens, and session cost. OpenRouter returns an authoritative `usage.cost`
 * when `usage: {include: true}` is set on the request; that is preferred over
 * local arithmetic, which is only a fallback.
 */

import type { ModelPricing } from "../providers/models.ts";
import { formatFixed, pyRound, strftime } from "../py.ts";

/** Usage for a single provider call. */
export interface TurnUsage {
  /** Prompt tokens billed at the full rate. Excludes `cacheReadTokens`. */
  inputTokens: number;
  outputTokens: number;
  /** Prompt tokens served from the provider's KV cache (billed at a discount). */
  cacheReadTokens: number;
  /** Prompt tokens written into the cache (billed at a premium on Anthropic). */
  cacheWriteTokens: number;
  reasoningTokens: number;
  /** Provider-reported cost. `null` means fall back to local pricing. */
  costUsd: number | null;
  latencyMs: number;
}

export function turnUsage(fields: Partial<TurnUsage> = {}): TurnUsage {
  return {
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    reasoningTokens: 0,
    costUsd: null,
    latencyMs: 0,
    ...fields,
  };
}

/** Every prompt token, cached or not. */
export const promptTokens = (usage: TurnUsage) => usage.inputTokens + usage.cacheReadTokens;

/** Cached share of prompt tokens, 0.0-1.0. */
export function cacheHitRate(usage: TurnUsage): number {
  const total = promptTokens(usage);
  return total ? usage.cacheReadTokens / total : 0;
}

const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);

/** Running totals for a session. */
export class UsageLedger {
  turns: TurnUsage[] = [];
  /** Tokens currently occupied by the assembled context. */
  contextTokens = 0;
  contextWindow = 0;

  record(usage: TurnUsage): void {
    this.turns.push(usage);
  }

  get totalInput(): number {
    return sum(this.turns.map((t) => t.inputTokens));
  }

  get totalOutput(): number {
    return sum(this.turns.map((t) => t.outputTokens));
  }

  get totalCacheRead(): number {
    return sum(this.turns.map((t) => t.cacheReadTokens));
  }

  get totalCacheWrite(): number {
    return sum(this.turns.map((t) => t.cacheWriteTokens));
  }

  get totalCostUsd(): number {
    return sum(this.turns.map((t) => t.costUsd ?? 0));
  }

  get cacheHitRate(): number {
    const prompt = this.totalInput + this.totalCacheRead;
    return prompt ? this.totalCacheRead / prompt : 0;
  }

  get lastLatencyMs(): number {
    return this.turns.at(-1)?.latencyMs ?? 0;
  }

  /** `contextTokens / contextWindow`, clamped to 0.0-1.0. Drives the
   * compaction trigger and the status bar gauge. */
  get contextFraction(): number {
    if (this.contextWindow <= 0) return 0;
    return Math.min(1, Math.max(0, this.contextTokens / this.contextWindow));
  }
}

/**
 * One of a subscription's usage windows, as its route last reported it.
 *
 * A subscription has no per-token price; what it has instead is a share of a
 * window - five hours, seven days - and a time when that share comes back.
 */
export class PlanWindow {
  /** How much of it is spent, 0-100. `null` when the route did not say,
   * which is not the same as nothing spent. */
  readonly usedPercent: number | null;

  constructor(
    /** How long the window is: 300 for five hours, 10080 for seven days. */
    readonly minutes: number,
    usedPercent: number | null = null,
    /** When it is empty again, in epoch seconds. */
    readonly resetsAt: number | null = null,
    /** What the window is limited to, when not every model. */
    readonly scope = "",
  ) {
    // Held to 0-100 here rather than by each route: the colours and
    // formatPercent assume it, whoever reported the figure.
    this.usedPercent = usedPercent === null ? null : Math.min(Math.max(usedPercent, 0), 100);
  }

  /** Which window this is. A newer report of the same key replaces it. */
  get key(): string {
    return `${this.minutes}\u0000${this.scope}`;
  }

  /** `5h`, `7d`, `7d opus` - short, for the status bar. */
  get label(): string {
    const [amount, unit] = this.length();
    const length = `${amount}${unit[0]}`;
    return this.scope ? `${length} ${this.scope}` : length;
  }

  /** `5-hour`, `7-day opus` - spelled out, for /cost. */
  get title(): string {
    const [amount, unit] = this.length();
    const length = `${amount}-${unit}`;
    return this.scope ? `${length} ${this.scope}` : length;
  }

  private length(): [number, string] {
    if (this.minutes % 1440 === 0) return [Math.floor(this.minutes / 1440), "day"];
    if (this.minutes % 60 === 0) return [Math.floor(this.minutes / 60), "hour"];
    return [this.minutes, "minute"];
  }

  /**
   * The window as it stands at `now`.
   *
   * Past its reset a window is empty again: one that had a figure is at 0%.
   * One that had none had only its reset to show, and that is gone - so there
   * is nothing left to show, rather than a guess.
   */
  at(now: number = Date.now() / 1000): PlanWindow | null {
    if (this.usedPercent === null && this.resetsAt === null) return null;
    if (this.resetsAt === null || now < this.resetsAt) return this;
    if (this.usedPercent === null) return null;
    return new PlanWindow(this.minutes, 0, null, this.scope);
  }
}

/** `95%`. Never `100%` short of the limit: a window rounded up to full reads
 * as one that refuses the next turn. */
export function formatPercent(usedPercent: number): string {
  const shown = usedPercent >= 100 ? 100 : Math.min(pyRound(usedPercent), 99);
  return `${Math.max(shown, 0)}%`;
}

/** Closer than this, a renewal is given as a clock time alone. */
export const RENEWAL_CLOCK_SECONDS = 86_400;

/**
 * When a window renews, in local time: `06:30 am` within a day, and
 * `Mon Oct 26 04:00 am` beyond it. `short` drops the clock from a date.
 */
export function formatRenewal(
  resetsAt: number,
  options: { short?: boolean; now?: number } = {},
): string {
  const now = options.now ?? Date.now() / 1000;
  const clock = strftime("%I:%M %p", resetsAt).toLowerCase();
  if (resetsAt - now < RENEWAL_CLOCK_SECONDS) return clock;
  const day = `${strftime("%a %b", resetsAt)} ${new Date(resetsAt * 1000).getDate()}`;
  return options.short ? day : `${day} ${clock}`;
}

/**
 * Local cost fallback when the provider reports none.
 *
 * Cache reads and writes are priced separately from ordinary input tokens, and
 * `inputTokens` deliberately *excludes* cached tokens - double-counting here
 * silently inflates every cost display.
 */
export function computeCost(usage: TurnUsage, pricing: ModelPricing): number {
  const cacheReadRate = pricing.cacheRead || pricing.prompt;
  const cacheWriteRate = pricing.cacheWrite || pricing.prompt;
  return (
    usage.inputTokens * pricing.prompt +
    usage.outputTokens * pricing.completion +
    usage.cacheReadTokens * cacheReadRate +
    usage.cacheWriteTokens * cacheWriteRate +
    usage.reasoningTokens * pricing.reasoning
  );
}

/** Human-readable cost for the status bar (e.g. `$0.0431`). */
export function formatCost(costUsd: number): string {
  if (costUsd >= 100) return `$${formatFixed(costUsd, 0, true)}`;
  if (costUsd >= 1) return `$${formatFixed(costUsd, 2)}`;
  return `$${formatFixed(costUsd, 4)}`;
}

/** Compact token count (e.g. `12.4k`, `1.2M`). */
export function formatTokens(count: number): string {
  if (count < 1_000) return String(count);
  if (count < 1_000_000) return `${formatFixed(count / 1_000, 1)}k`.replace(".0k", "k");
  return `${formatFixed(count / 1_000_000, 1)}M`.replace(".0M", "M");
}

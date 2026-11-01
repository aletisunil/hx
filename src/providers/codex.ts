/**
 * Codex provider - ChatGPT Plus/Pro subscription over the Responses API.
 *
 * Talks to `chatgpt.com/backend-api/codex/responses`, the endpoint OpenAI
 * exposes for OSS harnesses. Wire encoding lives in `responses_codec.ts`; this
 * module is transport only. The credential is fetched per request, because an
 * OAuth access token expires in the middle of a long session.
 */

import { ORIGINATOR } from "../auth/oauth/codex.ts";
import type { AuthResolver, ResolvedAuth } from "../auth/resolve.ts";
import { formatRenewal, PlanWindow } from "../core/usage.ts";
import { lines, request } from "../net.ts";
import { strip } from "../py.ts";
import { VERSION } from "../version.ts";
import {
  type Provider,
  ProviderError,
  type ProviderRequest,
  type StreamItem,
  unreached,
} from "./base.ts";
import type { ModelRegistry } from "./models.ts";
import { encodeBody, sleep } from "./openrouter.ts";
import { BUILDERS } from "./registry.ts";
import { buildBody, endOf, StreamState } from "./responses_codec.ts";

export const API_BASE = "https://chatgpt.com/backend-api";
const RESPONSES_PATH = "/codex/responses";

/** Points the Codex route at another backend - the stand-in the end-to-end
 * suite runs, so a test's stored login never reaches chatgpt.com. */
export const BASE_URL_VAR = "HX_CODEX_BASE_URL";

export function apiBase(): string {
  return strip(process.env[BASE_URL_VAR] ?? "").replace(/\/+$/, "") || API_BASE;
}

/** HX namespaces model ids by provider; the wire wants the bare id. */
export const MODEL_PREFIX = "openai-codex/";

export type TokenSource = () => Promise<ResolvedAuth>;

/** How deeply to reason, asked per model: each model publishes its own levels
 * and its own default, and the model can change without the route changing. */
export type EffortSource = (modelId: string) => string | null;

/** Used when nothing was wired in to answer. Every Codex model accepts it. */
export const DEFAULT_EFFORT = "medium";

const RETRY_STATUSES = new Set([408, 409, 429, 500, 502, 503, 504]);

type Headers = Record<string, string | string[] | undefined>;
type Json = Record<string, unknown>;

export class CodexProvider implements Provider {
  readonly name = "openai-codex";
  readonly baseUrl: string;
  sessionId: string | null;
  readonly effort: EffortSource;
  private readonly timeout: number;
  private readonly maxRetries: number;

  constructor(
    private readonly auth: TokenSource,
    options: {
      baseUrl?: string;
      sessionId?: string | null;
      effort?: EffortSource | null;
      timeout?: number;
      maxRetries?: number;
    } = {},
  ) {
    this.baseUrl = (options.baseUrl ?? apiBase()).replace(/\/+$/, "");
    this.sessionId = options.sessionId ?? null;
    this.effort = options.effort ?? (() => DEFAULT_EFFORT);
    this.timeout = options.timeout ?? 600;
    this.maxRetries = options.maxRetries ?? 3;
  }

  /** The session id is the prompt-cache key, so it must follow /clear. */
  setSessionId(sessionId: string): void {
    this.sessionId = sessionId;
  }

  async *astream(request: ProviderRequest, signal: AbortSignal): AsyncGenerator<StreamItem> {
    const body = buildBody(request, this.sessionId, this.effort(request.model));
    body.model = wireModel(request.model);
    const url = `${this.baseUrl}${RESPONSES_PATH}`;
    for (let attempt = 0; ; attempt++) {
      const started = performance.now();
      let yielded = false;
      try {
        for await (const item of this.streamOnce(url, body, started, signal)) {
          yielded = true;
          yield item;
        }
        return;
      } catch (error) {
        // Only retry before anything was yielded; a mid-stream restart would
        // duplicate text the consumer has already rendered.
        if (
          !(error instanceof ProviderError) ||
          !error.retryable ||
          attempt === this.maxRetries ||
          yielded
        )
          throw error;
        await sleep(backoff(attempt), signal);
      }
    }
  }

  private async *streamOnce(
    url: string,
    body: Json,
    started: number,
    signal: AbortSignal,
  ): AsyncGenerator<StreamItem> {
    const headers = await this.headers();
    const state = new StreamState();
    let response: Awaited<ReturnType<typeof request>>;
    try {
      response = await request(url, {
        method: "POST",
        body: encodeBody(body),
        headers,
        timeout: 15,
        readTimeout: this.timeout,
        signal,
      });
    } catch (error) {
      if (signal.aborted) throw error;
      throw unreached(error);
    }
    const windows = planWindows(response.headers);
    try {
      if (response.status >= 400) {
        const raw = await response.text();
        const limit = usageLimit(raw);
        throw new ProviderError(
          limit !== null ? limitMessage(limit, windows) : errorMessage(response.status, raw),
          {
            status: response.status,
            // A spent window stays spent until it resets.
            retryable: limit === null && RETRY_STATUSES.has(response.status),
            planWindows: windows,
          },
        );
      }
      try {
        for await (const event of iterSse(response)) yield* state.consume(event);
        if (signal.aborted) throw signal.reason;
      } catch (error) {
        if (signal.aborted || error instanceof ProviderError) throw error;
        throw new ProviderError((error as Error).message, { retryable: true });
      }
    } finally {
      response.close();
    }
    yield { ...endOf(state, performance.now() - started), planWindows: windows };
  }

  /** Built per request so a token refreshed mid-session takes effect. */
  private async headers(): Promise<Record<string, string>> {
    const auth = await this.auth();
    const accountId = auth.extra.account_id;
    if (!accountId)
      throw new ProviderError(
        "No ChatGPT account id on the stored login. Run `hx auth login openai-codex`.",
      );
    const headers: Record<string, string> = {
      Authorization: `Bearer ${auth.token}`,
      "chatgpt-account-id": String(accountId),
      originator: ORIGINATOR,
      "User-Agent": `hx/${VERSION}`,
      "OpenAI-Beta": "responses=experimental",
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    };
    if (this.sessionId) {
      headers["session-id"] = this.sessionId;
      headers["x-client-request-id"] = this.sessionId;
    }
    return headers;
  }

  async aclose(): Promise<void> {}
}

/** Decoded `data:` payloads. Event names are ignored - every payload carries
 * its own `type`. */
async function* iterSse(response: Awaited<ReturnType<typeof request>>): AsyncGenerator<Json> {
  for await (const line of lines(response)) {
    if (!line.startsWith("data:")) continue;
    const data = strip(line.slice(5));
    if (!data || data === "[DONE]") continue;
    try {
      const parsed = JSON.parse(data);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) yield parsed;
    } catch {
      // Not JSON: skipped.
    }
  }
}

/** `openai-codex/gpt-5.5` -> `gpt-5.5`. */
export const wireModel = (modelId: string) =>
  modelId.startsWith(MODEL_PREFIX) ? modelId.slice(MODEL_PREFIX.length) : modelId;

/** What the backend says when this account is not entitled to this model -
 * per model, not per account. The catalogue knows the difference. */
const NOT_ENTITLED = "is not supported when using Codex with a ChatGPT account";

function errorMessage(status: number, body: string): string {
  if (status === 401 || status === 403)
    return `Codex rejected the credential (HTTP ${status}). Run \`hx auth login openai-codex\` to sign in again.`;
  if (status === 400 && body.includes(NOT_ENTITLED))
    return (
      "This ChatGPT account is not entitled to this model. Entitlement is " +
      "per model and per account, and the account's real list is what the " +
      "Codex backend serves - press ctrl+r in /model and pick one of the " +
      "openai-codex models it offers. If the list comes back empty, check " +
      "that the subscription is active and Codex is enabled for the account " +
      "(chatgpt.com/codex); `hx auth` shows which account the stored login is on."
    );
  let message: unknown = null;
  try {
    const parsed = JSON.parse(body);
    const detail = parsed.error;
    message =
      typeof detail === "object" && detail !== null && !Array.isArray(detail)
        ? detail.message
        : detail;
    message = message || parsed.detail || parsed.message;
  } catch {
    message = null;
  }
  return `Codex ${status}: ${message ? String(message) : [...body].slice(0, 400).join("")}`;
}

/** The two windows ChatGPT reports, by the names its headers use. */
const WINDOWS = ["primary", "secondary"];

const header = (headers: Headers, name: string): string | undefined => {
  const value = headers[name.toLowerCase()];
  return Array.isArray(value) ? value[0] : value;
};

/**
 * The usage windows a Codex response reports in its `x-codex-*` headers. A
 * window whose percentage is missing or not a number is left out: the figure
 * from the last good reply is closer to the truth than none.
 */
export function planWindows(headers: Headers): PlanWindow[] {
  const windows: PlanWindow[] = [];
  for (const name of WINDOWS) {
    const used = number(header(headers, `x-codex-${name}-used-percent`));
    const minutes = number(header(headers, `x-codex-${name}-window-minutes`));
    if (used === null || minutes === null || minutes <= 0) continue;
    let resetsAt = number(header(headers, `x-codex-${name}-reset-at`));
    if (resetsAt === null) {
      const after = number(header(headers, `x-codex-${name}-reset-after-seconds`));
      if (after !== null) resetsAt = Date.now() / 1000 + after;
    }
    windows.push(new PlanWindow(Math.trunc(minutes), used, resetsAt));
  }
  return windows;
}

/** Python's `float(value)`, or `null`. */
function number(value: string | undefined | null): number | null {
  if (value === undefined || value === null) return null;
  const text = strip(value).toLowerCase().replaceAll("_", "");
  if (/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/.test(text)) return Number(text);
  if (/^[+-]?(inf|infinity)$/.test(text)) return text.startsWith("-") ? -Infinity : Infinity;
  if (/^[+-]?nan$/.test(text)) return Number.NaN;
  return null;
}

/** The refusal's error when it is a spent window, `usage_limit_reached`. */
function usageLimit(body: string): Json | null {
  try {
    const error = JSON.parse(body).error;
    if (typeof error === "object" && error !== null && error.type === "usage_limit_reached")
      return error;
  } catch {
    return null;
  }
  return null;
}

/** Which window is spent and when it comes back, in HX's words. */
function limitMessage(error: Json, windows: readonly PlanWindow[]): string {
  const spent = windows.find((w) => (w.usedPercent ?? 0) >= 100) ?? null;
  const which = spent !== null ? `${spent.title} usage limit` : "usage limit";
  let resetsAt = spent !== null ? spent.resetsAt : null;
  if (resetsAt === null) resetsAt = error.resets_at ? number(String(error.resets_at)) : null;
  const when = resetsAt ? ` It resets at ${formatRenewal(resetsAt)}.` : "";
  return `Your ChatGPT plan's ${which} is reached.${when}`;
}

/** Exponential with jitter. */
const backoff = (attempt: number) => Math.min(2 ** attempt, 8) * (0.5 + Math.random() / 2);

/** Resolved per request: the resolver refreshes an expiring token under the
 * store's lock. The catalogue knows what each model will reason at. */
BUILDERS["openai-codex"] = (
  spec,
  resolver: AuthResolver,
  sessionId: string | null,
  models: ModelRegistry | null,
) =>
  new CodexProvider(() => resolver.resolve(spec.id), {
    sessionId,
    effort: models !== null ? (modelId: string) => models.reasoningEffort(modelId) : null,
  });

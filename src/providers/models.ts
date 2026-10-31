/**
 * Model catalogue: context windows, pricing, and caching behaviour.
 *
 * Populated from OpenRouter's `/api/v1/models` and, for each signed-in
 * subscription, from that backend's own per-account catalogue. Cached to
 * `~/.hx/models.json` and refreshed on demand (ctrl+r in the `/model` picker),
 * after a login, or when the cache is older than a day. The registry drives the
 * `/model` picker, the context gauge, and cost fallback maths.
 */

import { createHash } from "node:crypto";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import type { AuthResolver, ResolvedAuth } from "../auth/resolve.ts";
import { OPENROUTER } from "../auth/store.ts";
import { compareCodePoints, dumps, PyFloat } from "../json.ts";
import { describe } from "../net.ts";
import { exists, parent, readText } from "../pathlib.ts";
import { modelsCacheFile } from "../paths.ts";
import { floatRepr, splitWhitespace, strip, toFloat, toInt, truthy } from "../py.ts";

export type CacheMode = "explicit" | "implicit" | "none";

/** USD per token, as OpenRouter reports it. */
export interface ModelPricing {
  readonly prompt: number;
  readonly completion: number;
  readonly cacheRead: number;
  readonly cacheWrite: number;
  readonly reasoning: number;
}

export const NO_PRICING: ModelPricing = {
  prompt: 0,
  completion: 0,
  cacheRead: 0,
  cacheWrite: 0,
  reasoning: 0,
};

export interface ModelInfo {
  readonly id: string;
  readonly name: string;
  readonly contextWindow: number;
  readonly maxOutputTokens: number;
  readonly pricing: ModelPricing;
  readonly cacheMode: CacheMode;
  readonly supportsTools: boolean;
  readonly supportsReasoning: boolean;
  /** Accepts image input. Off unless the catalogue says so: an image sent to a
   * text-only model is a failed turn, one withheld is a line of text saying so. */
  readonly supportsImages: boolean;
  /** Which route serves this model. Derived from the id's namespace. */
  readonly providerId: string;
  /** Billed to a subscription rather than per token, so cost display is
   * meaningless and the picker says so instead of printing $0.00. */
  readonly isSubscription: boolean;
  /** Reasoning efforts this model accepts, weakest first. Empty means unknown, not none. */
  readonly reasoningLevels: readonly string[];
  /** The effort the vendor picks when the caller does not. */
  readonly defaultReasoningLevel: string | null;
  /** `[effort, backend model id]` pairs, for a route that serves each
   * reasoning depth as a separate model rather than as a request field. */
  readonly effortRoutes: readonly (readonly [string, string])[];
  /** The id names a server-side dispatcher, not a model. */
  readonly modelRouter: boolean;
}

export function modelInfo(
  fields: Pick<ModelInfo, "id" | "name" | "contextWindow" | "maxOutputTokens"> & Partial<ModelInfo>,
): ModelInfo {
  return {
    pricing: NO_PRICING,
    cacheMode: "none",
    supportsTools: true,
    supportsReasoning: false,
    supportsImages: false,
    providerId: OPENROUTER,
    isSubscription: false,
    reasoningLevels: [],
    defaultReasoningLevel: null,
    effortRoutes: [],
    modelRouter: false,
    ...fields,
  };
}

export const CODEX_NAMESPACE = "openai-codex/";

/** Assumed for a Codex model HX was told about but does not ship. */
export const CODEX_FALLBACK_CONTEXT = 200_000;
export const CODEX_FALLBACK_OUTPUT = 64_000;

const CODEX_EFFORTS = ["low", "medium", "high", "xhigh", "max"];

/** Fallback list of models on a ChatGPT Plus/Pro subscription, used only when
 * the backend's own catalogue cannot be reached. */
export const CODEX_MODELS: readonly ModelInfo[] = (
  [
    ["gpt-6-astra", "GPT-6-Astra", [...CODEX_EFFORTS, "ultra"], "low"],
    ["gpt-5.6-sol", "GPT-5.6-Sol", [...CODEX_EFFORTS, "ultra"], "low"],
    ["gpt-5.6-terra", "GPT-5.6-Terra", [...CODEX_EFFORTS, "ultra"], "medium"],
    ["gpt-5.6-luna", "GPT-5.6-Luna", CODEX_EFFORTS, "medium"],
    ["gpt-5.5", "GPT-5.5", CODEX_EFFORTS.slice(0, -1), "medium"],
  ] as const
).map(([slug, name, levels, defaultLevel]) =>
  modelInfo({
    id: `${CODEX_NAMESPACE}${slug}`,
    name,
    contextWindow: 272_000,
    maxOutputTokens: 128_000,
    cacheMode: "implicit",
    supportsReasoning: true,
    supportsImages: true,
    providerId: "openai-codex",
    isSubscription: true,
    reasoningLevels: levels,
    defaultReasoningLevel: defaultLevel,
  }),
);

export const DEVIN_NAMESPACE = "devin/";

/** Fallback list for a Devin subscription: the two every plan has been seen to include. */
export const DEVIN_MODELS: readonly ModelInfo[] = (
  [
    ["swe-1-6", "SWE-1.6"],
    ["swe-1-6-fast", "SWE-1.6 Fast"],
  ] as const
).map(([slug, name]) =>
  modelInfo({
    id: `${DEVIN_NAMESPACE}${slug}`,
    name,
    contextWindow: 200_000,
    maxOutputTokens: 128_000,
    cacheMode: "implicit",
    supportsReasoning: true,
    providerId: "devin",
    isSubscription: true,
  }),
);

export const CLAUDE_CODE_NAMESPACE = "claude-code/";

/** Fallback list for a Claude subscription: Claude Code's own aliases. */
export const CLAUDE_CODE_MODELS: readonly ModelInfo[] = (
  [
    ["sonnet", "Sonnet"],
    ["opus", "Opus"],
    ["haiku", "Haiku"],
  ] as const
).map(([alias, name]) =>
  modelInfo({
    id: `${CLAUDE_CODE_NAMESPACE}${alias}`,
    name,
    contextWindow: 200_000,
    maxOutputTokens: 64_000,
    cacheMode: "implicit",
    supportsReasoning: true,
    supportsImages: true,
    providerId: "claude-code",
    isSubscription: true,
  }),
);

/** What one subscription's backend said its login may call. */
interface Entitlement {
  /** `null` until the backend has answered; the fallback list stands in. */
  models: readonly ModelInfo[] | null;
  /** Which login `models` describes. */
  account: string | null;
  /** Why the list could not be fetched, in one line. */
  error: string | null;
  /** The version of the program that answers for this login, when last asked. */
  version: string | null;
  /** What would widen the list, in one line. */
  advice: string | null;
}

const entitlement = (fields: Partial<Entitlement> = {}): Entitlement => ({
  models: null,
  account: null,
  error: null,
  version: null,
  advice: null,
  ...fields,
});

/** How to ask one subscription route for its models. */
export interface Subscription {
  readonly providerId: string;
  readonly label: string;
  readonly fallback: readonly ModelInfo[];
  fetch(auth: ResolvedAuth): Promise<ModelInfo[]>;
  account(auth: ResolvedAuth): string | null;
  /** For a route whose list comes from a program installed here: its version. */
  version?(): Promise<string | null>;
  /** Given `version`, the update that would list newer models, if any. */
  advise?(installed: string | null): Promise<string | null>;
}

/**
 * The subscription routes. Their catalogue modules register here, so this
 * module does not load every route's client to list one.
 */
export const SUBSCRIPTIONS: Subscription[] = [];

/** Stands in for an account id on a route whose token carries none. */
export function tokenFingerprint(auth: ResolvedAuth): string | null {
  return auth.token ? createHash("sha256").update(auth.token).digest("hex").slice(0, 16) : null;
}

/** Lookup and refresh for the model catalogue. */
export class ModelRegistry {
  /** Vendor prefixes whose models need explicit `cache_control` breakpoints. */
  static readonly EXPLICIT_CACHE_VENDORS = new Set(["anthropic", "google"]);
  /** Vendors that cache automatically on a stable prefix. */
  static readonly IMPLICIT_CACHE_VENDORS = new Set([
    "openai",
    "deepseek",
    "x-ai",
    "mistralai",
    "moonshotai",
    "qwen",
  ]);
  static readonly CACHE_TTL_SECONDS = 86_400;

  /** Codex ids from settings. Merged alongside `CODEX_MODELS`. */
  private extraCodex: ModelInfo[] = [];
  /** `models.reasoning_effort` from settings, clamped per model. */
  private requested: string | null = null;
  private entitled = new Map<string, Entitlement>(
    SUBSCRIPTIONS.map((sub) => [sub.providerId, entitlement()]),
  );
  private models = new Map<string, ModelInfo>();
  /** Per model id, `[context window, max output]` as its backend reported them. */
  private reported = new Map<string, [number, number]>();
  private fetchedAt = 0;
  private loaded = false;
  /** Why the last refresh failed, in one line, or `null`. */
  refreshError: string | null = null;

  private entitlementOf(providerId: string): Entitlement {
    let entry = this.entitled.get(providerId);
    if (entry === undefined) {
      entry = entitlement();
      this.entitled.set(providerId, entry);
    }
    return entry;
  }

  /** `[route label, reason]` for every subscription whose list failed. */
  subscriptionErrors(): [string, string][] {
    return SUBSCRIPTIONS.flatMap((sub) => {
      const error = this.entitlementOf(sub.providerId).error;
      return error ? [[sub.label, error] as [string, string]] : [];
    });
  }

  /** One line per subscription whose list an update would widen. */
  subscriptionAdvice(routes?: Iterable<string>): string[] {
    const wanted = routes === undefined ? null : new Set(routes);
    return SUBSCRIPTIONS.flatMap((sub) => {
      const advice = this.entitlementOf(sub.providerId).advice;
      return (wanted === null || wanted.has(sub.providerId)) && advice ? [advice] : [];
    });
  }

  /** Throws `UnknownModel` if absent. */
  get(modelId: string): ModelInfo {
    if (!this.loaded) this.loadCache();
    const info = this.models.get(modelId);
    if (info === undefined) throw new UnknownModel(modelId);
    return info;
  }

  /**
   * Never throws. An unknown id yields a conservative placeholder: a model
   * missing from a stale catalogue should still be usable, it just cannot show
   * accurate cost or a real context gauge.
   */
  getOrDefault(modelId: string): ModelInfo {
    try {
      return this.get(modelId);
    } catch (error) {
      if (!(error instanceof UnknownModel)) throw error;
      const spec = routeOf(modelId);
      return modelInfo({
        id: modelId,
        name: modelId,
        contextWindow: 128_000,
        maxOutputTokens: 8192,
        cacheMode: inferCacheMode(modelId),
        providerId: spec.id,
        isSubscription: spec.isSubscription,
      });
    }
  }

  /** Keep the limits a backend reported for a model, over the catalogue's.
   * Saved at once, so the next session starts on the true window. */
  adoptLimits(modelId: string, contextWindow: number, maxOutputTokens: number): void {
    if (!this.loaded) this.loadCache();
    const known = this.reported.get(modelId);
    if (known?.[0] === contextWindow && known[1] === maxOutputTokens) return;
    this.reported.set(modelId, [contextWindow, maxOutputTokens]);
    this.applyReported();
    this.saveCache();
  }

  private forgetReported(providerId: string): void {
    for (const modelId of [...this.reported.keys()]) {
      if (routeOf(modelId).id === providerId) this.reported.delete(modelId);
    }
  }

  private applyReported(): void {
    for (const [modelId, [window, output]] of this.reported) {
      const info = this.models.get(modelId);
      if (info !== undefined) {
        this.models.set(modelId, { ...info, contextWindow: window, maxOutputTokens: output });
      }
    }
  }

  all(): ModelInfo[] {
    if (!this.loaded) this.loadCache();
    return [...this.models.values()].sort((a, b) => compareCodePoints(a.id, b.id));
  }

  get isStale(): boolean {
    return Date.now() / 1000 - this.fetchedAt > ModelRegistry.CACHE_TTL_SECONDS;
  }

  /**
   * Fetch every route's live catalogue and rewrite the cache.
   *
   * A subscription failure is recorded but not raised: it costs the account's
   * real model list, which the fallback stands in for, and there is no reason
   * for it to also cost the OpenRouter catalogue that did arrive.
   */
  async refresh(resolver: AuthResolver): Promise<void> {
    const { fetchModels } = await import("./openrouter.ts");
    // So a failure below leaves the cached catalogue in place.
    if (!this.loaded) this.loadCache();
    await resolver.checkExternalLogins();
    // Asked for first: a broken OpenRouter key throws below, and it must not
    // also cost a subscription its model list.
    for (const sub of SUBSCRIPTIONS) await this.refreshSubscription(sub, resolver);

    const fetched = new Map<string, ModelInfo>();
    if (resolver.hasCredential(OPENROUTER)) {
      let raw: unknown[];
      try {
        raw = await fetchModels(resolver.resolveStatic(OPENROUTER).token);
      } catch (error) {
        // The catalogue already held is left alone: a refresh that cannot
        // reach the network is no reason to lose yesterday's models.
        this.refreshError = describe(error);
        this.mergeStatic();
        this.saveCache();
        throw error;
      }
      for (const entry of raw) {
        try {
          const info = parseModelEntry(entry as Record<string, unknown>);
          fetched.set(info.id, info);
        } catch {
          // One malformed catalogue entry must not cost the whole list.
        }
      }
    }
    this.models = fetched;
    this.mergeStatic();
    this.loaded = true;
    this.refreshError = null;
    this.fetchedAt = Date.now() / 1000;
    this.saveCache();
  }

  /**
   * Ask again every subscription whose program has changed version since.
   * Costs one `--version` when nothing changed. Returns whether the catalogue changed.
   */
  async revalidate(resolver: AuthResolver): Promise<boolean> {
    if (!this.loaded) this.loadCache();
    let changed = false;
    for (const sub of SUBSCRIPTIONS) {
      if (sub.version === undefined || !resolver.hasCredential(sub.providerId)) continue;
      const installed = await sub.version();
      // null is a CLI that did not answer in time, not a new version.
      if (installed === null || installed === this.entitlementOf(sub.providerId).version) continue;
      await this.refreshSubscription(sub, resolver, installed);
      changed = true;
    }
    if (changed) {
      this.mergeStatic();
      this.saveCache();
    }
    return changed;
  }

  /**
   * Ask a subscription backend which models this login may actually call. An
   * empty answer is treated as a failure to answer: emptying the picker is
   * worse than showing the list HX already had.
   */
  private async refreshSubscription(
    sub: Subscription,
    resolver: AuthResolver,
    known?: string | null,
  ): Promise<void> {
    const entitled = this.entitlementOf(sub.providerId);
    if (!resolver.hasCredential(sub.providerId)) {
      // Signed out: the previous account's entitlements are not ours.
      this.entitled.set(sub.providerId, entitlement());
      this.forgetReported(sub.providerId);
      return;
    }
    let auth: ResolvedAuth;
    try {
      auth = await resolver.resolve(sub.providerId);
    } catch (error) {
      entitled.error = describe(error);
      return;
    }
    const installed = known !== undefined ? known : sub.version ? await sub.version() : null;
    const account = sub.account(auth);
    if (account !== entitled.account) {
      // A different login, so what is cached describes somebody else's subscription.
      if (entitled.account !== null) this.forgetReported(sub.providerId);
      entitled.models = null;
      entitled.account = null;
    }
    const listing = async () => {
      try {
        return await sub.fetch(auth);
      } catch (error) {
        entitled.error = describe(error);
        return null;
      }
    };
    const advice = async () => (sub.advise ? sub.advise(installed) : null);
    // Side by side: the advice asks the network, the list a process.
    const [fetched, advised] = await Promise.all([listing(), advice()]);
    entitled.advice = advised;
    entitled.version = installed;
    if (fetched === null) return;
    entitled.error = null;
    if (fetched.length) {
      entitled.models = fetched;
      entitled.account = account;
    }
  }

  /** Add the subscription routes, whose models are never in the OpenRouter fetch. */
  private mergeStatic(): void {
    const routes = new Set(SUBSCRIPTIONS.map((sub) => sub.providerId));
    for (const [modelId, info] of [...this.models]) {
      if (routes.has(info.providerId)) this.models.delete(modelId);
    }
    for (const sub of SUBSCRIPTIONS) {
      const fetched = this.entitlementOf(sub.providerId).models;
      for (const info of fetched ?? sub.fallback) this.models.set(info.id, info);
    }
    // Never over a fetched entry: an escape-hatch id carries guessed figures.
    for (const info of this.extraCodex)
      if (!this.models.has(info.id)) this.models.set(info.id, info);
    this.applyReported();
  }

  /** Ask for an effort on every model that offers one. `null` leaves each
   * model at the depth its vendor chose for it. */
  setReasoningEffort(effort: string | null): void {
    this.requested = effort || null;
  }

  get requestedEffort(): string | null {
    return this.requested;
  }

  /** The effort to show for a model, or `null` when showing one would be a
   * claim HX cannot back. */
  displayedEffort(modelId: string): string | null {
    if (this.getOrDefault(modelId).reasoningLevels.length === 0) return null;
    return this.reasoningEffort(modelId);
  }

  /** The effort to send for one model, or `null` to leave it to the backend. */
  reasoningEffort(modelId: string): string | null {
    return resolveEffortFor(this.requested, this.getOrDefault(modelId));
  }

  /** Register Codex ids from settings, on top of whatever was fetched.
   * Returns the ids added, ignoring any already known. */
  addCodexModels(modelIds: Iterable<string>): string[] {
    const added: string[] = [];
    for (const raw of modelIds) {
      const bare = strip(raw);
      if (!bare) continue;
      const modelId = bare.startsWith(CODEX_NAMESPACE) ? bare : `${CODEX_NAMESPACE}${bare}`;
      const known = this.entitlementOf("openai-codex").models ?? CODEX_MODELS;
      if (
        this.extraCodex.some((info) => info.id === modelId) ||
        known.some((info) => info.id === modelId)
      ) {
        continue;
      }
      this.extraCodex.push(
        modelInfo({
          id: modelId,
          name: `${modelId.slice(CODEX_NAMESPACE.length)} (ChatGPT subscription)`,
          contextWindow: CODEX_FALLBACK_CONTEXT,
          maxOutputTokens: CODEX_FALLBACK_OUTPUT,
          cacheMode: "implicit",
          supportsReasoning: true,
          // Every model the Codex catalogue has listed takes images.
          supportsImages: true,
          providerId: "openai-codex",
          isSubscription: true,
        }),
      );
      added.push(modelId);
    }
    if (added.length && this.loaded) this.mergeStatic();
    return added;
  }

  /** Read the cache, and register the static routes either way. */
  loadCache(): void {
    this.loaded = true;
    this.models = new Map();
    let payload: Record<string, unknown> = {};
    const path = modelsCacheFile();
    if (exists(path)) {
      try {
        const parsed = JSON.parse(readText(path));
        payload = typeof parsed === "object" && parsed !== null ? parsed : {};
      } catch {
        payload = {};
      }
    }
    this.fetchedAt = (payload.fetched_at as number) ?? 0;
    // Written before image support was recorded: treated as stale, so the
    // refresh at startup replaces it rather than withholding images for a day.
    if (predatesImages(payload)) this.fetchedAt = 0;
    for (const sub of SUBSCRIPTIONS) this.loadCachedSubscription(sub, payload[cacheKey(sub)]);
    this.reported = reportedLimits(payload.reported_limits);
    for (const entry of (payload.models as unknown[]) ?? []) {
      try {
        const info = parseModelEntry(entry as Record<string, unknown>);
        this.models.set(info.id, info);
      } catch {
        // Skipped, not fatal.
      }
    }
    this.mergeStatic();
  }

  /** Restore one account's fetched list. A malformed block is dropped whole:
   * half an entitlement list is worse than none. */
  private loadCachedSubscription(sub: Subscription, payload: unknown): void {
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return;
    const block = payload as Record<string, unknown>;
    const entitled = entitlement({ version: text(block.version), advice: text(block.advice) });
    this.entitled.set(sub.providerId, entitled);
    if (!Array.isArray(block.models)) return;
    let restored: ModelInfo[];
    try {
      restored = block.models.map((entry) => subscriptionModel(sub.providerId, entry));
    } catch {
      return;
    }
    if (restored.length) {
      entitled.models = restored;
      entitled.account = block.account_id ? String(block.account_id) : null;
    }
  }

  saveCache(): void {
    const path = modelsCacheFile();
    mkdirSync(parent(path), { recursive: true });
    const payload: Record<string, unknown> = {
      fetched_at: new PyFloat(this.fetchedAt),
      models: this.all()
        .filter((m) => !m.isSubscription)
        .map((m) => ({
          id: m.id,
          name: m.name,
          context_length: m.contextWindow,
          top_provider: { max_completion_tokens: m.maxOutputTokens },
          pricing: {
            prompt: floatRepr(m.pricing.prompt),
            completion: floatRepr(m.pricing.completion),
            input_cache_read: floatRepr(m.pricing.cacheRead),
            input_cache_write: floatRepr(m.pricing.cacheWrite),
            internal_reasoning: floatRepr(m.pricing.reasoning),
          },
          supported_parameters: [
            ...(m.supportsTools ? ["tools"] : []),
            ...(m.supportsReasoning ? ["reasoning"] : []),
          ],
          architecture: { input_modalities: m.supportsImages ? ["text", "image"] : ["text"] },
        })),
    };
    for (const sub of SUBSCRIPTIONS) {
      const entitled = this.entitlementOf(sub.providerId);
      if (entitled.models === null && entitled.version === null) continue;
      const block: Record<string, unknown> = {
        account_id: entitled.account,
        models: (entitled.models ?? []).map(subscriptionEntry),
      };
      if (entitled.version !== null) block.version = entitled.version;
      if (entitled.advice !== null) block.advice = entitled.advice;
      payload[cacheKey(sub)] = block;
    }
    if (this.reported.size) {
      payload.reported_limits = Object.fromEntries(
        [...this.reported].map(([modelId, [window, output]]) => [
          modelId,
          { context_window: window, max_output_tokens: output },
        ]),
      );
    }
    const tmp = `${path.replace(/\.[^./]*$/, "")}.json.tmp`;
    writeFileSync(tmp, dumps(payload, { indent: 2, sortKeys: true }));
    renameSync(tmp, path);
  }
}

/** The effort to send for `info`; the Codex catalogue's rule, which every
 * route that publishes levels follows. */
function resolveEffortFor(requested: string | null, info: ModelInfo): string | null {
  const levels = info.reasoningLevels;
  if (!requested) return info.defaultReasoningLevel;
  if (levels.length === 0 || levels.includes(requested)) return requested;
  const order = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
  const ceiling = order.indexOf(requested);
  if (ceiling < 0) return requested;
  const weaker = levels.filter((level) => order.indexOf(level) <= ceiling);
  return weaker.at(-1) ?? (levels[0] as string);
}

/** The route a model id belongs to, without importing the provider registry. */
function routeOf(modelId: string): { id: string; isSubscription: boolean } {
  for (const [namespace, id] of [
    [CODEX_NAMESPACE, "openai-codex"],
    [CLAUDE_CODE_NAMESPACE, "claude-code"],
    [DEVIN_NAMESPACE, "devin"],
  ] as const) {
    if (modelId.startsWith(namespace)) return { id, isSubscription: true };
  }
  return { id: OPENROUTER, isSubscription: false };
}

/** The cache's `reported_limits`, dropping any entry that is not two positive
 * integers: a guess is better than a window of zero. */
function reportedLimits(block: unknown): Map<string, [number, number]> {
  const limits = new Map<string, [number, number]>();
  if (typeof block !== "object" || block === null || Array.isArray(block)) return limits;
  for (const [modelId, entry] of Object.entries(block)) {
    if (typeof entry !== "object" || entry === null) continue;
    const { context_window: window, max_output_tokens: output } = entry as Record<string, unknown>;
    if (
      Number.isInteger(window) &&
      Number.isInteger(output) &&
      (window as number) > 0 &&
      (output as number) > 0
    ) {
      limits.set(modelId, [window as number, output as number]);
    }
  }
  return limits;
}

/** Whether a cache file was written by a version that did not record image input. */
function predatesImages(payload: Record<string, unknown>): boolean {
  const isEntry = (entry: unknown): entry is Record<string, unknown> =>
    typeof entry === "object" && entry !== null && !Array.isArray(entry);
  const models = payload.models;
  if (
    Array.isArray(models) &&
    models.some((entry) => isEntry(entry) && !("architecture" in entry))
  ) {
    return true;
  }
  for (const sub of SUBSCRIPTIONS) {
    const block = payload[cacheKey(sub)];
    const entries = isEntry(block) ? block.models : null;
    if (
      Array.isArray(entries) &&
      entries.some((entry) => isEntry(entry) && !("supports_images" in entry))
    ) {
      return true;
    }
  }
  return false;
}

const text = (value: unknown) => (typeof value === "string" && value ? value : null);

/** `codex` predates the other routes and keeps its key, so an existing cache
 * still restores after an upgrade. */
const cacheKey = (sub: Subscription) =>
  sub.providerId === "openai-codex" ? "codex" : sub.providerId;

function subscriptionEntry(model: ModelInfo): Record<string, unknown> {
  const entry: Record<string, unknown> = {
    id: model.id,
    name: model.name,
    context_window: model.contextWindow,
    max_output_tokens: model.maxOutputTokens,
    supports_tools: model.supportsTools,
    supports_reasoning: model.supportsReasoning,
    supports_images: model.supportsImages,
    levels: [...model.reasoningLevels],
    default_level: model.defaultReasoningLevel,
  };
  if (model.effortRoutes.length)
    entry.effort_routes = model.effortRoutes.map((route) => [...route]);
  if (model.modelRouter) entry.model_router = true;
  return entry;
}

/** Inverse of `subscriptionEntry`. Throws on a malformed entry. */
function subscriptionModel(providerId: string, raw: unknown): ModelInfo {
  if (typeof raw !== "object" || raw === null) throw new TypeError("not an entry");
  const entry = raw as Record<string, unknown>;
  if (!("id" in entry)) throw new TypeError("no id");
  const routes = ((entry.effort_routes as unknown[]) ?? []).map((route) => {
    const [effort, uid] = route as [unknown, unknown];
    return [String(effort), String(uid)] as const;
  });
  return modelInfo({
    id: String(entry.id),
    name: String(entry.name || entry.id),
    contextWindow: toInt(entry.context_window),
    maxOutputTokens: toInt(entry.max_output_tokens),
    cacheMode: "implicit",
    supportsTools: truthy(entry.supports_tools ?? true),
    supportsReasoning: truthy(entry.supports_reasoning ?? true),
    supportsImages: truthy(entry.supports_images ?? false),
    providerId,
    isSubscription: true,
    reasoningLevels: ((entry.levels as unknown[]) ?? []).map(String),
    defaultReasoningLevel: (entry.default_level as string) || null,
    effortRoutes: routes,
    modelRouter: truthy(entry.model_router ?? false),
  });
}

/**
 * Rank models against a query: id prefix first, then words in the id, then
 * words in the name, then subsequence.
 *
 * Lives here rather than in the picker so that `/model opus` and typing `opus`
 * into the picker's filter box narrow the same list the same way.
 */
export function matchModels(models: readonly ModelInfo[], query: string): ModelInfo[] {
  const needle = strip(query).toLowerCase();
  if (!needle) return [...models];
  const words = splitWhitespace(needle);
  const scored: [number, boolean, string, ModelInfo][] = [];
  for (const model of models) {
    const ids = [model.id.toLowerCase(), ...makerIds(model)];
    const inIds = ids.join(" ");
    const haystack = `${inIds} ${model.name.toLowerCase()}`;
    let score: number;
    if (words.every((word) => inIds.includes(word))) {
      score = ids.some((id) => id.startsWith(words[0] as string)) ? 0 : 1;
    } else if (words.every((word) => haystack.includes(word))) {
      score = 2;
    } else if ([...ids, model.name.toLowerCase()].some((field) => subsequence(needle, field))) {
      // Per field: across all of them joined, letters strewn over two ids
      // match nearly anything.
      score = 3;
    } else continue;
    // Between equally good matches, the one a subscription already pays for
    // comes before the same model billed per token.
    scored.push([score, !model.isSubscription, model.id, model]);
  }
  return scored
    .sort((a, b) => a[0] - b[0] || Number(a[1]) - Number(b[1]) || compareCodePoints(a[2], b[2]))
    .map((item) => item[3]);
}

/** Model-name prefix to maker, for the subscription routes' models. */
const MAKERS: readonly (readonly [string, string])[] = [
  ["claude", "anthropic"],
  ["sonnet", "anthropic"],
  ["opus", "anthropic"],
  ["haiku", "anthropic"],
  ["gpt", "openai"],
];

/** `anthropic/claude-sonnet-5` for `claude-code/claude-sonnet-5`, so typing
 * `anthropic` finds the Claude models a subscription pays for too. */
function makerIds(model: ModelInfo): string[] {
  if (!model.isSubscription) return [];
  const index = model.id.indexOf("/");
  const slug = (index < 0 ? "" : model.id.slice(index + 1)).toLowerCase();
  return MAKERS.filter(([prefix]) => slug.startsWith(prefix))
    .map(([, maker]) => `${maker}/${slug}`)
    .slice(0, 1);
}

/** Map one OpenRouter catalogue entry to a `ModelInfo`. */
export function parseModelEntry(entry: Record<string, unknown>): ModelInfo {
  if (!("id" in entry)) throw new TypeError("catalogue entry has no id");
  const pricing = (entry.pricing as Record<string, unknown>) || {};
  const modelId = String(entry.id);
  const top = (entry.top_provider as Record<string, unknown>) || {};
  const contextWindow = toInt(entry.context_length || top.context_length || 0);
  const params = (entry.supported_parameters as unknown[]) || [];
  const modalities = (entry.architecture as Record<string, unknown>)?.input_modalities as
    | unknown[]
    | undefined;
  return modelInfo({
    id: modelId,
    name: String(entry.name || modelId),
    contextWindow: contextWindow || 128_000,
    maxOutputTokens: toInt(top.max_completion_tokens || 0) || 8192,
    pricing: {
      prompt: price(pricing.prompt),
      completion: price(pricing.completion),
      cacheRead: price(pricing.input_cache_read),
      cacheWrite: price(pricing.input_cache_write),
      reasoning: price(pricing.internal_reasoning),
    },
    cacheMode: inferCacheMode(modelId),
    supportsTools: params.includes("tools"),
    supportsReasoning: params.includes("reasoning"),
    supportsImages: (modalities || []).includes("image"),
  });
}

/** OpenRouter reports prices as strings in USD per token. */
function price(value: unknown): number {
  try {
    return value === null || value === undefined ? 0 : toFloat(value);
  } catch {
    return 0;
  }
}

function subsequence(needle: string, haystack: string): boolean {
  let index = 0;
  const chars = [...haystack];
  for (const char of needle) {
    while (index < chars.length && chars[index] !== char) index++;
    if (index >= chars.length) return false;
    index++;
  }
  return true;
}

/**
 * Derive caching behaviour from the model's vendor prefix. OpenRouter does not
 * expose this in the catalogue, so it is kept in one place.
 */
export function inferCacheMode(modelId: string): CacheMode {
  const vendor = (modelId.split("/")[0] as string).toLowerCase();
  // All cache server-side off a stable prefix, and none takes HX's breakpoints.
  if (["openai-codex", "devin", "claude-code"].includes(vendor)) return "implicit";
  if (ModelRegistry.EXPLICIT_CACHE_VENDORS.has(vendor)) return "explicit";
  if (ModelRegistry.IMPLICIT_CACHE_VENDORS.has(vendor)) return "implicit";
  return "none";
}

export class UnknownModel extends Error {
  override name = "UnknownModel";
}

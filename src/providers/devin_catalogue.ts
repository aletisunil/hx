/**
 * Which models a Devin subscription can reach.
 *
 * The Cascade backend answers `GetCliModelConfigs` per account, so the live
 * list is asked for at sign-in and on the usual refresh, and `DEVIN_MODELS`
 * stands in when it cannot be.
 *
 * Devin publishes a reasoning depth as a model of its own: "Claude Opus 5
 * High" and "Claude Opus 5 Max" are separate configs that share family
 * metadata. Those families are folded into one model whose `effortRoutes` say
 * which backend id serves each depth, so `/effort` works as it does on Codex.
 */

import type { ResolvedAuth } from "../auth/resolve.ts";
import { EFFORT_ORDER } from "../config.ts";
import { compareCodePoints } from "../json.ts";
import { strip } from "../py.ts";
import { ProviderError } from "./base.ts";
import {
  DEFAULT_BASE_URL,
  DISPLAY_INTERNAL_DEFAULT,
  DISPLAY_MODEL_ROUTER,
  DISPLAY_QUICK_REVIEW,
  MODEL_CONFIGS_PATH,
  type ModelConfig,
  modelConfigsRequest,
  parseModelConfigs,
  UNARY_HEADERS,
  unaryError,
} from "./devin_wire.ts";
import { type ModelInfo, modelInfo, NO_PRICING } from "./models.ts";
import { WireError } from "./protowire.ts";

export const NAMESPACE = "devin/";

const DEFAULT_CONTEXT = 200_000;
const DEFAULT_OUTPUT = 64_000;
const HTTP_TIMEOUT = 30;

/** Display slots the server only sends because they were asked for: a review
 * model and internal defaults. Reachable, but not offers. */
const INTERNAL_DISPLAYS = new Set([DISPLAY_QUICK_REVIEW, DISPLAY_INTERNAL_DEFAULT]);

const EFFORT_BY_NAME: Record<string, string> = {
  none: "none",
  nothinking: "none",
  minimal: "minimal",
  low: "low",
  medium: "medium",
  high: "high",
  xhigh: "xhigh",
  max: "max",
};
const EFFORT_KEYS = new Set(["effort", "reasoning effort"]);
/** Family axes. Fast service and a 1M window are separate models in HX; effort
 * is the one axis `/effort` selects along. */
const FAST_KEY = "fast mode";
const THINKING_KEY = "thinking";
const CONTEXT_1M_KEY = "1m context";

/**
 * The models this login may call, ready for the picker. Throws
 * `ProviderError` on any answer that is not a usable catalogue, an empty one
 * included. The caller keeps what it already had.
 */
export async function fetchModels(
  auth: ResolvedAuth,
  baseUrl = DEFAULT_BASE_URL,
): Promise<ModelInfo[]> {
  const { requestUserJwt, post } = await import("./devin.ts");
  const base = baseUrl.replace(/\/+$/, "");
  // Asked first for the API server: an enterprise tenant's catalogue lives on
  // its own host.
  const session = await requestUserJwt(base, auth.token, HTTP_TIMEOUT);
  const api = session.baseUrl || base;
  const [status, body] = await post(
    `${api}${MODEL_CONFIGS_PATH}`,
    modelConfigsRequest(auth.token),
    UNARY_HEADERS,
    HTTP_TIMEOUT,
  );
  if (status >= 400) throw new ProviderError(errorMessage(status, body));
  let configs: ModelConfig[];
  try {
    configs = parseModelConfigs(body);
  } catch (error) {
    if (!(error instanceof WireError)) throw error;
    throw new ProviderError(`Devin model list was not readable: ${error.message}`);
  }
  const models = normalize(configs);
  if (!models.length)
    throw new ProviderError(
      "Devin listed no models for this account. The CLI identity HX presents may be out of date.",
    );
  return models;
}

/** Filter, map and fold the raw configs into picker entries, sorted by id. */
export function normalize(configs: readonly ModelConfig[]): ModelInfo[] {
  const kept: ModelConfig[] = [];
  const seen = new Set<string>();
  for (const config of configs) {
    if (config.disabled || INTERNAL_DISPLAYS.has(config.displayOption)) continue;
    if (!config.uid || seen.has(config.uid)) continue;
    seen.add(config.uid);
    kept.push(config);
  }
  const lanes = new Map<string, Lane>();
  // A router is a dispatcher, not an effort tier, even when filed under a family.
  for (const config of kept) if (!isRouter(config)) fileUnderLane(lanes, config);

  const byUid = new Map(kept.map((config) => [config.uid, config]));
  const folded = new Set<string>();
  const models = new Map<string, ModelInfo>();
  for (const lane of lanes.values()) {
    const info = familyInfo(lane, byUid);
    if (info === null) continue;
    for (const [, uid] of info.effortRoutes) folded.add(uid);
    models.set(info.id, info);
  }
  for (const config of kept) {
    if (folded.has(config.uid)) continue;
    const info = standaloneInfo(config);
    // A family can take the id of one of its own members; the family wins.
    if (!models.has(info.id)) models.set(info.id, info);
  }
  return [...models.values()].sort((a, b) => compareCodePoints(a.id, b.id));
}

const isRouter = (config: ModelConfig) =>
  config.displayOption === DISPLAY_MODEL_ROUTER || config.isModelRouter;

/** A router resolved per turn through `AssignModel`; a harness-backed
 * composite is a valid chat id in its own right. */
const isAssignRouter = (config: ModelConfig) => isRouter(config) && !config.harnessUids.length;

function standaloneInfo(config: ModelConfig): ModelInfo {
  return modelInfo({
    id: `${NAMESPACE}${config.uid}`,
    name: config.label || config.uid,
    contextWindow: config.maxTokens > 0 ? config.maxTokens : DEFAULT_CONTEXT,
    maxOutputTokens: config.maxOutputTokens > 0 ? config.maxOutputTokens : DEFAULT_OUTPUT,
    // Credits, not dollars per token: the subscription already paid.
    pricing: NO_PRICING,
    cacheMode: "implicit",
    // Routers ship no features; Cascade only serves tool-calling models.
    supportsTools: config.hasFeatures ? config.supportsTools : true,
    supportsReasoning: supportsThinking(config),
    supportsImages: config.supportsImages,
    providerId: "devin",
    isSubscription: true,
    modelRouter: isAssignRouter(config),
  });
}

const REASONING_LABEL = /think|minimal|high|medium|low|xhigh|max|reasoning/i;
const NO_REASONING_LABEL = /\bno thinking\b/i;

/** Server features decide; the label is read only when there are none. */
function supportsThinking(config: ModelConfig): boolean {
  if (config.hasFeatures) return config.supportsThinking;
  if (NO_REASONING_LABEL.test(config.label)) return false;
  return REASONING_LABEL.test(config.label);
}

interface Lane {
  readonly id: string;
  readonly name: string;
  readonly members: string[];
  defaultMember: string | null;
  /** HX effort -> member uid. The first member to claim an effort keeps it. */
  readonly routes: Map<string, string>;
}

const normalizeKey = (text: string) => strip(text.toLowerCase().replace(/[^a-z0-9]+/g, " "));

function fileUnderLane(lanes: Map<string, Lane>, config: ModelConfig): void {
  if (!config.familyLabel) return;
  let effort: string | null = null;
  let thinking: boolean | null = null;
  let fast = false;
  let wide = false;
  for (const entry of config.familyEntries) {
    const key = normalizeKey(entry.key);
    if (key === FAST_KEY) fast = entry.order === 1;
    else if (key === THINKING_KEY) thinking = entry.order === 1;
    else if (key === CONTEXT_1M_KEY) wide = entry.order === 1;
    else if (EFFORT_KEYS.has(key))
      effort = EFFORT_BY_NAME[entry.name.toLowerCase().replace(/[^a-z0-9]/g, "")] ?? null;
  }
  // Claude pairs a thinking and a non-thinking config under the same effort
  // label; the explicit Thinking axis tells them apart.
  if (thinking === false) effort = "none";
  const base = config.familyLabel
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!base) return;
  const laneId = `${base}${wide ? "-1m" : ""}${fast ? "-fast" : ""}`;
  let lane = lanes.get(laneId);
  if (lane === undefined) {
    const name = `${config.familyLabel}${wide ? " 1M" : ""}${fast ? " Fast" : ""}`;
    lane = { id: laneId, name, members: [], defaultMember: null, routes: new Map() };
    lanes.set(laneId, lane);
  }
  lane.members.push(config.uid);
  if (lane.defaultMember === null && config.isDefaultInFamily) lane.defaultMember = config.uid;
  if (effort !== null && !lane.routes.has(effort)) lane.routes.set(effort, config.uid);
}

/** Fold a lane into one model, or `null` when it has no effort ladder to
 * choose along. */
function familyInfo(lane: Lane, byUid: Map<string, ModelConfig>): ModelInfo | null {
  const levels = EFFORT_ORDER.filter((level) => lane.routes.has(level));
  if (!levels.some((level) => level !== "none")) return null;
  const routed = new Set(lane.routes.values());
  // The server's own default when it routes somewhere; otherwise the first
  // member it listed, the order the CLI's picker shows them in.
  const defaultUid =
    lane.defaultMember !== null && routed.has(lane.defaultMember)
      ? lane.defaultMember
      : (lane.members.find((uid) => routed.has(uid)) as string);
  const defaultLevel = levels.find((level) => lane.routes.get(level) === defaultUid) as string;
  const template = standaloneInfo(byUid.get(defaultUid) as ModelConfig);
  return {
    ...template,
    id: `${NAMESPACE}${lane.id}`,
    name: lane.name,
    supportsReasoning: true,
    // Any effort may be picked, so the family takes images only if every
    // tier it routes to does.
    supportsImages: [...routed].every((uid) => (byUid.get(uid) as ModelConfig).supportsImages),
    modelRouter: false,
    reasoningLevels: levels,
    defaultReasoningLevel: defaultLevel,
    effortRoutes: levels.map((level) => [level, lane.routes.get(level) as string] as const),
  };
}

/** The backend id to send for `info` at `effort`, already clamped to the
 * model's levels; an effort with no route only happens for an unknown model. */
export function wireModel(info: ModelInfo, effort: string | null): string {
  const routes = new Map(info.effortRoutes);
  if (routes.size) {
    for (const candidate of [effort, info.defaultReasoningLevel])
      if (candidate && routes.has(candidate)) return routes.get(candidate) as string;
    return (info.effortRoutes[0] as readonly [string, string])[1];
  }
  return info.id.startsWith(NAMESPACE) ? info.id.slice(NAMESPACE.length) : info.id;
}

function errorMessage(status: number, body: Buffer): string {
  const error = unaryError(body);
  const detail =
    error !== null ? error.message : new TextDecoder("utf-8").decode(body.subarray(0, 200));
  if (status === 401 || status === 403)
    return `Devin rejected the credential while listing models (HTTP ${status}). Run \`hx auth login devin\` to sign in again.`;
  return `Devin model list failed (${status}): ${detail}`;
}

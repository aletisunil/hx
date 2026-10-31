/**
 * Which models a ChatGPT subscription can actually reach.
 *
 * The Codex backend publishes a catalogue - `/codex/models` - and it answers per
 * account, so it is the only thing that knows what a given login may call.
 * Entitlement is not derivable from the plan name. When the catalogue cannot be
 * reached, `CODEX_MODELS` stands in.
 */

import type { ResolvedAuth } from "../auth/resolve.ts";
import { EFFORT_ORDER } from "../config.ts";
import { request } from "../net.ts";
import { rstrip, strip } from "../py.ts";
import { VERSION } from "../version.ts";
import { ProviderError } from "./base.ts";
import { CODEX_NAMESPACE, type ModelInfo, modelInfo } from "./models.ts";

/** Under the same backend the turns go to - `HX_CODEX_BASE_URL` moves both. */
const CATALOGUE_PATH = "/codex/models";

/** The Codex client version HX presents itself as, *not* HX's own version. The
 * backend filters the catalogue by each model's `minimal_client_version`. */
const CLIENT_VERSION = "0.160.0";

/** `visibility` of a model meant for a person to pick. */
const LISTED = "list";

/** Not in the catalogue, and the same for every Codex model today. */
const MAX_OUTPUT_TOKENS = 128_000;

/** For an entry whose `context_window` is missing or unparseable. */
const FALLBACK_CONTEXT = 272_000;

const HTTP_TIMEOUT = 30;

export const API_BASE = "https://chatgpt.com/backend-api";
export const BASE_URL_VAR = "HX_CODEX_BASE_URL";
export const ORIGINATOR = "hx";

/** `HX_CODEX_BASE_URL` when set, otherwise ChatGPT's backend. */
export function apiBase(): string {
  return rstrip(strip(process.env[BASE_URL_VAR] ?? ""), "/") || API_BASE;
}

/** The models this login may call, as catalogue entries HX can show. */
export async function fetchModels(auth: ResolvedAuth): Promise<ModelInfo[]> {
  return listed(await fetchRaw(auth)).map(parseEntry);
}

/** GET the catalogue, unfiltered and unmapped. */
export async function fetchRaw(auth: ResolvedAuth): Promise<Record<string, unknown>[]> {
  const accountId = auth.extra.account_id;
  if (!accountId) {
    throw new ProviderError(
      "No ChatGPT account id on the stored login. Run `hx auth login openai-codex`.",
    );
  }
  const response = await request(`${apiBase()}${CATALOGUE_PATH}?client_version=${CLIENT_VERSION}`, {
    headers: {
      Authorization: `Bearer ${auth.token}`,
      "chatgpt-account-id": String(accountId),
      originator: ORIGINATOR,
      "User-Agent": `hx/${VERSION}`,
    },
    timeout: HTTP_TIMEOUT,
  });
  const body = await response.text();
  if (response.status >= 400) throw new ProviderError(errorMessage(response.status, body));
  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch (error) {
    throw new ProviderError(`Codex catalogue was not JSON: ${(error as Error).message}`);
  }
  const models =
    typeof payload === "object" && payload !== null
      ? (payload as { models?: unknown }).models
      : null;
  if (!Array.isArray(models)) throw new ProviderError("Codex catalogue carried no model list.");
  return models.filter(
    (entry): entry is Record<string, unknown> => typeof entry === "object" && entry !== null,
  );
}

function listed(entries: Record<string, unknown>[]) {
  return entries.filter((entry) => entry.visibility === LISTED && entry.slug);
}

/**
 * Map one catalogue entry to a `ModelInfo`. `context_window` is preferred over
 * `max_context_window`: the larger figure is a long-context tier the default
 * request does not buy.
 */
export function parseEntry(entry: Record<string, unknown>): ModelInfo {
  const slug = String(entry.slug);
  let contextWindow = Math.trunc(Number(entry.context_window || 0));
  if (Number.isNaN(contextWindow)) contextWindow = 0;
  const defaultLevel = entry.default_reasoning_level;
  return modelInfo({
    id: `${CODEX_NAMESPACE}${slug}`,
    name: String(entry.display_name || slug),
    contextWindow: contextWindow || FALLBACK_CONTEXT,
    maxOutputTokens: MAX_OUTPUT_TOKENS,
    cacheMode: "implicit",
    supportsTools: true,
    supportsReasoning: Array.isArray(entry.supported_reasoning_levels)
      ? entry.supported_reasoning_levels.length > 0
      : Boolean(entry.supported_reasoning_levels),
    supportsImages: takesImages(entry.input_modalities),
    providerId: "openai-codex",
    isSubscription: true,
    reasoningLevels: parseLevels(entry.supported_reasoning_levels),
    defaultReasoningLevel: defaultLevel ? String(defaultLevel) : null,
  });
}

/** An entry without `input_modalities` predates it, and every Codex model then
 * took images; one with the field but no `image` in it does not. */
function takesImages(raw: unknown): boolean {
  if (raw === null || raw === undefined) return true;
  return Array.isArray(raw) && raw.includes("image");
}

/** The efforts a catalogue entry advertises, in `EFFORT_ORDER` order. */
export function parseLevels(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const named = new Set(
    raw
      .filter((item) => typeof item === "object" && item !== null && item.effort)
      .map((item) => String(item.effort)),
  );
  return EFFORT_ORDER.filter((effort) => named.has(effort));
}

/**
 * The effort to send for `info`, honouring what the model accepts.
 *
 * With nothing requested, the model's own default is used. A requested effort
 * the model does not advertise is lowered to the strongest one it does.
 */
export function resolveEffort(requested: string | null, info: ModelInfo): string | null {
  const levels = info.reasoningLevels;
  if (!requested) return info.defaultReasoningLevel;
  if (levels.length === 0 || levels.includes(requested)) return requested;
  const ceiling = (EFFORT_ORDER as readonly string[]).indexOf(requested);
  // An effort off the known scale: pass it through and let the backend rule.
  if (ceiling < 0) return requested;
  const weaker = levels.filter(
    (level) => (EFFORT_ORDER as readonly string[]).indexOf(level) <= ceiling,
  );
  return weaker.at(-1) ?? (levels[0] as string);
}

function errorMessage(status: number, body: string): string {
  if (status === 401 || status === 403) {
    return (
      `Codex rejected the credential while listing models (HTTP ${status}). ` +
      "Run `hx auth login openai-codex` to sign in again."
    );
  }
  return `Codex model list failed (${status}): ${[...body].slice(0, 200).join("")}`;
}

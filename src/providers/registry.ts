/**
 * Which provider serves a model, and how to build it.
 *
 * Routing is a property of the model id, exactly as in the settings file:
 *
 * - `anthropic/claude-sonnet-4.5` - no known provider namespace, so OpenRouter.
 * - `openai-codex/gpt-5.6-terra` - the ChatGPT subscription route.
 * - `devin/swe-1-6` - the Devin subscription route.
 * - `claude-code/claude-sonnet-5` - a Claude subscription, through the `claude` CLI.
 *
 * There is deliberately no `/route` command and no auto-selection: with two
 * credentials installed, "which one paid for that turn" must be answerable by
 * reading the model id alone.
 */

import * as claudeCodeAuth from "../auth/claude_code.ts";
import type { LoginInteraction } from "../auth/oauth/browser.ts";
import * as codexOAuth from "../auth/oauth/codex.ts";
import * as devinOAuth from "../auth/oauth/devin.ts";
import { type AuthResolver, MissingCredential, missingMessage } from "../auth/resolve.ts";
import { type OAuthCredential, OPENROUTER } from "../auth/store.ts";
import type { Provider } from "./base.ts";
import type { ModelRegistry } from "./models.ts";
import { OpenRouterProvider } from "./openrouter.ts";

export const CODEX = codexOAuth.PROVIDER_ID;
export const DEVIN = devinOAuth.PROVIDER_ID;
export const CLAUDE_CODE = claudeCodeAuth.PROVIDER_ID;

export type AuthKind = "api_key" | "subscription";

type BuildProvider = (
  spec: ProviderSpec,
  resolver: AuthResolver,
  sessionId: string | null,
  models: ModelRegistry | null,
) => Provider;

export interface ProviderSpec {
  readonly id: string;
  readonly label: string;
  readonly kind: AuthKind;
  /** Model-id prefix that selects this provider. `null` means the default. */
  readonly namespace: string | null;
  readonly build: BuildProvider;
  readonly isSubscription: boolean;
}

/**
 * Builders for the routes whose providers live in modules of their own. Each
 * registers here when loaded, so the OpenRouter route never loads the others.
 */
export const BUILDERS: Partial<Record<string, BuildProvider>> = {};

function buildRegistered(
  spec: ProviderSpec,
  ...rest: Parameters<BuildProvider> extends [unknown, ...infer R] ? R : never
): Provider {
  const [resolver] = rest;
  // Checked eagerly so a machine that is not signed in gets the onboarding
  // prompt at startup, not a failed turn a minute later.
  if (!resolver.hasCredential(spec.id))
    throw new MissingCredential(spec.id, missingMessage(spec.id));
  const build = BUILDERS[spec.id];
  if (build === undefined)
    throw new Error(`the ${spec.label} route is not available in this build`);
  return build(spec, ...rest);
}

function spec(
  id: string,
  label: string,
  kind: AuthKind,
  namespace: string | null,
  build: BuildProvider,
): ProviderSpec {
  return { id, label, kind, namespace, build, isSubscription: kind === "subscription" };
}

export const SPECS: readonly ProviderSpec[] = [
  // In the order /login and `hx auth login` offer them.
  spec(CODEX, "OpenAI (ChatGPT Plus/Pro)", "subscription", `${CODEX}/`, buildRegistered),
  spec(
    CLAUDE_CODE,
    "Claude (Pro/Max, via Claude Code)",
    "subscription",
    `${CLAUDE_CODE}/`,
    buildRegistered,
  ),
  // An OpenRouter key never expires, so it is resolved once at construction.
  spec(
    OPENROUTER,
    "OpenRouter API key",
    "api_key",
    null,
    (s, resolver) => new OpenRouterProvider(resolver.resolveStatic(s.id).token),
  ),
  spec(DEVIN, "Devin (personal or Enterprise)", "subscription", `${DEVIN}/`, buildRegistered),
];

export const BY_ID = new Map(SPECS.map((s) => [s.id, s]));
export const DEFAULT_PROVIDER = BY_ID.get(OPENROUTER) as ProviderSpec;

/**
 * The subscription tier behind a stored login, or `null`. Reported, never
 * acted on: the plan does not predict whether the route's models can be called.
 */
export function subscriptionPlan(providerId: string, resolver: AuthResolver): string | null {
  if (providerId === CLAUDE_CODE) {
    const status = claudeCodeAuth.status();
    return status?.loggedIn ? status.plan : null;
  }
  if (providerId !== CODEX) return null;
  const stored = resolver.store.read(providerId);
  return stored?.type === "oauth" ? codexOAuth.planOf(stored) : null;
}

/** Who a stored subscription login belongs to, or `null`. An email or a name, never the token. */
export function subscriptionAccount(providerId: string, resolver: AuthResolver): string | null {
  if (providerId === CLAUDE_CODE) {
    const status = claudeCodeAuth.status();
    return status?.loggedIn ? status.account : null;
  }
  if (providerId !== DEVIN) return null;
  const stored = resolver.store.read(providerId);
  return stored?.type === "oauth" ? devinOAuth.accountOf(stored) : null;
}

/** The browser sign-in for a subscription route, or `null` for a key route. */
export function browserLogin(
  providerId: string,
): ((interaction: LoginInteraction) => Promise<OAuthCredential>) | null {
  if (providerId === CODEX) return codexOAuth.loginBrowser;
  if (providerId === DEVIN) return devinOAuth.loginBrowser;
  return null;
}

/** Whether the route's credential lives with another program, not in HX's store. */
export const holdsOwnLogin = (providerId: string) => providerId === CLAUDE_CODE;

export class UnknownProvider extends Error {
  override name = "UnknownProvider";
}

export function get(providerId: string): ProviderSpec {
  const found = BY_ID.get(providerId);
  if (found === undefined) throw new UnknownProvider(providerId);
  return found;
}

/** `openai-codex/gpt-5.6-terra` -> (codex spec, `gpt-5.6-terra`). An id with no
 * known namespace belongs to OpenRouter, whose own ids are `vendor/model` shaped. */
export function splitModelId(modelId: string): [ProviderSpec, string] {
  for (const s of SPECS) {
    if (s.namespace && modelId.startsWith(s.namespace))
      return [s, modelId.slice(s.namespace.length)];
  }
  return [DEFAULT_PROVIDER, modelId];
}

export const providerFor = (modelId: string) => splitModelId(modelId)[0];

/**
 * Construct the provider that serves `modelId`. Throws `MissingCredential` when
 * that route has no credential, which callers turn into onboarding.
 */
export function buildProvider(
  modelId: string,
  resolver: AuthResolver,
  options: { sessionId?: string | null; models?: ModelRegistry | null } = {},
): Provider {
  const found = providerFor(modelId);
  return found.build(found, resolver, options.sessionId ?? null, options.models ?? null);
}

/** Specs with a usable credential. Drives which models `/model` offers. */
export const available = (resolver: AuthResolver) =>
  SPECS.filter((s) => resolver.hasCredential(s.id));

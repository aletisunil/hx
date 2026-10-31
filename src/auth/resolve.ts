/**
 * Turning a stored credential into request auth.
 *
 * The rule, per provider id:
 *
 * - `openrouter` keeps HX's long-standing precedence - the environment wins over
 *   the saved file. Changing that would silently switch which key an existing
 *   user's session runs on.
 * - Everything else follows the safer order: a stored credential owns the
 *   provider, and the environment is consulted only when nothing is stored. A
 *   *failed* refresh does not quietly fall back to an env var - the user gets
 *   told their login expired instead of a confusing bill.
 * - `claude-code` stores nothing here at all. Its credential is Claude Code's
 *   own login, which the `claude` CLI keeps and refreshes.
 *
 * OAuth tokens are refreshed inside `AuthStore.modify`, so two turns starting at
 * once cannot both spend the same refresh token.
 */

import * as claudeCode from "./claude_code.ts";
import * as codexOAuth from "./oauth/codex.ts";
import * as devinOAuth from "./oauth/devin.ts";
import {
  AuthStore,
  type Credential,
  expired,
  type OAuthCredential,
  OPENROUTER,
  TAVILY,
} from "./store.ts";

/** Environment variables consulted per provider, highest priority first. */
export const PROVIDER_ENV: Record<string, readonly string[]> = {
  [OPENROUTER]: ["HX_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"],
  [TAVILY]: ["HX_TAVILY_API_KEY", "TAVILY_API_KEY"],
  // A Devin session token, for a machine with no browser to sign in from.
  [devinOAuth.PROVIDER_ID]: ["HX_DEVIN_API_KEY", "DEVIN_API_KEY"],
};

/** Providers whose environment variable wins over the saved credential. */
export const ENV_FIRST = new Set([OPENROUTER]);

type Refresher = (credential: OAuthCredential) => Promise<OAuthCredential>;

/** How to renew an expiring OAuth credential, per provider. */
const REFRESHERS: Record<string, Refresher> = {
  [codexOAuth.PROVIDER_ID]: codexOAuth.refresh,
};

/** No usable credential for a provider. Callers turn this into onboarding. */
export class MissingCredential extends Error {
  override name = "MissingCredential";
  constructor(
    readonly providerId: string,
    message: string,
  ) {
    super(message);
  }
}

/** A stored login could not be renewed. The user has to sign in again. */
export class ExpiredCredential extends Error {
  override name = "ExpiredCredential";
  constructor(
    readonly providerId: string,
    message: string,
  ) {
    super(message);
  }
}

/** Everything a provider needs to authenticate one request. */
export interface ResolvedAuth {
  readonly token: string;
  /** Human-readable origin, for `/login` and status output. Never the secret. */
  readonly source: string;
  /** Provider-specific companions to the token, such as a ChatGPT account id. */
  readonly extra: Readonly<Record<string, unknown>>;
}

const envValue = (name: string) => process.env[name] || null;

export class AuthResolver {
  readonly store: AuthStore;

  constructor(store?: AuthStore) {
    this.store = store ?? new AuthStore();
  }

  /** The token to send now, refreshing it first if it is about to expire. */
  async resolve(providerId: string): Promise<ResolvedAuth> {
    if (providerId === claudeCode.PROVIDER_ID) {
      await claudeCode.statusAsync();
      return claudeCodeAuth();
    }
    const [ready, stale] = this.peek(providerId);
    if (ready !== null) return ready;
    if (stale === null) throw new MissingCredential(providerId, missingMessage(providerId));
    const refreshed = await this.refresh(providerId, stale);
    return {
      token: refreshed.access,
      source: `${providerId} login`,
      extra: { ...refreshed.extra },
    };
  }

  /**
   * Synchronous resolution for credentials that cannot expire. An OAuth
   * credential that needs renewing throws rather than blocking.
   */
  resolveStatic(providerId: string): ResolvedAuth {
    if (providerId === claudeCode.PROVIDER_ID) return claudeCodeAuth();
    const [ready, stale] = this.peek(providerId);
    if (ready !== null) return ready;
    if (stale !== null) {
      throw new ExpiredCredential(
        providerId,
        `The ${providerId} login has expired. Run \`hx auth login ${providerId}\`.`,
      );
    }
    throw new MissingCredential(providerId, missingMessage(providerId));
  }

  /** Resolve without any network call: `[usable auth, credential needing
   * refresh]` - exactly one is set when a credential exists at all. */
  private peek(providerId: string): [ResolvedAuth | null, OAuthCredential | null] {
    if (ENV_FIRST.has(providerId)) {
      const fromEnv = this.fromEnv(providerId);
      if (fromEnv !== null) return [fromEnv, null];
    }
    const stored = this.store.read(providerId);
    if (stored?.type === "api_key") {
      return [{ token: stored.key, source: this.store.path, extra: { ...stored.env } }, null];
    }
    if (stored?.type === "oauth") {
      if (expired(stored)) return [null, stored];
      return [
        { token: stored.access, source: `${providerId} login`, extra: { ...stored.extra } },
        null,
      ];
    }
    return [this.fromEnv(providerId), null];
  }

  /** Ask the programs that keep their own login - Claude Code - for its state,
   * without blocking, so the synchronous checks that follow read it from memory. */
  async checkExternalLogins(): Promise<void> {
    await claudeCode.statusAsync();
  }

  /** Whether a route is usable, without refreshing or exposing anything. */
  hasCredential(providerId: string): boolean {
    if (providerId === claudeCode.PROVIDER_ID) return claudeCode.signedIn();
    if (this.store.read(providerId) !== null) return true;
    return (PROVIDER_ENV[providerId] ?? []).some((name) => envValue(name));
  }

  /** Where the active credential comes from, or `null` when there is none.
   * The origin only - never the secret. */
  source(providerId: string): string | null {
    if (providerId === claudeCode.PROVIDER_ID) {
      return claudeCode.signedIn() ? "Claude Code login (`claude auth status`)" : null;
    }
    const names = PROVIDER_ENV[providerId] ?? [];
    if (ENV_FIRST.has(providerId)) {
      for (const name of names) if (envValue(name)) return `environment (${name})`;
    }
    if (this.store.read(providerId) !== null) return this.store.path;
    for (const name of names) if (envValue(name)) return `environment (${name})`;
    return null;
  }

  /** The env var that would override a credential saved right now. */
  shadowedByEnv(providerId: string): string | null {
    if (!ENV_FIRST.has(providerId)) return null;
    return (PROVIDER_ENV[providerId] ?? []).find((name) => envValue(name)) ?? null;
  }

  private fromEnv(providerId: string): ResolvedAuth | null {
    for (const name of PROVIDER_ENV[providerId] ?? []) {
      const value = envValue(name);
      if (value) return { token: value, source: `environment (${name})`, extra: {} };
    }
    return null;
  }

  private async refresh(providerId: string, stored: OAuthCredential): Promise<OAuthCredential> {
    const refresher = REFRESHERS[providerId];
    if (refresher === undefined) {
      throw new ExpiredCredential(
        providerId,
        `The ${providerId} login has expired and cannot be renewed. ` +
          `Run \`hx auth login ${providerId}\`.`,
      );
    }
    const renew = async (current: Credential | null): Promise<Credential | null> => {
      // Another turn may have refreshed while this one waited for the lock;
      // spending the stale refresh token then would revoke the good one.
      if (current?.type === "oauth" && !expired(current)) return null;
      return refresher(current?.type === "oauth" ? current : stored);
    };
    let updated: Credential | null;
    try {
      updated = await this.store.modify(providerId, renew);
    } catch (error) {
      throw new ExpiredCredential(
        providerId,
        `Could not renew the ${providerId} login (${(error as Error).message}). ` +
          `Run \`hx auth login ${providerId}\`.`,
      );
    }
    if (updated?.type !== "oauth") {
      throw new ExpiredCredential(
        providerId,
        `The ${providerId} login is no longer valid. Run \`hx auth login ${providerId}\`.`,
      );
    }
    return updated;
  }
}

/** Claude Code's login, as far as HX sees it: who, never a token. */
function claudeCodeAuth(): ResolvedAuth {
  const current = claudeCode.status();
  if (current === null || !current.isSubscription) {
    throw new MissingCredential(claudeCode.PROVIDER_ID, claudeCode.missingMessage());
  }
  return {
    token: "",
    source: "Claude Code login",
    extra: { account: current.orgId || current.account, email: current.email, plan: current.plan },
  };
}

/** What to tell the user, and what to run, when a route has no credential. */
export function missingMessage(providerId: string): string {
  if (providerId === claudeCode.PROVIDER_ID) return claudeCode.missingMessage();
  if (providerId === OPENROUTER) {
    return (
      "No OpenRouter API key found. Set OPENROUTER_API_KEY, or run `hx` interactively " +
      "to save one. Get a key at https://openrouter.ai/keys"
    );
  }
  if (providerId === codexOAuth.PROVIDER_ID) {
    return (
      "Not signed in to ChatGPT. Run `hx auth login openai-codex`, or `/login` " +
      "inside HX. Requires a ChatGPT Plus or Pro subscription."
    );
  }
  if (providerId === devinOAuth.PROVIDER_ID) {
    return (
      "Not signed in to Devin. Run `hx auth login devin`, or `/login` inside HX. " +
      "Requires a Devin subscription."
    );
  }
  if (providerId === TAVILY) {
    return (
      "No Tavily API key found, so WebSearch and WebFetch are off. Run " +
      "`hx auth set tavily` or set TAVILY_API_KEY. The free tier at " +
      "https://app.tavily.com is 1000 searches a month."
    );
  }
  return `No credential for ${providerId}. Run \`hx auth login ${providerId}\`.`;
}

/**
 * The credential file: `~/.hx/auth.json`.
 *
 * One type-tagged credential per provider id:
 *
 *     {
 *       "openrouter":   {"type": "api_key", "key": "sk-or-..."},
 *       "openai-codex": {"type": "oauth", "access": "...", "refresh": "...",
 *                        "expires": 1757000000.0, "account_id": "..."}
 *     }
 *
 * Earlier versions stored a bare `openrouter_api_key` at the top level. That
 * shape is still read and is rewritten into the tagged form on the next write.
 *
 * Every refresh goes through `AuthStore.modify`, which serialises per provider
 * id. A token refresh that raced a second turn would otherwise burn one of the
 * two refresh tokens and leave the stored one dead.
 */

import { chmodSync, closeSync, mkdirSync, openSync, renameSync, rmSync, writeSync } from "node:fs";
import { compareCodePoints, dumps, PyFloat } from "../json.ts";
import { isFile, parent, readText } from "../pathlib.ts";
import { authFile } from "../paths.ts";

export const LEGACY_OPENROUTER_KEY = "openrouter_api_key";
export const OPENROUTER = "openrouter";
/** Web search. Not a model route - it never appears in the provider registry. */
export const TAVILY = "tavily";

export interface ApiKeyCredential {
  readonly type: "api_key";
  readonly key: string;
  readonly env: Readonly<Record<string, string>>;
}

/** `expires` is epoch seconds, not the milliseconds other harnesses use. */
export interface OAuthCredential {
  readonly type: "oauth";
  readonly access: string;
  readonly refresh: string;
  readonly expires: number;
  readonly extra: Readonly<Record<string, unknown>>;
}

export type Credential = ApiKeyCredential | OAuthCredential;

export const apiKey = (key: string, env: Record<string, string> = {}): ApiKeyCredential => ({
  type: "api_key",
  key,
  env,
});

export const oauth = (
  access: string,
  refresh: string,
  expires: number,
  extra: Record<string, unknown> = {},
): OAuthCredential => ({ type: "oauth", access, refresh, expires, extra });

/** True once the token is inside `leeway` seconds of expiry. Refreshing early
 * is cheap; a token that expires mid-stream is not. */
export function expired(credential: OAuthCredential, leeway = 300): boolean {
  return Date.now() / 1000 >= credential.expires - leeway;
}

function toJson(credential: Credential): Record<string, unknown> {
  if (credential.type === "api_key") {
    const data: Record<string, unknown> = { type: "api_key", key: credential.key };
    if (Object.keys(credential.env).length) data.env = { ...credential.env };
    return data;
  }
  return {
    type: "oauth",
    access: credential.access,
    refresh: credential.refresh,
    expires: new PyFloat(credential.expires),
    ...credential.extra,
  };
}

/** What a credential is, without the secret. For `/login` and status output. */
export interface CredentialInfo {
  readonly providerId: string;
  readonly type: "api_key" | "oauth";
  readonly expires: number | null;
}

/**
 * Reads and writes the credential file.
 *
 * Reads hit the disk every time rather than caching: `hx auth login` in one
 * terminal must be visible to a session already running in another.
 */
export class AuthStore {
  private readonly locks = new Map<string, Promise<unknown>>();

  constructor(private readonly fixedPath: string | null = null) {}

  /** Resolved late, so `$HX_HOME` is read when the file is. */
  get path(): string {
    return this.fixedPath ?? authFile();
  }

  read(providerId: string): Credential | null {
    return this.load().get(providerId) ?? null;
  }

  list(): CredentialInfo[] {
    return [...this.load()]
      .map(([providerId, credential]) => ({
        providerId,
        type: credential.type,
        expires: credential.type === "oauth" ? credential.expires : null,
      }))
      .sort((a, b) => compareCodePoints(a.providerId, b.providerId));
  }

  save(providerId: string, credential: Credential): void {
    const data = this.load();
    data.set(providerId, credential);
    this.write(data);
  }

  /** Returns whether anything was removed. */
  delete(providerId: string): boolean {
    const data = this.load();
    if (!data.delete(providerId)) return false;
    this.write(data);
    return true;
  }

  /**
   * Serialised read-modify-write.
   *
   * `fn` receives the current credential and returns the replacement, or
   * `null` to leave it alone. It sees the current value because the correct new
   * value depends on it - a refresh needs the refresh token that is on disk
   * now, not the one this process read a minute ago.
   */
  async modify(
    providerId: string,
    fn: (current: Credential | null) => Promise<Credential | null>,
  ): Promise<Credential | null> {
    const previous = this.locks.get(providerId) ?? Promise.resolve();
    const run = previous
      .catch(() => undefined)
      .then(async () => {
        const current = this.read(providerId);
        const updated = await fn(current);
        if (updated === null) return current;
        this.save(providerId, updated);
        return updated;
      });
    this.locks.set(providerId, run);
    return run;
  }

  private load(): Map<string, Credential> {
    const path = this.path;
    if (!isFile(path)) return new Map();
    let raw: unknown;
    try {
      raw = JSON.parse(readText(path));
    } catch {
      return new Map();
    }
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return new Map();
    return migrate(raw as Record<string, unknown>);
  }

  private write(data: Map<string, Credential>): void {
    const path = this.path;
    mkdirSync(parent(path), { recursive: true });
    const payload: Record<string, unknown> = {};
    for (const providerId of [...data.keys()].sort(compareCodePoints)) {
      payload[providerId] = toJson(data.get(providerId) as Credential);
    }

    // Created 0600, not chmod'd to it afterwards: there is no instant at which
    // the descriptor exists and the permissions are wrong.
    const tmp = `${path.replace(/\.[^./]*$/, "")}.json.tmp`;
    const descriptor = openSync(tmp, "w", 0o600);
    try {
      writeSync(descriptor, dumps(payload, { indent: 2 }));
    } catch (error) {
      closeSync(descriptor);
      rmSync(tmp, { force: true });
      throw error;
    }
    closeSync(descriptor);
    // An existing file keeps its mode through O_CREAT, so a store written by an
    // older HX is narrowed here rather than keeping its old permissions.
    chmodSync(tmp, 0o600);
    renameSync(tmp, path);
  }
}

/**
 * Parse the file, folding the legacy flat key into the tagged form.
 *
 * Unparseable entries are dropped rather than raising: one corrupt provider
 * entry must not lock the user out of every other provider.
 */
function migrate(raw: Record<string, unknown>): Map<string, Credential> {
  const creds = new Map<string, Credential>();
  const legacy = raw[LEGACY_OPENROUTER_KEY];
  if (typeof legacy === "string" && legacy) creds.set(OPENROUTER, apiKey(legacy));

  for (const [providerId, entry] of Object.entries(raw)) {
    if (providerId === LEGACY_OPENROUTER_KEY) continue;
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) continue;
    const parsed = parse(entry as Record<string, unknown>);
    // A tagged entry wins over the legacy key it supersedes.
    if (parsed !== null) creds.set(providerId, parsed);
  }
  return creds;
}

function parse(entry: Record<string, unknown>): Credential | null {
  if (entry.type === "api_key") {
    const key = entry.key;
    if (typeof key !== "string" || !key) return null;
    const env = entry.env;
    return apiKey(
      key,
      typeof env === "object" && env !== null && !Array.isArray(env)
        ? (env as Record<string, string>)
        : {},
    );
  }
  if (entry.type === "oauth") {
    const { access, refresh } = entry;
    if (typeof access !== "string" || typeof refresh !== "string") return null;
    let expires = Number(entry.expires ?? 0);
    if (typeof entry.expires === "boolean") expires = entry.expires ? 1 : 0;
    if (Number.isNaN(expires)) expires = 0;
    const extra: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(entry)) {
      if (!["type", "access", "refresh", "expires"].includes(key)) extra[key] = value;
    }
    return oauth(access, refresh, expires, extra);
  }
  return null;
}

/** Rotate the tokens, keeping provider-specific `extra` fields. */
export function withTokens(
  credential: OAuthCredential,
  tokens: { access: string; refresh: string; expires: number },
): OAuthCredential {
  return { ...credential, ...tokens };
}

/** A fragment safe to display. Never the whole thing. */
export function mask(secret: string): string {
  const chars = [...secret];
  if (chars.length <= 12) return "…";
  return `${chars.slice(0, 6).join("")}…${chars.slice(-4).join("")}`;
}

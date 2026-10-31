/**
 * OAuth for remote MCP servers - the client half of the MCP authorization spec.
 *
 * A server that wants a signed-in user answers an unauthenticated request with
 * `401` and a `WWW-Authenticate: Bearer resource_metadata="..."` header. From
 * there the steps are all standard:
 *
 * 1. Protected Resource Metadata (RFC 9728) names the authorization server.
 * 2. Authorization Server Metadata (RFC 8414, or OpenID discovery) names its
 *    endpoints, path-aware for issuers with a path.
 * 3. Dynamic Client Registration (RFC 7591) gets HX a client id, unless
 *    `oauth.clientId` in `mcp.json` names one.
 * 4. Authorization code with PKCE (S256), through the same loopback callback
 *    and paste fallback the model routes use, with `resource` (RFC 8707).
 *
 * Tokens live in `auth.json` under `mcp:<resource url>`, keyed by the server
 * rather than by its name in `mcp.json`.
 */

import { createServer } from "node:net";
import { authorizeInBrowser, type LoginInteraction, OAuthError } from "../auth/oauth/browser.ts";
import { callbackHost } from "../auth/oauth/callback.ts";
import { generatePkce, randomState } from "../auth/oauth/pkce.ts";
import { AuthStore, type Credential, expired, type OAuthCredential, oauth } from "../auth/store.ts";
import { dumps } from "../json.ts";
import { describe, request } from "../net.ts";
import { rstrip, splitWhitespace, urlencode } from "../py.ts";
import { VERSION } from "../version.ts";

export const CREDENTIAL_PREFIX = "mcp:";
export const CALLBACK_PATH = "/callback";
const LOOPBACK = "127.0.0.1";
const HTTP_TIMEOUT = 30;
const CLIENT_NAME = "HX";
const CLIENT_URI = "https://hx.sunilaleti.dev";

/** For a token response with no `expires_in`. Short on purpose: guessing low
 * costs one early refresh, guessing high costs a request that fails first. */
const DEFAULT_LIFETIME = 3600;

/** How long before expiry a token is refreshed, for a token that lives long
 * enough to spare it; one that does not is refreshed at half its life. */
const REFRESH_MARGIN = 300;

/** What an unauthenticated probe sends: the one request a server is
 * guaranteed to route rather than 404. */
const PROBE_MESSAGE = {
  jsonrpc: "2.0",
  id: 0,
  method: "initialize",
  params: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "hx", version: VERSION },
  },
};

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const strOrNull = (value: unknown): string | null =>
  typeof value === "string" && value ? value : null;

/** The `oauth` block of an `mcp.json` entry. Everything is optional. */
export interface OAuthOptions {
  /** A pre-registered client, for a server without dynamic registration. */
  readonly clientId: string | null;
  readonly clientSecret: string | null;
  /** Overrides the scopes the server advertises, space-separated. */
  readonly scope: string | null;
  /** A fixed loopback port, for a client registered with one redirect URI. */
  readonly callbackPort: number | null;
}

export const noOptions = (): OAuthOptions => ({
  clientId: null,
  clientSecret: null,
  scope: null,
  callbackPort: null,
});

export function optionsFromJson(raw: unknown): OAuthOptions {
  if (!isObject(raw)) return noOptions();
  const port = raw.callbackPort;
  const digits =
    (typeof port === "number" && Number.isInteger(port) && port >= 0) ||
    (typeof port === "string" && /^\d+$/.test(port));
  return {
    clientId: strOrNull(raw.clientId),
    clientSecret: strOrNull(raw.clientSecret),
    scope: strOrNull(raw.scope),
    // `int(0)` is a port of 0, which every use then treats as none.
    callbackPort: digits ? Number(port) : null,
  };
}

export function optionsToJson(options: OAuthOptions): Json {
  const data: Json = {};
  if (options.clientId) data.clientId = options.clientId;
  if (options.clientSecret) data.clientSecret = options.clientSecret;
  if (options.scope) data.scope = options.scope;
  if (options.callbackPort) data.callbackPort = options.callbackPort;
  return data;
}

/** The parameters of a `WWW-Authenticate: Bearer` challenge. */
export interface Challenge {
  readonly resourceMetadata: string | null;
  readonly scope: string | null;
  readonly error: string | null;
}

const emptyChallenge = (): Challenge => ({ resourceMetadata: null, scope: null, error: null });

export interface AuthServer {
  readonly issuer: string;
  readonly authorizationEndpoint: string;
  readonly tokenEndpoint: string;
  readonly registrationEndpoint: string | null;
  readonly codeChallengeMethods: readonly string[];
}

export interface Discovery {
  /** What the token is minted for, sent as RFC 8707 `resource`. */
  readonly resource: string;
  readonly server: AuthServer;
  readonly scope: string | null;
}

export interface ClientInfo {
  readonly clientId: string;
  readonly clientSecret: string | null;
  /** `none`, `client_secret_post` or `client_secret_basic`. */
  readonly authMethod: string;
  readonly redirectUri: string;
}

/** The server answered without asking for a token. */
export class SignInNotRequired extends OAuthError {
  override name = "SignInNotRequired";
}

/** The token endpoint refused a grant - a dead refresh token, usually. This
 * one means signing in again; a network failure means trying again. */
export class TokenRejected extends OAuthError {
  override name = "TokenRejected";
}

// -- identifiers ---------------------------------------------------------------

/** `urllib.parse.urlsplit`, as far as HX reads it. */
export function urlsplit(url: string): {
  scheme: string;
  netloc: string;
  path: string;
  query: string;
  fragment: string;
} {
  const match =
    /^(?:([A-Za-z][A-Za-z0-9+.-]*):)?(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/s.exec(
      url,
    );
  return {
    scheme: (match?.[1] ?? "").toLowerCase(),
    netloc: match?.[2] ?? "",
    path: match?.[3] ?? "",
    query: match?.[4] ?? "",
    fragment: match?.[5] ?? "",
  };
}

function urlunsplit(scheme: string, netloc: string, path: string): string {
  let url = path;
  if (netloc || (scheme && path.startsWith("//"))) {
    if (url && !url.startsWith("/")) url = `/${url}`;
    url = `//${netloc}${url}`;
  }
  return scheme ? `${scheme}:${url}` : url;
}

/** The port of a URL's netloc, as `urlsplit(...).port` reads it. */
function portOf(url: string): number | null {
  const host = urlsplit(url).netloc.split("@").at(-1) as string;
  const match = /:(\d+)$/.exec(host.replace(/^\[[^\]]*\]/, ""));
  return match ? Number(match[1]) : null;
}

/** The server's identity: scheme, host and path, with no query or fragment.
 * `?tools=all` changes what a server shows, not which server it is. */
export function canonicalResource(url: string): string {
  const parts = urlsplit(url);
  const path = parts.path === "" || parts.path === "/" ? "" : rstrip(parts.path, "/");
  return urlunsplit(parts.scheme.toLowerCase(), parts.netloc.toLowerCase(), path);
}

export const credentialId = (url: string) => CREDENTIAL_PREFIX + canonicalResource(url);

// -- WWW-Authenticate ------------------------------------------------------------

const PARAM = /([A-Za-z0-9_-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s,]+)/g;

/** The Bearer challenge in a `WWW-Authenticate` header, or `null`. Only the
 * Bearer challenge's parameters are read. */
export function parseWwwAuthenticate(value: string | null | undefined): Challenge | null {
  if (!value) return null;
  const match = /(?:^|[\s,])Bearer(?:\s+|$)/i.exec(value);
  if (match === null) return null;
  let rest = value.slice(match.index + match[0].length);
  // Stop at the next challenge: a scheme name followed by a space, not an `=`.
  const next = /,\s*[A-Za-z][A-Za-z0-9_-]*\s+[A-Za-z0-9_-]+\s*=/.exec(rest);
  if (next !== null) rest = rest.slice(0, next.index);
  const params = new Map<string, string>();
  for (const [, key, raw] of rest.matchAll(PARAM)) {
    let text = raw as string;
    if (text.startsWith('"')) text = text.slice(1, -1).replace(/\\(.)/gs, "$1");
    params.set((key as string).toLowerCase(), text);
  }
  return {
    resourceMetadata: params.get("resource_metadata") ?? null,
    scope: params.get("scope") ?? null,
    error: params.get("error") ?? null,
  };
}

// -- HTTP --------------------------------------------------------------------------

interface Reply {
  readonly status: number;
  readonly headers: Record<string, string | string[] | undefined>;
  readonly text: string;
}

/** A network failure, as distinct from an answer the server gave. */
export class HttpFailure extends Error {
  override name = "HTTPError";
}

async function fetchText(
  url: string,
  options: { method?: string; headers?: Record<string, string>; body?: string },
): Promise<Reply> {
  try {
    const response = await request(url, { ...options, timeout: HTTP_TIMEOUT });
    try {
      return { status: response.status, headers: response.headers, text: await response.text() };
    } finally {
      response.close();
    }
  } catch (error) {
    throw new HttpFailure(describe(error));
  }
}

const postJson = (url: string, body: unknown, headers: Record<string, string> = {}) =>
  fetchText(url, {
    method: "POST",
    body: dumps(body, { separators: [",", ":"], ensureAscii: false }),
    headers: { "Content-Type": "application/json", ...headers },
  });

const head = (text: string, limit: number) => [...text].slice(0, limit).join("");

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

// -- discovery -------------------------------------------------------------------

/** Ask the server unauthenticated. `null` when it did not refuse. */
export async function probe(url: string): Promise<Challenge | null> {
  const response = await postJson(url, PROBE_MESSAGE, {
    Accept: "application/json, text/event-stream",
  });
  if (response.status !== 401) return null;
  const value = response.headers["www-authenticate"];
  return parseWwwAuthenticate(Array.isArray(value) ? value.join(", ") : value) ?? emptyChallenge();
}

/** Find the authorization server for `url` and what to ask it for. */
export async function discover(
  url: string,
  challenge: Challenge,
  options: OAuthOptions = noOptions(),
): Promise<Discovery> {
  let resource = canonicalResource(url);
  const metadata = await protectedResourceMetadata(url, challenge);
  let scope = options.scope ?? challenge.scope;
  let server: AuthServer;
  if (metadata !== null) {
    const advertised = metadata.resource;
    if (typeof advertised === "string" && advertised) {
      // A server naming some other resource as its own would have the token
      // minted for that one - and sent there next.
      if (!covers(advertised, resource))
        throw new OAuthError(
          `${url} claims to be ${advertised}; refusing to sign in to a server that names a different resource.`,
        );
      resource = advertised;
    }
    const servers = Array.isArray(metadata.authorization_servers)
      ? metadata.authorization_servers
      : [];
    const issuer = servers.find((s): s is string => typeof s === "string" && s.length > 0);
    if (issuer === undefined) throw new OAuthError(`${url} names no authorization server.`);
    if (scope === null) {
      const scopes = metadata.scopes_supported;
      if (Array.isArray(scopes) && scopes.length) scope = scopes.map(String).join(" ");
    }
    const found = await authorizationServer(issuer);
    if (found === null) throw new OAuthError(`Could not read the metadata of ${issuer}.`);
    server = found;
  } else {
    // A server from before RFC 9728 is its own authorization server, at fixed
    // paths when it publishes no metadata.
    const origin = originOf(url);
    server = (await authorizationServer(origin)) ?? {
      issuer: origin,
      authorizationEndpoint: `${origin}/authorize`,
      tokenEndpoint: `${origin}/token`,
      registrationEndpoint: `${origin}/register`,
      codeChallengeMethods: [],
    };
  }
  if (server.codeChallengeMethods.length && !server.codeChallengeMethods.includes("S256"))
    throw new OAuthError(`${server.issuer} does not support PKCE with S256; refusing to sign in.`);
  return { resource, server, scope };
}

async function protectedResourceMetadata(url: string, challenge: Challenge): Promise<Json | null> {
  let candidates: string[];
  if (challenge.resourceMetadata) candidates = [challenge.resourceMetadata];
  else {
    const origin = originOf(url);
    const path = rstrip(urlsplit(url).path, "/");
    candidates = path ? [`${origin}/.well-known/oauth-protected-resource${path}`] : [];
    candidates.push(`${origin}/.well-known/oauth-protected-resource`);
  }
  for (const candidate of candidates) {
    const document = await getJson(candidate);
    if (document !== null) return document;
  }
  return null;
}

async function authorizationServer(issuer: string): Promise<AuthServer | null> {
  const origin = originOf(issuer);
  const path = rstrip(urlsplit(issuer).path, "/");
  const candidates = path
    ? [
        `${origin}/.well-known/oauth-authorization-server${path}`,
        `${origin}/.well-known/openid-configuration${path}`,
        `${origin}${path}/.well-known/openid-configuration`,
        `${origin}${path}/.well-known/oauth-authorization-server`,
      ]
    : [
        `${origin}/.well-known/oauth-authorization-server`,
        `${origin}/.well-known/openid-configuration`,
      ];
  let impostor: string | null = null;
  for (const candidate of candidates) {
    const document = await getJson(candidate);
    if (document === null) continue;
    const authorize = document.authorization_endpoint;
    const token = document.token_endpoint;
    if (typeof authorize !== "string" || typeof token !== "string") continue;
    const claimed = strOrNull(document.issuer) ?? issuer;
    if (rstrip(claimed, "/") !== rstrip(issuer, "/")) {
      // RFC 8414 section 3.3: metadata naming another issuer must not be
      // used - the mix-up attack. Another path may still hold the right one.
      impostor ??= `The authorization server metadata at ${candidate} names ${claimed} as its issuer, not ${issuer}; refusing to sign in.`;
      continue;
    }
    const methods = document.code_challenge_methods_supported;
    return {
      issuer: claimed,
      authorizationEndpoint: authorize,
      tokenEndpoint: token,
      registrationEndpoint: strOrNull(document.registration_endpoint),
      codeChallengeMethods: Array.isArray(methods) ? methods.map(String) : [],
    };
  }
  if (impostor !== null) throw new OAuthError(impostor);
  return null;
}

async function getJson(url: string): Promise<Json | null> {
  let response: Reply;
  try {
    response = await fetchText(url, { headers: { Accept: "application/json" } });
  } catch {
    return null;
  }
  if (response.status !== 200) return null;
  const document = parseJson(response.text);
  return isObject(document) ? document : null;
}

// -- registration and the grant ---------------------------------------------------

export async function register(
  server: AuthServer,
  redirectUri: string,
  scope: string | null,
): Promise<ClientInfo> {
  const endpoint = server.registrationEndpoint;
  if (!endpoint)
    throw new OAuthError(
      `${server.issuer} does not offer dynamic client registration. Register a client with it and set "oauth": {"clientId": "..."} on this server in mcp.json.`,
    );
  const body: Json = {
    client_name: CLIENT_NAME,
    client_uri: CLIENT_URI,
    redirect_uris: [redirectUri],
    grant_types: ["authorization_code", "refresh_token"],
    response_types: ["code"],
    token_endpoint_auth_method: "none",
  };
  if (scope) body.scope = scope;
  const response = await postJson(endpoint, body);
  if (response.status >= 400)
    throw new OAuthError(
      `Client registration at ${endpoint} failed (${response.status}): ${head(response.text, 300)}`,
    );
  const data = parseJson(response.text);
  if (data === undefined)
    throw new OAuthError(
      `Client registration at ${endpoint} answered ${response.status} with a body that is not JSON: ${head(splitWhitespace(response.text).join(" "), 200)}`,
    );
  const clientId = isObject(data) ? data.client_id : null;
  if (typeof clientId !== "string" || !clientId)
    throw new OAuthError(`${endpoint} returned no client_id.`);
  const secret = strOrNull((data as Json).client_secret);
  const method =
    strOrNull((data as Json).token_endpoint_auth_method) ??
    (secret ? "client_secret_basic" : "none");
  return { clientId, clientSecret: secret, authMethod: method, redirectUri };
}

export function authorizeUrl(
  discovery: Discovery,
  client: ClientInfo,
  challenge: string,
  state: string,
): string {
  const params: Record<string, string> = {
    response_type: "code",
    client_id: client.clientId,
    redirect_uri: client.redirectUri,
    code_challenge: challenge,
    code_challenge_method: "S256",
    state,
    resource: discovery.resource,
  };
  if (discovery.scope) params.scope = discovery.scope;
  const endpoint = discovery.server.authorizationEndpoint;
  return `${endpoint}${endpoint.includes("?") ? "&" : "?"}${urlencode(params)}`;
}

async function tokenRequest(
  endpoint: string,
  given: Record<string, string>,
  client: ClientInfo,
): Promise<Json> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "Content-Type": "application/x-www-form-urlencoded",
  };
  const form = { ...given };
  if (client.authMethod === "client_secret_basic" && client.clientSecret) {
    const pair = Buffer.from(`${client.clientId}:${client.clientSecret}`).toString("base64");
    headers.Authorization = `Basic ${pair}`;
  } else {
    form.client_id = client.clientId;
    if (client.clientSecret) form.client_secret = client.clientSecret;
  }
  const response = await fetchText(endpoint, { method: "POST", body: urlencode(form), headers });
  const parsed = parseJson(response.text);
  const data = parsed === undefined ? {} : parsed;
  if (response.status >= 400) {
    const error = isObject(data) ? data.error : null;
    const detail = (isObject(data) ? data.error_description : null) || "";
    let message = `${endpoint} refused the grant (${response.status}`;
    message += error ? `, ${String(error)}): ${String(detail)}` : `): ${head(response.text, 300)}`;
    throw new TokenRejected(rstrip(message, ": "));
  }
  if (!isObject(data) || typeof data.access_token !== "string")
    throw new OAuthError(`${endpoint} returned no access_token.`);
  return data;
}

function credentialFrom(
  token: Json,
  serverUrl: string,
  discovery: Discovery,
  client: ClientInfo,
  previousRefresh = "",
): OAuthCredential {
  let lifetime = Number(token.expires_in || DEFAULT_LIFETIME);
  if (!Number.isFinite(lifetime)) lifetime = DEFAULT_LIFETIME;
  const extra: Json = {
    lifetime,
    server_url: serverUrl,
    resource: discovery.resource,
    issuer: discovery.server.issuer,
    token_endpoint: discovery.server.tokenEndpoint,
    client_id: client.clientId,
    auth_method: client.authMethod,
    redirect_uri: client.redirectUri,
  };
  if (client.clientSecret) extra.client_secret = client.clientSecret;
  if (discovery.scope) extra.scope = discovery.scope;
  // A server that does not rotate refresh tokens leaves the field out; the
  // one it issued first is still the one to use.
  return oauth(
    String(token.access_token),
    String(token.refresh_token || previousRefresh),
    Date.now() / 1000 + lifetime,
    extra,
  );
}

// -- the two entry points ----------------------------------------------------------

/**
 * Run the browser sign-in for the server at `url`. Does not save. Throws
 * `SignInNotRequired` when the server answers without a token, `OAuthError`
 * when discovery, registration or the grant fails, and `CallbackError` when
 * the browser step fails or is abandoned.
 */
export async function login(
  url: string,
  interaction: LoginInteraction,
  options: OAuthOptions = noOptions(),
  label = "",
  store: AuthStore = new AuthStore(),
): Promise<OAuthCredential> {
  interaction.progress(`Looking up how ${label || url} signs in…`);
  let discovery: Discovery;
  let client: ClientInfo;
  try {
    const challenge = await probe(url);
    if (challenge === null)
      throw new SignInNotRequired(`${label || url} does not ask for sign-in.`);
    discovery = await discover(url, challenge, options);
    client = await clientFor(url, discovery, options, store);
  } catch (error) {
    if (error instanceof HttpFailure)
      throw new OAuthError(`Could not reach ${url}: ${error.message}`);
    throw error;
  }
  const pkce = generatePkce();
  const state = randomState();
  const result = await authorizeInBrowser(authorizeUrl(discovery, client, pkce.challenge, state), {
    port: redirectPort(client.redirectUri),
    path: CALLBACK_PATH,
    state,
    interaction,
  });
  interaction.progress("Exchanging the authorization code…");
  let token: Json;
  try {
    token = await tokenRequest(
      discovery.server.tokenEndpoint,
      {
        grant_type: "authorization_code",
        code: result.code,
        redirect_uri: client.redirectUri,
        code_verifier: pkce.verifier,
        resource: discovery.resource,
      },
      client,
    );
  } catch (error) {
    if (error instanceof HttpFailure)
      throw new OAuthError(`Could not reach ${discovery.server.tokenEndpoint}: ${error.message}`);
    throw error;
  }
  return credentialFrom(token, url, discovery, client);
}

/** Trade the refresh token for a new pair, from what the credential recorded.
 * Throws `TokenRejected` when it is no longer accepted, `HttpFailure` when the
 * token endpoint cannot be reached. */
export async function refresh(credential: OAuthCredential): Promise<OAuthCredential> {
  const extra = credential.extra;
  const endpoint = String(extra.token_endpoint || "");
  if (!credential.refresh || !endpoint)
    throw new TokenRejected("There is no refresh token to use.");
  const client: ClientInfo = {
    clientId: String(extra.client_id || ""),
    clientSecret: strOrNull(extra.client_secret),
    authMethod: String(extra.auth_method || "none"),
    redirectUri: String(extra.redirect_uri || ""),
  };
  const form: Record<string, string> = {
    grant_type: "refresh_token",
    refresh_token: credential.refresh,
  };
  if (extra.resource) form.resource = String(extra.resource);
  const token = await tokenRequest(endpoint, form, client);
  const refreshed = credentialFrom(
    token,
    String(extra.server_url || ""),
    {
      resource: String(extra.resource || ""),
      server: {
        issuer: String(extra.issuer || ""),
        authorizationEndpoint: "",
        tokenEndpoint: endpoint,
        registrationEndpoint: null,
        codeChallengeMethods: [],
      },
      scope: strOrNull(extra.scope),
    },
    client,
    credential.refresh,
  );
  // Fields this module did not write are carried over rather than dropped.
  return { ...refreshed, extra: { ...extra, ...refreshed.extra } };
}

/**
 * The client to sign in as: configured, reused from last time, or new. A
 * registration is reused while its loopback port is free, so signing in again
 * does not leave a trail of dead clients on the server.
 */
async function clientFor(
  url: string,
  discovery: Discovery,
  options: OAuthOptions,
  store: AuthStore,
): Promise<ClientInfo> {
  if (options.clientId) {
    const port = options.callbackPort || (await freePort());
    return {
      clientId: options.clientId,
      clientSecret: options.clientSecret,
      authMethod: options.clientSecret ? "client_secret_post" : "none",
      redirectUri: redirectUriFor(port),
    };
  }
  const previous = store.read(credentialId(url));
  if (previous?.type === "oauth") {
    const extra = previous.extra;
    const redirect = String(extra.redirect_uri || "");
    if (
      extra.issuer === discovery.server.issuer &&
      extra.client_id &&
      redirect &&
      (options.callbackPort === null || options.callbackPort === redirectPort(redirect)) &&
      (await portIsFree(redirectPort(redirect)))
    )
      return {
        clientId: String(extra.client_id),
        clientSecret: strOrNull(extra.client_secret),
        authMethod: String(extra.auth_method || "none"),
        redirectUri: redirect,
      };
  }
  const port = options.callbackPort || (await freePort());
  return register(discovery.server, redirectUriFor(port), discovery.scope);
}

// -- the token a transport sends -----------------------------------------------------

/**
 * Supplies one server's bearer token, refreshing it when it runs out. Reads
 * the store rather than holding the token: a sign-in in another terminal has
 * to be picked up without a restart.
 */
export class OAuthSession {
  private cached: OAuthCredential | null = null;

  constructor(
    readonly url: string,
    readonly store: AuthStore = new AuthStore(),
  ) {}

  get key(): string {
    return credentialId(this.url);
  }

  /** The access token to send, or `null` when never signed in. */
  async token(): Promise<string | null> {
    let credential = this.cached;
    if (credential === null || stale(credential)) credential = this.load();
    if (credential === null) return null;
    if (stale(credential) && credential.refresh)
      credential = (await this.refreshFrom(credential.access)) ?? credential;
    return credential.access;
  }

  /** After a 401: a token other than `rejected`, or `null`. Another process
   * may already hold a fresher one, which is checked first. */
  async recover(rejected: string | null): Promise<string | null> {
    const credential = this.load();
    if (credential === null) return null;
    if (credential.access !== rejected) return credential.access;
    const refreshed = await this.refreshFrom(rejected);
    return refreshed !== null ? refreshed.access : null;
  }

  private load(): OAuthCredential | null {
    const credential = this.store.read(this.key);
    this.cached = credential?.type === "oauth" ? credential : null;
    return this.cached;
  }

  /** Refresh under the store's lock. `null` when the refresh token is dead.
   * The check against `stale` is repeated inside the lock, so a request that
   * queued behind a refresh uses its result. */
  private async refreshFrom(staleToken: string | null): Promise<OAuthCredential | null> {
    const result = await this.store.modify(this.key, async (current: Credential | null) => {
      if (current?.type !== "oauth" || current.access !== staleToken) return null;
      try {
        return await refresh(current);
      } catch (error) {
        if (error instanceof TokenRejected) return null;
        throw error;
      }
    });
    if (result?.type !== "oauth" || result.access === staleToken) return null;
    this.cached = result;
    return result;
  }
}

// -- helpers --------------------------------------------------------------------

/** Whether to refresh before sending: inside the margin of expiry, or past
 * half its life if it was issued for less than twice that. */
function stale(credential: OAuthCredential): boolean {
  let lifetime = Number(credential.extra.lifetime || 0);
  if (!Number.isFinite(lifetime)) lifetime = 0;
  const margin = lifetime > 0 ? Math.min(REFRESH_MARGIN, lifetime / 2) : REFRESH_MARGIN;
  return expired(credential, margin);
}

function originOf(url: string): string {
  const parts = urlsplit(url);
  return urlunsplit(parts.scheme, parts.netloc, "");
}

/** Whether a token for `advertised` is a token for `resource`. */
function covers(advertised: string, resource: string): boolean {
  const a = urlsplit(canonicalResource(advertised));
  const r = urlsplit(resource);
  if (a.scheme !== r.scheme || a.netloc !== r.netloc) return false;
  return r.path === a.path || r.path.startsWith(`${rstrip(a.path, "/")}/`) || !a.path;
}

/** The loopback literal, not "localhost" (RFC 8252 section 7.3). */
const redirectUriFor = (port: number) => `http://${LOOPBACK}:${port}${CALLBACK_PATH}`;

const redirectPort = (redirectUri: string) => portOf(redirectUri) || 80;

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, callbackHost(), () => {
      const address = server.address();
      const port = typeof address === "object" && address !== null ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

/** Whether the callback could bind `port`, asked the way it binds. */
function portIsFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = createServer();
    server.once("error", () => resolve(false));
    server.listen(port, callbackHost(), () => server.close(() => resolve(true)));
  });
}

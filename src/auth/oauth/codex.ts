/**
 * ChatGPT Plus/Pro sign-in for the Codex backend.
 *
 * OpenAI endorses subscription use by third-party harnesses, so this is the
 * supported path rather than a workaround: a normal PKCE authorization-code flow
 * against `auth.openai.com`, with a device-code variant for machines that have
 * no browser.
 *
 * The account id the Codex backend requires is not returned by the token
 * endpoint - it is a claim inside the access token, so it is decoded here and
 * kept alongside the credential. The subscription plan rides along for the same
 * reason.
 */

import { request } from "../../net.ts";
import { toFloat, urlencode } from "../../py.ts";
import { type OAuthCredential, oauth } from "../store.ts";
import { authorizeInBrowser, type LoginInteraction, OAuthError } from "./browser.ts";
import { generatePkce, jwtClaims, type PKCE, randomState } from "./pkce.ts";

export const PROVIDER_ID = "openai-codex";
export const CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann";
const AUTH_BASE = "https://auth.openai.com";
const AUTHORIZE_URL = `${AUTH_BASE}/oauth/authorize`;
const TOKEN_URL = `${AUTH_BASE}/oauth/token`;
const CALLBACK_PORT = 1455;
const CALLBACK_PATH = "/auth/callback";
const REDIRECT_URI = `http://localhost:${CALLBACK_PORT}${CALLBACK_PATH}`;
const SCOPE = "openid profile email offline_access";
const DEVICE_USER_CODE_URL = `${AUTH_BASE}/api/accounts/deviceauth/usercode`;
const DEVICE_TOKEN_URL = `${AUTH_BASE}/api/accounts/deviceauth/token`;
const DEVICE_VERIFICATION_URI = `${AUTH_BASE}/codex/device`;
const DEVICE_REDIRECT_URI = `${AUTH_BASE}/deviceauth/callback`;
const DEVICE_TIMEOUT_SECONDS = 15 * 60;

/** The access token is a JWT; the account id lives under this namespaced claim. */
const JWT_CLAIM = "https://api.openai.com/auth";

/** The one plan known to complete the OAuth flow and then have every Codex
 * model rejected. */
export const FREE_PLAN = "free";

/** A plan name the token did not carry. Treated as entitled. */
export const UNKNOWN_PLAN = "unknown";

const ORIGINATOR = "hx";
const HTTP_TIMEOUT = 30;

export function authorizeUrl(pkce: PKCE, state: string, originator = ORIGINATOR): string {
  return `${AUTHORIZE_URL}?${urlencode({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    scope: SCOPE,
    code_challenge: pkce.challenge,
    code_challenge_method: pkce.method,
    state,
    id_token_add_organizations: "true",
    codex_cli_simplified_flow: "true",
    originator,
  })}`;
}

function authClaim(token: string): Record<string, unknown> | null {
  const claims = jwtClaims(token);
  if (claims === null) return null;
  const claim = claims[JWT_CLAIM];
  return typeof claim === "object" && claim !== null ? (claim as Record<string, unknown>) : {};
}

/** Pull `chatgpt_account_id` out of the access token. The Codex backend
 * rejects requests without it, so failing here beats failing on the first turn. */
export function accountIdFromToken(accessToken: string): string {
  if (accessToken.split(".").length !== 3) {
    throw new OAuthError("Access token is not a JWT; cannot read the ChatGPT account id.");
  }
  const claim = authClaim(accessToken);
  if (claim === null) throw new OAuthError("Could not decode the access token payload.");
  const accountId = claim.chatgpt_account_id;
  if (typeof accountId !== "string" || !accountId) {
    throw new OAuthError(
      "No ChatGPT account id in the token. Is this account on a Plus or Pro plan?",
    );
  }
  return accountId;
}

/** The ChatGPT plan the token was minted for, lowercased; `unknown` when the
 * claim is absent or the token is not a readable JWT. Never throws. */
export function planFromToken(accessToken: string): string {
  const plan = authClaim(accessToken)?.chatgpt_plan_type;
  return typeof plan === "string" && plan ? plan.toLowerCase() : UNKNOWN_PLAN;
}

/** Cached on the credential; falls back to decoding the token. */
export function planOf(credential: OAuthCredential): string {
  const cached = credential.extra.plan;
  if (typeof cached === "string" && cached) return cached.toLowerCase();
  return planFromToken(credential.access);
}

/** Whether `plan` can run Codex models. Only `free` is refused. */
export const isEntitled = (plan: string) => ![FREE_PLAN, ""].includes(plan.toLowerCase());

/** Cached on the credential; falls back to decoding the token. */
export function accountId(credential: OAuthCredential): string {
  const cached = credential.extra.account_id;
  if (typeof cached === "string" && cached) return cached;
  return accountIdFromToken(credential.access);
}

function credentialFrom(token: Record<string, unknown>): OAuthCredential {
  const access = token.access_token;
  const refresh = token.refresh_token;
  if (typeof access !== "string" || typeof refresh !== "string") {
    throw new OAuthError("Token response was missing access_token or refresh_token.");
  }
  const lifetime = positiveFloat(token.expires_in, 3600);
  return oauth(access, refresh, Date.now() / 1000 + lifetime, {
    account_id: accountIdFromToken(access),
    // Re-derived on every refresh: an upgrade from Free to Plus has to be
    // visible without signing in again.
    plan: planFromToken(access),
  });
}

async function postForm(
  url: string,
  form: Record<string, string>,
): Promise<Record<string, unknown>> {
  const response = await request(url, {
    method: "POST",
    body: urlencode(form),
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    timeout: HTTP_TIMEOUT,
  });
  const text = await response.text();
  if (response.status >= 400) {
    throw new OAuthError(`${url} returned ${response.status}: ${[...text].slice(0, 400).join("")}`);
  }
  return JSON.parse(text);
}

async function postJson(url: string, body: unknown) {
  const response = await request(url, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
    timeout: HTTP_TIMEOUT,
  });
  return { status: response.status, text: await response.text() };
}

async function exchange(code: string, verifier: string, redirectUri: string) {
  return credentialFrom(
    await postForm(TOKEN_URL, {
      grant_type: "authorization_code",
      client_id: CLIENT_ID,
      code,
      code_verifier: verifier,
      redirect_uri: redirectUri,
    }),
  );
}

/** Swap the refresh token for a new pair. The account id is re-derived from
 * the new access token rather than carried over. */
export async function refresh(credential: OAuthCredential): Promise<OAuthCredential> {
  return credentialFrom(
    await postForm(TOKEN_URL, {
      grant_type: "refresh_token",
      client_id: CLIENT_ID,
      refresh_token: credential.refresh,
    }),
  );
}

/** PKCE flow through the local browser, with a paste fallback. */
export async function loginBrowser(interaction: LoginInteraction): Promise<OAuthCredential> {
  const pkce = generatePkce();
  const state = randomState();
  const result = await authorizeInBrowser(authorizeUrl(pkce, state), {
    port: CALLBACK_PORT,
    path: CALLBACK_PATH,
    state,
    interaction,
  });
  interaction.progress("Exchanging the authorization code…");
  return exchange(result.code, pkce.verifier, REDIRECT_URI);
}

const sleep = (seconds: number) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));
const excerpt = (text: string) => [...text].slice(0, 400).join("");

/** Headless flow: show a short code, poll until the user enters it elsewhere. */
export async function loginDeviceCode(interaction: LoginInteraction): Promise<OAuthCredential> {
  const response = await postJson(DEVICE_USER_CODE_URL, { client_id: CLIENT_ID });
  if (response.status === 404) {
    throw new OAuthError("Device-code login is not enabled for this account. Use browser login.");
  }
  if (response.status >= 400) {
    throw new OAuthError(
      `Device code request failed (${response.status}): ${excerpt(response.text)}`,
    );
  }
  const payload = JSON.parse(response.text);
  const deviceId = payload.device_auth_id;
  const userCode = payload.user_code;
  if (!deviceId || !userCode) {
    throw new OAuthError("Device code response was missing device_auth_id or user_code.");
  }
  let interval = positiveFloat(payload.interval, 5);
  interaction.showDeviceCode(String(userCode), DEVICE_VERIFICATION_URI);

  const deadline = performance.now() / 1000 + DEVICE_TIMEOUT_SECONDS;
  while (performance.now() / 1000 < deadline) {
    await sleep(interval);
    const poll = await postJson(DEVICE_TOKEN_URL, {
      device_auth_id: deviceId,
      user_code: userCode,
    });
    if (poll.status < 400) {
      const data = JSON.parse(poll.text);
      const code = data.authorization_code;
      const verifier = data.code_verifier;
      if (!code || !verifier) {
        throw new OAuthError("Device auth completed without an authorization code.");
      }
      interaction.progress("Exchanging the authorization code…");
      return exchange(code, verifier, DEVICE_REDIRECT_URI);
    }
    const status = deviceError(poll.status, poll.text);
    if (status === "slow_down") interval += 2;
    else if (status !== "pending") {
      throw new OAuthError(`Device auth failed (${poll.status}): ${excerpt(poll.text)}`);
    }
  }
  throw new OAuthError("Device code expired before it was approved.");
}

/** Classify a non-2xx poll as pending, slow_down, or fatal. */
function deviceError(status: number, text: string): "pending" | "slow_down" | "fatal" {
  if (status === 403 || status === 404) return "pending";
  let error: unknown;
  try {
    error = JSON.parse(text).error;
  } catch {
    return "fatal";
  }
  const code =
    typeof error === "object" && error !== null ? (error as { code?: unknown }).code : error;
  if (code === "deviceauth_authorization_pending") return "pending";
  if (code === "slow_down") return "slow_down";
  return "fatal";
}

function positiveFloat(value: unknown, fallback: number): number {
  let parsed: number;
  try {
    parsed = toFloat(value);
  } catch {
    return fallback;
  }
  return parsed > 0 ? parsed : fallback;
}

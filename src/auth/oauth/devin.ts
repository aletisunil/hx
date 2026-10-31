/**
 * Devin subscription sign-in.
 *
 * The same browser sign-in the official `devin` CLI performs: a PKCE
 * authorization-code flow against `app.devin.ai`, redeemed at
 * `api.devin.ai/auth/cli/token` for a Devin session token. There is no client id
 * and no refresh token - the session token is long-lived, and when it does
 * expire the only way forward is signing in again.
 *
 * The session token is not what chat requests carry. Each request trades it
 * for a short-lived user JWT first, so what is stored here is the one secret
 * that can mint the rest.
 */

import { randomUUID } from "node:crypto";
import { request } from "../../net.ts";
import { urlencode } from "../../py.ts";
import { type OAuthCredential, oauth } from "../store.ts";
import { authorizeInBrowser, type LoginInteraction, OAuthError } from "./browser.ts";
import { generatePkce, jwtClaims, type PKCE } from "./pkce.ts";

export const PROVIDER_ID = "devin";
const AUTHORIZE_URL = "https://app.devin.ai/auth/cli/continue";
const TOKEN_URL = "https://api.devin.ai/auth/cli/token";
const CALLBACK_PORT = 59653;
const CALLBACK_PATH = "/callback";
/** `127.0.0.1` rather than `localhost`: the redirect is matched as a string. */
const REDIRECT_URI = `http://127.0.0.1:${CALLBACK_PORT}${CALLBACK_PATH}`;
/** For a session token whose expiry cannot be read. The CLI assumes a year. */
const DEFAULT_LIFETIME = 365 * 24 * 3600;
const HTTP_TIMEOUT = 30;
const ENTERPRISE_HINT =
  'For Devin Enterprise, choose "Log in with Devin for Enterprise" and enter your company.';

export function authorizeUrl(pkce: PKCE, state: string): string {
  return `${AUTHORIZE_URL}?${urlencode({
    response_type: "code",
    redirect_uri: REDIRECT_URI,
    code_challenge: pkce.challenge,
    code_challenge_method: pkce.method,
    state,
    // Lets someone with a work and a personal account pick.
    prompt: "select_account",
  })}`;
}

/** The payload of a JWT-shaped token, or `{}` when it is not one. */
export function claimsOf(token: string): Record<string, unknown> {
  const bare = token.startsWith("devin-session-token$")
    ? token.slice("devin-session-token$".length)
    : token;
  return jwtClaims(bare) ?? {};
}

/** Epoch seconds at which `token` stops working. */
export function expiryOf(token: string, now = Date.now() / 1000): number {
  const exp = claimsOf(token).exp;
  if (typeof exp === "number" && exp > 0) return exp;
  return now + DEFAULT_LIFETIME;
}

/** Store a session token. `refresh` holds the token itself: there is no
 * refresh token, and the store needs the field. */
export function credentialFrom(token: string): OAuthCredential {
  const extra: Record<string, unknown> = {};
  const claims = claimsOf(token);
  for (const key of ["email", "name"]) {
    const value = claims[key];
    if (typeof value === "string" && value) extra[key] = value;
  }
  return oauth(token, token, expiryOf(token), extra);
}

/** Redeem an authorization code for a session token. */
export async function exchange(code: string, verifier: string): Promise<OAuthCredential> {
  const response = await request(TOKEN_URL, {
    method: "POST",
    body: JSON.stringify({ code, code_verifier: verifier }),
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    timeout: HTTP_TIMEOUT,
  });
  const text = await response.text();
  if (response.status >= 400) {
    throw new OAuthError(`Devin refused the sign-in (${response.status}): ${detail(text)}`);
  }
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new OAuthError("Devin's token endpoint did not answer with JSON.");
  }
  const token =
    typeof payload === "object" && payload !== null ? (payload as { token?: unknown }).token : null;
  if (typeof token !== "string" || !token) {
    throw new OAuthError("Devin's token response carried no session token.");
  }
  return credentialFrom(token);
}

/** PKCE flow through the local browser, with a paste fallback. */
export async function loginBrowser(interaction: LoginInteraction): Promise<OAuthCredential> {
  const pkce = generatePkce();
  const state = randomUUID();
  const result = await authorizeInBrowser(authorizeUrl(pkce, state), {
    port: CALLBACK_PORT,
    path: CALLBACK_PATH,
    state,
    interaction,
    instructions: ENTERPRISE_HINT,
  });
  interaction.progress("Exchanging the authorization code…");
  return exchange(result.code, pkce.verifier);
}

/** Who the login belongs to, for `hx auth` - never the token. */
export function accountOf(credential: OAuthCredential): string | null {
  for (const key of ["email", "name"]) {
    const value = credential.extra[key];
    if (typeof value === "string" && value) return value;
  }
  return null;
}

/** The reason in an error body - `{"detail": ...}` - or a short excerpt. */
function detail(text: string): string {
  const excerpt = [...text].slice(0, 400).join("");
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    return excerpt;
  }
  if (typeof payload === "object" && payload !== null) {
    for (const key of ["detail", "message", "error"]) {
      const value = (payload as Record<string, unknown>)[key];
      if (typeof value === "string" && value) return value;
    }
  }
  return excerpt;
}

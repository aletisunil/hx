/** PKCE (RFC 7636) code verifier and S256 challenge. */

import { createHash, randomBytes } from "node:crypto";

export interface PKCE {
  readonly verifier: string;
  readonly challenge: string;
  readonly method: "S256";
}

/** base64url without padding, as every OAuth spec wants it. */
export const b64url = (raw: Buffer) => raw.toString("base64url");

export const challengeFor = (verifier: string) =>
  b64url(createHash("sha256").update(verifier, "ascii").digest());

/** A fresh verifier/challenge pair. 32 bytes of entropy yields a 43-character
 * verifier, the shortest length the spec allows. */
export function generatePkce(entropyBytes = 32): PKCE {
  const verifier = b64url(randomBytes(entropyBytes));
  return { verifier, challenge: challengeFor(verifier), method: "S256" };
}

export const randomState = () => randomBytes(16).toString("hex");

/** The payload of a JWT, or `null` when `token` is not a readable one. */
export function jwtClaims(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const claims = JSON.parse(Buffer.from(parts[1] as string, "base64url").toString("utf8"));
    return typeof claims === "object" && claims !== null && !Array.isArray(claims) ? claims : null;
  } catch {
    return null;
  }
}

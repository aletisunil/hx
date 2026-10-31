/**
 * One TLS policy for every outbound request, and the HTTP client that applies it.
 *
 * On a network that terminates TLS - a corporate proxy, a VPN client, a
 * security agent - the interception CA lives in the OS store, so every browser
 * on the box works and a client with its own bundle alone fails with
 * "certificate verify failed". So the trust store is decided here, once:
 *
 * - `HX_CA_BUNDLE` (or `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`) - an explicit PEM
 *   file wins, because a user who names a bundle means it.
 * - otherwise the OS trust store, so a CA the machine already trusts is enough
 *   and nothing has to be exported by hand.
 *
 * `HX_SSL_NO_VERIFY=1` turns verification off. It exists because a blocked user
 * will otherwise reach for a worse workaround; it says so out loud in
 * `tlsNotice` rather than failing quietly open.
 *
 * The `claude-code` route runs the `claude` CLI, which makes its own
 * connections. `nodeTlsEnv` restates the same policy in the variables Node
 * reads, so a setting that fixes one route fixes them all.
 */

import { X509Certificate } from "node:crypto";
import { statSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import tls from "node:tls";
import { expandUser, isFile, readText } from "./pathlib.ts";
import { purePath, rstrip, strip } from "./py.ts";

export const CA_BUNDLE_VARS = ["HX_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"] as const;
export const NO_VERIFY_VAR = "HX_SSL_NO_VERIFY";
const TRUTHY = new Set(["1", "true", "yes", "on"]);

/** The CA bundle the environment names, if it points at a file of certificates. */
export function caBundle(): string | null {
  const named = namedBundle();
  if (named === null || loadError(named[1]) !== null) return null;
  return named[1];
}

/** The first of `CA_BUNDLE_VARS` that names a real file, and that file. */
function namedBundle(): [string, string] | null {
  for (const name of CA_BUNDLE_VARS) {
    const raw = strip(process.env[name] ?? "");
    if (raw && isFile(expandUser(raw))) return [name, purePath(expandUser(raw))];
  }
  return null;
}

const loadErrors = new Map<string, string | null>();

/** Why `path` cannot serve as a CA bundle - a DER file, a key, an empty file -
 * or `null` when it can. Asked again only when the file changes. */
function loadError(path: string): string | null {
  let stamp: string;
  try {
    const info = statSync(path, { bigint: true });
    stamp = `${path}:${info.mtimeNs}:${info.size}`;
  } catch (error) {
    return (error as Error).message;
  }
  if (!loadErrors.has(stamp)) loadErrors.set(stamp, checkBundle(path));
  return loadErrors.get(stamp) ?? null;
}

function checkBundle(path: string): string | null {
  let pems: string[];
  try {
    pems = certificates(readText(path));
  } catch (error) {
    return flatten((error as Error).message);
  }
  if (pems.length === 0) return "no certificate or crl found";
  for (const pem of pems) {
    try {
      new X509Certificate(pem);
    } catch {
      return "no certificate or crl found";
    }
  }
  return null;
}

/** The PEM certificate blocks in `text`. */
function certificates(text: string): string[] {
  return text.match(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g) ?? [];
}

export function verificationDisabled(): boolean {
  return TRUTHY.has(strip(process.env[NO_VERIFY_VAR] ?? "").toLowerCase());
}

let tlsOptions: https.AgentOptions | null = null;

/** The TLS settings every client uses. Cached: reading the trust store reads
 * every certificate in it. */
export function tlsAgentOptions(): https.AgentOptions {
  if (tlsOptions !== null) return tlsOptions;
  if (verificationDisabled()) {
    tlsOptions = { rejectUnauthorized: false };
  } else {
    const bundle = caBundle();
    let ca: string[];
    if (bundle !== null) ca = certificates(readText(bundle));
    else {
      ca = tls.getCACertificates("system");
      // A machine whose store cannot be read still has Node's own roots.
      if (ca.length === 0) ca = tls.getCACertificates("default");
    }
    tlsOptions = { ca, ALPNProtocols: ["http/1.1"] };
  }
  return tlsOptions;
}

/** One line for the startup transcript when TLS is not stock. */
export function tlsNotice(): string | null {
  if (verificationDisabled()) {
    return `${NO_VERIFY_VAR} is set: TLS certificates are not being verified.`;
  }
  const named = namedBundle();
  if (named !== null) {
    const error = loadError(named[1]);
    if (error !== null) {
      // Ignored, as OpenSSL and Node ignore a CA file they cannot read: failing
      // to start would cost every route, the ones this file is not for too.
      const [name, path] = named;
      return (
        `${name} names ${path}, which holds no usable CA certificates (${error}). ` +
        "Verifying TLS against the system trust store instead."
      );
    }
  }
  const bundle = caBundle();
  return bundle !== null ? `Verifying TLS against ${bundle}.` : null;
}

/**
 * What to add to a Node child's environment so it trusts what HX trusts.
 *
 * A bundle HX verifies against becomes `NODE_EXTRA_CA_CERTS`, unless the user
 * already gave Node one; `HX_SSL_NO_VERIFY` becomes
 * `NODE_TLS_REJECT_UNAUTHORIZED=0`.
 */
export function nodeTlsEnv(environ: Record<string, string | undefined>): Record<string, string> {
  const added: Record<string, string> = {};
  const bundle = caBundle();
  if (bundle !== null && !strip(environ.NODE_EXTRA_CA_CERTS ?? "")) {
    added.NODE_EXTRA_CA_CERTS = bundle;
  }
  if (verificationDisabled()) added.NODE_TLS_REJECT_UNAUTHORIZED = "0";
  return added;
}

const CERT_FAILURE =
  /certificate verif|self[- ]signed certificate|unable to (?:get local issuer|verify the first) certificate|UNABLE_TO_VERIFY_LEAF_SIGNATURE|UNABLE_TO_GET_ISSUER_CERT|SELF_SIGNED_CERT_IN_CHAIN|certificate purpose|INVALID_PURPOSE/i;

/** A chain the client does trust, but with a certificate in it that may not
 * serve as a TLS server's: OpenSSL says "unsuitable", BoringSSL "unsupported". */
const WRONG_PURPOSE = /(?:unsupported|unsuitable) certificate purpose|INVALID_PURPOSE/;

/** What to do about a certificate the client would not accept. */
export function tlsHint(reason = ""): string {
  if (WRONG_PURPOSE.test(reason)) {
    return (
      "Your network intercepts TLS with a certificate that is not valid for servers. " +
      "Ask its administrator to fix that certificate or to exclude this service from " +
      `inspection; until then, ${NO_VERIFY_VAR}=1 turns verification off.`
    );
  }
  const bundle = caBundle();
  if (bundle !== null) {
    return `HX is verifying TLS against ${bundle}; check that it holds the CA your network signs with.`;
  }
  return (
    "If your network intercepts TLS, add its CA to the system trust store " +
    `or set ${CA_BUNDLE_VARS[0]} to its PEM file.`
  );
}

/** `message`, followed by `tlsHint` when it reports a certificate failure - in
 * any client's words: OpenSSL's, Node's, Claude Code's. */
export function withTlsHint(message: string): string {
  const hint = tlsHint(message);
  if (!CERT_FAILURE.test(message) || message.includes(hint)) return message;
  return `${rstrip(rstrip(message), ".")}. ${hint}`;
}

/** The OpenSSL verification errors Node reports by code. */
const VERIFY_CODES = new Set([
  "UNABLE_TO_GET_ISSUER_CERT",
  "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
  "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
  "SELF_SIGNED_CERT_IN_CHAIN",
  "DEPTH_ZERO_SELF_SIGNED_CERT",
  "CERT_HAS_EXPIRED",
  "CERT_NOT_YET_VALID",
  "CERT_UNTRUSTED",
  "CERT_REJECTED",
  "CERT_SIGNATURE_FAILURE",
  "HOSTNAME_MISMATCH",
  "ERR_TLS_CERT_ALTNAME_INVALID",
  "INVALID_PURPOSE",
]);

/** An HTTP request that took longer than its timeout. */
export class TimeoutError extends Error {
  override name = "TimeoutException";
  constructor(readonly host: string) {
    super(`timed out talking to ${host}`);
  }
}

/** A request that never reached the server: refused, unresolvable, reset. */
export class ConnectError extends Error {
  override name = "ConnectError";
  constructor(
    readonly host: string,
    message: string,
    override readonly cause?: unknown,
  ) {
    super(message);
  }
}

/**
 * A network failure in one line, naming the fix when there is one.
 *
 * Callers put this in a notice, so the raw error - which for a TLS failure is
 * several nested causes deep - is no use.
 */
export function describe(error: unknown): string {
  for (const err of chain(error)) {
    const code = (err as NodeJS.ErrnoException).code ?? "";
    if (VERIFY_CODES.has(code)) {
      const reason = flatten((err as Error).message || "certificate verify failed");
      return `TLS certificate verification failed (${reason}). ${tlsHint(reason)}`;
    }
    if (code.startsWith("ERR_SSL") || code.startsWith("ERR_TLS")) {
      return `TLS handshake failed: ${flatten((err as Error).message)}.`;
    }
  }
  if (error instanceof TimeoutError) return `Timed out talking to ${error.host}.`;
  if (error instanceof ConnectError) {
    return `Cannot reach ${error.host}: ${flatten(error.message)}.`;
  }
  // A child process's failure arrives as text, already flattened by whoever
  // raised it: the hint is all there is left to add.
  const failure = error as Error;
  return withTlsHint(`${failure?.name ?? "Error"}: ${flatten(String(failure?.message ?? error))}`);
}

function chain(error: unknown): unknown[] {
  const seen: unknown[] = [];
  let current: unknown = error;
  while (current !== undefined && current !== null && !seen.includes(current)) {
    seen.push(current);
    current = (current as { cause?: unknown }).cause;
  }
  return seen;
}

export function flatten(text: string, limit = 160): string {
  const collapsed = rstrip(strip(String(text).replace(/\s+/g, " ")), ".");
  return [...collapsed].length <= limit
    ? collapsed
    : `${[...collapsed].slice(0, limit - 1).join("")}…`;
}

/** A response whose body is read as it arrives. */
export interface HttpResponse {
  readonly status: number;
  readonly headers: http.IncomingHttpHeaders;
  /** The body in chunks, as the server sends them. */
  chunks(): AsyncGenerator<Buffer>;
  /** The body as text, read to the end. */
  text(): Promise<string>;
  /** The body as JSON, read to the end. */
  json(): Promise<unknown>;
  /** Stop reading and drop the connection. */
  close(): void;
}

export interface RequestOptions {
  readonly method?: string;
  readonly headers?: Record<string, string>;
  readonly body?: string | Buffer;
  /** Seconds to connect and to wait for each read. */
  readonly timeout?: number;
  /** Seconds to wait for each read once the headers are in, when that differs
   * from `timeout`. 0 waits forever: a stream the server holds open. */
  readonly readTimeout?: number;
  readonly signal?: AbortSignal;
}

/**
 * Send one request with HX's trust store applied, and hand back the response
 * as soon as its headers arrive.
 */
export function request(url: string, options: RequestOptions = {}): Promise<HttpResponse> {
  const target = new URL(url);
  const client = target.protocol === "http:" ? http : https;
  const timeout = (options.timeout ?? 30) * 1000;
  const body = options.body === undefined ? undefined : Buffer.from(options.body);
  // A length, not chunked encoding: what httpx sent, and what every server reads.
  const headers =
    body === undefined
      ? { ...options.headers }
      : { ...options.headers, "Content-Length": String(body.length) };
  return new Promise((resolve, reject) => {
    const outgoing = client.request(
      target,
      {
        method: options.method ?? "GET",
        headers,
        ...(target.protocol === "https:" ? tlsAgentOptions() : {}),
        timeout,
        signal: options.signal,
      },
      (incoming) => {
        const read = options.readTimeout === undefined ? timeout : options.readTimeout * 1000;
        if (read > 0)
          incoming.setTimeout(read, () => incoming.destroy(new TimeoutError(target.hostname)));
        resolve(wrap(incoming));
      },
    );
    outgoing.on("timeout", () => outgoing.destroy(new TimeoutError(target.hostname)));
    outgoing.on("error", (error: NodeJS.ErrnoException) => {
      if (error instanceof TimeoutError || error.name === "AbortError") reject(error);
      else if (VERIFY_CODES.has(error.code ?? "") || (error.code ?? "").startsWith("ERR_SSL")) {
        reject(error);
      } else reject(new ConnectError(target.hostname, error.message, error));
    });
    outgoing.end(body);
  });
}

function wrap(incoming: http.IncomingMessage): HttpResponse {
  const chunks = async function* (): AsyncGenerator<Buffer> {
    for await (const chunk of incoming) yield chunk as Buffer;
  };
  const text = async () => {
    const parts: Buffer[] = [];
    for await (const chunk of chunks()) parts.push(chunk);
    return Buffer.concat(parts).toString("utf8");
  };
  return {
    status: incoming.statusCode ?? 0,
    headers: incoming.headers,
    chunks,
    text,
    json: async () => JSON.parse(await text()),
    close: () => incoming.destroy(),
  };
}

/**
 * The lines of a streamed body, split on `\n` with a trailing `\r` removed, as
 * they arrive. A multi-byte character split across chunks stays whole.
 */
export async function* lines(response: HttpResponse): AsyncGenerator<string> {
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for await (const chunk of response.chunks()) {
    buffer += decoder.decode(chunk, { stream: true });
    let index = buffer.search(/\r\n|\r|\n/);
    while (index >= 0) {
      const breakLength = buffer.startsWith("\r\n", index) ? 2 : 1;
      if (buffer[index] === "\r" && index === buffer.length - 1) break;
      yield buffer.slice(0, index);
      buffer = buffer.slice(index + breakLength);
      index = buffer.search(/\r\n|\r|\n/);
    }
  }
  buffer += decoder.decode();
  if (buffer) yield buffer;
}

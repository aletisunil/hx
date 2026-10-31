/**
 * Loopback listener for the OAuth authorization-code redirect.
 *
 * The provider redirects the browser to `http://localhost:<port>/<path>`; this
 * serves that one request, checks `state`, and hands the code back.
 *
 * It binds `127.0.0.1` rather than `0.0.0.0`: the redirect only ever comes from
 * this machine, and a wider bind would let anything on the network post a
 * forged code. `$HX_OAUTH_CALLBACK_HOST` overrides it for container setups
 * where the browser reaches the host by another address.
 */

import http from "node:http";
import { lstrip, strip } from "../../py.ts";

export const HOST_ENV = "HX_OAUTH_CALLBACK_HOST";
export const DEFAULT_HOST = "127.0.0.1";

export interface CallbackResult {
  readonly code: string;
  readonly state: string | null;
}

export class CallbackError extends Error {
  override name = "CallbackError";
}

export const callbackHost = () => process.env[HOST_ENV] || DEFAULT_HOST;

/** `html.escape`: what the page interpolates arrives from the network. */
function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#x27;");
}

function page(title: string, body: string, ok: boolean): Buffer {
  const colour = ok ? "#7ee787" : "#ff7b72";
  return Buffer.from(`<!doctype html>
<html><head><meta charset="utf-8"><title>HX</title><style>
body{background:#111418;color:#e6e6e6;font:15px/1.6 ui-sans-serif,system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{text-align:center;max-width:28rem;padding:2rem}
h1{font-size:1.1rem;margin:0 0 .5rem;color:${colour}}
p{margin:0;color:#9aa4b2}
</style></head><body><div class="card"><h1>${escapeHtml(title)}</h1><p>${escapeHtml(body)}</p></div></body></html>
`);
}

/** `parse_qs(...)[key][0]`: the first non-blank value, or `null`. */
function first(params: URLSearchParams, key: string): string | null {
  return params.getAll(key).find((value) => value !== "") ?? null;
}

/**
 * Owns the one-shot callback server. Start it *before* opening the browser, so
 * a fast redirect cannot arrive at a closed port.
 */
export class LoopbackCallback {
  private server: http.Server | null = null;
  private bindError: Error | null = null;
  private result: CallbackResult | null = null;
  private failure: string | null = null;
  private closing = false;
  private settle: (() => void) | null = null;
  private readonly done: Promise<void>;

  constructor(
    readonly port: number,
    readonly path: string,
    private readonly state: string | null = null,
  ) {
    this.done = new Promise((resolve) => {
      this.settle = resolve;
    });
  }

  /** False when the port could not be bound; only pasting will work then. */
  get listening(): boolean {
    return this.server !== null;
  }

  /** Bind and serve. A failed bind is not an error: the paste path still works. */
  async start(): Promise<this> {
    const server = http.createServer((request, response) => this.handle(request, response));
    try {
      await new Promise<void>((resolve, reject) => {
        server.once("error", reject);
        server.listen(this.port, callbackHost(), () => resolve());
      });
    } catch (error) {
      this.bindError = error as Error;
      return this;
    }
    this.server = server;
    return this;
  }

  private handle(request: http.IncomingMessage, response: http.ServerResponse): void {
    const url = new URL(request.url ?? "/", "http://localhost");
    const respond = (status: number, title: string, body: string, ok: boolean) => {
      const payload = page(title, body, ok);
      response.writeHead(status, {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Length": String(payload.length),
      });
      response.end(payload);
    };
    if (request.method !== "GET") {
      respond(501, "Not found", "That is not the callback route.", false);
      return;
    }
    if (url.pathname !== this.path) {
      respond(404, "Not found", "That is not the callback route.", false);
      return;
    }
    const error = first(url.searchParams, "error");
    if (error) {
      respond(400, "Login failed", `The provider returned: ${error}`, false);
      this.failure = error;
      this.settle?.();
      return;
    }
    const code = first(url.searchParams, "code");
    const state = first(url.searchParams, "state");
    if (!code) {
      respond(400, "Login failed", "No authorization code in the redirect.", false);
      return;
    }
    if (this.state !== null && state !== this.state) {
      // A mismatched state is the CSRF case the parameter exists to catch.
      respond(400, "Login failed", "State mismatch - ignoring this redirect.", false);
      return;
    }
    respond(200, "Signed in", "You can close this window and return to HX.", true);
    this.result = { code, state };
    this.settle?.();
  }

  /** Stop serving and release the port. Safe to call twice. */
  async close(): Promise<void> {
    this.closing = true;
    this.settle?.();
    const server = this.server;
    this.server = null;
    if (server !== null) {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  }

  /**
   * Wait until the browser redirect lands. When the port could not be bound
   * this never resolves, leaving the paste to decide the outcome.
   */
  async wait(): Promise<CallbackResult> {
    if (this.server === null && !this.closing) await new Promise(() => {});
    await this.done;
    if (this.failure) throw new CallbackError(`Authorization failed: ${this.failure}`);
    if (this.result === null) throw new CallbackError("Authorization was cancelled.");
    return this.result;
  }

  bindMessage(): string {
    return (
      `Could not listen on ${callbackHost()}:${this.port} (${this.bindError?.message}). ` +
      "Close whatever is using that port, or paste the redirect URL instead."
    );
  }
}

/** Accept what a user might paste: a full redirect URL, `code#state`, a query
 * fragment, or a bare code. */
export function parseRedirect(value: string): CallbackResult {
  const text = strip(value);
  if (!text) throw new CallbackError("Nothing pasted.");

  if (text.includes("://")) {
    let params: URLSearchParams;
    try {
      params = new URL(text).searchParams;
    } catch {
      params = new URLSearchParams();
    }
    const code = first(params, "code");
    if (!code) throw new CallbackError("That URL has no ?code= parameter.");
    return { code, state: first(params, "state") };
  }
  if (text.includes("code=")) {
    const params = new URLSearchParams(lstrip(text, "?"));
    const code = first(params, "code");
    if (!code) throw new CallbackError("Could not find a code in that text.");
    return { code, state: first(params, "state") };
  }
  if (text.includes("#")) {
    const index = text.indexOf("#");
    const state = text.slice(index + 1);
    return { code: text.slice(0, index), state: state || null };
  }
  return { code: text, state: null };
}

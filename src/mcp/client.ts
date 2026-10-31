/**
 * MCP client - stdio and streamable HTTP transports.
 *
 * Implements the subset HX needs: `initialize`, `tools/list`, `tools/call`,
 * and `notifications/tools/list_changed`. JSON-RPC 2.0 over either transport.
 */

import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { dumps } from "../json.ts";
import { type HttpResponse, lines, request } from "../net.ts";
import { formatFixed, rstrip, splitLines, strip } from "../py.ts";
import { count } from "../text.ts";
import { VERSION } from "../version.ts";
import type { OAuthSession } from "./oauth.ts";

export const PROTOCOL_VERSION = "2025-06-18";
export const CLIENT_INFO = { name: "hx", version: VERSION };

export type JsonObject = Record<string, unknown>;

export class MCPError extends Error {
  override name = "MCPError";
}

/** The server ran the tool and it failed - distinct from a protocol failure. */
export class MCPToolError extends MCPError {
  override name = "MCPToolError";
}

/** The server wants a signed-in user, and there is no token it accepts. */
export class MCPAuthRequired extends MCPError {
  override name = "MCPAuthRequired";
}

/** The server no longer knows the session; `initialize` starts a new one. */
export class MCPSessionExpired extends MCPError {
  override name = "MCPSessionExpired";
}

export interface MCPToolDef {
  readonly name: string;
  readonly description: string;
  readonly inputSchema: JsonObject;
}

/** What a `tools/call` returned, reduced to what the model can be shown. */
export interface MCPToolOutput {
  readonly text: string;
  /** `[base64 data, mime type]` per `image` content block, as sent. */
  readonly images: [string, string][];
}

export interface ServerCapabilities {
  readonly tools: boolean;
  readonly prompts: boolean;
  readonly resources: boolean;
  readonly raw: JsonObject;
}

const noCapabilities = (): ServerCapabilities => ({
  tools: false,
  prompts: false,
  resources: false,
  raw: {},
});

const isObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Python's `json.dumps(message)` - what a stdio server is sent, one per line. */
const encode = (message: JsonObject) => dumps(message);

/** A queue of messages, read by one consumer. `null` ends it. */
class Inbox {
  private readonly items: (JsonObject | Error | null)[] = [];
  private waiter: (() => void) | null = null;

  push(item: JsonObject | Error | null): void {
    this.items.push(item);
    this.waiter?.();
    this.waiter = null;
  }

  async *drain(): AsyncGenerator<JsonObject> {
    while (true) {
      if (!this.items.length) {
        await new Promise<void>((resolve) => {
          this.waiter = resolve;
        });
        continue;
      }
      const item = this.items.shift() as JsonObject | Error | null;
      if (item === null) return;
      if (item instanceof Error) throw item;
      yield item;
    }
  }
}

export interface Transport {
  connect(): Promise<void>;
  send(message: JsonObject): Promise<void>;
  receive(): AsyncGenerator<JsonObject>;
  close(): Promise<void>;
  /** The version `initialize` settled on. Only HTTP has to repeat it. */
  setProtocolVersion(version: string): void;
  /** Start taking messages the server sends outside a request. A no-op where
   * every message already arrives on one stream, as on stdio. */
  listen(): void;
}

/**
 * Longest JSON-RPC line a server may send. A tool that returns a file puts
 * the whole thing on one line, so the ceiling is high; it is there so a
 * server that streams something unbounded still fails.
 */
export const STDIO_LINE_LIMIT = 16 * 1024 * 1024;

/**
 * Newline-delimited JSON-RPC over a subprocess's stdio. The server's stderr is
 * drained and dropped; a chatty server would otherwise corrupt the TUI.
 */
export class StdioTransport implements Transport {
  private process: ChildProcessWithoutNullStreams | null = null;
  private readonly inbox = new Inbox();
  private exited: Promise<void> = Promise.resolve();

  constructor(
    readonly command: string,
    readonly args: string[],
    readonly env: Record<string, string> | null = null,
    readonly cwd: string | null = null,
  ) {}

  async connect(): Promise<void> {
    const child = spawn(this.command, this.args, {
      env: { ...process.env, ...(this.env ?? {}) },
      cwd: this.cwd ?? undefined,
      stdio: ["pipe", "pipe", "pipe"],
    });
    await new Promise<void>((resolve, reject) => {
      child.once("spawn", resolve);
      child.once("error", reject);
    }).catch((error: NodeJS.ErrnoException) => {
      throw new MCPError(osError(error, this.command));
    });
    this.process = child;
    this.exited = new Promise((resolve) => child.once("close", () => resolve()));
    child.stderr.resume();
    child.stdin.on("error", () => {});
    this.readStdout(child);
  }

  private readStdout(child: ChildProcessWithoutNullStreams): void {
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    child.stdout.on("data", (chunk: Buffer) => {
      buffer += decoder.decode(chunk, { stream: true });
      let index = buffer.indexOf("\n");
      while (index >= 0) {
        this.take(buffer.slice(0, index));
        buffer = buffer.slice(index + 1);
        index = buffer.indexOf("\n");
      }
      if (buffer.length > STDIO_LINE_LIMIT) {
        // The reader is parked mid-message with no way to find the next
        // boundary, so the connection is finished - but it says why.
        this.inbox.push(
          new MCPError(
            `${this.command}: a message exceeded the ${STDIO_LINE_LIMIT / (1024 * 1024)} MiB line limit`,
          ),
        );
        child.stdout.destroy();
      }
    });
    child.stdout.on("end", () => {
      buffer += decoder.decode();
      if (buffer) this.take(buffer);
      this.inbox.push(null);
    });
    // Destroyed rather than ended - on close, or past the line limit.
    child.stdout.on("close", () => this.inbox.push(null));
  }

  private take(raw: string): void {
    const line = strip(raw);
    if (!line) return;
    try {
      const message = JSON.parse(line);
      if (isObject(message)) this.inbox.push(message);
    } catch {
      // A server printing plain text on stdout is a server bug, not a reason
      // to tear down the session.
      process.stderr.write(
        `mcp[${this.command}] non-JSON on stdout: ${[...line].slice(0, 200).join("")}\n`,
      );
    }
  }

  async send(message: JsonObject): Promise<void> {
    const child = this.process;
    if (child === null || child.stdin.destroyed) throw new MCPError("transport is not connected");
    // A write to a server that has exited is not reported here: the read
    // loop sees the stream end and fails every waiting request with why.
    await new Promise<void>((resolve) => {
      child.stdin.write(`${encode(message)}\n`, () => resolve());
    });
  }

  receive(): AsyncGenerator<JsonObject> {
    if (this.process === null) throw new MCPError("transport is not connected");
    return this.inbox.drain();
  }

  setProtocolVersion(_version: string): void {}

  listen(): void {}

  async close(): Promise<void> {
    const child = this.process;
    if (child === null) return;
    this.process = null;
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      let timer: NodeJS.Timeout | undefined;
      await Promise.race([
        this.exited,
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, 5000);
        }),
      ]);
      clearTimeout(timer);
    }
    child.stdin.destroy();
    child.stdout.destroy();
    child.stderr.destroy();
  }
}

/** Python's `str(OSError)` for a command that could not start. */
function osError(error: NodeJS.ErrnoException, command: string): string {
  if (error.code === "ENOENT") return `[Errno 2] No such file or directory: '${command}'`;
  if (error.code === "EACCES") return `[Errno 13] Permission denied: '${command}'`;
  return error.message;
}

/** A response's status, headers and body, read whole. */
interface Reply {
  readonly status: number;
  readonly headers: HttpResponse["headers"];
  readonly body: string;
}

async function post(url: string, body: string, headers: Record<string, string>): Promise<Reply> {
  const response = await request(url, {
    method: "POST",
    body,
    headers,
    timeout: 15,
    readTimeout: 120,
  });
  try {
    return { status: response.status, headers: response.headers, body: await response.text() };
  } finally {
    response.close();
  }
}

const header = (headers: HttpResponse["headers"], name: string): string => {
  const value = headers[name.toLowerCase()];
  return Array.isArray(value) ? (value[0] ?? "") : (value ?? "");
};

/**
 * Streamable HTTP transport. Requests are POSTed; each reply arrives as JSON
 * or as an SSE stream. Messages the server sends on its own come down a GET
 * stream held open beside them, when the server offers one.
 *
 * With `auth`, requests carry the server's OAuth token, a `401` is met with
 * one refresh and one retry, and a server that still refuses raises
 * `MCPAuthRequired`.
 */
export class HTTPTransport implements Transport {
  readonly headers: Record<string, string>;
  private connected = false;
  private sessionId: string | null = null;
  private protocolVersion: string | null = null;
  private readonly inbox = new Inbox();
  private listener: AbortController | null = null;
  /** The session the GET stream was opened for: a new session needs a new one. */
  private listeningTo: string | null = null;

  constructor(
    readonly url: string,
    headers: Record<string, string> | null = null,
    readonly auth: OAuthSession | null = null,
  ) {
    this.headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      ...(headers ?? {}),
    };
  }

  async connect(): Promise<void> {
    this.connected = true;
  }

  setProtocolVersion(version: string): void {
    this.protocolVersion = version;
  }

  async send(message: JsonObject): Promise<void> {
    if (!this.connected) throw new MCPError("transport is not connected");
    // `initialize` starts a session, so it never names one.
    const session = message.method === "initialize" ? null : this.sessionId;
    const body = dumps(message, { separators: [",", ":"], ensureAscii: false });
    const response = await this.authorized((headers) => post(this.url, body, headers), session);
    if (response.status === 404 && session !== null) {
      // The server forgot the session. Forgotten here only if it is still the
      // current one: a request beside the one that already renewed it must
      // not end that too.
      if (this.sessionId === session) this.sessionId = null;
      throw new MCPSessionExpired(`${this.url}: the server ended the session`);
    }
    if (response.status >= 400)
      throw new MCPError(
        `${this.url} returned ${response.status}: ${[...response.body].slice(0, 200).join("")}`,
      );
    const sessionId = header(response.headers, "Mcp-Session-Id");
    if (sessionId) this.sessionId = sessionId;
    if (response.status === 202 || !response.body) return;
    for (const payload of decodeBody(response, this.url)) this.inbox.push(payload);
  }

  /** Make `send` with the current token, recovering once from a 401. */
  private async authorized(
    send: (headers: Record<string, string>) => Promise<Reply>,
    session: string | null,
  ): Promise<Reply> {
    const token = await this.token();
    let response = await send(this.requestHeaders(token, session));
    if (response.status !== 401 || this.auth === null) return response;
    let fresh: string | null;
    try {
      fresh = await this.auth.recover(token);
    } catch (error) {
      throw new MCPError(`${this.url}: could not refresh the sign-in: ${(error as Error).message}`);
    }
    if (fresh !== null) {
      response = await send(this.requestHeaders(fresh, session));
      if (response.status !== 401) return response;
    }
    throw new MCPAuthRequired(
      token === null ? "needs sign-in" : "needs sign-in: the last one expired or was revoked",
    );
  }

  private async token(): Promise<string | null> {
    if (this.auth === null) return null;
    try {
      return await this.auth.token();
    } catch (error) {
      throw new MCPError(`${this.url}: could not refresh the sign-in: ${(error as Error).message}`);
    }
  }

  private requestHeaders(token: string | null, session: string | null): Record<string, string> {
    const headers = { ...this.headers };
    if (session) headers["Mcp-Session-Id"] = session;
    if (this.protocolVersion) headers["MCP-Protocol-Version"] = this.protocolVersion;
    if (token) headers.Authorization = `Bearer ${token}`;
    return headers;
  }

  receive(): AsyncGenerator<JsonObject> {
    return this.inbox.drain();
  }

  /** Open the GET stream for the current session, unless one is open. Called
   * after every `initialize`: the stream belongs to its session. */
  listen(): void {
    if (!this.connected) return;
    if (this.listener !== null && !this.listener.signal.aborted) {
      if (this.listeningTo === this.sessionId) return;
      this.listener.abort();
    }
    this.listeningTo = this.sessionId;
    const listener = new AbortController();
    this.listener = listener;
    void this.hold(this.sessionId, listener.signal).catch(() => {});
  }

  /**
   * Hold `session`'s GET stream open, reconnecting when it drops. A server
   * answering 405 has nothing to say outside a request and is left alone, as
   * is a session the server no longer knows. A dropped stream is reopened
   * with backoff, reset only once one has carried a message.
   */
  private async hold(session: string | null, signal: AbortSignal): Promise<void> {
    let backoff = 1;
    let lastEventId: string | null = null;
    let recovered = false;
    while (this.connected && this.sessionId === session && !signal.aborted) {
      let response: HttpResponse | null = null;
      try {
        const token = await this.token();
        const headers = this.requestHeaders(token, session);
        headers.Accept = "text/event-stream";
        if (lastEventId) headers["Last-Event-ID"] = lastEventId;
        response = await request(this.url, { headers, timeout: 15, readTimeout: 0, signal });
        if (response.status === 401 && this.auth !== null) {
          // One refresh per refusal, or a server that rejects even a fresh
          // token would cost a refresh token on every pass.
          if (recovered || (await this.auth.recover(token)) === null) return;
          recovered = true;
          continue;
        }
        if (
          response.status >= 400 ||
          !header(response.headers, "content-type").includes("text/event-stream")
        )
          return;
        recovered = false;
        const decoder = new SSEDecoder();
        for await (const line of lines(response)) {
          const payload = decoder.feed(line);
          lastEventId = decoder.lastEventId ?? lastEventId;
          if (payload !== null) {
            backoff = 1;
            this.inbox.push(payload);
          }
        }
      } catch {
        if (signal.aborted) return;
        // The stream dropped; it is reopened below.
      } finally {
        response?.close();
      }
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, backoff * 1000);
        signal.addEventListener("abort", () => {
          clearTimeout(timer);
          resolve();
        });
      });
      backoff = Math.min(backoff * 2, 60);
    }
  }

  async close(): Promise<void> {
    this.listener?.abort();
    this.listener = null;
    this.connected = false;
    this.inbox.push(null);
  }
}

/**
 * Server-sent events, one line at a time, yielding each JSON message. An
 * event's `data:` lines are joined with newlines and end at a blank line.
 */
export class SSEDecoder {
  private data: string[] = [];
  lastEventId: string | null = null;

  feed(raw: string): JsonObject | null {
    const line = rstrip(raw, "\r");
    if (!line) return this.dispatch();
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const name = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (name === "data") this.data.push(value);
    else if (name === "id") this.lastEventId = value || null;
    return null;
  }

  flush(): JsonObject | null {
    return this.dispatch();
  }

  private dispatch(): JsonObject | null {
    if (!this.data.length) return null;
    const text = this.data.join("\n");
    this.data = [];
    try {
      const payload = JSON.parse(text);
      return isObject(payload) ? payload : null;
    } catch {
      return null;
    }
  }
}

function decodeBody(response: Reply, url: string): JsonObject[] {
  if (header(response.headers, "content-type").includes("text/event-stream")) {
    const decoder = new SSEDecoder();
    const payloads: JsonObject[] = [];
    for (const line of splitLines(response.body)) {
      const payload = decoder.feed(line);
      if (payload !== null) payloads.push(payload);
    }
    const last = decoder.flush();
    if (last !== null) payloads.push(last);
    return payloads;
  }
  let body: unknown;
  try {
    body = JSON.parse(response.body);
  } catch (error) {
    throw new MCPError(`invalid JSON from ${url}: ${(error as Error).message}`);
  }
  return (Array.isArray(body) ? body : [body]) as JsonObject[];
}

interface Pending {
  resolve(message: JsonObject): void;
  reject(error: Error): void;
}

/** One connected server. */
export class MCPClient {
  capabilities = noCapabilities();
  private nextId = 0;
  private readonly pending = new Map<number, Pending>();
  private reader: Promise<void> | null = null;
  /** How many sessions `initialize` has started. */
  private generation = 0;
  private renewing: Promise<void> | null = null;

  constructor(
    readonly name: string,
    readonly transport: Transport,
    /** Seconds to wait for one reply. */
    readonly timeout = 30,
    /** Called when the server says its tool list changed. Must not block. */
    readonly onToolsChanged: (() => void) | null = null,
  ) {}

  async initialize(): Promise<ServerCapabilities> {
    await this.transport.connect();
    this.reader = this.readLoop();
    await this.handshake();
    return this.capabilities;
  }

  private async handshake(): Promise<void> {
    const result = await this.request(
      "initialize",
      { protocolVersion: PROTOCOL_VERSION, capabilities: {}, clientInfo: CLIENT_INFO },
      false,
    );
    this.transport.setProtocolVersion(String(result.protocolVersion || PROTOCOL_VERSION));
    const raw = isObject(result.capabilities) ? result.capabilities : {};
    this.capabilities = {
      tools: "tools" in raw,
      prompts: "prompts" in raw,
      resources: "resources" in raw,
      raw,
    };
    await this.notify("notifications/initialized", {});
    this.generation++;
    this.transport.listen();
  }

  /** Start a session in place of the one generation `expired` used - once,
   * however many requests found it gone. */
  private async renewSession(expired: number): Promise<void> {
    while (this.renewing !== null) await this.renewing.catch(() => {});
    if (this.generation !== expired) return;
    this.renewing = this.handshake();
    try {
      await this.renewing;
    } finally {
      this.renewing = null;
    }
  }

  async listTools(): Promise<MCPToolDef[]> {
    if (!this.capabilities.tools) return [];
    const result = await this.request("tools/list", {});
    const tools = Array.isArray(result.tools) ? result.tools : [];
    return tools.map((item: JsonObject) => ({
      name: String(item.name),
      description: String(item.description || ""),
      inputSchema:
        isObject(item.inputSchema) && Object.keys(item.inputSchema).length
          ? item.inputSchema
          : { type: "object", properties: {} },
    }));
  }

  /**
   * Call a tool and reduce its content blocks to text and images. The result
   * is data from a third-party server, returned to the model as tool output
   * and never treated as instructions to HX. Audio and embedded resources
   * have no route to the model and are dropped.
   */
  async callTool(name: string, args: JsonObject): Promise<MCPToolOutput> {
    const result = await this.request("tools/call", { name, arguments: args });
    const parts: string[] = [];
    const images: [string, string][] = [];
    for (const block of Array.isArray(result.content) ? result.content : []) {
      if (!isObject(block)) continue;
      if (block.type === "text") parts.push(String(block.text ?? ""));
      else if (block.type === "image" && typeof block.data === "string")
        images.push([block.data, String(block.mimeType || "")]);
    }
    let text = parts.filter(Boolean).join("\n");
    if (result.isError) throw new MCPToolError(text || `${name} failed`);
    if (!text) text = images.length ? `(${count(images.length, "image")})` : "(no content)";
    return { text, images };
  }

  private async request(
    method: string,
    params: JsonObject,
    reinitialize = true,
  ): Promise<JsonObject> {
    this.nextId++;
    const id = this.nextId;
    const reply = new Promise<JsonObject>((resolve, reject) =>
      this.pending.set(id, { resolve, reject }),
    );
    reply.catch(() => {});
    const message = { jsonrpc: "2.0", id, method, params };
    const generation = this.generation;
    try {
      try {
        await this.transport.send(message);
      } catch (error) {
        if (!(error instanceof MCPSessionExpired) || !reinitialize) throw error;
        // Once, and then the request again: a long session outlives the
        // server's idea of it.
        await this.renewSession(generation);
        await this.transport.send(message);
      }
    } catch (error) {
      this.pending.delete(id);
      throw error;
    }
    let timer: NodeJS.Timeout | undefined;
    let answer: JsonObject;
    try {
      answer = await Promise.race([
        reply,
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new MCPError(
                  `${this.name}: ${method} timed out after ${formatFixed(this.timeout, 0)}s`,
                ),
              ),
            this.timeout * 1000,
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
      this.pending.delete(id);
    }
    const error = answer.error;
    if (error) {
      const text = isObject(error) && "message" in error ? String(error.message) : String(error);
      throw new MCPError(`${this.name}: ${text}`);
    }
    return isObject(answer.result) ? answer.result : {};
  }

  private async notify(method: string, params: JsonObject): Promise<void> {
    await this.transport.send({ jsonrpc: "2.0", method, params });
  }

  private async readLoop(): Promise<void> {
    try {
      for await (const message of this.transport.receive()) {
        if ("method" in message) {
          this.fromServer(message);
          continue;
        }
        const id = message.id;
        const waiter = typeof id === "number" ? this.pending.get(id) : undefined;
        waiter?.resolve(message);
      }
    } catch (error) {
      this.failPending(`transport failed: ${(error as Error).message}`);
      return;
    }
    // The stream ended: the server exited. Fail the waiters now rather than
    // letting each one burn its full timeout against a process that is gone.
    this.failPending("the server exited");
  }

  /** A notification or request the server started, told apart from a reply
   * by `method`: a server's own request id can collide with one of ours. */
  private fromServer(message: JsonObject): void {
    const method = message.method;
    if (!("id" in message)) {
      if (method === "notifications/tools/list_changed") this.onToolsChanged?.();
      return;
    }
    // HX offers the server no capabilities, so only `ping` gets an answer;
    // anything else is refused rather than left hanging.
    const reply: JsonObject = { jsonrpc: "2.0", id: message.id };
    if (method === "ping") reply.result = {};
    else reply.error = { code: -32601, message: `method not found: ${String(method)}` };
    void this.transport.send(reply).catch(() => {});
  }

  private failPending(reason: string): void {
    for (const waiter of this.pending.values())
      waiter.reject(new MCPError(`${this.name}: ${reason}`));
  }

  async close(): Promise<void> {
    await this.transport.close();
    await this.reader;
    this.reader = null;
  }
}

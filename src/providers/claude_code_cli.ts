/**
 * One running `claude` process, spoken to over its stream-json protocol.
 *
 * Claude Code in print mode with `--input-format stream-json` and
 * `--output-format stream-json` reads newline-delimited JSON on stdin and
 * writes it on stdout. Three kinds of traffic share the two pipes:
 *
 * - conversation frames - `user` messages in; `stream_event`, `assistant`,
 *   `user`, `system` and `result` out;
 * - control requests either way, each answered by a `control_response` with
 *   the same `request_id` - HX sends `initialize`;
 * - MCP over the control channel: a server declared `{"type": "sdk"}` in
 *   `--mcp-config` lives in *this* process, reached with `mcp_message`.
 *
 * That last one is how HX's tools reach the model: the CLI runs with every
 * built-in tool off and HX's tools served as one SDK MCP server. A
 * `tools/call` is answered whenever HX has run the tool.
 */

import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { constants as osConstants } from "node:os";
import { dumps } from "../json.ts";
import { formatFixed, osErrorText, repr, rstrip, strip } from "../py.ts";
import { ProviderError } from "./base.ts";

/** Longest stdout line accepted: the CLI echoes every tool result back. */
const READ_LIMIT = 256 * 1024 * 1024;
const STDERR_LINES = 40;
export const INITIALIZE_TIMEOUT = 60;
export const SERVER_NAME = "hx";
/** Where the CLI puts the id of the `tool_use` block a `tools/call` serves. */
const TOOL_USE_META = "claudecode/toolUseId";
const MCP_PROTOCOL_VERSION = "2025-06-18";

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** `(toolUseId, name, arguments) -> MCP CallToolResult`. */
export type ToolHandler = (toolUseId: string, name: string, args: Json) => Promise<Json>;

export interface ServedTool {
  /** What the CLI calls it. The model sees `mcp__hx__<name>`. */
  readonly name: string;
  readonly description: string;
  readonly inputSchema: Json;
}

const result = (id: unknown, value: Json): Json => ({ jsonrpc: "2.0", id, result: value });
const errorContent = (text: string): Json => ({ content: [{ type: "text", text }], isError: true });

/** The MCP server HX's tools are served from. */
export class ToolServer {
  constructor(
    readonly tools: ServedTool[],
    public handler: ToolHandler,
  ) {}

  /** The JSON-RPC response to `message`; `null` for a notification. A
   * `tools/call` is answered whenever HX has run the tool. */
  answer(message: Json): Promise<Json | null> | Json | null {
    const method = message.method;
    const id = message.id;
    if (id === null || id === undefined) return null;
    if (method === "initialize") {
      const params = isObject(message.params) ? message.params : {};
      return result(id, {
        protocolVersion: params.protocolVersion || MCP_PROTOCOL_VERSION,
        capabilities: { tools: {} },
        serverInfo: { name: SERVER_NAME, version: "1" },
      });
    }
    if (method === "tools/list")
      return result(id, {
        tools: this.tools.map((tool) => ({
          name: tool.name,
          description: tool.description,
          inputSchema: tool.inputSchema,
        })),
      });
    if (method === "ping") return result(id, {});
    if (method === "tools/call")
      return this.call(id, isObject(message.params) ? message.params : {});
    return {
      jsonrpc: "2.0",
      id,
      error: { code: -32601, message: `method not found: ${String(method)}` },
    };
  }

  private async call(id: unknown, params: Json): Promise<Json> {
    const meta = isObject(params._meta) ? params._meta : {};
    const toolUseId = meta[TOOL_USE_META];
    const name = String(params.name || "");
    if (typeof toolUseId !== "string" || !toolUseId)
      // Refused rather than guessed at: pairing by order would hand one call
      // another's result the first time two of them overlap.
      return result(
        id,
        errorContent(
          `${name}: the call carried no tool_use id, so HX cannot pair it with its result. Update Claude Code (\`claude update\`).`,
        ),
      );
    const args = isObject(params.arguments) ? params.arguments : {};
    return result(id, await this.handler(toolUseId, name, args));
  }
}

/** Put on the frame queue when stdout ends. */
export class Exit {
  constructor(
    readonly code: number | null,
    readonly stderr: string,
  ) {}
}

/** Thrown into a held call when the CLI or HX gave up on it. */
export class CallCancelled extends Error {
  override name = "CallCancelled";
}

/** Frames in arrival order, read by one consumer at a time. */
class FrameQueue {
  private readonly items: (Json | Exit)[] = [];
  private waiter: (() => void) | null = null;

  push(item: Json | Exit): void {
    this.items.push(item);
    this.waiter?.();
    this.waiter = null;
  }

  /** The next frame, or the signal's reason once it aborts. */
  async get(signal: AbortSignal | null = null): Promise<Json | Exit> {
    while (!this.items.length) {
      if (signal?.aborted) throw signal.reason;
      await new Promise<void>((resolve) => {
        this.waiter = resolve;
        signal?.addEventListener("abort", () => resolve(), { once: true });
      });
    }
    return this.items.shift() as Json | Exit;
  }
}

const stderrSuffix = (stderr: string) => {
  const tail = strip(stderr);
  return tail ? `\n${[...tail].slice(-1500).join("")}` : "";
};

interface Pending {
  resolve(value: Json): void;
  reject(error: Error): void;
}

/**
 * A spawned `claude` and what serves its pipes. Conversation frames are
 * queued in arrival order, followed by one `Exit` when stdout closes. Control
 * requests are answered here and never reach the queue.
 */
export class CliProcess {
  readonly frames = new FrameQueue();
  private readonly stderr: string[] = [];
  private readonly pending = new Map<string, Pending>();
  /** Control requests being served, by id; a cancel stops the answer. */
  private readonly handlers = new Map<string, { cancelled: boolean }>();
  private closed = false;
  private writing: Promise<void> = Promise.resolve();
  private readonly exited: Promise<number | null>;
  private reading: Promise<void> = Promise.resolve();

  private constructor(
    readonly process: ChildProcessWithoutNullStreams,
    readonly server: ToolServer | null,
  ) {
    // Python's returncode: the exit status, or minus the signal that ended it.
    this.exited = new Promise((resolve) =>
      process.once("close", (code, signal) =>
        resolve(code ?? (signal ? -(osConstants.signals[signal] ?? 0) : null)),
      ),
    );
  }

  static async spawn(
    argv: string[],
    options: { cwd: string; env: Record<string, string>; server: ToolServer | null },
  ): Promise<CliProcess> {
    const [command, ...args] = argv as [string, ...string[]];
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: ["pipe", "pipe", "pipe"],
      // Its own process group: Ctrl+C in HX's terminal is HX's to handle.
      detached: true,
    });
    try {
      await new Promise<void>((resolve, reject) => {
        child.once("spawn", resolve);
        child.once("error", reject);
      });
    } catch (error) {
      throw new ProviderError(`Could not start Claude Code (${command}): ${osErrorText(error)}`);
    }
    child.stdin.on("error", () => {});
    const cli = new CliProcess(child, options.server);
    cli.readStderr();
    cli.reading = cli.readStdout();
    return cli;
  }

  get alive(): boolean {
    return !this.closed && this.process.exitCode === null && this.process.signalCode === null;
  }

  stderrTail(): string {
    return this.stderr.join("\n");
  }

  private noteStderr(line: string): void {
    this.stderr.push(line);
    if (this.stderr.length > STDERR_LINES) this.stderr.shift();
  }

  // -- writing -----------------------------------------------------------------

  async send(frame: Json): Promise<void> {
    const stdin = this.process.stdin;
    if (stdin.destroyed || !stdin.writable || this.closed)
      throw new ProviderError("Claude Code is no longer running.");
    const data = `${dumps(frame, { ensureAscii: false })}\n`;
    const write = this.writing.then(
      () =>
        new Promise<void>((resolve, reject) => {
          stdin.write(data, (error) =>
            error
              ? reject(new ProviderError(`Claude Code closed its input: ${osErrorText(error)}`))
              : resolve(),
          );
        }),
    );
    this.writing = write.catch(() => {});
    await write;
  }

  /** Send a control request and wait for its response. */
  async request(payload: Json, deadline: number | null = null): Promise<Json> {
    const id = `hx_${randomUUID().replaceAll("-", "").slice(0, 12)}`;
    const answer = new Promise<Json>((resolve, reject) =>
      this.pending.set(id, { resolve, reject }),
    );
    answer.catch(() => {});
    let timer: NodeJS.Timeout | undefined;
    try {
      await this.send({ type: "control_request", request_id: id, request: payload });
      if (deadline === null) return await answer;
      return await Promise.race([
        answer,
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new ProviderError(
                  `Claude Code did not answer ${repr(payload.subtype)} in ${formatFixed(deadline || 0, 0)}s.${stderrSuffix(this.stderrTail())}`,
                ),
              ),
            deadline * 1000,
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
      this.pending.delete(id);
    }
  }

  initialize(): Promise<Json> {
    return this.request({ subtype: "initialize", hooks: null }, INITIALIZE_TIMEOUT);
  }

  /** A user turn. `client_composed` keeps the CLI from treating the text as
   * its own input: no `@path` expansion, no slash commands. */
  sendUser(content: string | Json[]): Promise<void> {
    return this.send({
      type: "user",
      message: { role: "user", content },
      parent_tool_use_id: null,
      session_id: "",
      client_composed: true,
    });
  }

  // -- reading -----------------------------------------------------------------

  private async readStdout(): Promise<void> {
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    const take = (raw: string) => {
      let frame: unknown;
      try {
        frame = JSON.parse(raw);
      } catch {
        this.noteStderr(rstrip(raw));
        return;
      }
      if (isObject(frame)) this.route(frame);
    };
    try {
      for await (const chunk of this.process.stdout) {
        buffer += decoder.decode(chunk as Buffer, { stream: true });
        let index = buffer.indexOf("\n");
        while (index >= 0) {
          take(buffer.slice(0, index));
          buffer = buffer.slice(index + 1);
          index = buffer.indexOf("\n");
        }
        if (buffer.length > READ_LIMIT) {
          // Nothing after it can be trusted to be framed correctly.
          this.noteStderr("stdout line exceeded the read limit");
          buffer = "";
          break;
        }
      }
      buffer += decoder.decode();
      if (buffer) take(buffer);
    } catch {
      // The pipe went away with the process.
    } finally {
      const code = await this.waitExit();
      for (const waiter of this.pending.values())
        waiter.reject(
          new ProviderError(
            `Claude Code exited before answering.${stderrSuffix(this.stderrTail())}`,
          ),
        );
      this.frames.push(new Exit(code, this.stderrTail()));
    }
  }

  /** Stderr is read concurrently; it gets a moment to drain so an exit caused
   * by an error says what the error was. */
  private async waitExit(): Promise<number | null> {
    let timer: NodeJS.Timeout | undefined;
    const code = await Promise.race([
      this.exited,
      new Promise<null>((resolve) => {
        timer = setTimeout(() => resolve(null), 5000);
      }),
    ]);
    clearTimeout(timer);
    return code;
  }

  private readStderr(): void {
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    const take = (line: string) => {
      const text = rstrip(line);
      if (text) this.noteStderr(text);
    };
    this.process.stderr.on("data", (chunk: Buffer) => {
      buffer += decoder.decode(chunk, { stream: true });
      let index = buffer.indexOf("\n");
      while (index >= 0) {
        take(buffer.slice(0, index));
        buffer = buffer.slice(index + 1);
        index = buffer.indexOf("\n");
      }
    });
    this.process.stderr.on("end", () => take(buffer + decoder.decode()));
    this.process.stderr.on("error", () => {});
  }

  private route(frame: Json): void {
    const kind = frame.type;
    if (kind === "control_response") {
      const response = isObject(frame.response) ? frame.response : {};
      const waiter = this.pending.get(String(response.request_id));
      if (waiter !== undefined) {
        if (response.subtype === "error")
          waiter.reject(
            new ProviderError(`Claude Code refused the request: ${String(response.error)}`),
          );
        else waiter.resolve(isObject(response.response) ? response.response : {});
      }
      return;
    }
    if (kind === "control_request") {
      const id = String(frame.request_id);
      const handle = { cancelled: false };
      this.handlers.set(id, handle);
      void this.serve(id, isObject(frame.request) ? frame.request : {}, handle).finally(() => {
        if (this.handlers.get(id) === handle) this.handlers.delete(id);
      });
      return;
    }
    if (kind === "control_cancel_request") {
      const id = String(frame.request_id);
      const handle = this.handlers.get(id);
      if (handle !== undefined) handle.cancelled = true;
      this.handlers.delete(id);
      return;
    }
    this.frames.push(frame);
  }

  private async serve(id: string, request: Json, handle: { cancelled: boolean }): Promise<void> {
    const subtype = request.subtype;
    let payload: Json;
    try {
      if (subtype !== "mcp_message" || this.server === null)
        throw new Error(`unsupported control request ${repr(subtype)}`);
      if (request.server_name !== SERVER_NAME)
        throw new Error(`unknown MCP server ${repr(request.server_name)}`);
      const answer = await this.server.answer(isObject(request.message) ? request.message : {});
      payload = { mcp_response: answer ?? { jsonrpc: "2.0", result: {} } };
    } catch (error) {
      // The CLI gave up on this request, or HX did: no answer is expected.
      if (error instanceof CallCancelled || handle.cancelled) return;
      await this.send({
        type: "control_response",
        response: { subtype: "error", request_id: id, error: (error as Error).message },
      }).catch(() => {});
      return;
    }
    if (handle.cancelled) return;
    await this.send({
      type: "control_response",
      response: { subtype: "success", request_id: id, response: payload },
    }).catch(() => {});
  }

  // -- ending ------------------------------------------------------------------

  /** Stop the process and everything serving it. Safe to call twice. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    for (const handle of this.handlers.values()) handle.cancelled = true;
    this.handlers.clear();
    const pid = this.process.pid;
    if (this.process.exitCode === null && this.process.signalCode === null && pid !== undefined) {
      // The whole group: the CLI's own children must not outlive it.
      signalGroup(pid, "SIGTERM");
      let timer: NodeJS.Timeout | undefined;
      const stopped = await Promise.race([
        this.exited.then(() => true),
        new Promise<boolean>((resolve) => {
          timer = setTimeout(() => resolve(false), 3000);
        }),
      ]);
      clearTimeout(timer);
      if (!stopped) {
        signalGroup(pid, "SIGKILL");
        await this.exited;
      }
    }
    this.process.stdin.destroy();
    await this.reading;
  }
}

function signalGroup(pid: number, signal: NodeJS.Signals): void {
  try {
    process.kill(-pid, signal);
  } catch {
    // Already gone.
  }
}

/**
 * MCP server lifecycle and tool registration.
 *
 * Config lives in `<cwd>/.hx/mcp.json` and `~/.hx/mcp.json`, project last.
 * Servers connect concurrently at startup with a per-server timeout; a failure
 * is reported and drops that server, never kills the session.
 *
 * Tools are namespaced `mcp__<server>__<tool>` and registered into the same
 * `ToolRegistry` as builtins, sorted deterministically - a server whose tool
 * order varies between runs would invalidate the cache prefix.
 *
 * A remote server that wants a signed-in user is reported as needing sign-in,
 * and `MCPManager.login` runs the browser flow and brings it up without a
 * restart. Nothing opens a browser at startup on its own.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import type { LoginInteraction } from "../auth/oauth/browser.ts";
import { AuthStore } from "../auth/store.ts";
import { ImageError, loadImage } from "../core/images.ts";
import type { ImageBlock } from "../core/messages.ts";
import { compareCodePoints, dumps } from "../json.ts";
import { isFile, join, parent, readText } from "../pathlib.ts";
import { projectMcpFile, userHome } from "../paths.ts";
import { formatFixed, osErrorText, repr, toFloat, truthy } from "../py.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "../tools/base.ts";
import { capOutput, summarizeForUi } from "../tools/output.ts";
import type { ToolRegistry } from "../tools/registry.ts";
import type { WaitingServer } from "./approval.ts";
import {
  HTTPTransport,
  type JsonObject,
  MCPAuthRequired,
  MCPClient,
  MCPError,
  type MCPToolDef,
  StdioTransport,
  type Transport,
} from "./client.ts";
import * as oauth from "./oauth.ts";

/** Python's logging, unconfigured: a warning is its message, on stderr. */
const warn = (message: string) => process.stderr.write(`${message}\n`);

export const HTTP_TRANSPORTS = new Set(["http", "sse"]);
export const NEEDS_SIGN_IN = "needs sign-in";

export interface MCPServerConfig {
  readonly name: string;
  /** `stdio` or `http`. */
  readonly transport: string;
  readonly command: string | null;
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
  readonly url: string | null;
  readonly headers: Readonly<Record<string, string>>;
  readonly enabled: boolean;
  /** Seconds. */
  readonly timeout: number;
  readonly oauth: oauth.OAuthOptions;
  /** Which `mcp.json` it came from. A project's arrives with a clone, so its
   * servers wait for the user's approval - see `approval.ts`. */
  readonly source: "user" | "project";
}

export function serverConfig(
  fields: Pick<MCPServerConfig, "name" | "transport"> & Partial<MCPServerConfig>,
): MCPServerConfig {
  return {
    command: null,
    args: [],
    env: {},
    url: null,
    headers: {},
    enabled: true,
    timeout: 30,
    oauth: oauth.noOptions(),
    source: "user",
    ...fields,
  };
}

/** An HTTP server whose config does not already say how to authenticate: an
 * `Authorization` header in `mcp.json` is the user's choice. */
export const usesOauth = (config: MCPServerConfig) =>
  HTTP_TRANSPORTS.has(config.transport) &&
  !Object.keys(config.headers).some((key) => key.toLowerCase() === "authorization");

export interface ServerStatus {
  readonly name: string;
  readonly connected: boolean;
  readonly toolCount: number;
  readonly error: string | null;
  /** The server refused for want of a token: sign-in is the remedy. */
  readonly needsLogin: boolean;
}

const status = (
  name: string,
  connected: boolean,
  toolCount: number,
  error: string | null = null,
  needsLogin = false,
): ServerStatus => ({ name, connected, toolCount, error, needsLogin });

/** An MCP tool through the normal `Tool` interface. */
export class MCPTool extends Tool {
  /** MCP servers are third-party code: assume a call has side effects. */
  override readonly mutating = true;
  readonly name: string;
  readonly description: string;

  constructor(
    readonly client: MCPClient,
    readonly server: string,
    readonly definition: MCPToolDef,
  ) {
    super();
    this.name = `mcp__${server}__${definition.name}`;
    this.description = definition.description || `${definition.name} (from ${server})`;
  }

  schema(): Record<string, unknown> {
    return this.definition.inputSchema;
  }

  override permissionSpecifier(_params: Params): string | null {
    return this.definition.name;
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    let result: Awaited<ReturnType<MCPClient["callTool"]>>;
    try {
      result = await abortable(this.client.callTool(this.definition.name, params), ctx.signal);
    } catch (error) {
      if (error instanceof MCPError) throw new ToolError(error.message);
      throw error;
    }
    let output = result.text;
    const [images, problems] = await normalise(result.images, this.definition.name);
    if (problems.length) output = [output, ...problems].join("\n");
    const capped = capOutput(output, {
      sessionId: ctx.sessionId,
      toolUseId: ctx.toolUseId,
      charCap: ctx.settings.context.toolOutputCharCap,
      lineCap: ctx.settings.context.toolOutputLineCap,
    });
    return toolResult(capped.text, {
      spilledPath: capped.spilledPath,
      summary: summarizeForUi(output),
      images,
    });
  }
}

/** `work`, or the signal's reason as soon as it aborts. */
function abortable<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise<T>((resolve, reject) => {
    const stop = () => reject(signal.reason);
    signal.addEventListener("abort", stop, { once: true });
    work.then(resolve, reject).finally(() => signal.removeEventListener("abort", stop));
  });
}

/**
 * Normalise a server's images, and say which could not be. A server's image
 * is whatever size it chose, so each goes through the same normalisation as a
 * pasted one; one that fails becomes a line of text.
 */
async function normalise(raw: [string, string][], tool: string): Promise<[ImageBlock[], string[]]> {
  const images: ImageBlock[] = [];
  const problems: string[] = [];
  for (const [index, [data]] of raw.entries()) {
    const label = raw.length > 1 ? `${tool} image ${index + 1}` : `${tool} image`;
    try {
      images.push(await loadImage(Buffer.from(data, "base64"), label));
    } catch (error) {
      if (!(error instanceof ImageError)) throw error;
      problems.push(`[${label} could not be shown: ${error.message}]`);
    }
  }
  return [images, problems];
}

export class MCPManager {
  readonly configs: MCPServerConfig[];
  readonly clients = new Map<string, MCPClient>();
  private readonly statuses = new Map<string, ServerStatus>();
  private readonly tools = new Map<string, MCPToolDef[]>();
  private registry: ToolRegistry | null = null;
  /** Per server, the registry names its tools went in under. */
  private readonly registered = new Map<string, string[]>();
  private readonly refreshing = new Map<string, Promise<void>>();
  private readonly refreshAgain = new Set<string>();

  constructor(
    configs: readonly MCPServerConfig[],
    readonly store: AuthStore = new AuthStore(),
    /** The project's servers that wait for approval, never started here. */
    readonly waiting: readonly WaitingServer[] = [],
  ) {
    this.configs = configs.filter((config) => config.enabled);
  }

  config(name: string): MCPServerConfig | null {
    return this.configs.find((config) => config.name === name) ?? null;
  }

  /** Connect concurrently. Never throws for a single server's failure. */
  async connectAll(): Promise<ServerStatus[]> {
    await Promise.all(this.configs.map((config) => this.connectOrReport(config)));
    return this.status();
  }

  /** `connect`, with anything it did not expect recorded as the server's status. */
  private async connectOrReport(config: MCPServerConfig): Promise<void> {
    try {
      await this.connect(config);
    } catch (error) {
      const reason = (error as Error).message || (error as Error).name || String(error);
      this.statuses.set(config.name, status(config.name, false, 0, reason));
      warn(`mcp server ${config.name} failed to start: ${reason}`);
    }
  }

  private async connect(config: MCPServerConfig): Promise<void> {
    const client = new MCPClient(
      config.name,
      buildTransport(config, this.store),
      config.timeout,
      () => this.toolsChanged(config.name),
    );
    let definitions: MCPToolDef[];
    try {
      await withTimeout(client.initialize(), config.timeout);
      definitions = await client.listTools();
    } catch (error) {
      await client.close();
      if (error instanceof MCPAuthRequired) {
        this.statuses.set(
          config.name,
          status(config.name, false, 0, error.message || NEEDS_SIGN_IN, true),
        );
        return;
      }
      // One bad server must not cost the user their whole session.
      let reason: string;
      if (error instanceof DeadlineExceeded)
        reason = `did not respond within ${formatFixed(config.timeout, 0)}s`;
      else if (error instanceof MCPError) reason = error.message || error.name;
      else if ((error as NodeJS.ErrnoException).code) reason = osErrorText(error);
      else throw error;
      this.statuses.set(config.name, status(config.name, false, 0, reason));
      warn(`mcp server ${config.name} unavailable: ${reason}`);
      return;
    }
    this.clients.set(config.name, client);
    this.tools.set(config.name, definitions);
    this.statuses.set(config.name, status(config.name, true, definitions.length));
  }

  /**
   * Register every connected server's tools under its namespace, servers and
   * tools sorted, so the schema block is byte-identical between runs. The
   * registry is kept, so a server that signs in or changes its tools later
   * lands in it too.
   */
  registerTools(registry: ToolRegistry): void {
    this.registry = registry;
    for (const server of [...this.clients.keys()].sort(compareCodePoints))
      this.registerServer(server);
  }

  private registerServer(server: string): void {
    const registry = this.registry;
    const client = this.clients.get(server);
    if (registry === null || client === undefined) return;
    const names: string[] = [];
    const definitions = [...(this.tools.get(server) ?? [])].sort((a, b) =>
      compareCodePoints(a.name, b.name),
    );
    for (const definition of definitions) {
      const tool = new MCPTool(client, server, definition);
      if (registry.has(tool.name)) {
        warn(`mcp tool ${tool.name} already registered; skipping`);
        continue;
      }
      registry.register(tool);
      names.push(tool.name);
    }
    this.registered.set(server, names);
  }

  private unregisterServer(server: string): void {
    if (this.registry === null) return;
    for (const name of this.registered.get(server) ?? []) this.registry.unregister(name);
    this.registered.delete(server);
  }

  /** Re-list a server's tools after it said they changed - coalesced, so two
   * announcements during one re-list get exactly one more. */
  private toolsChanged(server: string): void {
    if (this.refreshing.has(server)) {
      this.refreshAgain.add(server);
      return;
    }
    const work = this.relist(server).finally(() => this.refreshing.delete(server));
    this.refreshing.set(server, work);
  }

  private async relist(server: string): Promise<void> {
    while (true) {
      this.refreshAgain.delete(server);
      const client = this.clients.get(server);
      if (client === undefined) return;
      let definitions: MCPToolDef[];
      try {
        definitions = await client.listTools();
      } catch (error) {
        if (!(error instanceof MCPError)) throw error;
        warn(`mcp server ${server}: could not re-list tools: ${error.message}`);
        return;
      }
      if (this.clients.get(server) !== client) return;
      this.unregisterServer(server);
      this.tools.set(server, definitions);
      this.registerServer(server);
      this.statuses.set(server, status(server, true, definitions.length));
      if (!this.refreshAgain.has(server)) return;
    }
  }

  /** Drop one server's connection and tools, and connect it again. */
  async reconnect(name: string): Promise<ServerStatus> {
    const config = this.config(name);
    if (config === null) throw new MCPError(`no MCP server named ${repr(name)}`);
    await this.disconnect(name);
    await this.connectOrReport(config);
    this.registerServer(name);
    return this.statuses.get(name) as ServerStatus;
  }

  /** Sign in to one server in the browser, save the token, reconnect. */
  async login(name: string, interaction: LoginInteraction): Promise<ServerStatus> {
    const config = this.config(name);
    if (config === null) throw new MCPError(`no MCP server named ${repr(name)}`);
    await signIn(config, interaction, this.store);
    return this.reconnect(name);
  }

  /** Forget one server's token, and reconnect it without. Whether one was
   * stored. A server with nothing stored is left as it is. */
  async logout(name: string): Promise<boolean> {
    const config = this.config(name);
    if (config === null || !config.url || !usesOauth(config)) return false;
    const removed = this.store.delete(oauth.credentialId(config.url));
    if (removed) await this.reconnect(name);
    return removed;
  }

  private async disconnect(name: string): Promise<void> {
    this.refreshing.delete(name);
    this.unregisterServer(name);
    this.tools.delete(name);
    const client = this.clients.get(name);
    this.clients.delete(name);
    await client?.close();
  }

  /** Backs `/mcp` and `hx mcp list`. */
  status(): ServerStatus[] {
    return this.configs.map(
      (config) => this.statuses.get(config.name) ?? status(config.name, false, 0, "not started"),
    );
  }

  async closeAll(): Promise<void> {
    this.refreshing.clear();
    await Promise.allSettled([...this.clients.values()].map((client) => client.close()));
    this.clients.clear();
  }
}

class DeadlineExceeded extends Error {
  override name = "TimeoutError";
}

/** `work`, or `DeadlineExceeded` after `seconds`. A late rejection of `work`
 * is absorbed: its caller has already moved on. */
function withTimeout<T>(work: Promise<T>, seconds: number): Promise<T> {
  work.catch(() => {});
  let timer: NodeJS.Timeout | undefined;
  return Promise.race([
    work,
    new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new DeadlineExceeded()), seconds * 1000);
    }),
  ]).finally(() => clearTimeout(timer));
}

/** Run the browser sign-in for `config` and save the token. */
export async function signIn(
  config: MCPServerConfig,
  interaction: LoginInteraction,
  store: AuthStore = new AuthStore(),
): Promise<void> {
  if (!config.url || !HTTP_TRANSPORTS.has(config.transport))
    throw new MCPError(`${config.name} is a local server; there is nothing to sign in to.`);
  if (!usesOauth(config))
    throw new MCPError(
      `${config.name} sends its own Authorization header from mcp.json; remove it to sign in with OAuth instead.`,
    );
  const credential = await oauth.login(config.url, interaction, config.oauth, config.name);
  store.save(oauth.credentialId(config.url), credential);
}

function buildTransport(config: MCPServerConfig, store: AuthStore): Transport {
  if (config.transport === "stdio") {
    if (!config.command) throw new MCPError(`${config.name}: stdio transport needs a command`);
    return new StdioTransport(config.command, [...config.args], { ...config.env });
  }
  if (HTTP_TRANSPORTS.has(config.transport)) {
    if (!config.url) throw new MCPError(`${config.name}: http transport needs a url`);
    const auth = usesOauth(config) ? new oauth.OAuthSession(config.url, store) : null;
    return new HTTPTransport(config.url, { ...config.headers }, auth);
  }
  throw new MCPError(`${config.name}: unknown transport ${repr(config.transport)}`);
}

const userMcpFile = () => join(userHome(), "mcp.json");

/** Read and merge user and project `mcp.json`. Project entries win by name. */
export function loadConfigs(cwd: string): MCPServerConfig[] {
  const merged = new Map<string, MCPServerConfig>();
  for (const [path, source] of [
    [userMcpFile(), "user"],
    [projectMcpFile(cwd), "project"],
  ] as const)
    for (const config of readFile(path, source)) merged.set(config.name, config);
  return [...merged.keys()]
    .sort(compareCodePoints)
    .map((name) => merged.get(name) as MCPServerConfig);
}

const isObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const strings = (raw: unknown): Record<string, string> =>
  Object.fromEntries(Object.entries(isObject(raw) ? raw : {}).map(([k, v]) => [k, String(v)]));

function readFile(path: string, source: MCPServerConfig["source"]): MCPServerConfig[] {
  if (!isFile(path)) return [];
  let data: unknown;
  try {
    data = JSON.parse(readText(path));
  } catch (error) {
    warn(`ignoring ${path}: ${(error as Error).message}`);
    return [];
  }
  const root = isObject(data) ? data : {};
  const servers = (
    truthy(root.mcpServers) ? root.mcpServers : truthy(root.servers) ? root.servers : {}
  ) as JsonObject;
  const configs: MCPServerConfig[] = [];
  for (const [name, raw] of Object.entries(isObject(servers) ? servers : {})) {
    if (!isObject(raw)) continue;
    const transport = String(raw.type || (truthy(raw.url) ? "http" : "stdio"));
    configs.push(
      serverConfig({
        name,
        transport,
        command: typeof raw.command === "string" ? raw.command : null,
        args: Array.isArray(raw.args) ? raw.args.map(String) : [],
        env: strings(raw.env),
        url: typeof raw.url === "string" ? raw.url : null,
        headers: strings(raw.headers),
        enabled: "enabled" in raw ? truthy(raw.enabled) : true,
        timeout: "timeout" in raw ? toFloat(raw.timeout) : 30,
        oauth: oauth.optionsFromJson(raw.oauth),
        source,
      }),
    );
  }
  return configs;
}

function readJson(path: string): JsonObject {
  if (!isFile(path)) return {};
  try {
    const data = JSON.parse(readText(path));
    return isObject(data) ? data : {};
  } catch {
    return {};
  }
}

const writeJson = (path: string, data: JsonObject) =>
  writeFileSync(path, `${dumps(data, { indent: 2, sortKeys: true })}\n`);

/** Backs `hx mcp add`. */
export function saveConfig(config: MCPServerConfig, cwd: string, userLevel = false): string {
  const path = userLevel ? userMcpFile() : projectMcpFile(cwd);
  const data = readJson(path);
  if (!isObject(data.mcpServers)) data.mcpServers = {};
  const servers = data.mcpServers as JsonObject;
  const entry: JsonObject = { type: config.transport };
  if (config.command) entry.command = config.command;
  if (config.args.length) entry.args = [...config.args];
  if (Object.keys(config.env).length) entry.env = { ...config.env };
  if (config.url) entry.url = config.url;
  if (Object.keys(config.headers).length) entry.headers = { ...config.headers };
  const block = oauth.optionsToJson(config.oauth);
  if (Object.keys(block).length) entry.oauth = block;
  servers[config.name] = entry;
  mkdirSync(parent(path), { recursive: true });
  writeJson(path, data);
  return path;
}

export function removeConfig(name: string, cwd: string, userLevel = false): boolean {
  const path = userLevel ? userMcpFile() : projectMcpFile(cwd);
  if (!isFile(path)) return false;
  let data: unknown;
  try {
    data = JSON.parse(readText(path));
  } catch {
    return false;
  }
  if (!isObject(data)) return false;
  const servers = isObject(data.mcpServers) ? data.mcpServers : {};
  if (!(name in servers)) return false;
  delete servers[name];
  writeJson(path, data);
  return true;
}

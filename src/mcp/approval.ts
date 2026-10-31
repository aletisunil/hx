/**
 * Which servers in a project's `.hx/mcp.json` the user has approved.
 *
 * A stdio server is a command HX runs, and the project's `mcp.json` arrives
 * with a clone - so, as with hooks, cloning a repository must not be enough to
 * run it. A project's server starts only once the user has approved it, with
 * `hx mcp approve` or by adding it with `hx mcp add`.
 *
 * An approval is for what the server runs or reaches, not for its name: it is
 * kept as a digest of the command, arguments, environment, URL and headers, so
 * a `git pull` that changes the command makes the server wait again. It is
 * kept in HX's own state for the project, never in the repository.
 */

import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dumps } from "../json.ts";
import { isFile, join, parent, readText } from "../pathlib.ts";
import { projectStateDir } from "../paths.ts";
import { join as shellJoin } from "../shlex.ts";
import type { MCPServerConfig } from "./manager.ts";

/** A project server that is not started, and why. */
export interface WaitingServer {
  readonly config: MCPServerConfig;
  /** It was approved, then its command or address changed. */
  readonly changed: boolean;
}

const approvalsFile = (cwd: string) => join(projectStateDir(cwd), "mcp-approved.json");

/** What the server runs or reaches, as one digest. */
export function fingerprint(config: MCPServerConfig): string {
  const fields = {
    transport: config.transport,
    command: config.command,
    args: config.args,
    env: config.env,
    url: config.url,
    headers: config.headers,
  };
  return createHash("sha256")
    .update(dumps(fields, { sortKeys: true }))
    .digest("hex");
}

function readApprovals(cwd: string): Record<string, string> {
  const path = approvalsFile(cwd);
  if (!isFile(path)) return {};
  try {
    const data = JSON.parse(readText(path)) as unknown;
    if (typeof data !== "object" || data === null || Array.isArray(data)) return {};
    return Object.fromEntries(
      Object.entries(data).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string",
      ),
    );
  } catch {
    return {};
  }
}

function writeApprovals(cwd: string, approvals: Record<string, string>): void {
  const path = approvalsFile(cwd);
  mkdirSync(parent(path), { recursive: true });
  writeFileSync(path, `${dumps(approvals, { indent: 2, sortKeys: true })}\n`);
}

export function approve(config: MCPServerConfig, cwd: string): void {
  writeApprovals(cwd, { ...readApprovals(cwd), [config.name]: fingerprint(config) });
}

export function revoke(name: string, cwd: string): void {
  const approvals = readApprovals(cwd);
  if (!(name in approvals)) return;
  delete approvals[name];
  writeApprovals(cwd, approvals);
}

/** Split servers into those that may start and the project's that wait. A
 * user's own `mcp.json` is theirs, so its servers never wait. */
export function partition(
  configs: readonly MCPServerConfig[],
  cwd: string,
): [MCPServerConfig[], WaitingServer[]] {
  const approvals = readApprovals(cwd);
  const startable: MCPServerConfig[] = [];
  const waiting: WaitingServer[] = [];
  for (const config of configs) {
    const approved = approvals[config.name];
    if (config.source === "user" || approved === fingerprint(config)) startable.push(config);
    else waiting.push({ config, changed: approved !== undefined });
  }
  return [startable, waiting];
}

/** What approving the server would run, or the address it would reach. */
export function describeServer(config: MCPServerConfig): string {
  if (config.url) return config.url;
  return shellJoin([config.command ?? "", ...config.args]);
}

/** One line on a server that is not started, with how to start it. */
export function waitingNotice({ config, changed }: WaitingServer): string {
  const why = changed
    ? "has changed since you approved it"
    : "came with the project and has not been approved";
  return (
    `MCP server ${config.name} in .hx/mcp.json ${why}, so HX has not started it. ` +
    `It ${config.url ? "connects to" : "runs"}: ${describeServer(config)}. ` +
    `Run \`hx mcp approve ${config.name}\` to start it.`
  );
}

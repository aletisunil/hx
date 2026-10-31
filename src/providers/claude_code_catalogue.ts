/**
 * Which Claude models a Claude Code login can run.
 *
 * Claude Code knows: the `initialize` handshake answers with the models the
 * signed-in account may pick, each with the reasoning efforts it accepts.
 * Asking costs a process start and no tokens.
 *
 * Ids are the resolved model names (`claude-code/claude-opus-4-8`), with
 * `[1m]` kept where Claude Code runs the model at its long context. An old
 * Claude Code lists old models, so `updateAdvice` compares `claude --version`
 * with the release npm publishes and says what to run.
 */

import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import { childEnv, findCli, missingMessage, PROVIDER_ID } from "../auth/claude_code.ts";
import { request } from "../net.ts";
import { home } from "../pathlib.ts";
import { strip } from "../py.ts";
import { ProviderError } from "./base.ts";
import { CliProcess } from "./claude_code_cli.ts";
import { type ModelInfo, modelInfo, NO_PRICING } from "./models.ts";

export const NAMESPACE = `${PROVIDER_ID}/`;

const CONTEXT = 200_000;
/** For a model Claude Code lists with a `[1m]` suffix: its long-context tier. */
const LONG_CONTEXT = 1_000_000;
const MAX_OUTPUT_TOKENS = 64_000;

/**
 * `[context window, max output]` Claude Code serves a model asked for by its
 * bare name, where every plan it was measured on agrees. Where a model starts,
 * not what it is held to: the first turn's report replaces these. A model
 * served 1M on one plan and 200k on another starts at `CONTEXT`.
 */
const MEASURED: Record<string, [number, number]> = {
  "claude-opus-5-5": [LONG_CONTEXT, 128_000],
  "claude-sonnet-5-5": [LONG_CONTEXT, 128_000],
  "claude-sonnet-5": [LONG_CONTEXT, 128_000],
  "claude-opus-4-7": [LONG_CONTEXT, MAX_OUTPUT_TOKENS],
  "claude-sonnet-4-6": [CONTEXT, 32_000],
  "claude-haiku-4-5": [CONTEXT, 32_000],
};

const EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"];

/** A snapshot date, which HX ids leave off: `claude-haiku-4-5-20251001`. */
export const DATED = /-\d{8}$/;

/** Claude Code's release channels, from the npm registry every install
 * method follows. */
const RELEASES_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags";
const RELEASES_ENV = "HX_CLAUDE_CODE_RELEASES_URL";
/** Stable trails latest, so a CLI behind it is behind on every channel. */
const RELEASE_CHANNEL = "stable";
const RELEASES_TIMEOUT = 5;
const VERSION_TIMEOUT = 10;

const VERSION = /^\s*(\d+(?:\.\d+)+)/;
const CASK = /\/Caskroom\/([^/]+)\//;

type Json = Record<string, unknown>;

/** The models this Claude Code login offers. Throws `ProviderError` when the
 * CLI is missing or does not answer. */
export async function fetchModels(): Promise<ModelInfo[]> {
  return parseModels(await fetchRaw());
}

export async function fetchRaw(): Promise<Json[]> {
  const { CHILD_ENV } = await import("./claude_code.ts");
  const cli = findCli();
  if (cli === null) throw new ProviderError(missingMessage());
  const process = await CliProcess.spawn(
    [
      cli,
      "-p",
      "--output-format",
      "stream-json",
      "--input-format",
      "stream-json",
      "--verbose",
      "--tools",
      "",
      "--setting-sources",
      "",
      "--disable-slash-commands",
      "--no-session-persistence",
    ],
    // Any directory will do: nothing is run, only the handshake.
    { cwd: home(), env: childEnv(CHILD_ENV), server: null },
  );
  let response: Json;
  try {
    response = await process.initialize();
  } finally {
    await process.close();
  }
  const models = response.models;
  if (!Array.isArray(models))
    throw new ProviderError("Claude Code did not list any models. Update it with `claude update`.");
  return models.filter(
    (entry): entry is Json => typeof entry === "object" && entry !== null && !Array.isArray(entry),
  );
}

/** Catalogue entries, one per model, aliases folded into the model they name. */
export function parseModels(entries: readonly Json[]): ModelInfo[] {
  const seen = new Map<string, ModelInfo>();
  for (const entry of entries) {
    const info = parseEntry(entry);
    if (info !== null && !seen.has(info.id)) seen.set(info.id, info);
  }
  return [...seen.values()];
}

export function parseEntry(entry: Json): ModelInfo | null {
  const value = String(entry.value || "");
  // "Default" is an alias of a model listed under its own name too.
  if (!value || value === "default") return null;
  let resolved = String(entry.resolvedModel || value).replace(DATED, "");
  // Claude Code marks a model it runs at its long context in `value`, and
  // asked for without the mark, the model runs at its standard context.
  const longContext = resolved.includes("[1m]") || value.endsWith("[1m]");
  if (longContext && !resolved.includes("[1m]")) resolved += "[1m]";
  const bare = resolved.endsWith("[1m]") ? resolved.slice(0, -4) : resolved;
  const [window, output] = MEASURED[bare] ?? [CONTEXT, MAX_OUTPUT_TOKENS];
  const supported = Array.isArray(entry.supportedEffortLevels) ? entry.supportedEffortLevels : [];
  const levels = EFFORT_ORDER.filter((level) => supported.includes(level));
  return modelInfo({
    id: `${NAMESPACE}${resolved}`,
    name: nameOf(entry, resolved, longContext),
    contextWindow: longContext ? LONG_CONTEXT : window,
    maxOutputTokens: output,
    // The subscription pays; nothing is charged per token.
    pricing: NO_PRICING,
    cacheMode: "implicit",
    supportsTools: true,
    supportsReasoning: Boolean(levels.length || entry.supportsAdaptiveThinking),
    supportsImages: true,
    providerId: PROVIDER_ID,
    isSubscription: true,
    reasoningLevels: levels,
    // Left to Claude Code, which picks per model and per plan.
    defaultReasoningLevel: null,
  });
}

/** `Sonnet 5`, from `"Sonnet 5 · Efficient for routine tasks"`, keeping a
 * note the account should know before picking - "Requires usage credits". */
function nameOf(entry: Json, resolved: string, longContext: boolean): string {
  const description = String(entry.description || "");
  const parts = description
    .split("·")
    .map((part) => strip(part))
    .filter(Boolean);
  let name = parts[0] ?? String(entry.displayName || resolved);
  if (longContext && !name.includes("1M")) name += " (1M context)";
  const notes = parts
    .slice(1)
    .filter(
      (part) => part.toLowerCase().includes("credit") || part.toLowerCase().includes("usage"),
    );
  const note = notes[0];
  if (note !== undefined) name += ` - ${note[0]?.toLowerCase()}${note.slice(1)}`;
  return name;
}

/** `2.1.285`, from `claude --version`; `null` when it cannot be told. */
export async function installedVersion(): Promise<string | null> {
  const cli = findCli();
  if (cli === null) return null;
  return new Promise((resolve) => {
    let out = "";
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(cli, ["--version"], { stdio: ["ignore", "pipe", "ignore"], env: childEnv() });
    } catch {
      resolve(null);
      return;
    }
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve(null);
    }, VERSION_TIMEOUT * 1000);
    child.stdout?.on("data", (chunk: Buffer) => {
      out += chunk.toString("utf8");
    });
    child.on("error", () => {
      clearTimeout(timer);
      resolve(null);
    });
    child.on("close", () => {
      clearTimeout(timer);
      resolve(versionOf(out));
    });
  });
}

/** The current release on the stable channel; `null` when the registry
 * cannot be asked - the answer only ever adds a hint. */
export async function releasedVersion(): Promise<string | null> {
  const url = strip(process.env[RELEASES_ENV] ?? "") || RELEASES_URL;
  let channels: unknown;
  try {
    const response = await request(url, { timeout: RELEASES_TIMEOUT });
    try {
      if (response.status < 200 || response.status >= 300) return null;
      channels = await response.json();
    } finally {
      response.close();
    }
  } catch {
    return null;
  }
  const tag =
    typeof channels === "object" && channels !== null ? (channels as Json)[RELEASE_CHANNEL] : null;
  return typeof tag === "string" ? versionOf(tag) : null;
}

/** What to run for newer models, when `installed` is behind the release. */
export async function updateAdvice(installed: string | null): Promise<string | null> {
  if (installed === null) return null;
  const released = await releasedVersion();
  if (released === null || compareVersions(installed, released) >= 0) return null;
  return `Claude Code ${installed} is behind the current release, ${released}, so newer Claude models are missing. Update it: ${updateCommand(findCli())}`;
}

/** `claude update`, or `brew upgrade --cask <name>` for a Homebrew cask. */
export function updateCommand(cli: string | null): string {
  let target: string | null = null;
  if (cli !== null) {
    try {
      target = realpathSync(cli);
    } catch {
      target = cli;
    }
  }
  const cask = target !== null ? CASK.exec(target) : null;
  return cask ? `brew upgrade --cask ${cask[1]}` : "claude update";
}

function versionOf(text: string): string | null {
  const match = VERSION.exec(text);
  return match ? (match[1] as string) : null;
}

/** Python's tuple comparison of two dotted versions. */
function compareVersions(a: string, b: string): number {
  const left = a.split(".").map(Number);
  const right = b.split(".").map(Number);
  for (let index = 0; index < Math.min(left.length, right.length); index++) {
    const difference = (left[index] as number) - (right[index] as number);
    if (difference) return difference;
  }
  return left.length - right.length;
}

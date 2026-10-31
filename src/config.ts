/**
 * Layered configuration.
 *
 * Precedence, lowest to highest:
 *
 *     defaults
 *       < ~/.hx/settings.json
 *       < <cwd>/.hx/settings.json          (shared, check it in)
 *       < ~/.hx/projects/<project>/settings.local.json   (this machine only)
 *       < environment
 *       < CLI flags
 *
 * Permission rules are kept as raw strings here; parsing them into matchers is
 * the permission engine's job.
 */

import { mkdirSync, readdirSync, renameSync, rmdirSync, unlinkSync, writeFileSync } from "node:fs";
import { dumps } from "./json.ts";
import { isDir, isFile, join, parent, readText, resolve } from "./pathlib.ts";
import {
  legacyProjectGitignoreFile,
  legacyProjectLocalSettingsFile,
  PROJECT_DIR_NAME,
  projectLocalSettingsFile,
  projectMigrationsFile,
  projectSettingsFile,
  userSettingsFile,
} from "./paths.ts";
import { repr, splitLines, strip, toFloat, toInt, truthy } from "./py.ts";

/** How aggressively HX asks before acting. */
export const PERMISSION_MODES = ["plan", "default", "acceptEdits", "bypass"] as const;
export type PermissionMode = (typeof PERMISSION_MODES)[number];

/** What Enter means while a turn is running. */
export const ENTER_WHILE_BUSY = ["queue", "steer"] as const;
export type EnterWhileBusy = (typeof ENTER_WHILE_BUSY)[number];

/** Reasoning efforts weakest first, as the Codex catalogue names them. */
export const EFFORT_ORDER = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
] as const;

export interface PermissionSettings {
  readonly mode: PermissionMode;
  readonly allow: readonly string[];
  readonly ask: readonly string[];
  readonly deny: readonly string[];
  /** Use the OS sandbox when available (seatbelt / bubblewrap). */
  readonly sandbox: boolean;
  /** Permit outbound network from sandboxed commands. */
  readonly allowNetwork: boolean;
}

export interface ContextSettings {
  /** Fraction of the model context window that triggers auto-compaction. */
  readonly compactAt: number;
  /** Turns kept verbatim across a compaction. */
  readonly keepRecentTurns: number;
  readonly toolOutputCharCap: number;
  readonly toolOutputLineCap: number;
  /** Late-inject the branch and files that changed on disk outside the session. */
  readonly gitNotices: boolean;
}

export interface ModelSettings {
  readonly model: string;
  /** Model used by subagents. Falls back to `model`. */
  readonly subagentModel: string | null;
  /** Model used to name a session. Falls back to `model`. */
  readonly titleModel: string | null;
  readonly maxTokens: number;
  readonly temperature: number | null;
  /**
   * How hard a reasoning model should think, for models that offer a choice.
   * `null` leaves each model at the depth its vendor picked for it.
   */
  readonly reasoningEffort: string | null;
  /** Extra Codex model ids to offer, bare (`gpt-5.6-terra`). */
  readonly codexModels: readonly string[];
}

/**
 * System prompt overrides supplied as text. `system` replaces the built-in
 * prompt outright; `append` is added after whichever prompt is in force.
 */
export interface PromptSettings {
  readonly system: string | null;
  readonly append: readonly string[];
}

export interface BashSettings {
  readonly timeoutSeconds: number;
  readonly maxTimeoutSeconds: number;
  /** Override the login shell used for the persistent Bash session. */
  readonly shell: string | null;
}

export interface TuiSettings {
  /** Queueing is the default because it cannot surprise anyone: a steer cuts
   * off the model mid-sentence, which is worth asking for deliberately. */
  readonly enterWhileBusy: EnterWhileBusy;
  /** Draw into the alternate screen, with the prompt pinned to the bottom. */
  readonly fullscreen: boolean;
}

export interface ToolSettings {
  /** `Read` labels lines with content anchors and `Edit` accepts them. */
  readonly hashline: boolean;
}

/** Fully resolved configuration for one HX session. */
export interface Settings {
  readonly cwd: string;
  readonly models: ModelSettings;
  readonly permissions: PermissionSettings;
  readonly context: ContextSettings;
  readonly bash: BashSettings;
  readonly prompt: PromptSettings;
  readonly tui: TuiSettings;
  readonly tools: ToolSettings;
  readonly theme: string;
  /** Skip the startup header. For anyone who has read it already. */
  readonly quietStartup: boolean;
}

/** A settings layer as read from JSON, the environment or the command line. */
export type Layer = Record<string, unknown>;

/** Raised for malformed configuration files. */
export class ConfigError extends Error {
  override name = "ConfigError";
}

/**
 * Resolve the settings layers into one `Settings`.
 *
 * @param cwd Project root. Defaults to the process working directory.
 * @param overrides CLI-flag layer, applied last.
 */
export function loadSettings(cwd?: string | null, overrides?: Layer | null): Settings {
  const root = resolve(cwd ?? process.cwd());
  migrateLocalSettings(root);
  const layers = [
    readSettingsFile(userSettingsFile()),
    readSettingsFile(projectSettingsFile(root)),
    readSettingsFile(projectLocalSettingsFile(root)),
    settingsFromEnv(process.env),
    overrides ?? {},
  ];
  return buildSettings(mergeLayers(layers), root);
}

/**
 * Move a repository's `.hx/settings.local.json` under the user's home.
 *
 * Earlier versions wrote permission grants into the checkout, along with a
 * `.hx/.gitignore` whose only job was to hide them. Both are moved out on the
 * next run rather than left for the user to find in a diff.
 *
 * Returns the new path when something moved, `null` otherwise. Best effort
 * throughout: a read-only checkout is a reason to keep reading the old file,
 * not to refuse to start.
 */
export function migrateLocalSettings(root: string): string | null {
  const legacy = legacyProjectLocalSettingsFile(root);
  if (!isFile(legacy)) return null;

  const destination = projectLocalSettingsFile(root);
  let moving: Layer;
  try {
    moving = readSettingsFile(legacy);
  } catch (error) {
    // Unparseable. Leave it exactly where it is: deleting a file we cannot
    // read loses whatever the user had in it.
    if (error instanceof ConfigError) return null;
    throw error;
  }

  try {
    // The destination wins where both exist - it is the newer location.
    writeSettingsFile(destination, mergeLayers([moving, readSettingsFile(destination)]));
  } catch {
    return null;
  }

  try {
    unlinkSync(legacy);
  } catch {
    // Already gone, or not ours to remove.
  }
  dropLegacyGitignore(root);
  pruneEmptyProjectDir(root);
  return destination;
}

/** Key in the migrations file for the one-time lift below. */
export const LIFTED_PROJECT_GRANTS = "lifted_project_grants";

/**
 * Move auto-written permission grants out of the project's shared settings.
 *
 * Versions before the local layer existed appended every "always allow" to
 * `./.hx/settings.json` - the file a project checks in. Only
 * `permissions.allow` moves; `deny` and `ask` are the rules a project genuinely
 * shares and stay where they are.
 *
 * Runs once, recorded in the project's migrations file, so a grant the user
 * later writes into the shared file by hand is left alone. Returns the rules
 * moved, or `null` when there was nothing to do.
 */
export function liftProjectGrants(root: string): string[] | null {
  if (migrationDone(root, LIFTED_PROJECT_GRANTS)) return null;

  const sharedPath = projectSettingsFile(root);
  let shared: Layer;
  try {
    shared = readSettingsFile(sharedPath);
  } catch (error) {
    if (error instanceof ConfigError) return null;
    throw error;
  }

  const permissions = shared.permissions;
  if (!isObject(permissions)) {
    recordMigration(root, LIFTED_PROJECT_GRANTS);
    return null;
  }

  const grants = permissions.allow;
  if (!Array.isArray(grants) || grants.length === 0) {
    // Nothing to move, but the question is settled either way.
    recordMigration(root, LIFTED_PROJECT_GRANTS);
    return null;
  }

  const moved = grants.map((rule) => String(rule));
  const destination = projectLocalSettingsFile(root);
  let local: Layer;
  try {
    local = readSettingsFile(destination);
  } catch (error) {
    if (error instanceof ConfigError) return null;
    throw error;
  }
  if (!("permissions" in local)) local.permissions = {};
  const localPermissions = local.permissions;
  if (!isObject(localPermissions)) return null;
  const existing = localPermissions.allow;
  const allow = Array.isArray(existing) ? [...existing] : [];
  for (const rule of moved) if (!allow.includes(rule)) allow.push(rule);
  localPermissions.allow = allow;

  delete permissions.allow;
  if (Object.keys(permissions).length === 0) delete shared.permissions;

  try {
    writeSettingsFile(destination, local);
    // The shared file is rewritten without the grants, or removed when they
    // were all it held - an empty settings file is clutter, not content.
    if (Object.keys(shared).length > 0) writeSettingsFile(sharedPath, shared);
    else unlinkSync(sharedPath);
  } catch {
    return null;
  }

  pruneEmptyProjectDir(root);
  recordMigration(root, LIFTED_PROJECT_GRANTS);
  return moved;
}

/** Remove `./.hx` once nothing is left in it. */
function pruneEmptyProjectDir(root: string): void {
  try {
    const directory = join(root, PROJECT_DIR_NAME);
    if (isDir(directory) && readdirSync(directory).length === 0) rmdirSync(directory);
  } catch {
    // Best effort.
  }
}

function readMigrations(root: string): unknown {
  try {
    return JSON.parse(readText(projectMigrationsFile(root)));
  } catch {
    return null;
  }
}

function migrationDone(root: string, name: string): boolean {
  const recorded = readMigrations(root);
  return isObject(recorded) && truthy(recorded[name]);
}

function recordMigration(root: string, name: string): void {
  const read = readMigrations(root);
  const recorded: Layer = isObject(read) ? read : {};
  recorded[name] = true;
  try {
    const path = projectMigrationsFile(root);
    mkdirSync(parent(path), { recursive: true });
    writeFileSync(path, `${dumps(recorded, { indent: 2, sortKeys: true })}\n`);
  } catch {
    // Best effort: the lift runs again next time.
  }
}

/**
 * Remove the `settings.local.json` line HX added to `.hx/.gitignore`.
 *
 * The file is deleted outright once that line was all it held; a user who
 * added rules of their own keeps the file, minus the one HX put there.
 */
function dropLegacyGitignore(root: string): void {
  const path = legacyProjectGitignoreFile(root);
  let lines: string[];
  try {
    lines = splitLines(readText(path));
  } catch {
    return;
  }
  const kept = lines.filter((line) => strip(line) !== "settings.local.json");
  if (kept.length === lines.length) return;
  try {
    if (kept.some((line) => strip(line))) writeFileSync(path, `${kept.join("\n")}\n`);
    else unlinkSync(path);
  } catch {
    // Best effort.
  }
}

function isObject(value: unknown): value is Layer {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function section(data: Layer, name: string): Layer {
  const value = data[name];
  return isObject(value) ? value : {};
}

function pick<T>(data: Layer, key: string, fallback: T): unknown {
  return key in data ? data[key] : fallback;
}

function buildSettings(data: Layer, cwd: string): Settings {
  const models = section(data, "models");
  const permissions = section(data, "permissions");
  const context = section(data, "context");
  const bash = section(data, "bash");
  const prompt = section(data, "prompt");
  const tui = section(data, "tui");
  const tools = section(data, "tools");

  const modeRaw = pick(permissions, "mode", "default");
  if (!PERMISSION_MODES.includes(modeRaw as PermissionMode)) {
    throw new ConfigError(`unknown permission mode: ${repr(modeRaw)}`);
  }

  const effortRaw = models.reasoning_effort ?? null;
  if (effortRaw !== null && !EFFORT_ORDER.includes(effortRaw as never)) {
    throw new ConfigError(
      `unknown models.reasoning_effort: ${repr(effortRaw)}. Known: ${EFFORT_ORDER.join(", ")}`,
    );
  }

  const busyRaw = pick(tui, "enterWhileBusy", pick(tui, "enter_while_busy", "queue"));
  if (!ENTER_WHILE_BUSY.includes(busyRaw as EnterWhileBusy)) {
    throw new ConfigError(`unknown tui.enterWhileBusy: ${repr(busyRaw)}`);
  }

  return {
    cwd,
    models: {
      model: pick(models, "model", "anthropic/claude-sonnet-4.5") as string,
      subagentModel: (models.subagent_model ?? null) as string | null,
      titleModel: (models.title_model ?? null) as string | null,
      maxTokens: toInt(pick(models, "max_tokens", 8192)),
      temperature: (models.temperature ?? null) as number | null,
      reasoningEffort: effortRaw as string | null,
      codexModels: iterate(pick(models, "codex_models", [])).map((entry) => pyStr(entry)),
    },
    permissions: {
      mode: modeRaw as PermissionMode,
      allow: iterate(pick(permissions, "allow", [])) as string[],
      ask: iterate(pick(permissions, "ask", [])) as string[],
      deny: iterate(pick(permissions, "deny", [])) as string[],
      sandbox: truthy(pick(permissions, "sandbox", true)),
      allowNetwork: truthy(pick(permissions, "allow_network", false)),
    },
    context: {
      compactAt: toFloat(pick(context, "compact_at", 0.8)),
      keepRecentTurns: toInt(pick(context, "keep_recent_turns", 6)),
      toolOutputCharCap: toInt(pick(context, "tool_output_char_cap", 25_000)),
      toolOutputLineCap: toInt(pick(context, "tool_output_line_cap", 2_000)),
      gitNotices: truthy(pick(context, "git_notices", true)),
    },
    bash: {
      timeoutSeconds: toInt(pick(bash, "timeout_seconds", 120)),
      maxTimeoutSeconds: toInt(pick(bash, "max_timeout_seconds", 600)),
      shell: (bash.shell ?? null) as string | null,
    },
    prompt: {
      system: (prompt.system ?? null) as string | null,
      append: asList(pick(prompt, "append", [])),
    },
    tui: { enterWhileBusy: busyRaw as EnterWhileBusy, fullscreen: truthy(tui.fullscreen ?? false) },
    tools: { hashline: truthy(pick(tools, "hashline", true)) },
    theme: pick(data, "theme", "dark") as string,
    quietStartup: truthy(pick(data, "quietStartup", pick(data, "quiet_startup", false))),
  };
}

/** `tuple(value)`: the items of a list, the characters of a string, the keys of an object. */
function iterate(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") return [...value];
  if (isObject(value)) return Object.keys(value);
  throw new TypeError(`${repr(value)} is not iterable`);
}

/** `str(value)` for a JSON value. */
function pyStr(value: unknown): string {
  if (value === null) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "string") return value;
  return dumps(value);
}

/** Accept either one string or a list of them - `prompt.append` reads
 * naturally both ways and a bare string is the common case. */
function asList(value: unknown): string[] {
  if (typeof value === "string") return [value];
  return iterate(value).map((item) => pyStr(item));
}

/**
 * Read one `settings.json` layer. A missing file is an empty layer.
 *
 * @throws ConfigError if the file exists but is not a JSON object.
 */
export function readSettingsFile(path: string): Layer {
  if (!isFile(path)) return {};
  let data: unknown;
  try {
    data = JSON.parse(readText(path));
  } catch (error) {
    if (error instanceof SyntaxError)
      throw new ConfigError(`${path}: invalid JSON (${error.message})`);
    throw new ConfigError(`${path}: ${(error as Error).message}`);
  }
  if (!isObject(data)) throw new ConfigError(`${path}: expected a JSON object`);
  return data;
}

/** Write a settings layer atomically (temp file + rename). */
export function writeSettingsFile(path: string, data: Layer): void {
  mkdirSync(parent(path), { recursive: true });
  const tmp = `${path.replace(/\.[^./]*$/, "")}.json.tmp`;
  writeFileSync(tmp, `${dumps(data, { indent: 2, sortKeys: true })}\n`);
  renameSync(tmp, path);
}

/**
 * Permission rule lists are unioned across layers rather than replaced - a
 * project must be able to add a deny rule without discarding the user's.
 * `prompt.append` unions for the same reason.
 */
const UNIONED_KEYS = new Set(["allow", "ask", "deny", "append"]);

/** Deep-merge settings layers. Later layers win; lists are replaced, not
 * concatenated, except the rule lists, which are unioned. */
export function mergeLayers(layers: Layer[]): Layer {
  const result: Layer = {};
  for (const layer of layers) mergeInto(result, layer);
  return result;
}

function mergeInto(target: Layer, source: Layer): void {
  for (const [key, value] of Object.entries(source)) {
    const existing = target[key];
    if (isObject(existing) && isObject(value)) {
      mergeInto(existing, value);
    } else if (UNIONED_KEYS.has(key) && Array.isArray(existing) && Array.isArray(value)) {
      target[key] = [...existing, ...value.filter((item) => !existing.some((e) => equal(e, item)))];
    } else {
      target[key] = value;
    }
  }
}

/** Python `==` for JSON values. */
function equal(a: unknown, b: unknown): boolean {
  return dumps(a, { sortKeys: true }) === dumps(b, { sortKeys: true });
}

const ENV_MAP: Record<string, readonly string[]> = {
  HX_MODEL: ["models", "model"],
  HX_SUBAGENT_MODEL: ["models", "subagent_model"],
  HX_MAX_TOKENS: ["models", "max_tokens"],
  HX_PERMISSION_MODE: ["permissions", "mode"],
  HX_SANDBOX: ["permissions", "sandbox"],
  HX_COMPACT_AT: ["context", "compact_at"],
  HX_GIT_NOTICES: ["context", "git_notices"],
  HX_THEME: ["theme"],
  HX_QUIET_STARTUP: ["quietStartup"],
};

/** The `HX_*` environment layer (e.g. `HX_MODEL`, `HX_PERMISSION_MODE`). */
export function settingsFromEnv(env: NodeJS.ProcessEnv): Layer {
  const layer: Layer = {};
  for (const [name, path] of Object.entries(ENV_MAP)) {
    const raw = env[name];
    if (raw === undefined) continue;
    let cursor = layer;
    for (const part of path.slice(0, -1)) {
      if (!isObject(cursor[part])) cursor[part] = {};
      cursor = cursor[part] as Layer;
    }
    cursor[path.at(-1) as string] = coerce(raw);
  }
  return layer;
}

function coerce(raw: string): unknown {
  const lowered = strip(raw).toLowerCase();
  if (["true", "1", "yes"].includes(lowered)) return true;
  if (["false", "0", "no"].includes(lowered)) return false;
  try {
    return toInt(raw);
  } catch {
    // Not an integer.
  }
  try {
    return toFloat(raw);
  } catch {
    return raw;
  }
}

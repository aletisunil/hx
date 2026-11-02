/**
 * Slash commands.
 *
 * Registered declaratively so the palette, `/help`, and tab completion all
 * read from one source rather than drifting apart. Only implemented commands
 * are registered.
 */

import { statSync } from "node:fs";
import { loginInteractive } from "../auth/claude_code.ts";
import * as codexOAuth from "../auth/oauth/codex.ts";
import { ExpiredCredential, MissingCredential } from "../auth/resolve.ts";
import { AuthStore, OPENROUTER } from "../auth/store.ts";
import {
  ConfigError,
  EFFORT_ORDER,
  type Layer,
  PERMISSION_MODES,
  type PermissionMode,
  readSettingsFile,
  type Settings,
  writeSettingsFile,
} from "../config.ts";
import { type Instructions, loadInstructions, resolveSystemPrompt } from "../core/context.ts";
import { resumableSessions, type Session } from "../core/session.ts";
import { formatCost, formatPercent, formatRenewal, formatTokens } from "../core/usage.ts";
import { compareCodePoints } from "../json.ts";
import { keymap } from "../keys.ts";
import { type MCPManager, type ServerStatus, usesOauth } from "../mcp/manager.ts";
import { describe } from "../net.ts";
import { expandUser, isDir, join } from "../pathlib.ts";
import {
  projectLocalSettingsFile,
  projectSettingsFile,
  tilde,
  userSettingsFile,
} from "../paths.ts";
import type { ModelInfo, ModelRegistry } from "../providers/models.ts";
import { matchModels } from "../providers/models.ts";
import { maskApiKey, saveApiKey } from "../providers/openrouter.ts";
import * as registry from "../providers/registry.ts";
import {
  formatFixed,
  lstrip,
  osErrorText,
  pyLen,
  repr,
  rstrip,
  splitWhitespace,
  strip,
} from "../py.ts";
import { count } from "../text.ts";
import {
  buildTrace,
  defaultTracePath,
  openInBrowser,
  TRACE_FILENAME,
  writeTrace,
} from "../trace.ts";
import type { HXSession } from "./runtime.ts";
import { palettes, THEME } from "./theme.ts";
import { ConfigureDialog, LoginDialog, ProviderDialog, type ProviderRow } from "./views/login.ts";
import {
  DEFAULT_EFFORT_ROW,
  EffortPicker,
  ModelPicker,
  RewindPicker,
  SessionPicker,
} from "./views/pickers.ts";

export type Handler = (ctx: CommandContext, args: string) => Promise<void>;

export class CommandContext {
  constructor(
    readonly app: HXSession,
    readonly settings: Settings,
    readonly registry: CommandRegistry,
  ) {}

  /** The session in force *now*, not the one HX started on: `/resume`
   * rebinds `loop.session`. */
  get session(): Session {
    return this.app.loop.session;
  }
}

export interface Command {
  readonly name: string;
  readonly summary: string;
  readonly handler: Handler;
  readonly argsHint: string;
  readonly takesArgs: boolean;
  /** Other spellings users reach for. They resolve, but only `name` is listed. */
  readonly aliases: readonly string[];
}

const command = (
  name: string,
  summary: string,
  handler: Handler,
  options: { argsHint?: string; takesArgs?: boolean; aliases?: string[] } = {},
): Command => ({
  name,
  summary,
  handler,
  argsHint: options.argsHint ?? "",
  takesArgs: options.takesArgs ?? false,
  aliases: options.aliases ?? [],
});

export class UnknownCommand extends Error {
  override name = "UnknownCommand";
}

export class CommandRegistry {
  private readonly commands = new Map<string, Command>();
  private readonly aliases = new Map<string, string>();

  register(command: Command): void {
    this.commands.set(command.name, command);
    for (const alias of command.aliases) this.aliases.set(alias, command.name);
  }

  get(name: string): Command {
    const found = this.commands.get(this.aliases.get(name) ?? name);
    if (found === undefined) throw new UnknownCommand(name);
    return found;
  }

  all(): Command[] {
    return [...this.commands.keys()]
      .sort(compareCodePoints)
      .map((name) => this.commands.get(name) as Command);
  }

  complete(prefix: string): Command[] {
    const needle = lstrip(prefix, "/").toLowerCase();
    return this.all().filter(
      (c) => c.name.startsWith(needle) || c.aliases.some((a) => a.startsWith(needle)),
    );
  }

  /** Run a `/command`. False when the line is not a command and should be
   * sent to the model instead. */
  async dispatch(ctx: CommandContext, line: string): Promise<boolean> {
    if (!line.startsWith("/")) return false;
    const rest = line.slice(1);
    const space = rest.indexOf(" ");
    const name = space < 0 ? rest : rest.slice(0, space);
    const args = space < 0 ? "" : rest.slice(space + 1);
    let found: Command;
    try {
      found = this.get(strip(name));
    } catch (error) {
      if (!(error instanceof UnknownCommand)) throw error;
      const matches = this.complete(name);
      const hint = matches.length ? ` Did you mean /${(matches[0] as Command).name}?` : "";
      ctx.app.notice(`Unknown command /${name}.${hint} Try /help.`, "error");
      return true;
    }
    await found.handler(ctx, strip(args));
    return true;
  }
}

/** Python's `f"{text:<width}"`: padded by characters, never cut. */
const ljust = (text: string, width: number) => text + " ".repeat(Math.max(0, width - pyLen(text)));
const rjust = (text: string, width: number) => " ".repeat(Math.max(0, width - pyLen(text))) + text;

const errorText = (error: unknown) => (error instanceof Error ? error.message : String(error));

/** Cancelled: by escape in a dialog, or by an interrupt. */
const isCancel = (error: unknown) => (error as Error | null)?.name === "AbortError";

// --- handlers ---------------------------------------------------------------

/** `/model [query]` - fuzzy picker over the models this machine can reach. */
async function cmdModel(ctx: CommandContext, args: string): Promise<void> {
  const models = ctx.app.models;
  if (models === null) {
    ctx.app.notice("No model registry is loaded.", "warning");
    return;
  }
  await ctx.app.auth.checkExternalLogins();
  if (await models.revalidate(ctx.app.auth)) ctx.app.catalogueRefreshed();
  let listed = reachable(ctx, models.all());

  let query = strip(args);
  if (query && listed.length) {
    // Over the reachable models only, as the picker is.
    const matches = matchModels(listed, query);
    // An id typed in full is a choice, not a query.
    const exact = matches.find((m) => m.id.toLowerCase() === query.toLowerCase());
    if (exact !== undefined || matches.length === 1) {
      await switchModel(ctx, (exact ?? (matches[0] as ModelInfo)).id);
      return;
    }
    if (!matches.length) {
      ctx.app.notice(`No model matches ${repr(query)}. Showing the full catalogue.`, "warning");
      query = "";
    } else listed = matches;
  }

  const reload = async (): Promise<[ModelInfo[], string[]]> => {
    try {
      await models.refresh(ctx.app.auth);
    } finally {
      ctx.app.catalogueRefreshed();
    }
    const fresh = reachable(ctx, models.all());
    const errors = models
      .subscriptionErrors()
      .map(([label, error]) => `Could not list this account's ${label} models: ${error}`);
    return [fresh, [...errors, ...modelNotes(models, fresh)]];
  };

  const chosen = await ctx.app.ask(
    new ModelPicker(listed, ctx.session.meta.model, query, modelNotes(models, listed), reload),
  );
  if (chosen) await switchModel(ctx, chosen);
}

/** What goes under the model list: why a model is missing from it. */
function modelNotes(models: ModelRegistry, listed: ModelInfo[]): string[] {
  const notes = models.subscriptionAdvice(new Set(listed.map((model) => model.providerId)));
  if (listed.length) return notes;
  // A recorded failure is the reason, ahead of the remedy.
  const reason = models.refreshError
    ? `No models available: ${models.refreshError}. ctrl+r tries again`
    : "No models available. ctrl+r fetches the catalogue";
  return [`${reason}; /login signs in.`, ...notes];
}

/** Only models on a route this machine has a credential for - plus the
 * route the session is already running on, which is demonstrably working. */
function reachable(ctx: CommandContext, models: ModelInfo[]): ModelInfo[] {
  const allowed = new Set(registry.available(ctx.app.auth).map((spec) => spec.id));
  allowed.add(registry.providerFor(ctx.app.loop.model).id);
  return models.filter((model) => allowed.has(model.providerId));
}

/** Switching resets the cached prefix - the new model has its own cache. A
 * switch that might fail is allowed to fail, where the provider can say so. */
async function switchModel(ctx: CommandContext, modelId: string): Promise<void> {
  const models = ctx.app.models as ModelRegistry;
  const info = models.getOrDefault(modelId);
  try {
    await ctx.app.useRouteFor(modelId);
  } catch (error) {
    if (error instanceof MissingCredential || error instanceof ExpiredCredential) {
      ctx.app.notice(error.message, "error");
      return;
    }
    throw error;
  }
  ctx.app.loop.setModel(modelId, info);
  ctx.app.status.setModel(modelId, { subscription: info.isSubscription, route: info.providerId });
  // Resolved per model, so the depth can change without the user asking.
  ctx.app.status.setEffort(models.displayedEffort(modelId));
  ctx.app.status.setContext(0, info.contextWindow);
  const billing = info.isSubscription
    ? `subscription, ${info.providerId}`
    : `per token, via ${info.providerId}`;
  ctx.app.notice(
    `Model set to ${modelId} (${formatTokens(info.contextWindow)} context, cache: ${info.cacheMode}, ${billing})`,
    "success",
  );
  persistModelChoice(ctx, modelId);
}

/** Record the choice in the user settings file so new sessions reuse it. */
function persistModelChoice(ctx: CommandContext, modelId: string): void {
  if (!persistSetting(ctx, "models", "model", modelId, "the model choice")) return;
  // A higher layer would quietly win next session, so say so.
  if (process.env.HX_MODEL) {
    ctx.app.notice("$HX_MODEL overrides this on the next start.", "warning");
    return;
  }
  warnIfPinned(ctx, "model");
}

/**
 * Write one `<group>.<key>` to the user settings file. Returns false when it
 * could not be written, having said so: the session has changed either way.
 */
function persistSetting(
  ctx: CommandContext,
  group: string,
  key: string,
  value: unknown,
  label: string,
): boolean {
  const path = userSettingsFile();
  try {
    const data = readSettingsFile(path);
    let section = data[group];
    if (typeof section !== "object" || section === null || Array.isArray(section)) {
      section = {};
      data[group] = section;
    }
    if (value === null) delete (section as Layer)[key];
    else (section as Layer)[key] = value;
    writeSettingsFile(path, data);
  } catch (error) {
    const reason = error instanceof ConfigError ? error.message : osErrorText(error);
    ctx.app.notice(`Could not save ${label} to ${path}: ${reason}`, "warning");
    return false;
  }
  return true;
}

/** Say when a project layer will overrule what was just saved. */
function warnIfPinned(ctx: CommandContext, key: string): void {
  for (const path of [
    projectSettingsFile(ctx.settings.cwd),
    projectLocalSettingsFile(ctx.settings.cwd),
  ]) {
    let models: unknown;
    try {
      models = readSettingsFile(path).models;
    } catch (error) {
      if (error instanceof ConfigError) continue;
      throw error;
    }
    const pinned =
      typeof models === "object" && models !== null && !Array.isArray(models)
        ? (models as Layer)[key]
        : null;
    if (pinned) {
      ctx.app.notice(
        `${path} pins models.${key} to ${String(pinned)} and overrides this on the next start.`,
        "warning",
      );
      return;
    }
  }
}

/** `/effort [level|default]` - how hard the model should think. */
async function cmdEffort(ctx: CommandContext, args: string): Promise<void> {
  const models = ctx.app.models as ModelRegistry;
  const modelId = ctx.app.loop.model;
  const info = models.getOrDefault(modelId);
  let choice = strip(args).toLowerCase();

  if (!choice) {
    if (!info.reasoningLevels.length) {
      ctx.app.notice(
        `${modelId} publishes no reasoning levels, so there is nothing to pick from. ctrl+r in /model fetches them for a subscription model.`,
        "warning",
      );
      return;
    }
    const picked = await ctx.app.ask(new EffortPicker(info, models.requestedEffort));
    if (picked === null) return;
    choice = picked;
  }

  let level: string | null;
  if (choice === DEFAULT_EFFORT_ROW || choice === "auto") level = null;
  else if ((EFFORT_ORDER as readonly string[]).includes(choice)) level = choice;
  else {
    const known = [DEFAULT_EFFORT_ROW, ...EFFORT_ORDER].join(", ");
    ctx.app.notice(`Unknown effort ${repr(choice)}. Try one of: ${known}`, "warning");
    return;
  }

  models.setReasoningEffort(level);
  const effective = models.reasoningEffort(modelId);
  ctx.app.status.setEffort(models.displayedEffort(modelId));
  persistSetting(ctx, "models", "reasoning_effort", level, "the reasoning effort");
  warnIfPinned(ctx, "reasoning_effort");

  const name = modelId.slice(modelId.lastIndexOf("/") + 1);
  if (level === null)
    ctx.app.notice(
      `Reasoning effort left to each model. ${name} runs at ${effective || "its own default"}.`,
      "success",
    );
  else if (effective !== level)
    // Clamped rather than refused, and silence would read as the deeper
    // setting having taken.
    ctx.app.notice(
      `Reasoning effort set to ${level}. ${name} tops out at ${effective}, so it runs there.`,
      "success",
    );
  else ctx.app.notice(`Reasoning effort set to ${level}.`, "success");
  if (!info.reasoningLevels.length && level !== null)
    ctx.app.notice(
      `${name} does not publish its reasoning levels, so this is sent as asked and the route decides. Only subscription models use it today.`,
      "warning",
    );
}

/** `/clear` - start a fresh session in the same directory, and wipe the
 * screen and its scrollback. */
async function cmdClear(ctx: CommandContext, _args: string): Promise<void> {
  if (ctx.app.isBusy) {
    ctx.app.notice("Interrupt the running turn before clearing.", "warning");
    return;
  }
  ctx.app.startNewSession();
  ctx.app.clearScreen();
}

/** `/fullscreen [on|off]` - take the whole window, or give it back. */
async function cmdFullscreen(ctx: CommandContext, args: string): Promise<void> {
  const argument = strip(args).toLowerCase();
  let enabled: boolean;
  if (argument === "" || argument === "toggle") enabled = !ctx.app.fullscreen;
  else if (["on", "true", "yes"].includes(argument)) enabled = true;
  else if (["off", "false", "no"].includes(argument)) enabled = false;
  else {
    ctx.app.notice("Usage: /fullscreen [on|off]", "warning");
    return;
  }
  ctx.app.setFullscreen(enabled);
  if (enabled)
    ctx.app.notice(
      `Fullscreen on. The transcript scrolls with pgup/pgdn and ${keymap().text("app.transcript.top")}; your terminal's own scrollback, selection and copy are not available until it is off again.`,
      "success",
    );
  else ctx.app.notice("Fullscreen off. The transcript is your terminal's scrollback again.");
  persistSetting(ctx, "tui", "fullscreen", enabled, "tui.fullscreen");
}

/** `/compact [instructions]` - compact now, optionally steering the summary.
 * The loop announces the step itself. */
async function cmdCompact(ctx: CommandContext, args: string): Promise<void> {
  const compacted = await ctx.app.loop.compact(args || null, "/compact");
  if (!compacted) ctx.app.notice("Nothing to compact yet.", "warning");
}

/** `/todos` - show the current plan. */
async function cmdTodos(ctx: CommandContext, _args: string): Promise<void> {
  ctx.app.showTodos();
}

/** `/resume` - switch to another session from this directory, in place. A
 * session belongs to the directory it was recorded in: its sandbox, rules,
 * prompt and tools. */
async function cmdResume(ctx: CommandContext, _args: string): Promise<void> {
  if (ctx.app.isBusy) {
    ctx.app.notice("Interrupt the running turn before resuming another.", "warning");
    return;
  }
  const sessions = resumableSessions(ctx.settings.cwd, ctx.app.loop.session.meta.sessionId);
  if (!sessions.length) {
    ctx.app.notice("No other sessions to resume in this directory.", "warning");
    return;
  }
  const chosen = await ctx.app.ask(new SessionPicker(sessions));
  if (chosen) ctx.app.resumeSession(chosen);
}

/** `/rewind` - take the session back to an earlier prompt, files and all. */
async function cmdRewind(ctx: CommandContext, _args: string): Promise<void> {
  if (ctx.app.isBusy) {
    ctx.app.notice("Interrupt the running turn before rewinding.", "warning");
    return;
  }
  const points = ctx.app.loop.session.rewindPoints();
  if (!points.length) {
    ctx.app.notice("Nothing to rewind to yet.", "warning");
    return;
  }
  const chosen = await ctx.app.ask(new RewindPicker(points));
  if (chosen === null) return;
  const report = ctx.app.rewindTo(Number(chosen));
  if (report === null) {
    ctx.app.notice("Rewound. No file checkpoints in this session.", "success");
    return;
  }
  ctx.app.notice(`Rewound. ${report.summary()}.`, "success");
  for (const path of report.conflicted)
    ctx.app.notice(`${path} changed since HX wrote it - left as it is.`, "warning");
  for (const [path, reason] of report.skipped)
    ctx.app.notice(`${path} not restored: ${reason}.`, "warning");
}

/** `/title [text]` - show or set the name this session shows in `/resume`. */
async function cmdTitle(ctx: CommandContext, args: string): Promise<void> {
  const session = ctx.app.loop.session;
  if (!strip(args)) {
    const current = session.meta.title;
    ctx.app.notice(
      current ? `Session title: ${current}` : "This session is not named yet.",
      current ? "info" : "warning",
    );
    return;
  }
  session.setTitle(strip(args), { pinned: true });
  ctx.app.notice(`Session title: ${session.meta.title}`, "success");
}

/** `/prompt` - the system prompt this session is actually running with. */
async function cmdPrompt(ctx: CommandContext, _args: string): Promise<void> {
  const inUse = ctx.app.loop.context.systemPrompt;
  const lines = [rstrip(inUse)];
  const resolved = resolveSystemPrompt(ctx.settings.cwd, ctx.settings.prompt);
  lines.push(`\nSource: ${resolved.source}`);
  for (const append of resolved.appends) lines.push(`Appended: ${append}`);
  const inForce = ctx.app.loop.instructions;
  for (const loaded of inForce) lines.push(`Instructions: ${loaded.path}`);
  // Read once at startup to keep the cache prefix stable, so an edit since
  // then is real but not yet in force.
  if (strip(resolved.text) !== strip(inUse))
    lines.push("An override has changed on disk since startup; it applies next run.");
  if (!sameInstructions(loadInstructions(ctx.settings.cwd), inForce))
    lines.push("An AGENTS.md has changed on disk since startup; it applies next run.");
  ctx.app.notice(lines.join("\n"));
}

const sameInstructions = (a: readonly Instructions[], b: readonly Instructions[]) =>
  a.length === b.length &&
  a.every((item, index) => {
    const other = b[index];
    return item.heading === other?.heading && item.path === other.path && item.text === other.text;
  });

/** `/cost` - per-turn token and cost breakdown including cache savings. */
async function cmdCost(ctx: CommandContext, _args: string): Promise<void> {
  const usage = ctx.session.usage;
  const prompts = ctx.session.meta.promptCount;
  // These count provider requests; /resume counts message records.
  let scale = count(usage.turns.length, "API request");
  if (prompts) scale += ` across ${count(prompts, "prompt")}`;
  const lines = [
    `Session cost:  ${formatCost(usage.totalCostUsd)}  over ${scale}`,
    `Input:         ${formatTokens(usage.totalInput)} uncached`,
    `Cache read:    ${formatTokens(usage.totalCacheRead)} (${formatFixed(usage.cacheHitRate * 100, 0)}% of prompt tokens)`,
    `Cache write:   ${formatTokens(usage.totalCacheWrite)}`,
    `Output:        ${formatTokens(usage.totalOutput)}`,
    ...planLines(ctx.app),
  ];
  ctx.app.notice(lines.join("\n"));
}

const PLAN_NAMES: Record<string, string> = {
  "openai-codex": "ChatGPT plan",
  "claude-code": "Claude plan",
};

/** The shown subscription's windows, spelled out with when each comes back. */
function planLines(app: HXSession): string[] {
  const status = app.status;
  const windows = status.planWindows();
  if (!windows.length) return [];
  const lines = ["", `${PLAN_NAMES[status.route] ?? "Subscription"}:`];
  for (const window of windows) {
    const parts: string[] = [];
    if (window.usedPercent !== null) parts.push(`${formatPercent(window.usedPercent)} used`);
    if (window.resetsAt !== null) parts.push(`renews ${formatRenewal(window.resetsAt)}`);
    lines.push(`  ${window.title} window: ${parts.join(", ")}`);
  }
  return lines;
}

/** `/context` - what is occupying the window. */
async function cmdContext(ctx: CommandContext, _args: string): Promise<void> {
  const assembled = ctx.app.lastContext;
  if (assembled === null) {
    ctx.app.notice("No context assembled yet - send a message first.", "warning");
    return;
  }
  const lines = [`Context: ${formatTokens(assembled.totalTokens)} tokens`];
  for (const section of assembled.sections)
    if (section.tokens)
      lines.push(`  ${ljust(section.name, 12)} ${rjust(formatTokens(section.tokens), 8)}`);
  lines.push(`  breakpoints  [${assembled.breakpoints.join(", ")}]`);
  ctx.app.notice(lines.join("\n"));
}

/** `/skills` - installed skills and which are loaded. */
async function cmdSkills(ctx: CommandContext, _args: string): Promise<void> {
  const skills = ctx.app.skills;
  if (!skills.size) {
    ctx.app.notice("No skills installed. Add one at .hx/skills/<name>/SKILL.md", "warning");
    return;
  }
  const lines = [`Skills (${skills.size} installed, listed by name and description only):`];
  for (const skill of [...skills.values()].sort((a, b) => compareCodePoints(a.name, b.name)))
    lines.push(`  ${ljust(skill.name, 20)} ${skill.description}`);
  ctx.app.notice(lines.join("\n"));
}

/** `/agents` - subagent types available to the Task tool. */
async function cmdAgents(ctx: CommandContext, _args: string): Promise<void> {
  const agents = ctx.app.agents;
  const lines = [`Agents (${agents.size}):`];
  for (const agent of [...agents.values()].sort((a, b) => compareCodePoints(a.name, b.name))) {
    const tools = agent.tools?.length ? agent.tools.join(", ") : "all tools except Task";
    lines.push(`  ${ljust(agent.name, 12)} ${agent.description}`);
    lines.push(`  ${ljust("", 12)} tools: ${tools}`);
  }
  ctx.app.notice(lines.join("\n"));
}

export type McpManager = MCPManager;

/** `/mcp [login|logout|reconnect NAME]` - server status, and signing in. */
async function cmdMcp(ctx: CommandContext, args: string): Promise<void> {
  const manager = ctx.app.mcp;
  if (manager === null || (!manager.configs.length && !manager.waiting.length)) {
    ctx.app.notice("No MCP servers configured. Add one with `hx mcp add`.", "warning");
    return;
  }
  const trimmed = strip(args);
  const space = trimmed.indexOf(" ");
  const action = space < 0 ? trimmed : trimmed.slice(0, space);
  let name = space < 0 ? "" : strip(trimmed.slice(space + 1));
  if (["login", "logout", "reconnect"].includes(action)) {
    if (!name) name = action === "login" ? onlyServerNeedingLogin(manager) : "";
    if (!name || manager.config(name) === null) {
      const names = manager.configs.map((config) => config.name).join(", ");
      ctx.app.notice(`Usage: /mcp ${action} <server>. Servers: ${names}`, "warning");
      return;
    }
    if (action === "login") await mcpLogin(ctx, manager, name);
    else if (action === "logout") {
      const config = manager.config(name);
      if (config === null || !usesOauth(config)) {
        ctx.app.notice(`${name} does not sign in with OAuth; nothing to forget.`, "warning");
        return;
      }
      const removed = await manager.logout(name);
      ctx.app.notice(
        removed ? `Signed out of ${name}.` : `Not signed in to ${name}.`,
        removed ? "success" : "warning",
      );
    } else mcpStatusNotice(ctx, name, await manager.reconnect(name));
    return;
  }
  if (action) {
    ctx.app.notice("Usage: /mcp [login|logout|reconnect <server>]", "warning");
    return;
  }
  const lines = ["MCP servers:"];
  for (const status of manager.status()) {
    if (status.connected)
      lines.push(`  ${ljust(status.name, 20)} connected, ${count(status.toolCount, "tool")}`);
    else if (status.needsLogin)
      lines.push(`  ${ljust(status.name, 20)} ${status.error} - /mcp login ${status.name}`);
    else lines.push(`  ${ljust(status.name, 20)} unavailable - ${status.error}`);
  }
  for (const { config } of manager.waiting)
    lines.push(`  ${ljust(config.name, 20)} not approved - run \`hx mcp approve ${config.name}\``);
  const healthy = manager.status().every((s) => s.connected) && !manager.waiting.length;
  ctx.app.notice(lines.join("\n"), healthy ? "info" : "warning");
}

/** The server `/mcp login` means when there is exactly one it could mean. */
function onlyServerNeedingLogin(manager: McpManager): string {
  const waiting = manager.status().filter((s) => s.needsLogin);
  return waiting.length === 1 ? (waiting[0] as ServerStatus).name : "";
}

async function mcpLogin(ctx: CommandContext, manager: McpManager, name: string): Promise<void> {
  const modal = new LoginDialog(`Sign in to ${name}`, () => ctx.app.repaint());
  ctx.app.show(modal);
  let status: ServerStatus;
  try {
    status = await manager.login(name, modal);
  } catch (error) {
    if (isCancel(error)) ctx.app.notice("Sign-in cancelled.", "warning");
    else if ((error as Error | null)?.name === "SignInNotRequired")
      ctx.app.notice(errorText(error), "info");
    // As in /login: a bug here must not cost the user their terminal.
    else ctx.app.notice(`Sign-in failed: ${errorText(error)}`, "error");
    return;
  } finally {
    ctx.app.dismissModal(modal);
  }
  mcpStatusNotice(ctx, name, status, true);
}

function mcpStatusNotice(
  ctx: CommandContext,
  name: string,
  status: ServerStatus,
  signedIn = false,
): void {
  const prefix = signedIn ? `Signed in to ${name}` : name;
  if (status.connected)
    ctx.app.notice(`${prefix}: ${count(status.toolCount, "tool")} available.`, "success");
  else if (status.needsLogin)
    ctx.app.notice(`${name} needs sign-in. Run /mcp login ${name}.`, "warning");
  else ctx.app.notice(`${prefix}, but it is unavailable: ${status.error}`, "error");
}

/** `/hooks` - what is wired to each event, and what was refused. */
async function cmdHooks(ctx: CommandContext, _args: string): Promise<void> {
  const engine = ctx.app.loop.hooks;
  if (engine === null) {
    ctx.app.notice("Hooks are not available in this session.", "warning");
    return;
  }
  const loaded = engine.allCommands();
  const lines: string[] = [];
  if (loaded.size) {
    const total = [...loaded.values()].reduce((sum, commands) => sum + commands.length, 0);
    lines.push(`Hooks (${total}):`);
    for (const [event, commands] of loaded) {
      for (const hook of commands) {
        const matcher = hook.matcher || "*";
        lines.push(`  ${ljust(event, 18)} ${ljust(matcher, 12)} ${hook.command}`);
        lines.push(`  ${ljust("", 18)} ${ljust("", 12)} from ${hook.source}`);
      }
    }
  } else {
    lines.push("No hooks configured.");
    lines.push('  Add them to ~/.hx/settings.json under "hooks".');
  }
  if (engine.ignored.length) {
    lines.push("");
    lines.push(`Ignored, declared in the project's shared settings (${engine.ignored.length}):`);
    lines.push(...engine.ignored.map((entry) => `  ${entry}`));
    lines.push("");
    lines.push("  Hooks run shell commands, so a file that arrives with a clone");
    lines.push("  cannot register one. Copy it into your own settings to run it.");
  }
  ctx.app.notice(lines.join("\n"), engine.ignored.length ? "warning" : "info");
}

/** `/permissions` - the active rules and what is actually enforcing them. */
async function cmdPermissions(ctx: CommandContext, _args: string): Promise<void> {
  const engine = ctx.app.loop.permissions;
  if (engine === null) {
    ctx.app.notice("No permission engine is attached to this session.", "warning");
    return;
  }
  const lines = [`Permission mode: ${engine.mode}`];
  lines.push(
    ctx.app.sandboxActive
      ? `Sandbox: ${ctx.app.sandboxBackend}`
      : "Sandbox: none - commands are NOT confined by the OS",
  );
  if (engine.rules.length) {
    lines.push("");
    lines.push("Rules (deny wins, then ask, then allow):");
    for (const decision of ["deny", "ask", "allow"]) {
      for (const rule of engine.rules) {
        if (rule.decision !== decision) continue;
        const spec = rule.specifier ? `(${rule.specifier})` : "";
        lines.push(`  ${ljust(decision, 5)} ${rule.tool}${spec}    [${rule.source}]`);
      }
    }
  } else {
    lines.push("");
    lines.push('No rules configured. Add them under "permissions" in .hx/settings.json.');
  }
  // A grant is machine-local, so it is kept per project under the user's
  // home instead of being dropped into the checkout.
  lines.push("");
  lines.push('"Always allow" writes, per project and on this machine only, to');
  lines.push(`  ${tilde(projectLocalSettingsFile(ctx.settings.cwd))}`);
  lines.push("Nothing is written into the repository.");
  ctx.app.notice(lines.join("\n"), ctx.app.sandboxActive ? "info" : "warning");
}

/** `/mode [plan|default|acceptEdits|bypass]`. */
async function cmdMode(ctx: CommandContext, args: string): Promise<void> {
  if (!args) {
    ctx.app.notice(`Mode is ${ctx.app.mode}. Options: ${PERMISSION_MODES.join(", ")}`);
    return;
  }
  const wanted = strip(args);
  const match = PERMISSION_MODES.find((m) => m.toLowerCase() === wanted.toLowerCase());
  if (match === undefined) {
    ctx.app.notice(`Unknown mode ${repr(wanted)}. Try /mode with no argument.`, "error");
    return;
  }
  ctx.app.setMode(match);
  const bypass = match === "bypass";
  const note = bypass ? " - every tool call is approved automatically" : "";
  ctx.app.notice(`Permission mode: ${match}${note}`, bypass ? "warning" : "success");
}

const INIT_PROMPT = `Write an AGENTS.md in {directory}, the directory HX was started in. Any AGENTS.md
between the repository root and there is loaded before it, so leave out what
those already say.

Read enough of the codebase to be accurate. Cover: what the project is, how to
build, test and lint it, the layout of the source tree, and any conventions a
newcomer would otherwise get wrong. Be concise and concrete - it is loaded into
context on every session, so every line costs.

If AGENTS.md already exists, improve it rather than replacing it wholesale.`;

/** `/init` - generate an AGENTS.md describing this project. */
async function cmdInit(ctx: CommandContext, _args: string): Promise<void> {
  ctx.app.submitToModel(INIT_PROMPT.replace("{directory}", ctx.settings.cwd));
}

/** `/configure` - session settings, and set or replace the OpenRouter key. */
async function cmdConfigure(ctx: CommandContext, _args: string): Promise<void> {
  const source = ctx.app.auth.source(OPENROUTER);
  let hint = "not set";
  if (source) {
    try {
      const key = ctx.app.auth.resolveStatic(OPENROUTER).token;
      hint = `${maskApiKey(key)} (from ${source})`;
    } catch (error) {
      if (!(error instanceof MissingCredential || error instanceof ExpiredCredential)) throw error;
      hint = `unusable (from ${source})`;
    }
  }
  // Saving to the file while an env var is set would look like a no-op.
  const warning = source?.startsWith("environment")
    ? `${source} takes precedence over the saved key. Unset it for a new key to take effect in future sessions.`
    : "";
  const key = await ctx.app.ask(
    new ConfigureDialog("Configure", configureSummary(ctx.app, hint), warning),
  );
  if (!key) return;
  try {
    saveApiKey(key);
  } catch (error) {
    ctx.app.notice(`Could not save the key: ${osErrorText(error)}`, "error");
    return;
  }
  ctx.app.setApiKey(key);
  // The key itself never reaches the transcript.
  ctx.app.notice(`API key saved (${maskApiKey(key)}) and applied to this session.`, "success");
}

/** `/login [provider]` - sign in to a model route. */
async function cmdLogin(ctx: CommandContext, args: string): Promise<void> {
  let providerId = strip(args);
  if (!providerId) {
    await ctx.app.auth.checkExternalLogins();
    const chosen = await ctx.app.ask(new ProviderDialog(providerRows(ctx)));
    if (!chosen) return;
    providerId = chosen;
  }
  let spec: registry.ProviderSpec;
  try {
    spec = registry.get(providerId);
  } catch (error) {
    if (!(error instanceof registry.UnknownProvider)) throw error;
    const known = registry.SPECS.map((s) => s.id).join(", ");
    ctx.app.notice(`Unknown provider ${repr(providerId)}. Known: ${known}`, "error");
    return;
  }
  // An API key has no flow to run - /configure is already that screen.
  if (!spec.isSubscription) {
    await cmdConfigure(ctx, "");
    return;
  }
  if (registry.holdsOwnLogin(spec.id)) {
    await runClaudeCodeLogin(ctx, spec);
    return;
  }
  await runOAuthLogin(ctx, spec);
}

/** Sign Claude Code in, through its own flow, with HX drawing the prompts.
 * Nothing is saved by HX: the login is Claude Code's. */
async function runClaudeCodeLogin(ctx: CommandContext, spec: registry.ProviderSpec): Promise<void> {
  const modal = new LoginDialog(spec.label, () => ctx.app.repaint());
  ctx.app.show(modal);
  let status: Awaited<ReturnType<typeof loginInteractive>>;
  try {
    status = await loginInteractive(modal);
  } catch (error) {
    if (isCancel(error)) ctx.app.notice("Sign-in cancelled.", "warning");
    else ctx.app.notice(`Sign-in failed: ${errorText(error)}`, "error");
    return;
  } finally {
    ctx.app.dismissModal(modal);
  }
  const who = status.account || "your Claude account";
  const plan = status.plan ? `, ${status.plan} plan` : "";
  const models = await refreshAfterLogin(ctx, spec.id);
  const offer = models ? `${count(models, "model")} available` : "Pick a model with /model";
  ctx.app.notice(`Claude Code signed in (${who}${plan}). ${offer}.`, "success");
}

async function runOAuthLogin(ctx: CommandContext, spec: registry.ProviderSpec): Promise<void> {
  const login = registry.browserLogin(spec.id);
  if (login === null) {
    ctx.app.notice(`${spec.label} has no sign-in flow.`, "error");
    return;
  }
  // Shown before the flow starts: the flow talks to it immediately.
  const modal = new LoginDialog(spec.label, () => ctx.app.repaint());
  ctx.app.show(modal);
  let credential: Awaited<ReturnType<typeof login>>;
  try {
    credential = await login(modal);
  } catch (error) {
    // Any failure, a bug included, goes to the transcript; the modal is torn
    // down either way.
    if (isCancel(error)) ctx.app.notice("Sign-in cancelled.", "warning");
    else ctx.app.notice(`Sign-in failed: ${errorText(error)}`, "error");
    return;
  } finally {
    ctx.app.dismissModal(modal);
  }
  try {
    new AuthStore().save(spec.id, credential);
  } catch (error) {
    ctx.app.notice(`Signed in, but could not save the credential: ${osErrorText(error)}`, "error");
    return;
  }
  // The tokens never reach the transcript. The plan is stated as a fact about
  // the login, not as a verdict on whether models will run.
  const plan = registry.subscriptionPlan(spec.id, ctx.app.auth);
  const signedIn = `Signed in to ${spec.label}${plan ? ` (${plan} plan)` : ""}`;
  if (plan === codexOAuth.FREE_PLAN) {
    ctx.app.notice(
      `${signedIn}. Codex is documented as requiring a paid ChatGPT plan, so the models may be refused. Pick one with /model and see.`,
      "warning",
    );
    return;
  }
  // The model list is per account, so ask now.
  const models = await refreshAfterLogin(ctx, spec.id);
  const offer = models ? `${count(models, "model")} available` : "Pick a model with /model";
  ctx.app.notice(`${signedIn}. ${offer}.`, "success");
}

/** Re-fetch the catalogue, and report how many models the route now offers. */
async function refreshAfterLogin(ctx: CommandContext, providerId: string): Promise<number> {
  const models = ctx.app.models as ModelRegistry;
  try {
    await models.refresh(ctx.app.auth);
  } catch (error) {
    ctx.app.notice(`Model refresh failed: ${describe(error)}`, "warning");
  }
  ctx.app.catalogueRefreshed();
  for (const [label, error] of models.subscriptionErrors())
    ctx.app.notice(`Could not list this account's ${label} models: ${error}`, "warning");
  for (const advice of models.subscriptionAdvice()) ctx.app.notice(advice, "warning");
  return models.all().filter((model) => model.providerId === providerId).length;
}

/** `/logout <provider>` - forget a stored credential. */
async function cmdLogout(ctx: CommandContext, args: string): Promise<void> {
  const providerId = strip(args);
  if (!providerId) {
    const known = registry.SPECS.map((s) => s.id).join(", ");
    ctx.app.notice(`Usage: /logout <provider>. Known: ${known}`, "warning");
    return;
  }
  if (registry.holdsOwnLogin(providerId)) {
    // Signing Claude Code out from here would sign out every Claude Code on
    // the machine - not HX's call.
    ctx.app.notice(
      "HX keeps no Claude credential: the login is Claude Code's. Run `claude auth logout` to sign Claude Code out (everywhere it runs).",
      "warning",
    );
    return;
  }
  if (new AuthStore().delete(providerId))
    ctx.app.notice(`Removed the ${providerId} credential.`, "success");
  else ctx.app.notice(`No stored credential for ${providerId}.`, "warning");
}

/** `/mouse [on|off]` - hand drag-selection back to the terminal, or take it back. */
async function cmdMouse(ctx: CommandContext, args: string): Promise<void> {
  const choice = strip(args).toLowerCase();
  if (!["", "on", "off", "toggle"].includes(choice)) {
    ctx.app.notice(`Usage: /mouse [on|off]. Got ${repr(strip(args))}.`, "warning");
    return;
  }
  const enabled = choice === "on" ? true : choice === "off" ? false : !ctx.app.mouseReporting;
  if (!ctx.app.setMouseReporting(enabled)) {
    ctx.app.notice("This terminal driver has no mouse reporting to toggle.", "warning");
    return;
  }
  if (enabled)
    ctx.app.notice(
      "Mouse reporting on. Scrolling and clicking work in HX; select with the mouse and copy with ctrl+c.",
      "success",
    );
  else
    ctx.app.notice(
      "Mouse reporting off. Your terminal's own selection and copy are back, and HX no longer sees scroll or clicks. /mouse on restores it. (In most macOS terminals, holding alt/option while dragging selects natively without turning this off at all.)",
      "success",
    );
}

/** `/theme [dark|light|ansi]` - switch palette for this session. */
async function cmdTheme(ctx: CommandContext, args: string): Promise<void> {
  const available = palettes();
  if (!args) {
    const options = [...available.keys()].sort(compareCodePoints).join(", ");
    ctx.app.notice(`Theme is ${THEME.palette.name}. Options: ${options}`);
    return;
  }
  const wanted = strip(args).toLowerCase();
  if (!available.has(wanted)) {
    ctx.app.notice(`Unknown theme ${repr(wanted)}. Try /theme with no argument.`, "error");
    return;
  }
  ctx.app.applyTheme(wanted);
  ctx.app.notice(`Theme: ${wanted}`, "success");
}

async function cmdHelp(ctx: CommandContext, _args: string): Promise<void> {
  const commands = ctx.registry.all();
  // Columns sized to their longest entry.
  const nameWidth = Math.max(...commands.map((c) => pyLen(c.name))) + 1;
  const lines = ["Commands"];
  for (const c of commands)
    lines.push(
      `  /${ljust(c.name, nameWidth)} ${c.summary}${c.aliases.length ? ` (also /${c.aliases.join(", /")})` : ""}`,
    );
  // Generated from the registry: a hand-written key list drifts.
  const keys = helpKeys();
  const extras: [string, string][] = [
    ["@path", "completes a file"],
    ["!command", "runs a shell command directly"],
    ["\\ then enter", "inserts a newline in any terminal"],
    ["drag an image", "attaches it"],
  ];
  const keyWidth = Math.max(...[...keys, ...extras].map(([key]) => pyLen(key)));
  lines.push("");
  lines.push("Keys");
  lines.push(...keys.map(([key, description]) => `  ${ljust(key, keyWidth)}  ${description}`));
  lines.push("");
  lines.push(...extras.map(([key, description]) => `  ${ljust(key, keyWidth)}  ${description}`));
  ctx.app.notice(lines.join("\n"));
}

/** Actions worth listing in `/help`, in the order a new user meets them. */
export const HELP_ACTIONS = [
  "tui.input.submit",
  "tui.input.newLine",
  "tui.input.complete",
  "tui.input.pasteImage",
  "app.interrupt",
  "app.clear",
  "app.exit",
  "app.mode.cycle",
  "app.commands",
  "app.model.select",
  "app.tools.expand",
  "app.todos.toggle",
  "app.message.copy",
  "app.transcript.previousPrompt",
  "app.transcript.nextPrompt",
  "app.transcript.top",
  "app.transcript.bottom",
  "app.suspend",
];

/** `[keys, description]` for every action `/help` lists. */
export function helpKeys(): [string, string][] {
  const keys = keymap();
  return HELP_ACTIONS.filter((action) => keys.text(action)).map((action) => [
    keys.text(action),
    keys.description(action),
  ]);
}

/** `/copy` - put the last assistant message on the clipboard. */
async function cmdCopy(ctx: CommandContext, _args: string): Promise<void> {
  const text = ctx.app.lastReplyText();
  if (!text) {
    ctx.app.notice("Nothing to copy yet.", "warning");
    return;
  }
  let used: string;
  try {
    used = await ctx.app.copy(text);
  } catch (error) {
    ctx.app.notice(`Could not copy: ${errorText(error)}`, "error");
    return;
  }
  ctx.app.notice(`Copied the last reply to the clipboard (${used}).`, "success");
}

/**
 * `/trace [path]` - write the whole session to a self-contained HTML page.
 * Traced from the live session, so the turn that just landed is in the page.
 * `--no-open` writes the file and stops there; opening is attempted only
 * where a graphical browser can be reached.
 */
async function cmdTrace(ctx: CommandContext, args: string): Promise<void> {
  const [target, openAfter] = traceArgs(args);
  let written: string;
  try {
    written = writeSessionTrace(ctx, target);
  } catch (error) {
    if (!(error as NodeJS.ErrnoException).code) throw error;
    ctx.app.notice(`Could not write the trace: ${osErrorText(error)}`, "error");
    return;
  }
  const opened = openAfter && (await openInBrowser(written));
  const trailer =
    openAfter && !opened ? " No browser could be launched here; open it yourself." : "";
  ctx.app.notice(
    `Trace written to ${written} (${formatBytes(statSync(written).size)}).${trailer}`,
    "success",
  );
}

/** Split `/trace` arguments into a destination and whether to open it. */
function traceArgs(args: string): [string, boolean] {
  const words = splitWhitespace(args);
  return [words.filter((word) => word !== "--no-open").join(" "), !words.includes("--no-open")];
}

/** Flush, build and write the trace of the session in force. */
function writeSessionTrace(ctx: CommandContext, target: string): string {
  const session = ctx.session;
  session.flush();
  const loop = ctx.app.loop;
  const trace = buildTrace(session, {
    systemPrompt: loop.context.systemPrompt,
    tools: loop.tools.schemas(),
    projectContext: loop.projectContext,
    skillsIndex: loop.skillsIndex,
    live: true,
  });
  let path = defaultTracePath(session);
  if (target) {
    path = expandUser(target);
    if (!path.startsWith("/")) path = join(ctx.settings.cwd, path);
    if (isDir(path)) path = join(path, TRACE_FILENAME);
  }
  return writeTrace(trace, path);
}

/** Compact file size for a notice (e.g. `184 KB`). */
function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${formatFixed(size / 1024, 0)} KB`;
  return `${formatFixed(size / (1024 * 1024), 1)} MB`;
}

/** `/queue [steer <n>|clear]` - see and act on what is waiting. */
async function cmdQueue(ctx: CommandContext, args: string): Promise<void> {
  const queued = ctx.app.queued;
  const argument = strip(args);
  if (argument === "clear" || argument === "drop") {
    if (!queued.length) {
      ctx.app.notice("Nothing queued.", "warning");
      return;
    }
    const dropped = ctx.app.clearQueue();
    ctx.app.notice(`Dropped ${dropped} queued message(s).`, "success");
    return;
  }
  if (argument.startsWith("steer")) {
    const rest = strip(argument.slice("steer".length));
    let index = 1;
    if (rest) {
      if (!/^\d+$/.test(rest)) {
        ctx.app.notice("Usage: /queue steer <n>", "warning");
        return;
      }
      index = Number(rest);
    }
    if (!(index >= 1 && index <= queued.length)) {
      ctx.app.notice(`No queued message ${index}. There are ${queued.length}.`, "warning");
      return;
    }
    ctx.app.steerQueued(index - 1);
    return;
  }
  if (argument) {
    ctx.app.notice("Usage: /queue [steer <n>|clear]", "warning");
    return;
  }
  if (!queued.length) {
    ctx.app.notice("Nothing queued.", "info");
    return;
  }
  const lines = [
    "Queued, oldest first:",
    ...queued.map((text, position) => `  ${position + 1}. ${text}`),
  ];
  lines.push("");
  lines.push(
    `/queue steer <n> sends one now; ${keymap().text("tui.input.steer")} sends the first.`,
  );
  ctx.app.notice(lines.join("\n"), "info");
}

async function cmdQuit(ctx: CommandContext, _args: string): Promise<void> {
  ctx.app.exit();
}

export function buildDefaultCommands(): CommandRegistry {
  const commands = new CommandRegistry();
  for (const item of [
    command("model", "Choose the model", cmdModel, { argsHint: "[query]", takesArgs: true }),
    command("effort", "Set the reasoning depth", cmdEffort, {
      argsHint: "[level|default]",
      takesArgs: true,
    }),
    command("clear", "Start a fresh session", cmdClear),
    command("compact", "Summarise older turns now", cmdCompact, {
      argsHint: "[focus]",
      takesArgs: true,
    }),
    command("todos", "Show the current plan", cmdTodos),
    command("resume", "Resume a previous session", cmdResume),
    command("rewind", "Go back to an earlier prompt", cmdRewind),
    command("title", "Show or set the session name", cmdTitle, {
      argsHint: "[text]",
      takesArgs: true,
    }),
    command("prompt", "Show the system prompt in force", cmdPrompt),
    command("cost", "Token and cost breakdown", cmdCost),
    command("context", "What is filling the context window", cmdContext),
    command("skills", "List installed skills", cmdSkills),
    command("agents", "List subagent types", cmdAgents),
    command("mcp", "MCP servers, and signing in to them", cmdMcp, {
      argsHint: "[login|logout|reconnect <server>]",
      takesArgs: true,
    }),
    command("hooks", "Show configured hooks", cmdHooks),
    command("permissions", "Show permission rules and sandbox", cmdPermissions),
    command("mode", "Set the permission mode", cmdMode, { argsHint: "[mode]", takesArgs: true }),
    command("init", "Generate an AGENTS.md for this project", cmdInit),
    command("theme", "Switch the colour palette", cmdTheme, {
      argsHint: "[name]",
      takesArgs: true,
    }),
    command("fullscreen", "Take the whole window, or give it back", cmdFullscreen, {
      argsHint: "[on|off]",
      takesArgs: true,
    }),
    command("mouse", "Mouse reporting, and with it terminal text selection", cmdMouse, {
      argsHint: "[on|off]",
      takesArgs: true,
    }),
    command("configure", "Settings and the OpenRouter API key", cmdConfigure),
    command("login", "Sign in to a model route", cmdLogin, {
      argsHint: "[provider]",
      takesArgs: true,
    }),
    command("logout", "Forget a stored credential", cmdLogout, {
      argsHint: "<provider>",
      takesArgs: true,
    }),
    command("queue", "Messages waiting for this turn to end", cmdQueue, {
      argsHint: "[steer <n>|clear]",
      takesArgs: true,
    }),
    command("copy", "Copy the last reply to the clipboard", cmdCopy),
    command("trace", "Write the whole session to an HTML page", cmdTrace, {
      argsHint: "[path]",
      takesArgs: true,
    }),
    command("help", "List commands and keys", cmdHelp),
    command("quit", "Exit HX", cmdQuit, { aliases: ["exit", "q"] }),
  ])
    commands.register(item);
  return commands;
}

/** Label/value rows describing the session, for the configure dialog. */
function configureSummary(app: HXSession, keyHint: string): [string, string][] {
  return [
    ["permissions", app.mode],
    ["sandbox", app.sandboxBackend || "none"],
    ["api key", keyHint],
  ];
}

/** `[id, label, hasCredential]` for each provider that can be signed in. */
function providerRows(ctx: CommandContext): ProviderRow[] {
  return registry.SPECS.map((spec) => {
    let present: boolean;
    try {
      present = ctx.app.auth.hasCredential(spec.id);
    } catch {
      present = false;
    }
    return [spec.id, spec.label, present];
  });
}

export type { PermissionMode };

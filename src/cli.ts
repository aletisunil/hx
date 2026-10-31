/**
 * Command-line entry point.
 *
 * `hx`                 launch the TUI in the current directory
 * `hx -p "..."`        print mode: run one prompt headless, stream to stdout
 * `hx resume [id]`     resume a session
 * `hx prompt`          print the resolved system prompt
 * `hx trace [id]`      write a session to a self-contained HTML page
 * `hx mcp ...`         manage MCP servers
 * `hx docs [section]`  print the shipped manual
 * `hx changelog [ver]` print what shipped in each version
 *
 * Print mode exists so the harness is scriptable and E2E-testable without
 * driving a terminal UI.
 */

import { spawnSync } from "node:child_process";
import { constants } from "node:os";
import * as claudeCode from "./auth/claude_code.ts";
import { CallbackError, type LoginInteraction, OAuthError } from "./auth/oauth/browser.ts";
import * as codexOAuth from "./auth/oauth/codex.ts";
import { AuthResolver, ENV_FIRST, ExpiredCredential, MissingCredential } from "./auth/resolve.ts";
import { AuthStore, apiKey, mask, OPENROUTER, TAVILY } from "./auth/store.ts";
import { type Layer, loadSettings } from "./config.ts";
import { EndOfInput, getpass, Interrupted, input, stdinIsTty } from "./console.ts";
import { loadInstructions, resolveSystemPrompt } from "./core/context.ts";
import { ImageError, loadImageFile } from "./core/images.ts";
import type { ImageBlock } from "./core/messages.ts";
import {
  latestSession,
  loadSession,
  readMeta,
  type Session,
  SessionNotFound,
} from "./core/session.ts";
import { changelog, DocsUnavailable, docText, manual, README } from "./docs.ts";
import {
  approve as approveMcp,
  describeServer,
  partition as partitionMcp,
  revoke as revokeMcp,
} from "./mcp/approval.ts";
import { MCPError } from "./mcp/client.ts";
import {
  loadConfigs as loadMcpConfigs,
  MCPManager,
  type MCPServerConfig,
  signIn as mcpSignIn,
  removeConfig as removeMcpConfig,
  type ServerStatus,
  saveConfig as saveMcpConfig,
  serverConfig,
  usesOauth,
} from "./mcp/manager.ts";
import { credentialId as mcpCredentialId, SignInNotRequired } from "./mcp/oauth.ts";
import { describe } from "./net.ts";
import { absolute, expandUser, isDir, join, readText, resolve } from "./pathlib.ts";
import { authFile } from "./paths.ts";
import { DeferredProvider, signInHint } from "./providers/deferred.ts";
import { ModelRegistry } from "./providers/models.ts";
import { saveApiKey } from "./providers/openrouter.ts";
import * as registry from "./providers/registry.ts";
import { formatFixed, osErrorText, purePath, repr, strip } from "./py.ts";
import { buildRuntime, type Runtime } from "./runtime.ts";
import { count } from "./text.ts";
import { buildTrace, defaultTracePath, TRACE_FILENAME, writeTrace } from "./trace.ts";
import { runSession } from "./tui/runtime.ts";
import { VERSION } from "./version.ts";

export const USAGE = `\
hx - an agent harness for the terminal

Usage:
  hx                        Start the interactive TUI
  hx -p, --print PROMPT     Run one prompt headlessly and print the result
  hx resume [SESSION_ID]    Resume a previous session
  hx prompt                 Print the system prompt this directory would use
  hx trace [SESSION_ID] [PATH]
                            Write a session to a self-contained HTML trace
  hx mcp list|add|remove|login|logout
                            Manage MCP servers, and sign in to remote ones
  hx auth [set|clear]       Show or change the OpenRouter API key
  hx docs [SECTION|--all]   Print the manual, or one section of it
  hx changelog [VERSION]    Print what shipped in each version
  hx --version              Show version
  hx --help                 Show this message

Options:
  --model MODEL             Override the model for this run
  --mode MODE               plan | default | acceptEdits | bypass
  --cwd PATH                Run against a different project directory
  --no-sandbox              Disable OS sandboxing (permission rules still apply)
  --system-prompt TEXT      Replace the system prompt (@path reads a file)
  --append-system-prompt TEXT
                            Append to the system prompt; repeatable (@path reads a file)
  --image PATH              Attach an image to the -p prompt; repeatable
`;

/** Commands that print a shipped document and exit, taking no run options. */
const DOC_COMMANDS = new Set(["docs", "changelog"]);

/**
 * Commands with flags of their own, so their arguments are taken as typed.
 *
 * `hx mcp add jira --url URL` and `hx mcp add fs npx -y server` are the
 * documented forms; read as run options, `--url` and `-y` would be refused as
 * unknown before the command ever saw them.
 */
const VERBATIM_COMMANDS = new Set([...DOC_COMMANDS, "mcp", "auth"]);

export interface ParsedArgs {
  command: string;
  prompt: string;
  sessionId: string | null;
  rest: string[];
  overrides: Layer | null;
  cwd: string | null;
  images: string[];
}

export class UsageError extends Error {
  override name = "UsageError";
}

/** `print()`: the text and a newline. */
function print(text: string, stream: NodeJS.WriteStream = process.stdout): void {
  stream.write(`${text}\n`);
}

/** Parse arguments and dispatch. Returns a process exit code. */
export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  const args = [...argv];
  if (args.length && VERBATIM_COMMANDS.has(args[0] as string)) {
    // `hx mcp add fs npx server --help` is a server's argument, not a
    // request for HX's usage.
    return dispatch(args);
  }

  if (args.includes("--version") || args.includes("-V")) {
    print(`hx ${VERSION}`);
    return 0;
  }
  if (args.includes("--help") || args.includes("-h")) {
    print(USAGE);
    return 0;
  }

  return dispatch(args);
}

async function dispatch(args: string[]): Promise<number> {
  let parsed: ParsedArgs;
  try {
    parsed = parseArgs(args);
  } catch (error) {
    if (!(error instanceof UsageError)) throw error;
    print(`error: ${error.message}\n\n${USAGE}`, process.stderr);
    return 2;
  }

  switch (parsed.command) {
    case "prompt":
      return runPromptCommand(parsed);
    case "docs":
      return runDocsCommand(parsed.rest);
    case "changelog":
      return runChangelogCommand(parsed.rest);
    case "auth":
      return runAuthCommand(parsed.rest);
    case "print":
      return runPrintCommand(parsed);
    case "mcp":
      return runMcpCommand(parsed.rest);
    case "trace":
      return runTraceCommand(parsed);
    default:
      return runTuiCommand(parsed);
  }
}

export function parseArgs(args: string[]): ParsedArgs {
  const parsed: ParsedArgs = {
    command: "tui",
    prompt: "",
    sessionId: null,
    rest: [],
    overrides: null,
    cwd: null,
    images: [],
  };

  // The documentation and management commands take their arguments verbatim:
  // `hx docs --all` is a request for a section of the manual, and
  // `hx mcp add jira --url URL` is for `hx mcp`, not a run of HX with an
  // unknown option.
  if (args.length && VERBATIM_COMMANDS.has(args[0] as string)) {
    return { ...parsed, command: args[0] as string, rest: args.slice(1) };
  }

  const overrides: Record<string, Record<string, unknown>> = {};
  const group = (name: string) => {
    overrides[name] ??= {};
    return overrides[name];
  };
  const positional: string[] = [];

  for (let index = 0; index < args.length; index++) {
    const arg = args[index] as string;
    const value = (message: string): string => {
      index += 1;
      if (index >= args.length) throw new UsageError(message);
      return args[index] as string;
    };
    if (arg === "-p" || arg === "--print") {
      parsed.command = "print";
      parsed.prompt = value("-p requires a prompt");
    } else if (arg === "--model") {
      group("models").model = value("--model requires a value");
    } else if (arg === "--mode") {
      group("permissions").mode = value("--mode requires a value");
    } else if (arg === "--cwd") {
      parsed.cwd = purePath(expandUser(value("--cwd requires a path")));
    } else if (arg === "--no-sandbox") {
      group("permissions").sandbox = false;
    } else if (arg === "--system-prompt") {
      group("prompt").system = promptValue(value("--system-prompt requires a value"));
    } else if (arg === "--append-system-prompt") {
      const raw = value("--append-system-prompt requires a value");
      const prompt = group("prompt");
      prompt.append ??= [];
      (prompt.append as string[]).push(promptValue(raw));
    } else if (arg === "--image") {
      // Against where the command was typed, not --cwd: that is where the
      // shell completed the path.
      parsed.images.push(absolute(expandUser(value("--image requires a path"))));
    } else if (arg.startsWith("-")) {
      throw new UsageError(`unknown option ${arg}`);
    } else {
      positional.push(arg);
    }
  }

  if (positional.length) {
    const [head, ...tail] = positional as [string, ...string[]];
    if (["resume", "prompt", "trace", "mcp", "auth"].includes(head)) {
      parsed.command = head;
      parsed.rest = tail;
      if (head === "resume" && tail.length) parsed.sessionId = tail[0] as string;
    } else if (parsed.command !== "print") {
      throw new UsageError(`unknown command ${repr(head)}`);
    }
  }

  if (parsed.images.length && parsed.command !== "print") {
    throw new UsageError("--image goes with -p; in the TUI, paste or drag the image in");
  }
  parsed.overrides = Object.keys(overrides).length ? overrides : null;
  return parsed;
}

/**
 * A prompt flag's value. `@path` reads the file, anything else is literal.
 *
 * A missing file is a usage error, not an empty prompt: silently running with
 * the built-in prompt after the user asked for theirs is the worse failure.
 */
function promptValue(raw: string): string {
  if (!raw.startsWith("@")) return raw;
  const path = purePath(expandUser(raw.slice(1)));
  try {
    return readText(path);
  } catch (error) {
    throw new UsageError(`cannot read prompt file ${path}: ${osErrorText(error)}`);
  }
}

/**
 * `hx prompt` - print the system prompt this directory resolves to.
 *
 * The prompt goes to stdout so it can be piped or diffed; where it came from,
 * and which AGENTS.md files ride alongside it, goes to stderr so it never
 * contaminates that output.
 */
function runPromptCommand(parsed: ParsedArgs): number {
  const settings = loadSettings(parsed.cwd, parsed.overrides);
  const resolved = resolveSystemPrompt(settings.cwd, settings.prompt);
  const instructions = loadInstructions(settings.cwd);

  print(resolved.text);
  print(`[source] ${resolved.source}`, process.stderr);
  for (const append of resolved.appends) print(`[append] ${append}`, process.stderr);
  for (const loaded of instructions) print(`[instructions] ${loaded.path}`, process.stderr);
  return 0;
}

/**
 * `hx docs [SECTION|--all]` - the manual that shipped with this version.
 *
 * Bare `hx docs` lists the sections rather than printing the whole manual: it
 * is thousands of tokens, and the caller is usually a session answering one
 * question about HX itself.
 */
function runDocsCommand(args: string[]): number {
  try {
    if (args.length && (args[0] === "--all" || args[0] === "-a")) print(strip(docText(README)));
    else print(manual(args.length ? args.join(" ") : null));
  } catch (error) {
    if (!(error instanceof DocsUnavailable)) throw error;
    print(`error: ${error.message}`, process.stderr);
    return 1;
  }
  return 0;
}

/**
 * `hx changelog [VERSION]` - what shipped in each version.
 *
 * `VERSION` accepts `0.1.5`, `v0.1.5`, `unreleased` or `latest`.
 */
function runChangelogCommand(args: string[]): number {
  try {
    print(changelog(args[0] ?? null));
  } catch (error) {
    if (!(error instanceof DocsUnavailable)) throw error;
    print(`error: ${error.message}`, process.stderr);
    return 1;
  }
  return 0;
}

/** `f"{text:<width}"`: padded on the right to `width` characters. */
const ljust = (text: string, width: number) =>
  text + " ".repeat(Math.max(0, width - [...text].length));

/**
 * First-run onboarding. Returns true once a key has been saved.
 *
 * The key is read without echo and written straight to `~/.hx/auth.json` with
 * mode 0600; it is never printed back or logged.
 */
export function promptForApiKey(): boolean {
  if (!stdinIsTty()) return false;
  print("HX needs an OpenRouter API key.");
  print("Create one at https://openrouter.ai/keys, then paste it below.");
  const key = readHidden("OpenRouter API key (input hidden): ");
  if (!key) return false;
  saveApiKey(key);
  print(`Saved to ${authFile()} (mode 0600).`);
  return true;
}

/** A hidden line, stripped; `null` when the user gave up on it. */
function readHidden(prompt: string): string | null {
  try {
    return strip(getpass(prompt));
  } catch (error) {
    if (error instanceof EndOfInput || error instanceof Interrupted) {
      print("");
      return null;
    }
    throw error;
  }
}

/**
 * Save a Tavily key for WebSearch/WebFetch. Returns true once stored. Not
 * onboarding: HX runs fine without it, and the two tools are simply absent
 * until it exists.
 */
export function promptForTavilyKey(): boolean {
  if (!stdinIsTty()) return false;
  print("A Tavily API key turns on the WebSearch and WebFetch tools.");
  print("Create one at https://app.tavily.com - the free tier is 1000 searches a month.");
  const key = readHidden("Tavily API key (input hidden): ");
  if (!key) return false;
  new AuthStore().save(TAVILY, apiKey(key));
  print(`Saved to ${authFile()} (mode 0600). WebSearch is on from the next \`hx\`.`);
  return true;
}

/** Web search sits below the model routes: it pays for searches, not turns. */
function printTavilyStatus(resolver: AuthResolver): void {
  const source = resolver.source(TAVILY);
  if (source === null) {
    print(`${ljust(TAVILY, 14)} ${ljust("not configured", 22)} WebSearch and WebFetch are off`);
    return;
  }
  const key = mask(resolver.resolveStatic(TAVILY).token);
  print(`${ljust(TAVILY, 14)} ${ljust(`key ${key}`, 22)} ${source}`);
}

const AUTH_USAGE = `\
hx auth                     Show which routes have a credential
hx auth set [provider]      Paste an API key (hidden) and save it
hx auth clear [provider]    Remove a saved API key
hx auth login [provider]    Sign in - openrouter, openai-codex, devin, claude-code
hx auth logout <provider>   Forget a stored credential

Providers:
  openrouter      API key. The environment (HX_OPENROUTER_API_KEY, then
                  OPENROUTER_API_KEY) takes precedence over the saved file.
  openai-codex    ChatGPT Plus/Pro subscription, signed in over OAuth.
  devin           Devin subscription, signed in over OAuth. A session token in
                  HX_DEVIN_API_KEY or DEVIN_API_KEY is used when none is saved.
  claude-code     Claude Pro/Max subscription, through the \`claude\` CLI and its
                  own login. HX stores nothing; \`claude auth logout\` signs out.
  tavily          API key for WebSearch and WebFetch. Not a model route:
                  it is spent per search, not per token.
`;

/**
 * Drives an OAuth flow from a plain terminal. The paste prompt waits on stdin
 * while the loopback callback can still win the race.
 */
class ConsoleLogin implements LoginInteraction {
  showUrl(url: string, instructions: string): void {
    print(`\n${instructions}\n\n  ${url}\n`);
  }

  showDeviceCode(userCode: string, verificationUri: string): void {
    print(`\nOpen ${verificationUri} and enter this code:\n\n  ${userCode}\n`);
  }

  progress(message: string): void {
    print(message);
  }

  promptPaste(message: string, signal: AbortSignal): Promise<string> {
    // Nothing to read from: let the callback server decide the outcome.
    if (!stdinIsTty()) return new Promise(() => {});
    process.stdout.write(`${message} `);
    return new Promise((resolve, reject) => {
      let buffer = "";
      const onData = (chunk: Buffer) => {
        buffer += chunk.toString("utf8");
        const end = buffer.indexOf("\n");
        if (end >= 0) {
          finish();
          resolve(buffer.slice(0, end).replace(/\r$/, ""));
        }
      };
      const onEnd = () => {
        finish();
        reject(new EndOfInput());
      };
      const finish = () => {
        process.stdin.off("data", onData);
        process.stdin.off("end", onEnd);
        process.stdin.pause();
      };
      signal.addEventListener("abort", finish, { once: true });
      process.stdin.on("data", onData);
      process.stdin.on("end", onEnd);
      process.stdin.resume();
    });
  }
}

/** Sign in to one provider and store the credential. */
async function runLogin(providerId: string): Promise<number> {
  if (providerId === OPENROUTER) return promptForApiKey() ? 0 : 1;

  let spec: registry.ProviderSpec;
  try {
    spec = registry.get(providerId);
  } catch (error) {
    if (!(error instanceof registry.UnknownProvider)) throw error;
    const known = registry.SPECS.map((s) => s.id).join(", ");
    print(`Unknown provider ${repr(providerId)}. Known: ${known}`, process.stderr);
    return 2;
  }
  if (registry.holdsOwnLogin(providerId)) return loginClaudeCode(spec);

  const browserFlow = registry.browserLogin(providerId);
  if (browserFlow === null) {
    print(`${spec.label} has no interactive login.`, process.stderr);
    return 2;
  }

  const interaction = new ConsoleLogin();
  const useDevice = !stdinIsTty() || process.env.HX_LOGIN_DEVICE_CODE === "1";
  // Only Codex offers a device code. Devin's browser flow still completes
  // headless by pasting the redirect URL.
  const flow =
    useDevice && providerId === codexOAuth.PROVIDER_ID ? codexOAuth.loginDeviceCode : browserFlow;

  let credential: Awaited<ReturnType<typeof flow>>;
  try {
    credential = await flow(interaction);
  } catch (error) {
    if (error instanceof OAuthError || error instanceof CallbackError) {
      print(`error: ${error.message}`, process.stderr);
      return 1;
    }
    throw error;
  }

  new AuthStore().save(providerId, credential);
  print(`Signed in to ${spec.label}. Saved to ${authFile()} (mode 0600).`);

  const plan = registry.subscriptionPlan(providerId, new AuthResolver());
  if (plan === codexOAuth.FREE_PLAN) {
    // Said once, as the documented requirement rather than as a prediction.
    print(
      `This account is on the ${plan} plan. Codex is documented as requiring a ` +
        "paid ChatGPT plan, so the models may be refused.",
      process.stderr,
    );
  }
  await reportModelsAfterLogin(providerId);
  return 0;
}

/** Hand the terminal to `claude auth login`. The login stays Claude Code's. */
async function loginClaudeCode(spec: registry.ProviderSpec): Promise<number> {
  const code = claudeCode.loginTerminal();
  const status = claudeCode.status(true);
  if (code !== 0 || status === null || !status.loggedIn) {
    print("Claude Code is not signed in.", process.stderr);
    return code || 1;
  }
  if (!status.isSubscription) {
    print(claudeCode.missingMessage(), process.stderr);
    return 1;
  }
  const plan = status.plan ? `, ${status.plan} plan` : "";
  print(`Signed in to ${spec.label} as ${status.account || "your Claude account"}${plan}.`);
  await reportModelsAfterLogin(spec.id);
  return 0;
}

/**
 * Refresh the catalogue and name what this account can now run. Done here, at
 * the one moment the answer changes: the first thing anyone does after signing
 * in is pick a model.
 */
async function reportModelsAfterLogin(providerId: string): Promise<void> {
  const models = new ModelRegistry();
  models.loadCache();
  try {
    await models.refresh(new AuthResolver());
  } catch (error) {
    print(`Could not refresh the model list: ${describe(error)}`, process.stderr);
  }
  for (const [label, error] of models.subscriptionErrors()) {
    print(`Could not list this account's ${label} models: ${error}`, process.stderr);
  }
  for (const advice of models.subscriptionAdvice()) print(advice, process.stderr);

  const available = models
    .all()
    .filter((m) => m.providerId === providerId)
    .map((m) => m.id);
  if (available.length === 0) {
    print("No models are listed for this account yet. Try `hx --model <id>`.", process.stderr);
    return;
  }
  print("\nModels on this subscription:");
  for (const modelId of available) print(`  ${modelId}`);
  print(`\nSelect one with: hx --model ${available[0]}`);
}

/**
 * Show or change stored credentials. The TUI has /login and /configure; this
 * is the same thing for a headless machine.
 */
async function runAuthCommand(args: string[]): Promise<number> {
  const action = args[0] ?? "status";
  if (["--help", "-h", "help"].includes(action)) {
    print(AUTH_USAGE);
    return 0;
  }
  const resolver = new AuthResolver();

  if (action === "status") {
    await resolver.checkExternalLogins();
    let signedIn = false;
    for (const spec of registry.SPECS) {
      const source = resolver.source(spec.id);
      if (source === null) {
        print(`${ljust(spec.id, 14)} not signed in`);
        continue;
      }
      signedIn = true;
      print(`${ljust(spec.id, 14)} ${ljust(credentialLabel(resolver, spec), 22)} ${source}`);
      if (source.startsWith("environment")) {
        // Only an ENV_FIRST route is actually overridden by its variable.
        print(
          ENV_FIRST.has(spec.id)
            ? `${ljust("", 14)} the environment overrides anything saved in ${authFile()}.`
            : `${ljust("", 14)} a credential saved in ${authFile()} would take precedence.`,
        );
      }
    }
    printTavilyStatus(resolver);
    if (!signedIn) {
      print(
        "\nRun `hx auth set` to save an OpenRouter key, or `hx auth login` " +
          "to sign in to a subscription.",
      );
      return 1;
    }
    return 0;
  }

  if (action === "set") {
    const target = args[1] ?? OPENROUTER;
    if (target !== OPENROUTER && target !== TAVILY) {
      print(
        `${target} does not take a pasted key. Try \`hx auth login ${target}\`.`,
        process.stderr,
      );
      return 2;
    }
    const saved = target === TAVILY ? promptForTavilyKey() : promptForApiKey();
    if (!saved) {
      print("No key entered.", process.stderr);
      return 1;
    }
    return 0;
  }

  if (action === "login") return runLogin(args[1] ?? chooseProvider());

  if (action === "logout") {
    const target = args[1];
    if (target === undefined) {
      print(AUTH_USAGE, process.stderr);
      return 2;
    }
    if (registry.holdsOwnLogin(target)) {
      print(
        "HX keeps no Claude credential: the login is Claude Code's. Run " +
          "`claude auth logout` to sign Claude Code out (everywhere it runs).",
      );
      return 0;
    }
    if (new AuthStore().delete(target)) {
      print(`Removed the ${target} credential from ${authFile()}.`);
      return 0;
    }
    print(`No stored credential for ${target}.`);
    return 0;
  }

  if (action === "clear") {
    const target = args[1] ?? OPENROUTER;
    if (new AuthStore().delete(target))
      print(`Removed the saved ${target} key from ${authFile()}.`);
    else print(`No saved ${target} key to remove.`);
    return 0;
  }

  print(AUTH_USAGE, process.stderr);
  return 2;
}

/** A one-word description of the credential, never the secret itself. */
function credentialLabel(resolver: AuthResolver, spec: registry.ProviderSpec): string {
  if (spec.isSubscription) {
    const detail =
      registry.subscriptionPlan(spec.id, resolver) ||
      registry.subscriptionAccount(spec.id, resolver);
    return detail ? `signed in (${detail})` : "signed in";
  }
  try {
    return `key ${mask(resolver.resolveStatic(spec.id).token)}`;
  } catch (error) {
    if (error instanceof MissingCredential || error instanceof ExpiredCredential) return "unusable";
    throw error;
  }
}

/** Ask which provider to sign in to. Defaults to OpenRouter when piped. */
function chooseProvider(): string {
  if (!stdinIsTty()) return OPENROUTER;
  print("Sign in to:");
  for (const [index, spec] of registry.SPECS.entries()) print(`  ${index + 1}  ${spec.label}`);
  // Enter still means OpenRouter, wherever it sits in the list.
  const fallback = String(registry.SPECS.findIndex((s) => s.id === OPENROUTER) + 1);
  let choice: string;
  try {
    choice = strip(input(`Choice [${fallback}]: `)) || fallback;
  } catch (error) {
    if (error instanceof EndOfInput || error instanceof Interrupted) {
      print("");
      return OPENROUTER;
    }
    throw error;
  }
  const index = Number.parseInt(choice, 10);
  const spec = /^[+-]?\d+$/.test(strip(choice)) ? registry.SPECS[index - 1] : undefined;
  return index >= 1 && spec !== undefined ? spec.id : OPENROUTER;
}

/**
 * Headless single-prompt run. Streams assistant text to stdout and tool
 * activity to stderr, so stdout stays pipeable.
 */
async function runPrintCommand(parsed: ParsedArgs): Promise<number> {
  const images: ImageBlock[] = [];
  try {
    for (const path of parsed.images) images.push(await loadImageFile(path));
  } catch (error) {
    if (!(error instanceof ImageError)) throw error;
    print(`error: ${error.message}`, process.stderr);
    return 2;
  }

  let runtime: Runtime;
  try {
    runtime = buildRuntime(parsed, resumeTarget(parsed));
  } catch (error) {
    return report(error);
  }

  const render = (async () => {
    for await (const event of runtime.bus.subscribe()) {
      switch (event.kind) {
        case "TextDelta":
          process.stdout.write(event.text);
          break;
        case "ToolCallStarted":
          print(`[tool] ${event.name} ${repr(event.input)}`, process.stderr);
          break;
        case "PermissionRequested":
          print(`[permission] ${event.description}`, process.stderr);
          break;
        case "ToolCallFinished": {
          const marker = event.isError ? "error" : "ok";
          const reason = event.detail ? ` ${event.detail}` : "";
          print(
            `[tool] ${marker} (${formatFixed(event.durationMs, 0)}ms)${reason}`,
            process.stderr,
          );
          break;
        }
        case "ErrorRaised":
          print(`[error] ${event.message}`, process.stderr);
          break;
      }
    }
  })();

  let info = runtime.loop.modelInfo;
  if (images.length && (info === null || !info.supportsImages)) {
    // Print mode does not refresh the catalogue - a scripted run should not
    // wait on it - but withholding an image on the word of a stale cache is
    // worse than the wait.
    await runtime.refreshModelsIfStale();
    info = runtime.loop.modelInfo;
  }
  for (const notice of runtime.notices) print(`[hx] ${notice}`, process.stderr);
  if (images.length && (info === null || !info.supportsImages)) {
    const name = info !== null ? info.name : runtime.loop.model;
    print(
      `[hx] ${name} does not accept images; it is told they were attached, not shown them`,
      process.stderr,
    );
  }
  for (const status of await runtime.connectMcp()) {
    if (status.connected) continue;
    if (status.needsLogin)
      print(
        `[mcp] ${status.name} ${status.error} - run \`hx mcp login ${status.name}\``,
        process.stderr,
      );
    else print(`[mcp] ${status.name} unavailable: ${status.error}`, process.stderr);
  }

  let result: Awaited<ReturnType<typeof runtime.loop.run>>;
  try {
    result = await runtime.loop.run(parsed.prompt, images);
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 50));
    runtime.bus.close();
    await render;
    await runtime.aclose();
  }

  print("");
  const usage = runtime.session.usage;
  print(
    `[usage] in=${usage.totalInput} out=${usage.totalOutput} cache_read=${usage.totalCacheRead} ` +
      `cache_write=${usage.totalCacheWrite} cost=$${formatFixed(usage.totalCostUsd, 4)}`,
    process.stderr,
  );
  return result.error ? 1 : 0;
}

/**
 * Whether a lone `hx trace` argument is a destination, not a session id. Ids
 * are `20260918-215601-7608a6b0`: no separator, no extension, no `~` or `.`.
 */
const looksLikeAPath = (arg: string) =>
  arg.includes("/") ||
  arg.startsWith("~") ||
  arg.startsWith(".") ||
  arg.toLowerCase().endsWith(".html");

/**
 * `hx trace [SESSION_ID] [PATH]` - write a session out as one HTML page. It
 * reads the transcript rather than running anything, so it works on a
 * session that ended months ago and on one another terminal has open now.
 */
function runTraceCommand(parsed: ParsedArgs): number {
  const rest = parsed.rest;
  const cwd = resolve(parsed.cwd ?? process.cwd());
  let target: string | null = null;
  let sessionId: string | null = null;
  if (rest.length > 1) [sessionId, target] = rest as [string, string];
  else if (rest.length) {
    if (looksLikeAPath(rest[0] as string)) target = rest[0] as string;
    else sessionId = rest[0] as string;
  }
  if (sessionId === null) {
    const latest = latestSession(cwd);
    if (latest === null) {
      print(`error: no sessions recorded for ${cwd}`, process.stderr);
      return 1;
    }
    sessionId = latest.sessionId;
  }
  let session: Session;
  try {
    session = loadSession(sessionId);
  } catch (error) {
    if (!(error instanceof SessionNotFound)) throw error;
    print(`error: no session ${repr(sessionId)}`, process.stderr);
    return 1;
  }
  let destination = defaultTracePath(session);
  if (target !== null) {
    // Relative to the shell, not to `--cwd`: that flag picks the project to
    // trace, and `out.html` means the directory the user is standing in.
    destination = purePath(expandUser(target));
    if (isDir(destination)) destination = join(destination, TRACE_FILENAME);
  }
  let written: string;
  try {
    written = writeTrace(buildTrace(session), destination);
  } catch (error) {
    if (!(error as NodeJS.ErrnoException).code) throw error;
    print(`error: could not write the trace: ${osErrorText(error)}`, process.stderr);
    return 1;
  }
  // The path on stdout alone, so `open "$(hx trace)"` works.
  print(written);
  return 0;
}

const MCP_USAGE = `\
hx mcp list                                  Show configured servers
hx mcp add NAME COMMAND [ARGS...]            Add a stdio server
hx mcp add NAME --url URL [--header K:V]...  Add an HTTP server
hx mcp approve NAME                          Let a server from the project's .hx/mcp.json start
hx mcp login NAME                            Sign in to an HTTP server in the browser
hx mcp logout NAME                           Forget a server's sign-in
hx mcp remove NAME                           Remove a server

Add --user to write to ~/.hx/mcp.json instead of the project's .hx/mcp.json.
A server in the project's .hx/mcp.json came with the project, so it starts only
once approved: \`hx mcp add\` approves what it adds, and \`hx mcp approve\` the rest.
An HTTP server that asks for sign-in gets it over OAuth unless a --header
already sends Authorization.
`;

/** `hx mcp ...`: manage the configured MCP servers. */
async function runMcpCommand(given: string[]): Promise<number> {
  if (given.length && ["--help", "-h", "help"].includes(given[0] as string)) {
    print(MCP_USAGE);
    return 0;
  }
  const userLevel = given.includes("--user");
  const args = given.filter((arg) => arg !== "--user");
  const cwd = process.cwd();

  if (!args.length || args[0] === "list") {
    const configs = loadMcpConfigs(cwd);
    if (!configs.length) {
      print("No MCP servers configured.\n");
      print(MCP_USAGE);
      return 0;
    }
    // Listing must not start what the user has not approved: that would run it.
    const [startable, waiting] = partitionMcp(configs, cwd);
    for (const { config, changed } of waiting) {
      const why = changed ? "changed since approved" : "not approved";
      print(
        `${ljust(config.name, 20)} ${why} - runs ${describeServer(config)}; run \`hx mcp approve ${config.name}\``,
      );
    }
    for (const status of await probeServers(startable)) {
      if (status.needsLogin) {
        print(`${ljust(status.name, 20)} ${status.error} - run \`hx mcp login ${status.name}\``);
        continue;
      }
      const mark = status.connected ? "ok" : "unavailable";
      const detail = status.error ? ` - ${status.error}` : ` (${count(status.toolCount, "tool")})`;
      print(`${ljust(status.name, 20)} ${mark}${detail}`);
    }
    return 0;
  }

  if (args[0] === "add") {
    if (args.length < 3) {
      print(MCP_USAGE, process.stderr);
      return 2;
    }
    const name = args[1] as string;
    let config: MCPServerConfig;
    if (args[2] === "--url") {
      let url: string;
      let headers: Record<string, string>;
      try {
        [url, headers] = parseHttpAdd(args.slice(3));
      } catch (error) {
        if (!(error instanceof UsageError)) throw error;
        print(`error: ${error.message}\n\n${MCP_USAGE}`, process.stderr);
        return 2;
      }
      config = serverConfig({ name, transport: "http", url, headers });
    } else {
      config = serverConfig({
        name,
        transport: "stdio",
        command: args[2] as string,
        args: args.slice(3),
      });
    }
    print(`Added ${name} to ${saveMcpConfig(config, cwd, userLevel)}`);
    // The user typed it, so it needs no further approval.
    if (!userLevel) approveMcp({ ...config, source: "project" }, cwd);
    return 0;
  }

  if (args[0] === "approve") {
    if (args.length < 2) {
      print(MCP_USAGE, process.stderr);
      return 2;
    }
    const server = loadMcpConfigs(cwd).find((c) => c.name === args[1]);
    if (server === undefined) {
      print(`No server named ${args[1]}.`, process.stderr);
      return 1;
    }
    if (server.source === "user") {
      print(`${server.name} is in your own ~/.hx/mcp.json; it needs no approval.`);
      return 0;
    }
    approveMcp(server, cwd);
    const does = server.url ? "connects to" : "runs";
    print(`Approved ${server.name}. It ${does}: ${describeServer(server)}`);
    return 0;
  }

  if (args[0] === "login" || args[0] === "logout") {
    if (args.length < 2) {
      print(MCP_USAGE, process.stderr);
      return 2;
    }
    const configs = loadMcpConfigs(cwd);
    const server = configs.find((c) => c.name === args[1]);
    if (server === undefined) {
      print(`No server named ${args[1]}.`, process.stderr);
      return 1;
    }
    if (args[0] === "login" && partitionMcp([server], cwd)[1].length) {
      print(
        `${server.name} has not been approved. Run \`hx mcp approve ${server.name}\` first.`,
        process.stderr,
      );
      return 1;
    }
    return args[0] === "login" ? mcpLogin(server) : mcpLogout(server);
  }

  if (args[0] === "remove") {
    if (args.length < 2) {
      print(MCP_USAGE, process.stderr);
      return 2;
    }
    if (removeMcpConfig(args[1] as string, cwd, userLevel)) {
      if (!userLevel) revokeMcp(args[1] as string, cwd);
      print(`Removed ${args[1]}.`);
      return 0;
    }
    print(`No server named ${args[1]}.`, process.stderr);
    return 1;
  }

  print(MCP_USAGE, process.stderr);
  return 2;
}

/** `URL [--header 'Name: value']...` from `hx mcp add NAME --url`. */
function parseHttpAdd(rest: string[]): [string, Record<string, string>] {
  if (!rest.length || (rest[0] as string).startsWith("--"))
    throw new UsageError("--url needs a URL");
  const url = rest[0] as string;
  const headers: Record<string, string> = {};
  const tail = rest.slice(1);
  while (tail.length) {
    const flag = tail.shift() as string;
    if (!["--header", "-H"].includes(flag) || !tail.length)
      throw new UsageError(`unexpected ${repr(flag)}`);
    const raw = tail.shift() as string;
    const colon = raw.indexOf(":");
    const name = colon < 0 ? raw : raw.slice(0, colon);
    if (colon < 0 || !strip(name)) throw new UsageError("a header is written 'Name: value'");
    headers[strip(name)] = strip(raw.slice(colon + 1));
  }
  return [url, headers];
}

async function mcpLogin(config: MCPServerConfig): Promise<number> {
  try {
    await mcpSignIn(config, new ConsoleLogin());
  } catch (error) {
    if (error instanceof SignInNotRequired) {
      print(error.message);
      return 0;
    }
    if (
      error instanceof MCPError ||
      error instanceof OAuthError ||
      error instanceof CallbackError
    ) {
      print(`error: ${error.message}`, process.stderr);
      return 1;
    }
    if (error instanceof Interrupted) {
      print("\nCancelled.", process.stderr);
      return 1;
    }
    throw error;
  }
  const [status] = (await probeServers([config])) as [ServerStatus];
  if (status.connected) {
    print(`Signed in to ${config.name}: ${count(status.toolCount, "tool")} available.`);
    return 0;
  }
  print(`Signed in, but ${config.name} is still unavailable: ${status.error}`, process.stderr);
  return 1;
}

function mcpLogout(config: MCPServerConfig): number {
  if (!config.url) {
    print(`${config.name} is a local server; there is no sign-in to forget.`);
    return 0;
  }
  if (!usesOauth(config)) {
    // The stored sign-in is the server's, not this entry's: another project
    // may reach the same server over OAuth and still need it.
    print(`${config.name} sends its own Authorization header; there is no sign-in to forget.`);
    return 0;
  }
  if (new AuthStore().delete(mcpCredentialId(config.url))) print(`Signed out of ${config.name}.`);
  else print(`Not signed in to ${config.name}.`);
  return 0;
}

async function probeServers(configs: MCPServerConfig[]): Promise<ServerStatus[]> {
  const manager = new MCPManager(configs);
  try {
    return await manager.connectAll();
  } finally {
    await manager.closeAll();
  }
}

/** Boot the full stack - settings, provider, tools, MCP, skills - and run the TUI. */
async function runTuiCommand(parsed: ParsedArgs): Promise<number> {
  // Taken before anything starts, so no tool, MCP server or nested hx inherits it.
  const renameLeft = process.env[RENAME_LEFT_ENV] ?? null;
  delete process.env[RENAME_LEFT_ENV];
  let runtime: Runtime;
  let resume: string | null = null;
  let moved: string | null = null;
  try {
    resume = resumeTarget(parsed);
    moved = adoptSessionDirectory(parsed, resume);
    // Signed out, the TUI still opens: it says to /login rather than asking
    // for a key in the shell before anything is on screen.
    runtime = buildRuntime(parsed, resume, true);
  } catch (error) {
    return report(error);
  }
  if (moved) runtime.notices.push(moved);
  const signIn =
    runtime.provider instanceof DeferredProvider
      ? signInHint(runtime.loop.model, runtime.auth)
      : null;

  let relaunch: string | null = null;
  try {
    await runtime.connectMcp();
    await runtime.refreshModelsIfStale();
    relaunch = await runSession(runtime.loop, runtime.bus, runtime.settings, {
      models: runtime.models,
      auth: runtime.auth,
      sandboxActive: runtime.sandboxActive,
      sandboxBackend: runtime.sandboxBackend,
      skills: runtime.skills,
      agents: runtime.agents,
      mcp: runtime.mcp,
      notices: runtime.notices,
      checkpoints: runtime.checkpoints,
      renameLeft,
      signIn,
    });
  } finally {
    runtime.bus.close();
    // Everything, not only the provider: a background job left running would
    // outlive the session that started it.
    await runtime.aclose();
  }
  if (relaunch === null) return 0;
  const left = runtime.loop.session;
  return relaunchInto(relaunch, left.messages.length ? left.meta.sessionId : null);
}

/**
 * Run `hx resume <id>` in the directory the session was recorded in: its
 * paths, tool calls and grants all mean something there. An explicit `--cwd`
 * still wins. Returns a notice when that directory is gone.
 */
function adoptSessionDirectory(parsed: ParsedArgs, sessionId: string | null): string | null {
  if (sessionId === null || parsed.cwd !== null) return null;
  let recorded: string;
  try {
    recorded = readMeta(sessionId).cwd;
  } catch (error) {
    if (error instanceof SessionNotFound) return null; // buildRuntime reports it
    throw error;
  }
  if (isDir(recorded)) {
    parsed.cwd = recorded;
    return null;
  }
  return `This session was recorded in ${recorded}, which no longer exists; resumed here.`;
}

const osSignals = constants.signals as Record<string, number>;

/** The run options that take the next argument as their value. */
const RUN_OPTIONS_WITH_VALUES = new Set([
  "--model",
  "--mode",
  "--cwd",
  "--system-prompt",
  "--append-system-prompt",
  "--image",
]);

/**
 * The arguments to start HX again on `sessionId`. The run's own options carry
 * over; what picked the old session - `resume` and its id, and `--cwd` - does not.
 */
export function relaunchArgv(argv: readonly string[], sessionId: string): string[] {
  const kept: string[] = [];
  let index = 0;
  while (index < argv.length) {
    const arg = argv[index] as string;
    if (arg === "--cwd") {
      index += 2;
      continue;
    }
    if (RUN_OPTIONS_WITH_VALUES.has(arg)) {
      kept.push(...argv.slice(index, index + 2));
      index += 2;
      continue;
    }
    if (arg === "resume") {
      index++;
      if (index < argv.length && !(argv[index] as string).startsWith("-")) index++;
      continue;
    }
    kept.push(arg);
    index++;
  }
  return [...kept, "resume", sessionId];
}

/**
 * The session a relaunch left behind, handed to the HX that replaces it, so
 * the new process renames it as `/clear` and `/resume` rename theirs.
 */
export const RENAME_LEFT_ENV = "HX_RENAME_LEFT_SESSION";

/**
 * Start HX again, resumed on `sessionId`, and hand the terminal to it until
 * it exits. Node cannot replace its own process image, so the new HX runs as
 * a child with this one's terminal, from the directory this one was started
 * in, and its exit status is this one's. `left` is the session being left,
 * for the new process to rename.
 */
function relaunchInto(sessionId: string, left: string | null): number {
  const env = { ...process.env };
  if (left !== null) env[RENAME_LEFT_ENV] = left;
  const child = spawnSync(
    process.execPath,
    [
      ...process.execArgv,
      process.argv[1] as string,
      ...relaunchArgv(process.argv.slice(2), sessionId),
    ],
    { stdio: "inherit", env },
  );
  if (child.error) throw child.error;
  if (child.signal) return 128 + (osSignals[child.signal] ?? 0);
  return child.status ?? 1;
}

/** Resolve `hx resume [id]` to a session id, newest-first when omitted. */
function resumeTarget(parsed: ParsedArgs): string | null {
  if (parsed.command !== "resume") return null;
  if (parsed.sessionId) return parsed.sessionId;
  const settings = loadSettings(parsed.cwd, parsed.overrides);
  const meta = latestSession(settings.cwd);
  if (meta === null) throw new UsageError("no previous session in this directory");
  return meta.sessionId;
}

/** A credential or usage problem is the user's to fix: said in one line,
 * exit 2. Anything else is a bug, and is not hidden. */
function report(error: unknown): number {
  if (
    error instanceof MissingCredential ||
    error instanceof ExpiredCredential ||
    error instanceof UsageError
  ) {
    print(`error: ${error.message}`, process.stderr);
    return 2;
  }
  throw error;
}

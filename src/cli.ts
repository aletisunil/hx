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
 * `hx upgrade`         self-update
 *
 * Print mode exists so the harness is scriptable and E2E-testable without
 * driving a terminal UI.
 */

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
import { latestSession } from "./core/session.ts";
import { changelog, DocsUnavailable, docText, manual, README } from "./docs.ts";
import { describe } from "./net.ts";
import { absolute, expandUser, readText } from "./pathlib.ts";
import { authFile } from "./paths.ts";
import { ModelRegistry } from "./providers/models.ts";
import { saveApiKey } from "./providers/openrouter.ts";
import * as registry from "./providers/registry.ts";
import { formatFixed, osErrorText, purePath, repr, strip } from "./py.ts";
import { buildRuntime, type Runtime } from "./runtime.ts";
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
  hx upgrade                Update hx to the latest version
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
    default:
      return notPorted(parsed.command);
  }
}

/**
 * The commands still to come across from the Python build. Said plainly, so a
 * half-finished build can never be mistaken for a working one.
 */
function notPorted(command: string): number {
  print(`error: \`hx ${command}\` is not ported to the TypeScript build yet`, process.stderr);
  return 70;
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
    if (["resume", "prompt", "trace", "mcp", "upgrade", "auth"].includes(head)) {
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

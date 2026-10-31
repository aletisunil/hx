/**
 * The Claude Code route's credential: Claude Code's own login.
 *
 * HX never holds a Claude token. The `claude-code` route runs the official
 * `claude` CLI, which signs in, stores and refreshes its credential itself, so
 * "is this route usable" is a question for the CLI: is it installed, and does
 * `claude auth status` say it is signed in to a Claude subscription.
 *
 * Every child process HX starts here gets a scrubbed environment. A variable
 * such as `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` silently moves Claude Code
 * off the subscription - onto per-token billing, or onto someone else's gateway
 * - and the model id `claude-code/...` promises the subscription paid.
 *
 * The same environment carries HX's TLS policy (`nodeTlsEnv`): the CLI makes
 * its own connections, so a CA bundle or `HX_SSL_NO_VERIFY` that HX's clients
 * honour has to be restated for it in Node's terms.
 */

import { execFile, spawn, spawnSync } from "node:child_process";
import { accessSync, constants } from "node:fs";
import { nodeTlsEnv, withTlsHint } from "../net.ts";
import { expandUser, isFile } from "../pathlib.ts";
import { strip } from "../py.ts";
import { which } from "../which.ts";
import type { LoginInteraction } from "./oauth/browser.ts";

export const PROVIDER_ID = "claude-code";

/** Points HX at a `claude` binary that is not the one on `PATH`. */
export const CLI_ENV = "HX_CLAUDE_CODE_PATH";

export const INSTALL_HINT =
  "Install it from https://claude.com/claude-code, then run `hx auth login claude-code`.";

/** How long one `claude auth status` answer is trusted. */
const STATUS_TTL = 30;
/** How old an answer `statusAsync` replaces: younger than the TTL, so the
 * synchronous reads that follow it still find it fresh. */
const PREPARE_AGE = STATUS_TTL / 2;
const STATUS_TIMEOUT = 15;

const KEEP = new Set([
  // A long-lived subscription token from `claude setup-token`.
  "CLAUDE_CODE_OAUTH_TOKEN",
  // Where the user keeps their Claude Code state, credentials included.
  "CLAUDE_CONFIG_DIR",
  // How the CLI's TLS is set up on a corporate network.
  "CLAUDE_CODE_CERT_STORE",
  "CLAUDE_CODE_CLIENT_CERT",
  "CLAUDE_CODE_CLIENT_KEY",
  "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
]);
const SCRUB_PREFIXES = ["ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_"];
const SCRUB_NAMES = new Set(["CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"]);

/**
 * The environment for a `claude` child: HX's own, minus anything that would
 * bill somewhere other than the subscription, plus HX's TLS policy.
 */
export function childEnv(extra: Record<string, string> = {}): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    const scrubbed =
      SCRUB_PREFIXES.some((prefix) => name.startsWith(prefix)) || SCRUB_NAMES.has(name);
    if (KEEP.has(name) || !scrubbed) env[name] = value;
  }
  Object.assign(env, nodeTlsEnv(env), extra);
  return env;
}

/** The `claude` executable to run, or `null` when there is none. */
export function findCli(): string | null {
  const override = cliOverride();
  if (override) {
    try {
      accessSync(override, constants.X_OK);
    } catch {
      return null;
    }
    return isFile(override) ? override : null;
  }
  return which("claude");
}

function cliOverride(): string | null {
  const value = strip(process.env[CLI_ENV] ?? "");
  return value ? expandUser(value) : null;
}

/** What `claude auth status` reports, minus anything secret. */
export class LoginStatus {
  constructor(
    readonly loggedIn: boolean,
    readonly authMethod = "",
    readonly email: string | null = null,
    readonly organization: string | null = null,
    readonly orgId: string | null = null,
    /** `pro`, `max`, `team`... as Claude Code names it. */
    readonly plan: string | null = null,
  ) {}

  /** Signed in to claude.ai, rather than to the Console (per-token billing). */
  get isSubscription(): boolean {
    return this.loggedIn && this.authMethod !== "console";
  }

  get account(): string | null {
    return this.email || this.organization;
  }
}

const monotonic = () => performance.now() / 1000;
let cache: { stamp: number; cli: string | null; status: LoginStatus | null } | null = null;

/** The cached answer for `cli` if younger than `maxAge`, boxed so that a cached
 * `null` (no usable CLI) is told apart from no answer at all. */
function cached(cli: string | null, maxAge: number): [LoginStatus | null] | null {
  if (cache === null || cache.cli !== cli || monotonic() - cache.stamp >= maxAge) return null;
  return [cache.status];
}

/**
 * The CLI's login state, or `null` when the CLI is missing or broken.
 * Cached for `STATUS_TTL`; `fresh` asks again, which a login does. Blocks while
 * the CLI answers: code on the event loop awaits `statusAsync` first.
 */
export function status(fresh = false): LoginStatus | null {
  const cli = findCli();
  if (!fresh) {
    const hit = cached(cli, STATUS_TTL);
    if (hit !== null) return hit[0];
  }
  const started = monotonic();
  let result: LoginStatus | null = null;
  if (cli) {
    const completed = spawnSync(cli, ["auth", "status", "--json"], {
      encoding: "utf8",
      timeout: STATUS_TIMEOUT * 1000,
      env: childEnv(),
      stdio: ["ignore", "pipe", "pipe"],
    });
    result = completed.error ? null : parseStatus(completed.stdout);
  }
  cache = { stamp: started, cli, status: result };
  return result;
}

/** `status`, asked without blocking the event loop. */
export async function statusAsync(fresh = false): Promise<LoginStatus | null> {
  const cli = findCli();
  if (!fresh) {
    const hit = cached(cli, PREPARE_AGE);
    if (hit !== null) return hit[0];
  }
  const started = monotonic();
  let result: LoginStatus | null = null;
  if (cli) {
    result = await new Promise((resolve) => {
      execFile(
        cli,
        ["auth", "status", "--json"],
        { encoding: "utf8", timeout: STATUS_TIMEOUT * 1000, env: childEnv() },
        (error, stdout) => resolve(error && !stdout ? null : parseStatus(stdout)),
      );
    });
  }
  // Stamped with when the question was put, not answered: a sign-in that lands
  // while the CLI is answering is newer than this answer.
  if (cache === null || cache.stamp <= started) cache = { stamp: started, cli, status: result };
  return result;
}

export function forgetStatus(): void {
  cache = null;
}

/** Parse `claude auth status --json`. `null` for anything else. */
export function parseStatus(raw: string): LoginStatus | null {
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return null;
  const data = payload as Record<string, unknown>;
  const text = (key: string) => (data[key] ? String(data[key]) : null);
  return new LoginStatus(
    Boolean(data.loggedIn),
    String(data.authMethod || ""),
    text("email"),
    text("orgName"),
    text("orgId"),
    text("subscriptionType"),
  );
}

export function signedIn(): boolean {
  const current = status();
  return current?.isSubscription ?? false;
}

/** Why the route cannot be used right now, and what to run. */
export function missingMessage(): string {
  if (findCli() === null) {
    const override = cliOverride();
    if (override) {
      return (
        `${CLI_ENV} is set to ${override}, which is not an executable \`claude\`. ` +
        "Point it at Claude Code's `claude`, or unset it to use the one on PATH."
      );
    }
    return `Claude Code is not installed (no \`claude\` on PATH). ${INSTALL_HINT}`;
  }
  const current = status();
  if (current?.loggedIn && !current.isSubscription) {
    return (
      "Claude Code is signed in to the Anthropic Console, which bills per token. " +
      "The claude-code route runs on a Claude subscription: run " +
      "`hx auth login claude-code` to sign in with your Claude account."
    );
  }
  return (
    "Claude Code is not signed in. Run `hx auth login claude-code`, or `/login` " +
    "inside HX. Requires a Claude Pro or Max subscription."
  );
}

/** Sign in with Claude Code's own flow, on this terminal. */
export function loginTerminal(): number {
  const cli = findCli();
  if (cli === null) {
    process.stdout.write(`Claude Code is not installed. ${INSTALL_HINT}\n`);
    return 1;
  }
  const completed = spawnSync(cli, ["auth", "login", "--claudeai"], {
    env: childEnv(),
    stdio: "inherit",
  });
  forgetStatus();
  return completed.status ?? 1;
}

const URL_PATTERN = /https:\/\/\S+/;
const PASTE_PROMPT = /^.*Paste code[^>]*>\s*/;
const FAILED = /^Login failed:\s*/i;

/** Why `claude auth login` failed: its last line of its own. */
function failure(output: string[]): string | null {
  for (const raw of [...output].reverse()) {
    const line = strip(raw.replace(PASTE_PROMPT, "").replace(FAILED, ""));
    if (line && !URL_PATTERN.test(line)) return line;
  }
  return null;
}

export class LoginFailed extends Error {
  override name = "LoginFailed";
}

/**
 * Sign in through Claude Code with HX drawing the prompts (the TUI).
 *
 * `claude auth login` on a pipe prints the sign-in URL, opens the browser
 * itself, and then waits for either the browser's loopback callback or a code
 * pasted on stdin - so the URL goes to `interaction` and its paste prompt races
 * the callback, exactly as HX's own OAuth flows do.
 */
export async function loginInteractive(interaction: LoginInteraction): Promise<LoginStatus> {
  const cli = findCli();
  if (cli === null) throw new LoginFailed(`Claude Code is not installed. ${INSTALL_HINT}`);

  const child = spawn(cli, ["auth", "login", "--claudeai"], {
    env: childEnv(),
    stdio: ["pipe", "pipe", "pipe"],
  });
  const output: string[] = [];
  const stopPaste = new AbortController();
  let pasting = false;
  let buffer = "";
  const decoder = new TextDecoder();

  const relayPaste = async () => {
    try {
      const code = await interaction.promptPaste(
        "Paste the code from the browser:",
        stopPaste.signal,
      );
      if (strip(code) && child.stdin.writable) child.stdin.write(`${strip(code)}\n`);
    } catch {
      // The login ended first.
    }
  };

  const onData = (chunk: Buffer) => {
    buffer += decoder.decode(chunk, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() as string;
    // The paste prompt has no newline after it; it is what says the URL has
    // been printed and the CLI is waiting.
    if (buffer.includes("Paste code")) {
      lines.push(buffer);
      buffer = "";
    }
    for (const line of lines) {
      output.push(line);
      const match = URL_PATTERN.exec(line);
      if (match && !pasting) {
        pasting = true;
        interaction.showUrl(
          match[0],
          "Sign in to your Claude account in the browser. If it did not open, visit this URL and paste the code it shows.",
        );
        void relayPaste();
      }
    }
  };
  child.stdout.on("data", onData);
  child.stderr.on("data", onData);

  const code = await new Promise<number>((resolve) => {
    child.on("error", () => resolve(1));
    child.on("close", (exit) => resolve(exit ?? 1));
  });
  stopPaste.abort();
  forgetStatus();

  if (code !== 0) {
    const reason = failure(output) ?? `exit ${code}`;
    throw new LoginFailed(withTlsHint(`\`claude auth login\` failed: ${reason}`));
  }
  const current = await statusAsync(true);
  if (current === null || !current.loggedIn) {
    throw new LoginFailed("Claude Code still reports no login.");
  }
  return current;
}

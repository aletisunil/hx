/**
 * Keeping HX up to date, as Claude Code keeps itself: `hx update` on demand,
 * and a background update about once a day while the TUI is open.
 *
 * Both ask npm's registry for the `latest` release and install that exact
 * version with `npm install --global`, using the npm on `PATH` - the one whose
 * global `bin` put `hx` there. Only an npm install can update itself: a
 * checkout's `dist/hx.js` is the developer's, and says so.
 *
 * The background update never blocks the session. npm runs detached, so
 * quitting HX mid-install does not leave a half-written package behind, and the
 * new version takes over on the next start.
 */

import { spawn } from "node:child_process";
import { mkdirSync, openSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dumps } from "./json.ts";
import { request } from "./net.ts";
import { join, parent } from "./pathlib.ts";
import { logsDir, userHome } from "./paths.ts";
import { strip } from "./py.ts";
import { PACKAGE, VERSION } from "./version.ts";
import { which } from "./which.ts";

const RELEASES_URL = `https://registry.npmjs.org/-/package/${PACKAGE}/dist-tags`;
/** Points the lookup at another registry: the E2E suite's stand-in. */
const RELEASES_ENV = "HX_RELEASES_URL";
/** Set to `1` to turn the background update off, whatever the settings say. */
export const DISABLE_ENV = "HX_DISABLE_AUTOUPDATER";
const RELEASES_TIMEOUT = 5;
const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;

const stateFile = () => join(userHome(), "update.json");

/** The `latest` release on npm, or `null` when the registry cannot be asked. */
export async function latestRelease(signal?: AbortSignal): Promise<string | null> {
  const url = strip(process.env[RELEASES_ENV] ?? "") || RELEASES_URL;
  try {
    const response = await request(url, {
      timeout: RELEASES_TIMEOUT,
      ...(signal ? { signal } : {}),
    });
    try {
      if (response.status < 200 || response.status >= 300) return null;
      const tags = (await response.json()) as unknown;
      const latest =
        typeof tags === "object" && tags !== null ? (tags as Record<string, unknown>).latest : null;
      return typeof latest === "string" && RELEASE.test(latest) ? latest : null;
    } finally {
      response.close();
    }
  } catch {
    return null;
  }
}

const RELEASE = /^\d+\.\d+\.\d+$/;

/** Order two `x.y.z` releases. */
export function compareReleases(a: string, b: string): number {
  const left = a.split(".").map(Number);
  const right = b.split(".").map(Number);
  for (let index = 0; index < 3; index++) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference) return difference;
  }
  return 0;
}

/** Where the running HX lives, when it is not a global npm install of the
 * package - a checkout, or a copy - or `null` when it is one. */
export function notAnNpmInstall(): string | null {
  const script = process.argv[1] ?? "";
  let real: string;
  try {
    real = realpathSync(script);
  } catch {
    return script;
  }
  return real.includes(`/node_modules/${PACKAGE}/`) ? null : parent(parent(real));
}

/** The npm that installs: the one on `PATH`, else the one beside this Node. */
function npm(): string {
  return which("npm") ?? join(parent(process.execPath), "npm");
}

/** What a user types to install the release by hand. */
export const INSTALL_COMMAND = `npm install --global ${PACKAGE}`;

/**
 * Run npm to install `version`. In the foreground its output is the user's to
 * read; in the background it goes to a log, and npm outlives HX if HX quits.
 * Resolves to npm's exit status, or `null` when npm could not be started.
 */
function runNpm(version: string, background: boolean): Promise<number | null> {
  const args = ["install", "--global", `${PACKAGE}@${version}`];
  let stdio: "inherit" | ["ignore", number, number] = "inherit";
  if (background) {
    mkdirSync(logsDir(), { recursive: true });
    const log = openSync(join(logsDir(), "update.log"), "a");
    stdio = ["ignore", log, log];
  }
  return new Promise((resolve) => {
    const child = spawn(npm(), args, { stdio, detached: background });
    child.once("error", () => resolve(null));
    child.once("exit", (code, signal) => resolve(code ?? (signal ? 1 : 0)));
    if (background) child.unref();
  });
}

/** `hx update` (and `hx upgrade`): install the newest release now. */
export async function runUpdate(
  print: (text: string, stream?: NodeJS.WriteStream) => void,
): Promise<number> {
  const elsewhere = notAnNpmInstall();
  if (elsewhere !== null) {
    print(
      `hx is running from ${elsewhere}, not from an npm install, so it cannot update itself. ` +
        `Update a checkout with git pull and npm run build, or install the release: ${INSTALL_COMMAND}`,
      process.stderr,
    );
    return 1;
  }
  const latest = await latestRelease();
  if (latest === null) {
    print("Could not reach the npm registry to check for a newer hx.", process.stderr);
    return 1;
  }
  if (compareReleases(latest, VERSION) <= 0) {
    print(`hx ${VERSION} is up to date.`);
    return 0;
  }
  print(`Updating hx ${VERSION} to ${latest}...`);
  const status = await runNpm(latest, false);
  if (status === null) {
    print(
      `npm is not installed, so hx cannot update itself. See https://nodejs.org/`,
      process.stderr,
    );
    return 1;
  }
  if (status !== 0) {
    print(`npm could not install ${PACKAGE}@${latest} (exit ${status}).`, process.stderr);
    return 1;
  }
  print(`Updated hx to ${latest}.`);
  return 0;
}

/** What the background update did, for the session to say. */
export interface UpdateOutcome {
  readonly message: string;
  readonly ok: boolean;
}

/** When the registry was last asked, in ms since the epoch; 0 for never. */
function lastChecked(): number {
  try {
    const state = JSON.parse(readFileSync(stateFile(), "utf8")) as unknown;
    const at =
      typeof state === "object" && state !== null
        ? (state as Record<string, unknown>).checkedAt
        : 0;
    return typeof at === "number" ? at : 0;
  } catch {
    return 0;
  }
}

/**
 * The background update: at most once a day, and only for an npm install with
 * updates on. Resolves to what to tell the user once npm is done, or `null`
 * when there was nothing to do. Never throws: a failed check is not worth
 * interrupting a session for. `signal` is the session ending: the check stops
 * there, so quitting never waits on the registry, and npm is not started.
 */
export async function autoUpdate(
  enabled: boolean,
  signal: AbortSignal,
): Promise<UpdateOutcome | null> {
  const disabled = strip(process.env[DISABLE_ENV] ?? "").toLowerCase();
  if (!enabled || ["1", "true", "yes"].includes(disabled)) return null;
  if (notAnNpmInstall() !== null) return null;
  const now = Date.now();
  if (now - lastChecked() < CHECK_INTERVAL_MS) return null;
  try {
    // Before asking, so an unreachable registry is not asked again on every start.
    writeFileSync(stateFile(), `${dumps({ checkedAt: now })}\n`);
  } catch {
    return null;
  }
  const latest = await latestRelease(signal);
  if (signal.aborted || latest === null || compareReleases(latest, VERSION) <= 0) return null;
  const status = await runNpm(latest, true);
  if (status === 0) return { message: `Updated hx to ${latest}. Restart hx to use it.`, ok: true };
  const why = status === null ? "npm could not be started" : `npm exited ${status}`;
  return {
    message: `Could not update hx to ${latest}: ${why}. Run hx update to see why.`,
    ok: false,
  };
}

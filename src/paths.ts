/**
 * Filesystem layout for HX state and configuration.
 *
 * User-level state lives under `$HX_HOME` (default `~/.hx`). A project's
 * shared, checked-in settings live in `<cwd>/.hx/settings.json`, written by
 * hand. Anything HX writes for itself - the permission grants a user accumulates
 * by answering prompts - lives under `$HX_HOME/projects/<slug>`, so HX never
 * leaves a file inside somebody's checkout.
 *
 * Nothing here touches the network or mutates state on import; call
 * `ensureUserDirs` explicitly at startup.
 */

import { createHash } from "node:crypto";
import { chmodSync, closeSync, mkdirSync, openSync, statSync } from "node:fs";
import { expandUser, home, isDir, isFile, join, parent, readText, resolve } from "./pathlib.ts";
import { isAlnum, purePath, strip } from "./py.ts";

export const HX_HOME_ENV = "HX_HOME";
export const PROJECT_DIR_NAME = ".hx";

/** Longest readable half of a project slug. A deep path is trimmed from the
 * left: the last few components are the ones that identify a checkout. */
const SLUG_MAX = 48;

/** Root of user-level HX state (`$HX_HOME` or `~/.hx`). */
export function userHome(): string {
  const override = process.env[HX_HOME_ENV];
  return override ? purePath(expandUser(override)) : join(purePath(home()), ".hx");
}

/**
 * `path` with the home directory written as `~`.
 *
 * Compared as text, not resolved: a session records the path it resolved
 * when it was made, and the directory may be gone by the time it is drawn.
 */
export function tilde(path: string): string {
  const text = path;
  const base = purePath(home());
  if (text === base) return "~";
  const prefix = `${base.replace(/\/+$/, "")}/`;
  if (text.startsWith(prefix)) return `~${text.slice(base.replace(/\/+$/, "").length)}`;
  return text;
}

export const userSettingsFile = () => join(userHome(), "settings.json");
/** Credential store. Created with mode 0600 by the auth layer. */
export const authFile = () => join(userHome(), "auth.json");
/** Cached OpenRouter model catalogue. */
export const modelsCacheFile = () => join(userHome(), "models.json");
export const sessionsDir = () => join(userHome(), "sessions");
export const sessionDir = (sessionId: string) => join(sessionsDir(), sessionId);
export const sessionTranscriptFile = (sessionId: string) =>
  join(sessionDir(sessionId), "transcript.jsonl");
/** Where oversized tool outputs are spilled. */
export const sessionOutputsDir = (sessionId: string) => join(sessionDir(sessionId), "outputs");
/** Content-addressed pre-images of the files HX changed. */
export const sessionCheckpointsDir = (sessionId: string) =>
  join(sessionDir(sessionId), "checkpoints");
/** User-supplied theme files (`*.json`). */
export const userThemesDir = () => join(userHome(), "themes");
export const userSkillsDir = () => join(userHome(), "skills");
export const userAgentsDir = () => join(userHome(), "agents");
/** User keybinding overrides. */
export const keybindingsFile = () => join(userHome(), "keybindings.json");
export const logsDir = () => join(userHome(), "logs");

/** Project-level config directory (`<cwd>/.hx`). */
export const projectDir = (cwd: string = process.cwd()) => join(purePath(cwd), PROJECT_DIR_NAME);
/** Shared project settings. Written by hand, and safe to check in. */
export const projectSettingsFile = (cwd?: string) => join(projectDir(cwd), "settings.json");

/**
 * Where HX keeps *its own* state for a project: `$HX_HOME/projects/<slug>`.
 *
 * Under the user's home rather than inside the repository. What HX writes on
 * the user's behalf is machine-local - an "always allow" grant is one
 * person's decision, on one machine, frequently spelling out absolute paths
 * from their home directory - and a tool that drops such a file into a
 * checkout makes it the user's problem.
 *
 * Still keyed by project, because the grants are genuinely project-scoped:
 * allowing `pytest` in one repository must not allow it everywhere.
 */
export const projectStateDir = (cwd?: string) => join(userHome(), "projects", projectSlug(cwd));

/**
 * A filesystem-safe, collision-free name for a project directory.
 *
 * The readable path, punctuation flattened, plus a short digest of the real
 * absolute path. The readable half is for a human browsing `~/.hx/projects`;
 * the digest is what keeps `~/work/api` and `~/personal/api` apart.
 */
export function projectSlug(cwd: string = process.cwd()): string {
  const resolved = resolve(cwd);
  const digest = createHash("sha256").update(resolved, "utf8").digest("hex").slice(0, 8);
  const readable = resolved
    .split("/")
    .filter((part) => part)
    .join("-");
  let safe = "";
  for (const char of readable) safe += isAlnum(char) || "-_.".includes(char) ? char : "-";
  if (!safe) return digest;
  const chars = [...safe];
  return `${strip(chars.slice(-SLUG_MAX).join(""), "-")}-${digest}`;
}

/** This machine's settings for this project - never checked in. */
export const projectLocalSettingsFile = (cwd?: string) =>
  join(projectStateDir(cwd), "settings.local.json");
/** Records the one-time cleanups already applied for this project. */
export const projectMigrationsFile = (cwd?: string) =>
  join(projectStateDir(cwd), "migrations.json");
/** Where the local layer lived when it was written into the repository. */
export const legacyProjectLocalSettingsFile = (cwd?: string) =>
  join(projectDir(cwd), "settings.local.json");
/** `.hx/.gitignore`, which existed only to hide the file above. */
export const legacyProjectGitignoreFile = (cwd?: string) => join(projectDir(cwd), ".gitignore");
export const projectMcpFile = (cwd?: string) => join(projectDir(cwd), "mcp.json");
export const projectSkillsDir = (cwd?: string) => join(projectDir(cwd), "skills");
export const projectAgentsDir = (cwd?: string) => join(projectDir(cwd), "agents");
/** User-level replacement for the built-in system prompt. */
export const userSystemPromptFile = () => join(userHome(), "system-prompt.md");
/** User-level text appended to whichever system prompt is in force. */
export const userSystemPromptAppendFile = () => join(userHome(), "system-prompt-append.md");
export const projectSystemPromptFile = (cwd?: string) => join(projectDir(cwd), "system-prompt.md");
export const projectSystemPromptAppendFile = (cwd?: string) =>
  join(projectDir(cwd), "system-prompt-append.md");
/** Your `AGENTS.md`: instructions that follow you into every project. */
export const userInstructionsFile = () => join(userHome(), "AGENTS.md");

/**
 * The repository `cwd` sits in: the nearest directory holding `.git`.
 *
 * `.git` is a directory in a clone and a file in a worktree or submodule.
 * A submodule is part of the repository around it, so the walk carries on
 * past one to the superproject. `null` outside a repository.
 */
export function projectRoot(cwd: string = process.cwd()): string | null {
  let submodule: string | null = null;
  for (const directory of ancestry(resolve(cwd))) {
    const marker = join(directory, ".git");
    if (isDir(marker) || (isFile(marker) && !isSubmodule(marker))) return directory;
    if (isFile(marker) && submodule === null) submodule = directory;
  }
  return submodule;
}

/** `path` and every directory above it, nearest first. */
function ancestry(path: string): string[] {
  const chain = [path];
  let current = path;
  while (current !== "/") {
    current = parent(current);
    chain.push(current);
  }
  return chain;
}

/** Whether a `.git` file points into a superproject's `.git/modules`. */
function isSubmodule(marker: string): boolean {
  let text: string;
  try {
    text = readText(marker);
  } catch {
    return false;
  }
  return text.startsWith("gitdir:") && text.replaceAll("\\", "/").includes("/modules/");
}

/**
 * Every `AGENTS.md` that governs `cwd`, least specific first.
 *
 * From the repository root down to `cwd`, one candidate per directory, so
 * starting HX in `src/app` still finds the root's file - and `src/app`'s own,
 * which comes last and so has the last word. Outside a repository only `cwd`
 * itself is looked at.
 */
export function projectInstructionsFiles(cwd: string, root: string | null): string[] {
  const here = resolve(cwd);
  if (root === null) return [join(here, "AGENTS.md")];
  const chain = ancestry(here);
  const index = chain.indexOf(root);
  return chain
    .slice(0, index + 1)
    .reverse()
    .map((directory) => join(directory, "AGENTS.md"));
}

/**
 * Create the user-level skeleton if it does not exist, private to the user:
 * transcripts hold whatever a session read, so `$HX_HOME` is made `0700` even
 * when it was already there with wider permissions.
 *
 * That includes an empty `AGENTS.md`, so there is a file to find and fill in;
 * empty, it adds nothing to the prompt. An existing one - including a symlink,
 * dangling or not - is never touched, and failing to create it never stops a
 * session: the file is optional.
 */
export function ensureUserDirs(): void {
  const root = userHome();
  mkdirSync(root, { recursive: true, mode: 0o700 });
  try {
    if (statSync(root).mode & 0o077) chmodSync(root, 0o700);
  } catch {
    // Not ours to change; the session still works.
  }
  for (const path of [sessionsDir(), userSkillsDir(), userAgentsDir(), logsDir()]) {
    mkdirSync(path, { recursive: true });
  }
  try {
    closeSync(openSync(userInstructionsFile(), "wx"));
  } catch {
    // Already there, or not ours to create.
  }
}

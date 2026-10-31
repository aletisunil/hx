/**
 * Working-tree awareness, carried by late injection.
 *
 * The branch and the set of changed files move constantly, so they belong
 * below the rolling cache breakpoint alongside the todo list, never in the
 * system prompt. One `git status --porcelain=v2 --branch -z` per provider call
 * answers both questions at once. Outside a repository - or with no git on
 * `PATH` - the watcher disables itself permanently rather than paying for a
 * failing subprocess every time.
 *
 * Only files HX has *not* read are reported: one it read that has since moved
 * is the `stale_files` injector's to report, so each file has exactly one
 * reporter and the model never sees the same path twice.
 */

import { spawnSync } from "node:child_process";
import { statSync } from "node:fs";
import type { Injection, Injector } from "./core/lateinject.ts";
import { isDir, isFile, join, readText } from "./pathlib.ts";
import { strip } from "./py.ts";

const STATUS_COMMAND = ["git", "status", "--porcelain=v2", "--branch", "-z"];
const ROOT_COMMAND = ["git", "rev-parse", "--show-toplevel"];
/** A turn must not wait on git. */
const TIMEOUT_SECONDS = 2;
/** A bulk checkout can change thousands of files; the list is a hint. */
const MAX_LISTED = 20;
/** Consecutive timeouts before the watcher gives up for the session. */
const MAX_TRANSIENT_FAILURES = 3;
export const DETACHED = "(detached)";

/** git's status letters, spelled the way the notice reads them out. */
const LABELS: Record<string, string> = {
  M: "modified",
  A: "added",
  D: "deleted",
  R: "renamed",
  C: "copied",
  T: "typechange",
  U: "conflicted",
  "?": "untracked",
};

/** git could not answer. `permanent` separates "this will never work" - no
 * git, not a repository - from "not this time". */
export class GitUnavailable extends Error {
  constructor(
    message: string,
    readonly permanent: boolean,
  ) {
    super(message);
  }
}

export interface Change {
  /** Repository-relative, as git prints it - what the model should see. */
  readonly path: string;
  readonly absolute: string;
  readonly label: string;
}

export interface Status {
  readonly branch: string;
  /** Files whose status or contents moved since the previous poll. Empty on
   * the first poll, which only establishes the baseline. */
  readonly changes: readonly Change[];
}

/** Runs a git command in the session directory and returns stdout. */
export type Runner = (command: readonly string[]) => string;

/** Polls the working tree once per turn and reports what moved. */
export class GitWatcher {
  private readonly run: Runner;
  private isEnabled = true;
  private failures = 0;
  private root: string | null = null;
  private last: Map<string, string> | null = null;

  constructor(
    readonly cwd: string,
    runner?: Runner,
  ) {
    this.run = runner ?? defaultRunner(cwd);
  }

  get enabled(): boolean {
    return this.isEnabled;
  }

  /** Current branch plus what changed since the last call, or `null` when git
   * has nothing to say. */
  poll(): Status | null {
    if (!this.isEnabled) return null;
    let raw: string;
    try {
      this.root ??= strip(this.run(ROOT_COMMAND));
      raw = this.run(STATUS_COMMAND);
    } catch (error) {
      if (!(error instanceof GitUnavailable)) throw error;
      this.noteFailure(error);
      return null;
    }
    this.failures = 0;
    const [branch, entries] = parseStatus(raw);
    const root = this.root;
    const snapshot = new Map(
      [...entries].map(([path, code]) => [path, `${code}\u0000${signature(join(root, path))}`]),
    );
    const previous = this.last;
    this.last = snapshot;
    if (previous === null) return { branch, changes: [] };
    const changes = [...snapshot]
      .filter(([path, state]) => previous.get(path) !== state)
      .map(([path]) => ({
        path,
        absolute: join(root, path),
        label: LABELS[entries.get(path) as string] ?? (entries.get(path) as string),
      }));
    return { branch, changes };
  }

  private noteFailure(error: GitUnavailable): void {
    if (error.permanent) {
      this.isEnabled = false;
      return;
    }
    this.failures++;
    if (this.failures >= MAX_TRANSIENT_FAILURES) this.isEnabled = false;
  }
}

/** `text.split(sep, maxsplit)`: at most `max` splits, the rest left whole. */
function splitMax(text: string, separator: string, max: number): string[] {
  const parts: string[] = [];
  let rest = text;
  while (parts.length < max) {
    const at = rest.indexOf(separator);
    if (at < 0) break;
    parts.push(rest.slice(0, at));
    rest = rest.slice(at + separator.length);
  }
  parts.push(rest);
  return parts;
}

/**
 * Parse `--porcelain=v2 --branch -z` into a branch and path -> code. Entry
 * kinds: `1` ordinary change, `2` rename or copy (its original path follows as
 * the next NUL-separated field), `u` unmerged, `?` untracked.
 */
export function parseStatus(raw: string): [string, Map<string, string>] {
  const fields = raw.split("\0");
  if (fields.length && fields.at(-1) === "") fields.pop();
  let branch = DETACHED;
  const entries = new Map<string, string>();
  let index = 0;
  while (index < fields.length) {
    const line = fields[index] as string;
    index++;
    if (!line) continue;
    const kind = line[0] as string;
    if (kind === "#") {
      if (line.startsWith("# branch.head ")) {
        const head = strip(line.slice("# branch.head ".length));
        branch = head === "" || head === DETACHED ? DETACHED : head;
      }
      continue;
    }
    if (kind === "?") {
      entries.set(line.slice(2), "?");
      continue;
    }
    if (kind === "!") continue;
    const splits = ({ "1": 8, "2": 9, u: 10 } as Record<string, number>)[kind];
    if (splits === undefined) continue;
    const parts = splitMax(line, " ", splits);
    if (parts.length <= splits) continue;
    entries.set(parts[splits] as string, statusCode(parts[1] as string));
    if (kind === "2") index++; // the rename's original path
  }
  return [branch, entries];
}

/**
 * The current branch, cheap enough to poll on a UI timer: `HEAD` is read the
 * way git writes it - one stat, and a short read only when the file moved.
 */
export class BranchWatcher {
  private head: string | null = null;
  private resolved = false;
  private stamp: string | null = null;
  private current: string | null = null;

  constructor(readonly cwd: string) {}

  /** The last value `poll` read. `null` outside a repository. */
  get branch(): string | null {
    return this.current;
  }

  /** Re-read the branch if `HEAD` moved, and return it either way. */
  poll(): string | null {
    const head = this.headFile();
    if (head === null) return null;
    let stamp: string;
    try {
      const info = statSync(head, { bigint: true });
      stamp = `${info.mtimeNs}:${info.size}`;
    } catch {
      // The repository went away mid-session: keep the last known branch.
      return this.current;
    }
    if (stamp === this.stamp) return this.current;
    this.stamp = stamp;
    this.current = branchFromHead(head);
    return this.current;
  }

  /** Locate `HEAD`, once: `.git` is a directory in a clone and a file holding
   * `gitdir: <path>` in a worktree or submodule. */
  private headFile(): string | null {
    if (this.resolved) return this.head;
    this.resolved = true;
    const marker = join(this.cwd, ".git");
    let gitDir: string | null = null;
    if (isDir(marker)) gitDir = marker;
    else if (isFile(marker)) {
      let pointer = "";
      try {
        pointer = strip(readText(marker));
      } catch {
        pointer = "";
      }
      if (pointer.startsWith("gitdir:")) {
        const target = strip(pointer.slice("gitdir:".length));
        gitDir = target.startsWith("/") ? target : join(this.cwd, target);
      }
    } else gitDir = gitDirOf(this.cwd);
    if (gitDir === null) return null;
    const head = join(gitDir, "HEAD");
    this.head = isFile(head) ? head : null;
    return this.head;
  }
}

/** Parse a `HEAD` file: a symbolic ref, or a raw sha when detached. */
function branchFromHead(head: string): string | null {
  let content: string;
  try {
    content = strip(readText(head));
  } catch {
    return null;
  }
  if (content.startsWith("ref:")) {
    const ref = strip(content.slice(4));
    return ref.startsWith("refs/heads/") ? ref.slice("refs/heads/".length) : ref || null;
  }
  return content ? DETACHED : null;
}

/** `git rev-parse --absolute-git-dir`, run once when `cwd` is not the root. */
function gitDirOf(cwd: string): string | null {
  const result = spawnSync("git", ["rev-parse", "--absolute-git-dir"], {
    cwd,
    encoding: "utf8",
    timeout: TIMEOUT_SECONDS * 1000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error || result.status !== 0) return null;
  return strip(result.stdout) || null;
}

/**
 * Late injector for branch and working-tree movement. `seen` reports whether HX
 * already knows a path; those files are left to the `stale_files` injector.
 */
export function gitInjector(
  watcher: GitWatcher,
  seen?: (path: string) => boolean,
  priority = 15,
): Injector {
  return (): Injection | null => {
    const status = watcher.poll();
    if (status === null) return null;
    const lines = [`Git branch: ${status.branch}`];
    const unknown = status.changes.filter((c) => seen === undefined || !seen(c.absolute));
    if (unknown.length) {
      const listed = unknown.slice(0, MAX_LISTED).map((c) => `- ${c.label}: ${c.path}`);
      if (unknown.length > MAX_LISTED) listed.push(`- … and ${unknown.length - MAX_LISTED} more`);
      lines.push(
        `These files changed on disk since the last turn and have not been read in this session. Read one before editing it:\n${listed.join("\n")}`,
      );
    }
    return { source: "git", text: lines.join("\n\n"), priority };
  };
}

function defaultRunner(cwd: string): Runner {
  return (command) => {
    const result = spawnSync(command[0] as string, command.slice(1), {
      cwd,
      encoding: "utf8",
      timeout: TIMEOUT_SECONDS * 1000,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const code = (result.error as NodeJS.ErrnoException | undefined)?.code;
    if (code === "ENOENT") throw new GitUnavailable("git is not installed", true);
    if (code === "ETIMEDOUT") throw new GitUnavailable("git timed out", false);
    if (result.error) throw new GitUnavailable(result.error.message, false);
    if (result.status !== 0) {
      // 128 is git's "not a repository" and friends: nothing to retry.
      throw new GitUnavailable(
        strip(result.stderr) || `git exited ${result.status}`,
        result.status === 128,
      );
    }
    return result.stdout;
  };
}

/** The meaningful half of git's two-character status field: the worktree
 * column wins when it says anything. */
function statusCode(xy: string): string {
  const worktree = xy.length > 1 ? (xy[1] as string) : ".";
  const index = xy ? (xy[0] as string) : ".";
  return worktree !== "." ? worktree : index;
}

/** Cheap stand-in for a content hash: mtime and size, for the price of a stat. */
function signature(path: string): string {
  try {
    const info = statSync(path, { bigint: true });
    return `${info.mtimeNs}:${info.size}`;
  } catch {
    return "none";
  }
}

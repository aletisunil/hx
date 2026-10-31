/**
 * OS-level sandboxing for executed commands.
 *
 * macOS uses `sandbox-exec` with a generated Seatbelt profile; Linux uses
 * `bubblewrap`. Both default to: read the filesystem, write only inside the
 * project and the temp dir, no outbound network.
 *
 * If neither backend exists the sandbox degrades to a no-op - and says so in
 * the status bar. Silently pretending to be sandboxed would be worse than being
 * visibly unsandboxed.
 */

import { randomBytes } from "node:crypto";
import { closeSync, existsSync, mkdirSync, openSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { absolute, expandUser, isDir, join, parent, resolve } from "../pathlib.ts";
import { which } from "../which.ts";

export type SandboxBackend = "seatbelt" | "bubblewrap" | "none";

/**
 * Denied even inside readable and writable roots. A coding agent has no
 * business reading these, and one prompt-injected `cat ~/.ssh/id_rsa` is all
 * it takes - or writing them, where an `authorized_keys` line is a way back in.
 * A trailing slash marks a directory.
 */
export const CREDENTIAL_PATHS = [
  "~/.ssh/",
  "~/.aws/",
  "~/.gnupg/",
  "~/.kube/",
  "~/.docker/config.json",
  "~/.config/gh/",
  "~/.netrc",
  "~/.npmrc",
  "~/.pypirc",
  "~/.hx/auth.json",
] as const;

export interface DeniedPath {
  readonly path: string;
  /** What the path is meant to be, which is what it gets created as. */
  readonly isDir: boolean;
}

export interface SandboxPolicy {
  readonly writablePaths: readonly string[];
  readonly readablePaths: readonly string[];
  /** Unreadable and unwritable even inside allowed roots. */
  readonly denyPaths: readonly DeniedPath[];
  readonly allowNetwork: boolean;
  readonly allowSubprocess: boolean;
  /** Directory the wrapped command starts in. `null` leaves it to bwrap. */
  readonly cwd: string | null;
}

/** Wraps a command so it runs under the platform's sandbox. */
export class Sandbox {
  readonly backend: SandboxBackend;
  private profilePath: string | null = null;

  constructor(
    readonly policy: SandboxPolicy,
    backend?: SandboxBackend,
  ) {
    this.backend = backend ?? detectBackend();
  }

  /** False when degraded to `none`. */
  get active(): boolean {
    return this.backend !== "none";
  }

  /** The argv to actually exec. */
  wrap(argv: string[]): string[] {
    if (this.backend === "seatbelt")
      return ["/usr/bin/sandbox-exec", "-f", this.writeProfile(), ...argv];
    if (this.backend === "bubblewrap") return buildBwrapArgv(this.policy, argv);
    return [...argv];
  }

  /** The generated Seatbelt profile - shown by `/permissions`. */
  profileText(): string {
    return buildSeatbeltProfile(this.policy);
  }

  private writeProfile(): string {
    if (this.profilePath === null || !existsSync(this.profilePath)) {
      const path = join(tmpdir(), `hx-sandbox-${randomBytes(4).toString("hex")}.sb`);
      writeFileSync(path, this.profileText(), { mode: 0o600 });
      this.profilePath = path;
    }
    return this.profilePath;
  }

  cleanup(): void {
    if (this.profilePath !== null) {
      rmSync(this.profilePath, { force: true });
      this.profilePath = null;
    }
  }
}

/** Probe for `sandbox-exec` / `bwrap`. */
export function detectBackend(): SandboxBackend {
  if (process.platform === "darwin" && existsSync("/usr/bin/sandbox-exec")) return "seatbelt";
  if (process.platform === "linux" && which("bwrap")) return "bubblewrap";
  return "none";
}

/** Writable: cwd and `$TMPDIR`. Denied: credential paths. Network off. */
export function defaultPolicy(cwd: string, allowNetwork = false): SandboxPolicy {
  // Resolved through /private on macOS; the resolved form is bound, not the
  // whole /var/folders tree, which would hand over every process's temp.
  const temp = resolve(tmpdir());
  const writable = [resolve(cwd), ...tempContainer(temp)];
  // Tool caches that live outside the project but must stay writable, or
  // ordinary commands fail in confusing ways.
  for (const extra of ["~/.cache", "~/Library/Caches"]) {
    const candidate = expandUser(extra);
    if (isDir(candidate)) writable.push(resolve(candidate));
  }
  return {
    writablePaths: [...new Set(writable)],
    readablePaths: ["/"],
    denyPaths: CREDENTIAL_PATHS.map((p) => ({
      path: real(absolute(expandUser(p))),
      isDir: p.endsWith("/"),
    })),
    allowNetwork,
    allowSubprocess: true,
    cwd: resolve(cwd),
  };
}

const DARWIN_TEMP_ROOT = "/private/var/folders";

const isRelativeTo = (path: string, root: string) =>
  path === root || path.startsWith(`${root.replace(/\/$/, "")}/`);

/**
 * The temp directory, plus this user's darwin container when there is one.
 * macOS gives each user a confined directory under `/private/var/folders`
 * holding both `T` (`$TMPDIR`) and `C` (per-user caches). The container - never
 * `/private/var/folders` itself, which is every user's.
 */
function tempContainer(temp: string): string[] {
  const paths = [temp];
  if (isRelativeTo(temp, DARWIN_TEMP_ROOT)) {
    const container = parent(temp);
    if (isRelativeTo(container, DARWIN_TEMP_ROOT) && container !== DARWIN_TEMP_ROOT)
      paths.push(container);
  }
  return paths;
}

/**
 * Generate a Seatbelt profile. Starts from `(deny default)` and allows
 * explicitly; SBPL's last match wins, so the credential denials come after the
 * broad read allowance. Paths are quoted `subpath` literals, not regexes, so a
 * directory named `proj (1)` cannot break out of the profile syntax.
 */
export function buildSeatbeltProfile(policy: SandboxPolicy): string {
  const lines = [
    "(version 1)",
    "(deny default)",
    "",
    ";; Process management",
    "(allow process-fork)",
    "(allow signal (target same-sandbox))",
    "(allow sysctl-read)",
    "(allow mach-lookup)",
    "(allow ipc-posix-shm)",
    policy.allowSubprocess ? "(allow process-exec*)" : "(deny process-exec*)",
    "",
    ";; Reads",
  ];
  for (const path of policy.readablePaths.length ? policy.readablePaths : ["/"]) {
    lines.push(`(allow file-read* (subpath ${sbplString(real(path))}))`);
  }
  lines.push("", ";; Writes");
  for (const path of policy.writablePaths)
    lines.push(`(allow file-write* (subpath ${sbplString(real(path))}))`);
  for (const device of ["/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/urandom"]) {
    lines.push(`(allow file-write-data file-read-data (literal ${sbplString(device)}))`);
  }
  lines.push("", ";; Network", policy.allowNetwork ? "(allow network*)" : "(deny network*)");
  if (policy.denyPaths.length) {
    // Writes too: a project opened in $HOME makes ~/.ssh writable, and a
    // planted authorized_keys is worse than a leaked key.
    lines.push("", ";; Credentials - last match wins, so these override both allowances");
    for (const denied of policy.denyPaths) {
      lines.push(`(deny file-read* file-write* (subpath ${sbplString(real(denied.path))}))`);
    }
  }
  return `${lines.join("\n")}\n`;
}

/** Symlinks resolved: Seatbelt matches against the real path, so an
 * unresolved `/var/folders/...` deny silently covers nothing. */
const real = (path: string) => resolve(path);

/** Quote a path as an SBPL string literal: only `\` and `"` are special. */
function sbplString(path: string): string {
  return `"${path.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

/**
 * Generate the `bwrap` invocation: `--ro-bind / /`, project bound read-write,
 * credential paths shadowed, `--unshare-net` unless network is allowed. A
 * credential path that does not exist but sits inside a writable root is
 * created on the host first - see `ensureMountpoint`.
 */
export function buildBwrapArgv(policy: SandboxPolicy, argv: string[]): string[] {
  const command = [
    "bwrap",
    "--ro-bind",
    "/",
    "/",
    "--dev",
    "/dev",
    "--proc",
    "/proc",
    "--die-with-parent",
  ];
  const writable = policy.writablePaths.map(real);
  for (const path of writable) command.push("--bind", path, path);
  for (const denied of policy.denyPaths) {
    const path = real(denied.path);
    if (!existsSync(path) && !ensureMountpoint(denied, writable)) continue;
    // There is no "deny" in bwrap, so the path is shadowed - with what matches
    // what is there: `--tmpfs` aimed at a regular file aborts bwrap outright.
    if (isDir(path)) command.push("--tmpfs", path, "--remount-ro", path);
    else command.push("--ro-bind", "/dev/null", path);
  }
  if (!policy.allowNetwork) command.push("--unshare-net");
  // The policy's own directory, not `$PWD`, which is whatever the parent
  // shell last exported.
  if (policy.cwd !== null) command.push("--chdir", real(policy.cwd));
  return [...command, "--", ...argv];
}

/**
 * Create a missing credential path on the host, so it can be shadowed. Inside
 * a writable root, a session opened in `$HOME` with no `~/.ssh` yet could
 * otherwise `mkdir ~/.ssh` and plant an `authorized_keys`. The path is created
 * empty and private, as what it is meant to be. Returns whether it now exists.
 */
function ensureMountpoint(denied: DeniedPath, writable: string[]): boolean {
  const path = real(denied.path);
  if (!writable.some((root) => isRelativeTo(path, root))) return false;
  try {
    mkdirSync(parent(path), { recursive: true });
    if (denied.isDir) mkdirSync(path, { mode: 0o700, recursive: true });
    else closeSync(openSync(path, "a", 0o600));
  } catch {
    return false;
  }
  return existsSync(path);
}

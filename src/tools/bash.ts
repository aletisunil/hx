/**
 * Bash tool: a persistent shell session.
 *
 * State (cwd, exported vars, activated venvs) persists across calls within a
 * session, which is what makes multi-step work feel natural. The shell is
 * spawned once inside the sandbox, so every command inherits it; changing the
 * sandbox policy requires `PersistentShell.restart`.
 */

import { type ChildProcess, execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { closeSync, mkdirSync, openSync, readFileSync } from "node:fs";
import { constants } from "node:os";
import { PROVIDER_ENV } from "../auth/resolve.ts";
import { isDir, isFile, join } from "../pathlib.ts";
import { formatFixed, repr, rstrip, splitLines, strip, toFloat, toInt, truthy } from "../py.ts";
import { quote } from "../shlex.ts";
import { count } from "../text.ts";
import {
  isAbort,
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";
import { capOutput, summarizeForUi } from "./output.ts";

const DESCRIPTION = `Run a shell command in a persistent session.

State persists between calls: cd, exported variables and activated environments
carry over. Prefer absolute paths. Use \`timeout\` for long commands, and
\`run_in_background\` for servers or watchers.`;

const DEFAULT_TIMEOUT = 120;
const MAX_TIMEOUT = 600;

/** Wraps a command so it runs under the OS sandbox. */
export interface CommandWrapper {
  wrap(argv: string[]): string[];
}

/**
 * The environment a command runs with: HX's own, minus HX's credentials. The
 * keys HX signs in with are HX's, not the project's; inherited, they were one
 * auto-approved `env` away from the model's context. Every variable HX reads
 * a credential from is withheld, the unprefixed fallbacks included.
 */
export function commandEnvironment(extra: Record<string, string> = {}): Record<string, string> {
  const withheld = new Set(Object.values(PROVIDER_ENV).flat());
  const inherited: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined && !withheld.has(key)) inherited[key] = value;
  }
  return { ...inherited, ...extra };
}

/** Raised by the reader when a command runs past its timeout. */
class Timeout extends Error {}

/** A promise-chain lock: one command in the shell at a time. */
class Lock {
  private tail: Promise<void> = Promise.resolve();

  async acquire(): Promise<() => void> {
    let release!: () => void;
    const next = new Promise<void>((resolve) => {
      release = resolve;
    });
    const previous = this.tail;
    this.tail = previous.then(() => next);
    await previous;
    return release;
  }
}

/** A stdout pipe read as a queue of chunks, so reads can wait with a timeout. */
class Reader {
  private chunks: Buffer[] = [];
  private ended = false;
  private waiter: (() => void) | null = null;

  constructor(stream: NodeJS.ReadableStream) {
    stream.on("data", (chunk: Buffer) => {
      this.chunks.push(chunk);
      this.wake();
    });
    stream.on("end", () => {
      this.ended = true;
      this.wake();
    });
    stream.on("error", () => {
      this.ended = true;
      this.wake();
    });
  }

  private wake(): void {
    this.waiter?.();
    this.waiter = null;
  }

  /** The next chunk; an empty buffer at end of stream. Throws `Timeout` past `seconds`. */
  async read(seconds: number, signal?: AbortSignal): Promise<Buffer> {
    while (this.chunks.length === 0 && !this.ended) {
      if (signal?.aborted) throw signal.reason;
      let timer: NodeJS.Timeout | undefined;
      let onAbort: (() => void) | undefined;
      const outcome = await new Promise<"data" | "timeout" | "abort">((resolve) => {
        this.waiter = () => resolve("data");
        timer = setTimeout(() => resolve("timeout"), Math.max(0, seconds * 1000));
        onAbort = () => resolve("abort");
        signal?.addEventListener("abort", onAbort, { once: true });
      });
      clearTimeout(timer);
      if (onAbort) signal?.removeEventListener("abort", onAbort);
      if (outcome === "timeout") throw new Timeout();
      if (outcome === "abort") throw signal?.reason;
    }
    return this.chunks.shift() ?? Buffer.alloc(0);
  }
}

/** Shells that read the POSIX sh the session speaks to them: `eval`, `$?`,
 * `printf`, `exec 2>&1`. */
const POSIX_SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh", "ksh93", "mksh", "ash", "yash"]);

/**
 * The shell to run commands in: `shell`, or `$SHELL`, when it speaks POSIX sh.
 * csh, tcsh, fish and the like do not - a command would run and its status
 * never arrive - so they give way to bash, or `/bin/sh` without it.
 */
export function posixShell(shell: string | null = null): string {
  const chosen = shell || process.env.SHELL || "";
  if (POSIX_SHELLS.has(chosen.slice(chosen.lastIndexOf("/") + 1))) return chosen;
  return isFile("/bin/bash") ? "/bin/bash" : "/bin/sh";
}

/**
 * A long-lived shell subprocess.
 *
 * Commands are delimited by a random sentinel echoed after each one, so output
 * framing survives commands that print arbitrary text. Without the per-session
 * random suffix a command printing a fixed marker could spoof the boundary and
 * hide its own output.
 */
export class PersistentShell {
  readonly shell: string;
  readonly env: Record<string, string>;
  private process: ChildProcess | null = null;
  private reader: Reader | null = null;
  private readonly sentinel = `__HX_DONE_${randomBytes(8).toString("hex")}__`;
  private readonly lock = new Lock();
  private exitCode = 0;
  private exitStatus: number | null = null;
  private timedOut = false;
  /** Whether this shell's stderr is already joined to its stdout. */
  private merged = false;
  durationMs = 0;

  constructor(
    private currentCwd: string,
    shell: string | null = null,
    env: Record<string, string> = {},
    readonly sandbox: CommandWrapper | null = null,
  ) {
    this.shell = posixShell(shell);
    this.env = commandEnvironment({ ...env, HX: "1", TERM: "dumb" });
  }

  /** Exit status of the most recently completed command. */
  get lastExitCode(): number {
    return this.exitCode;
  }

  /** Where the shell is standing now. Updated from the trailer every command
   * prints, so a `cd` in one call is visible to whatever runs next. */
  get cwd(): string {
    return this.currentCwd;
  }

  private alive(): boolean {
    return this.process !== null && this.exitStatus === null;
  }

  async start(): Promise<void> {
    let argv = [this.shell, "-s"];
    if (this.sandbox !== null) argv = this.sandbox.wrap(argv);
    const child = spawn(argv[0] as string, argv.slice(1), {
      cwd: this.currentCwd,
      env: this.env,
      stdio: ["pipe", "pipe", "pipe"],
      // A session of its own, so a signal to HX's process group is not
      // delivered to the commands it runs.
      detached: true,
    });
    this.exitStatus = null;
    child.on("exit", (code, signal) => {
      // A child killed by a signal reports minus its number, as Python did.
      this.exitStatus = code ?? (signal ? -signalNumber(signal) : 1);
    });
    child.on("error", () => {
      this.exitStatus ??= 127;
    });
    this.process = child;
    this.reader = new Reader(child.stdout as NodeJS.ReadableStream);
    // stderr joins stdout at the descriptor, inside the shell - see `stream`.
    child.stderr?.resume();
    child.stdin?.on("error", () => {});
    this.merged = false;
  }

  async restart(): Promise<void> {
    await this.close();
    await this.start();
  }

  async run(
    command: string,
    timeoutSeconds = DEFAULT_TIMEOUT,
  ): Promise<{ stdout: string; exitCode: number; timedOut: boolean }> {
    let stdout = "";
    for await (const chunk of this.stream(command, timeoutSeconds)) stdout += chunk;
    return { stdout, exitCode: this.exitCode, timedOut: this.timedOut };
  }

  /** Run a command, yielding output as it arrives. Stops the command when `signal` aborts. */
  async *stream(
    command: string,
    timeoutSeconds = DEFAULT_TIMEOUT,
    signal?: AbortSignal,
  ): AsyncGenerator<string> {
    const release = await this.lock.acquire();
    try {
      if (!this.alive()) await this.start();
      const started = performance.now();
      this.timedOut = false;
      this.exitCode = 0;

      // The command reaches the shell as one single-quoted word handed to
      // `eval`, with stdin from /dev/null, so a command that reads stdin cannot
      // eat the trailer, and an unclosed quote ends with the word. Only
      // builtins run here: `eval`, the redirect and `printf`. The trailer
      // carries the working directory as well as the status; `$PWD` is last so
      // a directory with spaces in it needs no quoting to parse.
      //
      // The first command of a shell also sends stderr to stdout, at the
      // descriptor, so the model sees what a user would in the order it was
      // written. On the same line as the command, so the shell numbers its
      // lines as it would have without it: `line 1:` stays line 1.
      const merge = this.merged ? "" : "exec 2>&1; ";
      this.merged = true;
      const payload = `${merge}eval ${quote(command)} </dev/null\nprintf '\\n%s %s %s\\n' ${this.sentinel} "$?" "$PWD"\n`;
      this.process?.stdin?.write(payload);

      try {
        yield* this.readUntilSentinel(timeoutSeconds, signal);
      } catch (error) {
        if (error instanceof Timeout) {
          this.timedOut = true;
          this.exitCode = 124;
          const interrupted = await this.interrupt();
          yield `\n[hx] command exceeded ${formatFixed(timeoutSeconds, 0)}s and was interrupted\n`;
          // The interrupted command still owes a sentinel. Left in the pipe, it
          // would make the *next* command return immediately with no output.
          if (!interrupted || !(await this.drainStaleSentinel())) {
            await this.restart();
            yield "[hx] shell did not recover from the interrupt; restarted\n";
          }
        } else if (isAbort(error, signal)) {
          // The user stopped the turn. The command must stop with it, and its
          // sentinel must not be left for the next command to read.
          if (!(await this.interrupt()) || !(await this.drainStaleSentinel())) await this.restart();
          throw error;
        } else throw error;
      }
      this.durationMs = performance.now() - started;
    } finally {
      release();
    }
  }

  private async *readUntilSentinel(
    timeoutSeconds: number,
    signal?: AbortSignal,
  ): AsyncGenerator<string> {
    const reader = this.reader as Reader;
    // Streaming, so a character split across two reads is decoded whole.
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    const deadline = performance.now() / 1000 + timeoutSeconds;
    while (true) {
      const remaining = deadline - performance.now() / 1000;
      if (remaining <= 0) throw new Timeout();
      const data = await reader.read(remaining, signal);
      if (data.length === 0) {
        // The shell died - `exit 3`, a crash, a kill. Wait for it to be reaped
        // before reading the status.
        const until = performance.now() + 2000;
        while (this.exitStatus === null && performance.now() < until) await sleep(10);
        this.exitCode = this.exitStatus ?? 1;
        buffer += decoder.decode();
        if (buffer) yield buffer;
        return;
      }
      buffer += decoder.decode(data, { stream: true });
      const at = buffer.indexOf(this.sentinel);
      if (at >= 0) {
        const head = buffer.slice(0, at);
        [this.exitCode, this.currentCwd] = parseTrailer(
          buffer.slice(at + this.sentinel.length),
          this.currentCwd,
        );
        if (head) yield head;
        return;
      }
      // Hold back only what could be the start of the sentinel, so it is never
      // emitted as output and a short line is not hidden until the end.
      const held = sentinelPrefixAtEnd(buffer, this.sentinel);
      if (buffer.length > held) {
        yield buffer.slice(0, buffer.length - held);
        buffer = buffer.slice(buffer.length - held);
      }
    }
  }

  /** Consume output up to the sentinel owed by an interrupted command. */
  private async drainStaleSentinel(timeoutSeconds = 5): Promise<boolean> {
    const reader = this.reader as Reader;
    let buffer = "";
    const deadline = performance.now() / 1000 + timeoutSeconds;
    while (true) {
      const remaining = deadline - performance.now() / 1000;
      if (remaining <= 0) return false;
      let data: Buffer;
      try {
        data = await reader.read(remaining);
      } catch {
        return false;
      }
      if (data.length === 0) return false;
      buffer += data.toString("utf8");
      if (buffer.includes(this.sentinel)) return true;
    }
  }

  /**
   * Interrupt the running command while leaving the shell alive. Signalling
   * the whole process group would kill the shell too, and with it the
   * session's cd and exported variables, so only the shell's own children are
   * signalled. Returns false when no child could be found.
   */
  async interrupt(): Promise<boolean> {
    if (!this.alive() || this.process?.pid === undefined) return false;
    const children = await childPids(this.process.pid);
    if (children.length === 0) return false;
    for (const pid of children) signalProcess(pid, "SIGINT");
    await sleep(200);
    for (const pid of children) signalProcess(pid, "SIGKILL");
    return true;
  }

  async close(): Promise<void> {
    const child = this.process;
    if (child === null) return;
    if (this.exitStatus === null) {
      child.kill("SIGTERM");
      const until = performance.now() + 5000;
      while (this.exitStatus === null && performance.now() < until) await sleep(10);
    }
    child.stdin?.destroy();
    child.stdout?.destroy();
    child.stderr?.destroy();
    this.process = null;
  }
}

function signalProcess(pid: number, signal: NodeJS.Signals): void {
  try {
    process.kill(pid, signal);
  } catch {
    // Already gone, or not ours.
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

interface BackgroundJob {
  readonly jobId: string;
  readonly command: string;
  readonly process: ChildProcess;
  readonly logPath: string;
  readonly started: number;
  exitCode: number | null;
  running: boolean;
  /** Lines already returned to the model, so polling only yields what is new. */
  cursor: number;
}

/**
 * Tracks commands started with `run_in_background`. Background work runs in
 * its own process rather than the persistent shell: a long-running server
 * would otherwise hold the shell's sentinel forever and wedge every later command.
 */
export class BackgroundJobs {
  private readonly jobs = new Map<string, BackgroundJob>();

  constructor(public logDir: string) {}

  /**
   * Returns a job id the model can poll. `env` is the persistent shell's, so a
   * command sees the same `HX` and `TERM` whichever way it runs.
   */
  async start(
    command: string,
    cwd: string,
    env: Record<string, string>,
    sandbox: CommandWrapper | null = null,
  ): Promise<string> {
    const jobId = `bg_${randomBytes(4).toString("hex")}`;
    mkdirSync(this.logDir, { recursive: true });
    const logPath = join(this.logDir, `${jobId}.log`);
    let argv = ["/bin/sh", "-c", command];
    if (sandbox !== null) argv = sandbox.wrap(argv);

    // Closed as soon as the child has it. stdin is /dev/null, not HX's own:
    // inherited, it is the terminal the prompt reads from.
    const log = openSync(logPath, "w");
    let child: ChildProcess;
    try {
      child = spawn(argv[0] as string, argv.slice(1), {
        cwd,
        env,
        stdio: ["ignore", log, log],
        detached: true,
      });
    } finally {
      closeSync(log);
    }
    const job: BackgroundJob = {
      jobId,
      command,
      process: child,
      logPath,
      started: Date.now() / 1000,
      exitCode: null,
      running: true,
      cursor: 0,
    };
    child.on("exit", (code, signal) => {
      job.running = false;
      job.exitCode = code ?? (signal ? -signalNumber(signal) : null);
    });
    child.on("error", () => {
      job.running = false;
      job.exitCode = 127;
    });
    child.unref();
    this.jobs.set(jobId, job);
    return jobId;
  }

  output(jobId: string, sinceLine = 0): string {
    return this.read(jobId, sinceLine)[0];
  }

  /** Output and the next line cursor. The cursor is remembered per job, so
   * repeated polling returns only what is new. */
  read(jobId: string, sinceLine: number | null = null): [string, number] {
    const job = this.get(jobId);
    let lines: string[];
    try {
      lines = splitLines(
        new TextDecoder().decode(readFileSync(job.logPath)).replace(/\r\n?/g, "\n"),
      );
    } catch {
      return ["(no output yet)", job.cursor];
    }
    const start = sinceLine === null ? job.cursor : Math.max(0, sinceLine);
    const chunk = lines.slice(start);
    job.cursor = lines.length;
    return [chunk.join("\n") || "(no new output)", job.cursor];
  }

  state(jobId: string): { job_id: string; running: boolean; exit_code: number | null } {
    const job = this.get(jobId);
    return { job_id: job.jobId, running: job.running, exit_code: job.exitCode };
  }

  kill(jobId: string): void {
    const job = this.get(jobId);
    if (job.process.pid !== undefined) signalProcess(-job.process.pid, "SIGTERM");
  }

  list(): Record<string, unknown>[] {
    return [...this.jobs.values()].map((job) => ({
      job_id: job.jobId,
      command: job.command,
      running: job.running,
      exit_code: job.exitCode,
      elapsed_s: Number(formatFixed(Date.now() / 1000 - job.started, 1)),
    }));
  }

  private get(jobId: string): BackgroundJob {
    const job = this.jobs.get(jobId);
    if (job === undefined) throw new ToolError(`unknown background job ${repr(jobId)}`);
    return job;
  }

  /** Signal every job, then wait for it to actually go. */
  async closeAll(): Promise<void> {
    const jobs = [...this.jobs.values()];
    for (const job of jobs) this.kill(job.jobId);
    const until = performance.now() + 5000;
    while (jobs.some((job) => job.running) && performance.now() < until) await sleep(10);
  }
}

/** A signal's number, for the `-signum` Python reports for a child it killed. */
function signalNumber(signal: NodeJS.Signals): number {
  return constants.signals[signal] ?? 1;
}

export class BashTool extends Tool {
  readonly name = "Bash";
  readonly description = DESCRIPTION;
  override readonly mutating = true;

  constructor(
    readonly shell: PersistentShell,
    readonly jobs: BackgroundJobs,
  ) {
    super();
  }

  schema() {
    return {
      type: "object",
      properties: {
        command: { type: "string" },
        timeout: {
          type: "number",
          description: "Seconds. Capped by the project's configured maximum.",
        },
        run_in_background: { type: "boolean", default: false },
        description: { type: "string", description: "5-10 word description of what this runs" },
      },
      required: ["command"],
    };
  }

  /** The raw command string. The permission parser splits it into segments
   * before matching, so compound commands cannot smuggle a denied segment past
   * an allowed prefix. */
  override permissionSpecifier(params: Params): string | null {
    return truthy(params.command) ? String(params.command) : null;
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const command = strip(String(params.command));
    if (!command) throw new ToolError("command is empty");
    const timeout = this.timeout(params, ctx);

    if (truthy(params.run_in_background)) {
      // The shell's directory, not the session's: `cd build && make` in one
      // call then a background `./run` in the next means what it would in a terminal.
      const jobId = await this.jobs.start(
        command,
        this.shell.cwd,
        this.shell.env,
        this.shell.sandbox,
      );
      return toolResult(
        `Started background job ${jobId}.\nRead its output with BashOutput(job_id=${repr(jobId)}), stop it with KillShell(job_id=${repr(jobId)}).`,
        { summary: `background ${jobId}`, metadata: { job_id: jobId } },
      );
    }

    let raw = "";
    for await (const chunk of this.shell.stream(command, timeout, ctx.signal)) {
      raw += chunk;
      ctx.emitProgress(chunk);
    }
    const capped = capOutput(raw, {
      sessionId: ctx.sessionId,
      toolUseId: ctx.toolUseId,
      charCap: ctx.settings.context.toolOutputCharCap,
      lineCap: ctx.settings.context.toolOutputLineCap,
    });
    const exitCode = this.shell.lastExitCode;
    let body = rstrip(capped.text) || "(no output)";
    if (exitCode !== 0) body = `${body}\n\n[exit code ${exitCode}]`;
    return toolResult(body, {
      isError: exitCode !== 0,
      spilledPath: capped.spilledPath,
      summary: strip(raw) ? summarizeForUi(raw) : `exit ${exitCode}`,
      metadata: { exit_code: exitCode, truncated: capped.truncated },
    });
  }

  /** Model request, bounded by the configured ceiling. */
  private timeout(params: Params, ctx: ToolContext): number {
    const settings = ctx.settings.bash;
    const requested = params.timeout;
    const fallback = settings?.timeoutSeconds ?? DEFAULT_TIMEOUT;
    const ceiling = settings?.maxTimeoutSeconds ?? MAX_TIMEOUT;
    return Math.min(truthy(requested) ? toFloat(requested) : fallback, ceiling);
  }
}

/** Direct children of a process, via `pgrep -P`. Empty when none or unavailable. */
function childPids(parent: number): Promise<number[]> {
  return new Promise((resolve) => {
    execFile("pgrep", ["-P", String(parent)], { timeout: 5000 }, (_error, stdout) => {
      resolve(
        String(stdout ?? "")
          .split(/\s+/)
          .filter((line) => /^\d+$/.test(line))
          .map(Number),
      );
    });
  });
}

/**
 * Read new output from a background job. Without this the Bash tool could
 * start a job and never hear from it again.
 */
export class BashOutputTool extends Tool {
  readonly name = "BashOutput";
  readonly description =
    "Read output from a background job started by Bash with run_in_background.\n\nReturns lines since the last read. Call it again to poll for more.";

  constructor(private readonly jobs: BackgroundJobs) {
    super();
  }

  schema() {
    return {
      type: "object",
      properties: {
        job_id: { type: "string" },
        since_line: {
          type: "integer",
          description: "Skip this many lines from the start (default: continue)",
        },
      },
      required: ["job_id"],
    };
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const jobId = String(params.job_id);
    const since = params.since_line;
    const [text, nextLine] = this.jobs.read(
      jobId,
      since !== null && since !== undefined ? toInt(since) : null,
    );
    const capped = capOutput(text, {
      sessionId: ctx.sessionId,
      toolUseId: ctx.toolUseId,
      charCap: ctx.settings.context.toolOutputCharCap,
      lineCap: ctx.settings.context.toolOutputLineCap,
    });
    const status = this.jobs.state(jobId);
    let body = capped.text;
    if (!status.running)
      body += `\n\n[job finished, exit code ${status.exit_code === null ? "None" : status.exit_code}]`;
    return toolResult(body, {
      spilledPath: capped.spilledPath,
      summary: `${count(nextLine, "line")}, ${status.running ? "running" : "finished"}`,
      metadata: { next_line: nextLine, ...status },
    });
  }
}

/** Stop a background job. */
export class KillShellTool extends Tool {
  readonly name = "KillShell";
  readonly description = "Stop a background job started by Bash with run_in_background.";
  override readonly mutating = true;

  constructor(private readonly jobs: BackgroundJobs) {
    super();
  }

  schema() {
    return { type: "object", properties: { job_id: { type: "string" } }, required: ["job_id"] };
  }

  override permissionSpecifier(params: Params): string {
    return String(params.job_id ?? "");
  }

  async run(params: Params): Promise<ToolResult> {
    const jobId = String(params.job_id);
    this.jobs.kill(jobId);
    return toolResult(`Killed ${jobId}.`, { summary: `killed ${jobId}` });
  }
}

/** Length of the longest end of `buffer` that `sentinel` starts with. */
function sentinelPrefixAtEnd(buffer: string, sentinel: string): number {
  for (let size = Math.min(buffer.length, sentinel.length - 1); size > 0; size--) {
    if (sentinel.startsWith(buffer.slice(-size))) return size;
  }
  return 0;
}

/**
 * Read the `<status> <pwd>` the sentinel is followed by. The directory is
 * taken only when the shell reported one that exists; a trailer cut short by a
 * crash leaves the last known cwd alone.
 */
function parseTrailer(tail: string, previous: string): [number, string] {
  const text = strip(tail);
  if (!text) return [0, previous];
  const space = text.indexOf(" ");
  const status = space < 0 ? text : text.slice(0, space);
  const rest = space < 0 ? "" : text.slice(space + 1);
  const code = parseExitCode(status);
  const directory = rest ? strip(splitLines(rest)[0] ?? "") : "";
  if (directory.startsWith("/") && isDir(directory)) return [code, directory];
  return [code, previous];
}

function parseExitCode(tail: string): number {
  for (const token of strip(tail).split(/\s+/)) {
    if (/^-*\d+$/.test(token)) return Number.parseInt(token.replace(/^-+/, "-"), 10);
  }
  return 0;
}

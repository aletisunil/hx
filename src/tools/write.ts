/** Write tool: create or fully replace a file. */

import { randomBytes } from "node:crypto";
import {
  chmodSync,
  closeSync,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import type { CheckpointStore } from "../core/checkpoints.ts";
import { basename, isDir, join, parent, resolve } from "../pathlib.ts";
import { count } from "../text.ts";
import { split } from "./anchors.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";
import { resolvePath } from "./read.ts";

const DESCRIPTION = `Write a file, overwriting it if it exists.

Overwriting a file that has not been read in this session is refused - use Read
first, or Edit for partial changes.`;

/**
 * How Edit and Write say what to do about an unread file. A model that looked
 * at the file through `cat` in Bash has seen it, and is told it has not;
 * without the second half it reads the refusal as a glitch and tries again.
 */
export const READ_FIRST =
  "Read it with the Read tool first - output from a shell command does not count.";

export class WriteTool extends Tool {
  readonly name = "Write";
  readonly description = DESCRIPTION;
  override readonly mutating = true;

  /** `checkpoints` is given the file's previous bytes so `/rewind` can put them
   * back. Optional: a caller with no session has nothing to rewind to. */
  constructor(private readonly checkpoints: CheckpointStore | null = null) {
    super();
  }

  schema() {
    return {
      type: "object",
      properties: { file_path: { type: "string" }, content: { type: "string" } },
      required: ["file_path", "content"],
    };
  }

  override permissionSpecifier(params: Params): string {
    return String(params.file_path ?? "");
  }

  /** Writes atomically (temp file + rename) so a crash cannot truncate the target. */
  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const path = resolvePath(String(params.file_path), ctx.cwd);
    const content = String(params.content);

    if (existsSync(path)) {
      if (isDir(path)) throw new ToolError(`${path} is a directory`);
      if (!ctx.reads.wasRead(path)) {
        throw new ToolError(
          `${path} exists but has not been read in this session, so the overwrite would not be deliberate. ${READ_FIRST}`,
        );
      }
      if (ctx.reads.changedSinceRead(path)) {
        throw new ToolError(
          `${path} changed on disk since it was read. Read it again before writing.`,
        );
      }
    }

    const existed = existsSync(path);
    mkdirSync(parent(path), { recursive: true });
    this.checkpoints?.capture(path);
    writeAtomic(path, content);
    ctx.reads.markRead(path);
    this.checkpoints?.settle(path);

    const verb = existed ? "updated" : "created";
    // Lines as Read numbers them: a final newline ends the last line rather
    // than starting another, so "done\n" is one line, not two.
    const lines = split(content)[0].length;
    return toolResult(`${verb} ${path}${throughLink(path)} (${count(lines, "line")})`, {
      summary: `${verb} ${basename(path)}`,
      metadata: { path, created: !existed },
    });
  }
}

function isSymlink(path: string): boolean {
  try {
    return lstatSync(path).isSymbolicLink();
  } catch {
    return false;
  }
}

/** The file a write to `path` really changes, when `path` is a symlink. The
 * link can point anywhere, so callers name it to the user and the model. */
export function linkTarget(path: string): string | null {
  return isSymlink(path) ? resolve(path) : null;
}

/** ` through the link to <target>` for a symlink, else nothing. */
export function throughLink(path: string): string {
  const target = linkTarget(path);
  return target !== null ? ` through the link to ${target}` : "";
}

/**
 * Write via a temp file in the same directory, then rename.
 *
 * A symlink is written through - the rename lands on the file it points at,
 * and the link stays a link. A link to a file that does not exist yet creates
 * it, provided its directory exists. The file's mode is carried onto the temp
 * file, which is created with it rather than narrowed after the write: in
 * between, a private file's new content would be readable by every user.
 *
 * Text is written as given, with no newline translation.
 */
export function writeAtomic(path: string, content: string): void {
  const target = linkTarget(path) ?? path;
  if (target !== path && !isDir(parent(target))) {
    throw new ToolError(`${path} is a link to ${target}, and ${parent(target)} does not exist`);
  }
  let mode: number | null;
  try {
    mode = statSync(target).mode & 0o7777;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    mode = null;
  }

  const tmp = join(parent(target), `.${basename(target)}.${randomBytes(4).toString("hex")}.hx-tmp`);
  // The kernel applies the umask on top: the temp file never allows more than
  // the file it replaces, and a new file gets 0o666 less the umask.
  const fd = openSync(tmp, "wx", mode ?? 0o666);
  try {
    try {
      writeFileSync(fd, content, "utf8");
    } finally {
      closeSync(fd);
    }
    // Again, exactly: the umask may have taken bits the file had.
    if (mode !== null) chmodSync(tmp, mode);
    renameSync(tmp, target);
  } catch (error) {
    rmSync(tmp, { force: true });
    throw error;
  }
}

/**
 * File checkpoints, so a rewind can undo edits as well as words.
 *
 * Before each mutation Write and Edit park the file's previous bytes in a
 * content-addressed store under the session directory and record where in the
 * transcript that happened; a rewind to message `k` puts back the bytes each
 * file had before the first change after `k`.
 *
 * Two hashes per entry, not one. The pre-image says what to put back; the
 * post-image says what HX left behind, and a file whose bytes have moved since
 * then was changed by someone else. Restoring over that would destroy work HX
 * never did, so those files are reported and left alone.
 *
 * Only Write and Edit are covered. A shell command that rewrites a file is
 * invisible here, and the restore report says so.
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join, parent, resolve } from "../pathlib.ts";
import { osErrorText, truthy } from "../py.ts";
import type { Session } from "./session.ts";

/** Files above this are recorded but not snapshotted. */
export const MAX_BLOB_BYTES = 8 * 1024 * 1024;

/** One file, at one point in the transcript. */
export interface Checkpoint {
  /** Number of messages in the transcript when the change was made, so a
   * rewind to message `k` restores exactly the entries with `index >= k`. */
  index: number;
  path: string;
  /** False when HX created the file - restoring means deleting it again. */
  existed: boolean;
  /** Digest of the pre-image blob. `null` when the file did not exist or was
   * too large to snapshot. */
  before: string | null;
  /** Digest of what HX left on disk, or `null` if that could not be read. */
  after: string | null;
  /** False when the pre-image could not be stored. */
  captured: boolean;
}

export const checkpointToDict = (c: Checkpoint) => ({
  index: c.index,
  path: c.path,
  existed: c.existed,
  before: c.before,
  after: c.after,
  captured: c.captured,
});

export function checkpointFromDict(raw: Record<string, unknown>): Checkpoint {
  return {
    index: Number(raw.index),
    path: String(raw.path),
    existed: truthy(raw.existed),
    before: (raw.before as string | null) ?? null,
    after: (raw.after as string | null) ?? null,
    captured: truthy(raw.captured ?? true),
  };
}

/** What a restore actually did. Every path lands in exactly one list. */
export class RestoreReport {
  restored: string[] = [];
  removed: string[] = [];
  /** Changed by someone other than HX since HX wrote them, so left alone. */
  conflicted: string[] = [];
  /** `[path, reason]` - no snapshot to put back. */
  skipped: [string, string][] = [];

  get touched(): number {
    return this.restored.length + this.removed.length;
  }

  summary(): string {
    const parts: string[] = [];
    if (this.restored.length) parts.push(`${this.restored.length} file(s) restored`);
    if (this.removed.length) parts.push(`${this.removed.length} removed`);
    if (this.conflicted.length)
      parts.push(`${this.conflicted.length} changed since and left alone`);
    if (this.skipped.length) parts.push(`${this.skipped.length} not snapshotted`);
    return parts.length ? parts.join(", ") : "no files to restore";
  }
}

/**
 * Content-addressed pre-images for one session.
 *
 * Capture and settle are two calls because one hash cannot answer both
 * questions a restore asks. Between them the entry is held in memory: the
 * transcript is append-only, so a record written at capture time could never
 * be given its post-image.
 */
export class CheckpointStore {
  private pending = new Map<string, Checkpoint>();

  constructor(
    public session: Session,
    public root: string,
    readonly maxBytes = MAX_BLOB_BYTES,
  ) {}

  /** Follow the app to another session. A capture that was never settled
   * belongs to the session being left behind. */
  attach(session: Session, root: string): void {
    this.session = session;
    this.root = root;
    this.pending.clear();
  }

  /** Park the current bytes of `path` before it is written. */
  capture(path: string): void {
    const key = storeKey(path);
    const index = this.session.messages.length;
    const entry = (fields: Partial<Checkpoint>): Checkpoint => ({
      index,
      path: key,
      existed: true,
      before: null,
      after: null,
      captured: true,
      ...fields,
    });
    let data: Buffer;
    try {
      data = readFileSync(path);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT" || code === "EISDIR") {
        this.pending.set(key, entry({ existed: false }));
      } else {
        this.pending.set(key, entry({ captured: false }));
      }
      return;
    }
    if (data.length > this.maxBytes) {
      this.pending.set(key, entry({ captured: false }));
      return;
    }
    const digest = this.store(data);
    this.pending.set(key, entry({ before: digest, captured: digest !== null }));
  }

  /** Record the entry now that `path` holds what HX wrote. A capture with no
   * settle is a mutation that never landed, so it is dropped. */
  settle(path: string): void {
    const key = storeKey(path);
    const entry = this.pending.get(key);
    if (entry === undefined) return;
    this.pending.delete(key);
    entry.after = digestOf(path);
    this.session.recordCheckpoint(entry);
  }

  /** Put every file back the way it was before message `index`. */
  restoreTo(index: number): RestoreReport {
    const report = new RestoreReport();
    const first = new Map<string, Checkpoint>();
    const last = new Map<string, Checkpoint>();
    for (const entry of this.session.checkpoints) {
      if (entry.index < index) continue;
      if (!first.has(entry.path)) first.set(entry.path, entry);
      last.set(entry.path, entry);
    }
    for (const [key, entry] of first) {
      if (digestOf(key) !== (last.get(key) as Checkpoint).after) {
        report.conflicted.push(key);
        continue;
      }
      if (!entry.existed) this.remove(key, report);
      else if (!entry.captured || entry.before === null) {
        report.skipped.push([key, "no snapshot was taken"]);
      } else this.putBack(key, entry.before, report);
    }
    return report;
  }

  private putBack(key: string, digest: string, report: RestoreReport): void {
    let data: Buffer;
    try {
      data = readFileSync(this.blobPath(digest));
    } catch {
      report.skipped.push([key, "the snapshot is gone"]);
      return;
    }
    try {
      mkdirSync(parent(key), { recursive: true });
      writeFileSync(key, data);
    } catch (error) {
      report.skipped.push([key, osErrorText(error)]);
      return;
    }
    report.restored.push(key);
  }

  private remove(key: string, report: RestoreReport): void {
    try {
      rmSync(key, { force: true });
    } catch (error) {
      report.skipped.push([key, osErrorText(error)]);
      return;
    }
    report.removed.push(key);
  }

  blobPath(digest: string): string {
    return join(this.root, digest);
  }

  /** Write the blob under its own digest. Identical bytes are stored once. */
  private store(data: Buffer): string | null {
    const digest = createHash("sha256").update(data).digest("hex");
    const blob = this.blobPath(digest);
    if (existsSync(blob)) return digest;
    try {
      mkdirSync(this.root, { recursive: true });
      const tmp = join(this.root, `.${digest}.hx-tmp`);
      writeFileSync(tmp, data);
      renameSync(tmp, blob);
    } catch {
      // A full or read-only disk must not fail the edit the user asked for;
      // the entry records that there is nothing to put back.
      return null;
    }
    return digest;
  }
}

/**
 * The identity of a file, as the store records it: resolved, the way the read
 * tracker keys its reads, so two spellings of one file share one chain.
 */
export function storeKey(path: string): string {
  return resolve(path);
}

/** SHA-256 of a file, or `null` when it cannot be read - which is also how
 * "the file is not there" is spelled. */
export function digestOf(path: string): string | null {
  try {
    return createHash("sha256").update(readFileSync(path)).digest("hex");
  } catch {
    return null;
  }
}

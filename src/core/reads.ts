/**
 * What a session has read: the record behind the read-before-edit rule.
 *
 * Edit and Write refuse a file the model has not read, because a model editing
 * a file it has never seen is guessing at it. "Read" means read in this
 * session's transcript, so the record lives in the session, not in the
 * process: a resumed session gets back the reads its transcript holds, `/clear`
 * starts with none, and a rewind loses the reads it cuts away.
 */

import { statSync } from "node:fs";
import { digestOf, storeKey } from "./checkpoints.ts";
import type { Session } from "./session.ts";

/** One file as the model saw it. */
export interface FileRead {
  /** `messages.length` when the file was read: where the result that showed
   * it lands. A rewind to this index or earlier cuts the read with it. */
  readonly index: number;
  /** Resolved, as `storeKey` spells it. */
  readonly path: string;
  /** SHA-256 of the content then. `null` when it could not be read. */
  readonly digest: string | null;
}

export const fileReadToDict = (read: FileRead) => ({
  index: read.index,
  path: read.path,
  digest: read.digest,
});

export const fileReadFromDict = (raw: Record<string, unknown>): FileRead => ({
  index: Number(raw.index),
  path: String(raw.path),
  digest: (raw.digest as string | null) ?? null,
});

/**
 * The reads of one session, as the tools ask about them.
 *
 * A view, not a store: the reads are `Session.reads`, so whatever replaces the
 * session replaces what counts as read with it.
 */
export class FileTracker {
  constructor(public session: Session) {}

  /** Record `path` as the model now sees it: after a Read, or after an Edit
   * or Write, whose result shows what HX itself put there. */
  markRead(path: string): void {
    this.session.recordRead(storeKey(path), digestOf(path));
  }

  wasRead(path: string): boolean {
    return this.session.readDigests.has(storeKey(path));
  }

  changedSinceRead(path: string): boolean {
    const key = storeKey(path);
    const known = this.session.readDigests;
    return known.has(key) && known.get(key) !== digestOf(path);
  }

  /** Every read file whose content is no longer what was read. A file is
   * hashed again only when its stat says it may have changed. */
  staleFiles(): string[] {
    return [...this.session.readDigests]
      .filter(([key, digest]) => cachedDigest(key) !== digest)
      .map(([key]) => key);
  }
}

/** Each checked path's (inode, size, mtime) and its digest at that stat. */
const STAT_DIGESTS = new Map<string, [string, string | null]>();

function cachedDigest(key: string): string | null {
  let stamp: string;
  try {
    const info = statSync(key, { bigint: true });
    stamp = `${info.ino}:${info.size}:${info.mtimeNs}`;
  } catch {
    STAT_DIGESTS.delete(key);
    return null;
  }
  const cached = STAT_DIGESTS.get(key);
  if (cached !== undefined && cached[0] === stamp) return cached[1];
  const digest = digestOf(key);
  STAT_DIGESTS.set(key, [stamp, digest]);
  return digest;
}

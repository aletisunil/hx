/**
 * Session persistence: append-only JSONL transcripts.
 *
 * Every message, tool result, usage record, file checkpoint, file read and
 * compaction boundary is appended as it happens, so a crashed session is fully
 * resumable and a compaction can be inspected or undone after the fact.
 *
 * Nothing is ever rewritten or removed - a rewind is one more record, and the
 * state it produces is whatever replaying the file yields. That is what keeps
 * "undo" and "resume" the same code path rather than two that must agree.
 */

import { randomBytes } from "node:crypto";
import { appendFileSync, mkdirSync, statSync, writeFileSync } from "node:fs";
import { entries } from "../glob.ts";
import { dumps, PyFloat } from "../json.ts";
import { exists, isDir, isFile, join, parent, readText, resolve } from "../pathlib.ts";
import { sessionDir, sessionOutputsDir, sessionsDir, sessionTranscriptFile } from "../paths.ts";
import { repr, strftime, strip } from "../py.ts";
import { type Checkpoint, checkpointFromDict, checkpointToDict } from "./checkpoints.ts";
import { fromDict, type Message, messageText, now, toDict } from "./messages.ts";
import { type FileRead, fileReadFromDict, fileReadToDict } from "./reads.ts";
import { type TurnUsage, turnUsage, UsageLedger } from "./usage.ts";

export interface SessionMeta {
  sessionId: string;
  cwd: string;
  createdAt: number;
  updatedAt: number;
  model: string;
  title: string | null;
  /** How many prompts the session held when the title was written, so the
   * session can tell when the conversation has moved on from its name. */
  titlePromptCount: number;
  /** The user named the session with `/title`. Their name is never replaced
   * by a generated one. */
  titlePinned: boolean;
  /** Every message record, tool results included: roughly twice the number
   * of provider calls, and no relation to the number of things typed. */
  messageCount: number;
  /** Distinct prompts the user actually sent - the number `/resume` leads with. */
  promptCount: number;
  /** Set for subagent sessions, which nest under their parent's directory. */
  parentId: string | null;
}

/** `meta.json`, with the keys in the order they have always been written. */
export function metaToDict(meta: SessionMeta): Record<string, unknown> {
  return {
    session_id: meta.sessionId,
    cwd: meta.cwd,
    created_at: new PyFloat(meta.createdAt),
    updated_at: new PyFloat(meta.updatedAt),
    model: meta.model,
    title: meta.title,
    title_prompt_count: meta.titlePromptCount,
    title_pinned: meta.titlePinned,
    message_count: meta.messageCount,
    prompt_count: meta.promptCount,
    parent_id: meta.parentId,
  };
}

/**
 * Build a `SessionMeta` from a `meta.json` payload.
 *
 * Unknown keys are dropped rather than raising: a session written by a newer
 * HX has to stay resumable by an older one. Missing keys fall back to the
 * defaults, which is what lets a session recorded before `prompt_count`
 * existed still list.
 */
function metaFromDict(raw: Record<string, unknown>): SessionMeta {
  for (const required of ["session_id", "cwd", "created_at", "updated_at", "model"]) {
    if (!(required in raw)) throw new TypeError(`meta.json is missing ${required}`);
  }
  return {
    sessionId: raw.session_id as string,
    cwd: raw.cwd as string,
    createdAt: raw.created_at as number,
    updatedAt: raw.updated_at as number,
    model: raw.model as string,
    title: (raw.title as string | null) ?? null,
    titlePromptCount: (raw.title_prompt_count as number) ?? 0,
    titlePinned: (raw.title_pinned as boolean) ?? false,
    messageCount: (raw.message_count as number) ?? 0,
    promptCount: (raw.prompt_count as number) ?? 0,
    parentId: (raw.parent_id as string | null) ?? null,
  };
}

/**
 * What the session was assembled from, as the provider received it.
 *
 * The transcript records what was said; this records what the model was told
 * before anybody said anything - the prompt and the tool schemas.
 */
export interface Environment {
  systemPrompt: string;
  tools: Record<string, unknown>[];
  projectContext: string | null;
  skillsIndex: string | null;
}

/** A prompt the session can be taken back to. */
export interface RewindPoint {
  /** Position in `messages`. Rewinding here drops this prompt and everything after it. */
  readonly index: number;
  readonly text: string;
  readonly timestamp: number;
  /** The prompt is behind a compaction summary. Rewinding to it undoes that
   * compaction too, which is the only way back to the turns it replaced. */
  readonly compacted: boolean;
}

export function usageToDict(usage: TurnUsage): Record<string, unknown> {
  return {
    input_tokens: usage.inputTokens,
    output_tokens: usage.outputTokens,
    cache_read_tokens: usage.cacheReadTokens,
    cache_write_tokens: usage.cacheWriteTokens,
    reasoning_tokens: usage.reasoningTokens,
    cost_usd: usage.costUsd === null ? null : new PyFloat(usage.costUsd),
    latency_ms: new PyFloat(usage.latencyMs),
  };
}

function usageFromDict(raw: Record<string, unknown>): TurnUsage {
  return turnUsage({
    inputTokens: (raw.input_tokens as number) ?? 0,
    outputTokens: (raw.output_tokens as number) ?? 0,
    cacheReadTokens: (raw.cache_read_tokens as number) ?? 0,
    cacheWriteTokens: (raw.cache_write_tokens as number) ?? 0,
    reasoningTokens: (raw.reasoning_tokens as number) ?? 0,
    costUsd: (raw.cost_usd as number | null) ?? null,
    latencyMs: (raw.latency_ms as number) ?? 0,
  });
}

/** In-memory session state, mirrored to `~/.hx/sessions/<id>/transcript.jsonl`. */
export class Session {
  messages: Message[] = [];
  usage = new UsageLedger();
  /** Pre-images of the files HX changed, in the order they were changed. */
  checkpoints: Checkpoint[] = [];
  /** The files the model has seen, in the order it saw them. */
  reads: FileRead[] = [];
  /** The prompt and tools this session ran with. `null` until the first
   * provider call, and for sessions recorded before it was written down. */
  environment: Environment | null = null;
  private pending: Record<string, unknown>[] = [];
  /** Identities of the prompts seen so far. A set rather than a counter
   * because a compaction re-appends the turns it kept verbatim. */
  private promptKeys = new Set<string>();
  /** Each read path's digest at its latest read. */
  private digests = new Map<string, string | null>();

  constructor(public meta: SessionMeta) {}

  /** Each file read so far, with its content hash at the latest read. */
  get readDigests(): ReadonlyMap<string, string | null> {
    return this.digests;
  }

  /** Add to memory and flush the JSONL line. Ephemeral messages are recorded
   * but marked so resume does not replay stale reminders. */
  append(message: Message): void {
    this.messages.push(message);
    if (isPrompt(message)) this.promptKeys.add(promptIdentity(message));
    this.meta.messageCount = this.messages.length;
    this.meta.promptCount = this.promptKeys.size;
    this.meta.updatedAt = now();
    this.pending.push({ kind: "message", data: toDict(message) });
    this.flush();
  }

  /**
   * Retire the pre-compaction history and re-lay it as summary + kept.
   *
   * The transcript is append-only, so the summary cannot be spliced in ahead
   * of the messages it replaces. Instead every live message is flagged
   * compacted and the new sequence is appended in order.
   */
  recordCompaction(dropped: Message[], kept: Message[], summary: Message): void {
    const superseded = [...dropped, ...kept];
    for (const m of superseded) m.compacted = true;
    this.pending.push({ kind: "compaction", data: { count: superseded.length } });
    this.append(summary);
    for (const m of kept) this.append({ ...m, compacted: false });
  }

  /**
   * Record the prompt and tool schemas in force. Once per session, on the
   * first provider call: the first moment the tool registry has settled.
   */
  recordEnvironment(environment: Environment): void {
    if (this.environment !== null) return;
    this.environment = environment;
    this.pending.push({
      kind: "environment",
      data: {
        system_prompt: environment.systemPrompt,
        tools: environment.tools,
        project_context: environment.projectContext,
        skills_index: environment.skillsIndex,
      },
    });
    this.flush();
  }

  /**
   * Name the session for `/resume`.
   *
   * `pinned` marks a name the user chose, which generated names never
   * replace. `updatedAt` is deliberately left alone: it orders the picker and
   * must keep reflecting real activity.
   */
  setTitle(title: string, options: { pinned?: boolean; promptCount?: number | null } = {}): void {
    this.meta.title = strip(title) || null;
    this.meta.titlePinned = (options.pinned ?? false) && this.meta.title !== null;
    this.meta.titlePromptCount = options.promptCount ?? this.meta.promptCount;
    this.writeMeta();
  }

  recordUsage(usage: TurnUsage): void {
    this.usage.record(usage);
    this.pending.push({ kind: "usage", data: usageToDict(usage) });
  }

  recordCheckpoint(checkpoint: Checkpoint): void {
    this.checkpoints.push(checkpoint);
    this.pending.push({ kind: "checkpoint", data: checkpointToDict(checkpoint) });
    this.flush();
  }

  /** Record that the model has seen `path` with content `digest`. Not flushed
   * here: the tool-result message that follows flushes every record at once. */
  recordRead(path: string, digest: string | null): void {
    const read: FileRead = { index: this.messages.length, path, digest };
    this.reads.push(read);
    this.digests.set(path, digest);
    this.pending.push({ kind: "read", data: fileReadToDict(read) });
  }

  /**
   * The prompts in this session, oldest first: only things the user actually
   * typed, one row per prompt at its earliest index.
   */
  rewindPoints(): RewindPoint[] {
    const points: RewindPoint[] = [];
    const seen = new Set<string>();
    this.messages.forEach((m, index) => {
      if (!isPrompt(m)) return;
      const identity = promptIdentity(m);
      if (seen.has(identity)) return;
      seen.add(identity);
      points.push({
        index,
        text: strip(messageText(m)),
        timestamp: m.timestamp,
        compacted: m.compacted,
      });
    });
    return points;
  }

  /**
   * Drop the transcript from `index` on.
   *
   * The file keeps every line: the rewind is appended as a record, and the
   * in-memory state is then whatever replaying the file produces. Usage is
   * deliberately not rewound - those tokens were spent.
   */
  rewindTo(index: number): void {
    if (!(index >= 0 && index <= this.messages.length)) {
      throw new RangeError(`cannot rewind to ${index}: the session has ${this.messages.length}`);
    }
    this.pending.push({ kind: "rewind", data: { to: index } });
    this.flush();

    const replayed = loadSession(this.meta.sessionId);
    this.messages = replayed.messages;
    this.checkpoints = replayed.checkpoints;
    this.reads = replayed.reads;
    this.recountPrompts();
    this.indexReads();
    this.meta.messageCount = this.messages.length;
    this.meta.updatedAt = now();
    this.writeMeta();
  }

  /** Rebuild the prompt identities from the messages in hand. */
  recountPrompts(): void {
    this.promptKeys = new Set(this.messages.filter(isPrompt).map(promptIdentity));
    this.meta.promptCount = this.promptKeys.size;
  }

  /** Rebuild `readDigests` from `reads`, latest read winning. */
  indexReads(): void {
    this.digests = new Map(this.reads.map((read) => [read.path, read.digest]));
  }

  /** Messages eligible for context: not `compacted`, not `ephemeral`. */
  activeMessages(): Message[] {
    return this.messages.filter((m) => !m.compacted && !m.ephemeral);
  }

  /**
   * Append the pending records and clear them. The queue is taken in one step
   * before any I/O; anything that fails to write goes back on the front of the
   * queue, so a full disk costs the next flush a retry rather than the records.
   */
  flush(): void {
    if (this.pending.length === 0) return;
    const records = this.pending;
    this.pending = [];
    const path = sessionTranscriptFile(this.meta.sessionId);
    try {
      mkdirSync(parent(path), { recursive: true });
      appendFileSync(path, records.map((record) => `${dumps(record)}\n`).join(""));
    } catch (error) {
      this.pending.unshift(...records);
      throw error;
    }
    this.writeMeta();
  }

  writeMeta(): void {
    const path = join(sessionDir(this.meta.sessionId), "meta.json");
    mkdirSync(parent(path), { recursive: true });
    writeFileSync(path, dumps(metaToDict(this.meta), { indent: 2 }));
  }
}

/**
 * What makes two prompt records the same prompt: text and timestamp together,
 * the identity `rewindPoints` uses too, so the count and the rewind list can
 * never disagree about what a prompt is.
 */
function promptIdentity(m: Message): string {
  return `${m.timestamp}\u0000${strip(messageText(m))}`;
}

function isPrompt(m: Message): boolean {
  return (
    m.role === "user" &&
    !m.ephemeral &&
    !m.metadata.compaction_summary &&
    !m.content.some((block) => block.type === "tool_result")
  );
}

function hex8(): string {
  return randomBytes(4).toString("hex");
}

/**
 * Create a session with a fresh id and its on-disk directory.
 *
 * A subagent session nests under its parent (`<parent>/sub-<id>`), which keeps
 * its transcript alongside the work it belongs to and out of the `/resume`
 * listing.
 */
export function newSession(cwd: string, model: string, parentId: string | null = null): Session {
  const created = now();
  let sessionId = `${strftime("%Y%m%d-%H%M%S", created)}-${hex8()}`;
  if (parentId) {
    if (parentId.includes("..") || parentId.startsWith("/")) {
      throw new Error(`unsafe parent session id: ${repr(parentId)}`);
    }
    sessionId = `${parentId}/sub-${hex8()}`;
  }
  const session = new Session({
    sessionId,
    cwd: resolve(cwd),
    createdAt: created,
    updatedAt: created,
    model,
    title: null,
    titlePromptCount: 0,
    titlePinned: false,
    messageCount: 0,
    promptCount: 0,
    parentId,
  });
  mkdirSync(sessionOutputsDir(sessionId), { recursive: true });
  session.writeMeta();
  return session;
}

export class SessionNotFound extends Error {
  override name = "SessionNotFound";
}

/** A session's metadata, without replaying its transcript. */
export function readMeta(sessionId: string): SessionMeta {
  const path = join(sessionDir(sessionId), "meta.json");
  if (!exists(path)) throw new SessionNotFound(sessionId);
  return metaFromDict(JSON.parse(readText(path)));
}

/** Rehydrate from JSONL. */
export function loadSession(sessionId: string): Session {
  const path = sessionTranscriptFile(sessionId);
  const session = new Session(readMeta(sessionId));
  if (!exists(path)) return session;

  /** `[summary index, indices it superseded]` per compaction still standing.
   * A rewind past a summary has to give those messages their content back. */
  const compactions: [number, number[]][] = [];

  for (const raw of readText(path).split("\n")) {
    const line = strip(raw);
    if (!line) continue;
    let record: Record<string, unknown>;
    try {
      record = JSON.parse(line);
    } catch {
      // A torn final line from a crashed write costs one record, not the session.
      continue;
    }
    const data = record.data as Record<string, unknown>;
    switch (record.kind) {
      case "message":
        session.messages.push(fromDict(data));
        break;
      case "usage":
        session.usage.record(usageFromDict(data));
        break;
      case "checkpoint":
        session.checkpoints.push(checkpointFromDict(data));
        break;
      case "read":
        session.reads.push(fileReadFromDict(data));
        break;
      case "environment":
        session.environment = {
          systemPrompt: (data.system_prompt as string) ?? "",
          tools: (data.tools as Record<string, unknown>[]) ?? [],
          projectContext: (data.project_context as string | null) ?? null,
          skillsIndex: (data.skills_index as string | null) ?? null,
        };
        break;
      case "compaction": {
        // Compaction always supersedes a prefix of the live messages, so
        // replaying in order reproduces the same partition.
        let remaining = Number(data.count);
        const flagged: number[] = [];
        for (const [index, m] of session.messages.entries()) {
          if (remaining <= 0) break;
          if (!m.compacted) {
            m.compacted = true;
            flagged.push(index);
            remaining -= 1;
          }
        }
        // The summary is the next message appended, so it lands here.
        compactions.push([session.messages.length, flagged]);
        break;
      }
      case "rewind":
        rewind(session, Number(data.to), compactions);
        break;
    }
  }
  session.meta.messageCount = session.messages.length;
  session.recountPrompts();
  session.indexReads();
  return session;
}

/**
 * Apply one rewind record while replaying. Any compaction whose summary is
 * being cut away is undone with it: the messages it replaced are all that is
 * left to represent those turns.
 */
function rewind(session: Session, to: number, compactions: [number, number[]][]): void {
  session.messages.splice(to);
  session.checkpoints = session.checkpoints.filter((c) => c.index < to);
  session.reads = session.reads.filter((r) => r.index < to);
  while (compactions.length && (compactions.at(-1) as [number, number[]])[0] >= to) {
    const [, flagged] = compactions.pop() as [number, number[]];
    for (const index of flagged) {
      const m = session.messages[index];
      if (m !== undefined) m.compacted = false;
    }
  }
}

/**
 * Most-recent-first. Filtered to `cwd` when given.
 *
 * `limit` of `null` returns every session. `backfill` caps how many of the
 * returned rows get their prompt count filled in. Sessions that never recorded
 * a message are skipped: they are the residue of a launch that went nowhere.
 */
export function listSessions(
  cwd: string | null = null,
  limit: number | null = 20,
  backfill: number | null = null,
): SessionMeta[] {
  const root = sessionsDir();
  if (!isDir(root)) return [];

  const here = cwd === null ? null : resolve(cwd);
  const metas: SessionMeta[] = [];
  for (const { name: entry } of entries(root)) {
    const metaPath = join(root, entry, "meta.json");
    if (!isFile(metaPath)) continue;
    let meta: SessionMeta;
    try {
      meta = metaFromDict(JSON.parse(readText(metaPath)));
    } catch {
      continue;
    }
    if (here !== null && meta.cwd !== here) continue;
    if (meta.messageCount <= 0) continue;
    metas.push(meta);
  }

  metas.sort((a, b) => b.updatedAt - a.updatedAt);
  // Backfilled after the cut, never before it: the backfill replays a
  // transcript, and doing that for every session on the machine to render a
  // list of five is a stall the user waits through.
  const kept = limit === null ? metas : metas.slice(0, limit);
  const fill = backfill ?? kept.length;
  return kept.map((meta, index) => (index < fill ? backfilled(meta) : meta));
}

/** Largest transcript worth replaying just to count its prompts. */
export const BACKFILL_MAX_BYTES = 4_000_000;

/** Fill in `promptCount` for a session recorded before it existed, and write
 * the answer back so a given session is replayed for this at most once. */
function backfilled(meta: SessionMeta): SessionMeta {
  if (meta.promptCount || meta.messageCount <= 0) return meta;
  try {
    if (statSync(sessionTranscriptFile(meta.sessionId)).size > BACKFILL_MAX_BYTES) return meta;
  } catch {
    return meta;
  }
  let counted: Session;
  try {
    counted = loadSession(meta.sessionId);
  } catch {
    return meta;
  }
  meta.promptCount = counted.meta.promptCount;
  try {
    counted.writeMeta();
  } catch {
    // Best effort: counted again next time.
  }
  return meta;
}

/** How many rows of the `/resume` list get their prompt count filled in. */
export const RESUME_BACKFILL = 50;

/**
 * Everything `/resume` can offer: the sessions recorded in this directory,
 * not its parents or children, newest first. `exclude` is the session
 * already open.
 */
export function resumableSessions(cwd: string, exclude: string | null = null): SessionMeta[] {
  const candidates = listSessions(cwd, null, 0).filter((meta) => meta.sessionId !== exclude);
  return candidates.map((meta, index) => (index < RESUME_BACKFILL ? backfilled(meta) : meta));
}

export function latestSession(cwd: string): SessionMeta | null {
  return listSessions(cwd, 1)[0] ?? null;
}

/**
 * The two lines at the bottom, and the one above them.
 *
 * Everything here is low contrast on purpose: reference material that should
 * be readable when looked at and invisible when not. The one exception is the
 * context gauge, which changes colour as the next turn nears compaction.
 */

import {
  formatPercent,
  formatRenewal,
  formatTokens,
  PlanWindow,
  RENEWAL_CLOCK_SECONDS,
} from "../../core/usage.ts";
import { compareCodePoints } from "../../json.ts";
import { keymap } from "../../keys.ts";
import { formatFixed } from "../../py.ts";
import { fillLine, wrap } from "../../term/ansi.ts";
import { Widget } from "../../term/component.ts";
import { Lines } from "../../term/primitives.ts";
import { cellWidth, truncateToWidth } from "../../term/width.ts";
import { columns, meterFill } from "../format.ts";
import { ELLIPSIS, METER_EMPTY, METER_FULL, SEPARATOR } from "../glyphs.ts";
import { fg } from "../paint.ts";

/** Minimum space between the two halves of a line. */
export const GAP = 2;
/** Below this the left half is not worth keeping, and the right half wins. */
export const MIN_LEFT = 12;

const WARN_FRACTION = 0.75;
const DANGER_FRACTION = 0.9;

/** Width of the context gauge: coarse, on purpose. */
const METER_CELLS = 6;
/** Below this the gauge is dropped, and the counts keep the columns. */
const METER_MIN_WIDTH = 60;

const MODE_ROLES: Record<string, string> = {
  plan: "border",
  default: "dim",
  acceptEdits: "warning",
  bypass: "error",
};

const now = () => Date.now() / 1000;

/**
 * Left half flush left, right half flush right; the left half yields first.
 *
 * `tail` ends the left half and is cut only after `left` is gone: a transient
 * count such as the queue depth must not be the first thing a long path
 * pushes off the line.
 */
export function justify(left: string, right: string, width: number, tail = ""): string {
  const rightWidth = cellWidth(right);
  const keepRight = Boolean(rightWidth) && width - rightWidth - GAP >= MIN_LEFT;
  const room = keepRight ? width - rightWidth - GAP : width;
  let out: string;
  if (cellWidth(left) + cellWidth(tail) > room) {
    const roomForLeft = room - cellWidth(tail) - cellWidth(ELLIPSIS);
    out =
      roomForLeft > 0
        ? truncateToWidth(left, roomForLeft) + ELLIPSIS + tail
        : truncateToWidth(left + tail, Math.max(0, room - cellWidth(ELLIPSIS))) + ELLIPSIS;
  } else out = left + tail;
  if (!keepRight) return out;
  return out + " ".repeat(Math.max(0, width - cellWidth(out) - rightWidth)) + right;
}

/** `path` cut from its start, at a separator, until it fits `width`: the end
 * of a path is the part that names the project. */
export function fitPath(path: string, width: number): string {
  if (cellWidth(path) <= width) return path;
  const parts = path.split("/");
  for (let start = 1; start < parts.length; start++) {
    const candidate = `${ELLIPSIS}/${parts.slice(start).join("/")}`;
    if (cellWidth(candidate) <= width) return candidate;
  }
  // Even the last name alone is too long; it keeps its end, like the rest.
  let name = Array.from(parts.at(-1) as string);
  while (name.length && cellWidth(ELLIPSIS + name.join("")) > width) name = name.slice(1);
  return name.length ? ELLIPSIS + name.join("") : "";
}

/** A `cells`-wide gauge: the filled run in `role`, the rest dim. */
export function meter(fraction: number, cells: number, role: string): string {
  const filled = meterFill(fraction, cells);
  return fg(role, METER_FULL.repeat(filled)) + fg("dim", METER_EMPTY.repeat(cells - filled));
}

export function formatCost(usd: number): string {
  return usd < 0.01 ? `$${formatFixed(usd, 4)}` : `$${formatFixed(usd, 2)}`;
}

/** Python's ordering of a window's `(minutes, scope)` key. */
const byKey = (a: PlanWindow, b: PlanWindow) =>
  a.minutes - b.minutes || compareCodePoints(a.scope, b.scope);

export interface StatusFields {
  cwd: string;
  branch: string | null;
  queued: number;
  model: string;
  subscription: boolean;
  route: string;
  effort: string | null;
  mode: string;
  sandboxActive: boolean;
  sandboxBackend: string;
  inputTokens: number;
  outputTokens: number;
  cacheRead: number;
  cacheWrite: number;
  cacheHitRate: number;
  costUsd: number;
  latencyMs: number;
  contextUsed: number;
  contextWindow: number;
  autoCompact: boolean;
}

/** Two docked lines: where you are, and what this is costing. */
export class StatusBar extends Widget implements StatusFields {
  cwd = "";
  branch: string | null = null;
  queued = 0;
  model = "";
  subscription = false;
  route = "";
  /** Each subscription route's usage windows, as last reported. Kept per
   * route, across model switches and /clear: they are the account's. */
  readonly plans = new Map<string, Map<string, PlanWindow>>();
  private redrawAt: number | null = null;
  effort: string | null = null;
  mode = "default";
  sandboxActive = true;
  sandboxBackend = "";
  inputTokens = 0;
  outputTokens = 0;
  cacheRead = 0;
  cacheWrite = 0;
  cacheHitRate = 0;
  costUsd = 0;
  latencyMs = 0;
  contextUsed = 0;
  contextWindow = 0;
  autoCompact = true;

  update(fields: Partial<StatusFields>): void {
    Object.assign(this, fields);
    this.invalidate();
  }

  setLocation(cwd: string, branch: string | null): void {
    this.update({ cwd, branch });
  }

  setModel(modelId: string, options: { subscription?: boolean; route?: string } = {}): void {
    this.update({
      model: modelId,
      subscription: options.subscription ?? false,
      route: options.route ?? "",
    });
  }

  /**
   * Take a route's report of its windows over what it said before, window by
   * window. A report without a figure does not undo one for the same window
   * and renewal.
   */
  setPlan(route: string, windows: readonly PlanWindow[]): void {
    let known = this.plans.get(route);
    if (known === undefined) {
      known = new Map();
      this.plans.set(route, known);
    }
    for (let window of windows) {
      const before = known.get(window.key);
      if (
        window.usedPercent === null &&
        before !== undefined &&
        before.usedPercent !== null &&
        before.resetsAt === window.resetsAt
      )
        window = new PlanWindow(window.minutes, before.usedPercent, window.resetsAt, window.scope);
      known.set(window.key, window);
    }
    this.invalidate();
  }

  /** The shown route's windows as they stand now, shortest first. */
  planWindows(at?: number): PlanWindow[] {
    if (!this.subscription) return [];
    const current = [...(this.plans.get(this.route)?.values() ?? [])].map((w) => w.at(at));
    return current.filter((w): w is PlanWindow => w !== null).sort(byKey);
  }

  /** Whether a window shown on the last frame has reset since, or come within
   * a day of it. Nothing else redraws an idle screen. */
  redrawDue(at: number = now()): boolean {
    return this.redrawAt !== null && at >= this.redrawAt;
  }

  setContext(used: number, window: number): void {
    this.update({ contextUsed: used, contextWindow: window });
  }

  setTokens(inputTokens: number, outputTokens: number): void {
    this.update({ inputTokens, outputTokens });
  }

  setCache(readTokens: number, writeTokens: number, hitRate: number): void {
    this.update({ cacheRead: readTokens, cacheWrite: writeTokens, cacheHitRate: hitRate });
  }

  /** Reasoning depth, resolved per model. */
  setEffort(effort: string | null): void {
    this.update({ effort });
  }

  setCost(costUsd: number): void {
    this.update({ costUsd });
  }

  setQueued(count: number): void {
    this.update({ queued: count });
  }

  setMode(mode: string, sandboxActive: boolean, backend = ""): void {
    this.update({ mode, sandboxActive, sandboxBackend: backend });
  }

  get contextFraction(): number {
    return this.contextWindow ? this.contextUsed / this.contextWindow : 0;
  }

  draw(width: number): string[] {
    const inner = Math.max(1, width - 2);
    const mode = this.modeField();
    const queue = this.queueField();
    const keepsMode = inner - cellWidth(mode) - GAP >= MIN_LEFT;
    const room = inner - cellWidth(queue) - (keepsMode ? cellWidth(mode) + GAP : 0);
    const model = this.modelField();
    const keepsModel = inner - cellWidth(model) - GAP >= MIN_LEFT;
    const statsRoom = inner - (keepsModel ? cellWidth(model) + GAP : 0);
    const windows = this.planWindows();
    const at = now();
    const changes = windows
      .filter((w) => w.resetsAt !== null)
      .flatMap((w) => [(w.resetsAt as number) - RENEWAL_CLOCK_SECONDS, w.resetsAt as number])
      .filter((moment) => moment > at);
    this.redrawAt = changes.length ? Math.min(...changes) : null;
    const lines = [fillLine(` ${justify(this.location(room), mode, inner, queue)}`, width)];
    if (windows.length) lines.push(fillLine(` ${this.plan(windows, inner)}`, width));
    lines.push(
      fillLine(
        ` ${justify(this.stats(inner, statsRoom, windows.length > 0), model, inner)}`,
        width,
      ),
    );
    return lines;
  }

  /** The project path and its branch, the path shortened first; when even
   * the name and the branch do not fit side by side, the branch goes. */
  private location(room: number): string {
    const cwd = this.cwd || "";
    let branch = this.branch ? ` (${this.branch})` : "";
    const name = cwd.slice(cwd.lastIndexOf("/") + 1);
    const shortest = Math.min(cellWidth(cwd), cellWidth(`${ELLIPSIS}/${name}`));
    if (branch && room - cellWidth(branch) < shortest) branch = "";
    return fg("dim", fitPath(cwd, Math.max(0, room - cellWidth(branch))) + branch);
  }

  private queueField(): string {
    return this.queued ? fg("muted", `  ⧗${this.queued} queued`) : "";
  }

  private modeField(): string {
    let field = fg(MODE_ROLES[this.mode] ?? "dim", this.mode);
    if (!this.sandboxActive) field += fg("warning", " · no-sandbox");
    else if (this.sandboxBackend && this.sandboxBackend !== "none")
      field += fg("dim", ` · sandbox ${this.sandboxBackend}`);
    return field;
  }

  /** What this is costing, in fields dropped whole to fit `room`: the counts
   * go first, then the cache, and a field is never cut mid-number. */
  private stats(width: number, room: number, hasPlan: boolean): string {
    // [priority, field]: the lowest number is the last to go.
    const fields: [number, string][] = [];
    if (this.inputTokens) fields.push([4, fg("dim", `↑${formatTokens(this.inputTokens)}`)]);
    if (this.outputTokens) fields.push([5, fg("dim", `↓${formatTokens(this.outputTokens)}`)]);
    fields.push([3, this.cacheField()]);
    // A subscription turn has no per-token price, so "$0.00" would be a claim
    // about spend rather than the absence of one.
    if (!this.subscription) fields.push([2, fg("muted", formatCost(this.costUsd))]);
    else if (!hasPlan) fields.push([2, fg("muted", "sub")]);
    if (this.latencyMs >= 100)
      fields.push([6, fg("dim", `${formatFixed(this.latencyMs / 1000, 1)}s`)]);
    fields.push([0, this.context(width)]);

    const joined = () => fields.map(([, field]) => field).join(" ");
    while (fields.length > 1 && cellWidth(joined()) > room) {
      let worst = 0;
      fields.forEach(([priority], index) => {
        if (priority > (fields[worst] as [number, string])[0]) worst = index;
      });
      fields.splice(worst, 1);
    }
    return joined();
  }

  /**
   * `5h 16% (renews 06:30 am) | 7d 14% (renews Mon Oct 26 04:00 am)`. Too
   * narrow for all of it, the line gives up the least first: the clock of a
   * renewal days away, then the renewals, then every window but the fullest.
   */
  private plan(windows: PlanWindow[], width: number): string {
    const at = now();
    let fullest = windows[0] as PlanWindow;
    for (const window of windows.slice(1)) {
      const [a, b] = [used(window), used(fullest)];
      if (a > b || (a === b && -window.minutes > -fullest.minutes)) fullest = window;
    }
    let line = "";
    for (let level = 0; level < 4; level++) {
      const shown = level === 3 ? [fullest] : windows;
      line = shown.map((w) => windowField(w, level, at)).join(fg("dim", " | "));
      if (cellWidth(line) <= width) return line;
    }
    return truncateToWidth(line, width);
  }

  /** Cache tokens read and written, plus the hit rate. */
  private cacheField(): string {
    if (!this.cacheRead && !this.cacheWrite) return fg("dim", "no cache");
    const role = this.cacheHitRate >= 0.5 ? "success" : "warning";
    return (
      fg(role, `R${formatTokens(this.cacheRead)}`) +
      fg("dim", ` W${formatTokens(this.cacheWrite)}`) +
      fg(role, ` CH${formatFixed(this.cacheHitRate * 100, 0)}%`)
    );
  }

  /** `▰▰▱▱▱▱ 24k/200k` - a gauge, then what it is a gauge of. */
  private context(width: number): string {
    const fraction = this.contextFraction;
    let role = "success";
    if (fraction >= DANGER_FRACTION) role = "error";
    else if (fraction >= WARN_FRACTION) role = "warning";
    const window = this.contextWindow ? formatTokens(this.contextWindow) : "?";
    let field = fg(role, `${formatTokens(this.contextUsed)}/${window}`);
    if (width >= METER_MIN_WIDTH) field = `${meter(fraction, METER_CELLS, role)} ${field}`;
    if (this.autoCompact) field += fg("dim", " (auto)");
    return field;
  }

  /** `gpt-5.6-terra (sub)` - the bare name, plus the route when it is not
   * the default one. */
  private modelField(): string {
    if (!this.model) return fg("muted", "no model");
    let field = fg("muted", this.model.slice(this.model.lastIndexOf("/") + 1));
    if (this.subscription) field += fg("success", " (sub)");
    if (this.effort) field += fg("dim", `${SEPARATOR}${this.effort}`);
    return field;
  }
}

/** One window at a level of detail: 0 is everything, 1 drops the clock from
 * a renewal days away, 2 and up drop renewals - except for a window without a
 * figure, whose renewal is all it has to say. */
function windowField(window: PlanWindow, level: number, at: number): string {
  let renewal = "";
  if (window.resetsAt !== null) {
    const when = formatRenewal(window.resetsAt, { short: level >= 1, now: at });
    if (level === 0) renewal = ` (renews ${when})`;
    else if (level === 1 || window.usedPercent === null) renewal = ` (${when})`;
  }
  if (window.usedPercent === null) return fg("muted", window.label) + fg("dim", renewal);
  let role = "muted";
  if (window.usedPercent >= DANGER_FRACTION * 100) role = "error";
  else if (window.usedPercent >= WARN_FRACTION * 100) role = "warning";
  return fg(role, `${window.label} ${formatPercent(window.usedPercent)}`) + fg("dim", renewal);
}

/** How spent a window is, for choosing between them: one with no figure
 * loses to one with any. */
const used = (window: PlanWindow) => window.usedPercent ?? -1;

/** One dim line of shortcuts under the prompt, dropped whole rather than
 * clipped mid-word. */
export class HintsBar extends Widget {
  private hints: [string, string][] = [];

  setHints(hints: [string, string][]): void {
    this.hints = hints;
    this.invalidate();
  }

  draw(width: number): string[] {
    const room = Math.max(1, width - 2);
    const parts: string[] = [];
    let usedCells = 0;
    for (const [key, description] of this.hints) {
      const rendered = fg("dim", key) + fg("muted", ` ${description}`);
      const step = cellWidth(rendered) + (parts.length ? cellWidth(SEPARATOR) : 0);
      if (usedCells + step > room) break;
      parts.push(rendered);
      usedCells += step;
    }
    return [fillLine(` ${parts.join(fg("dim", SEPARATOR))}`, width)];
  }
}

/** The block printed once, at the top of the session. */
export class Header extends Widget {
  private versionText: string;
  private isExpanded = false;

  constructor(
    version = "",
    public quiet = false,
  ) {
    super();
    this.versionText = version;
  }

  get version(): string {
    return this.versionText;
  }

  set version(value: string) {
    this.versionText = value;
    this.invalidate();
  }

  get expanded(): boolean {
    return this.isExpanded;
  }

  set expanded(value: boolean) {
    this.isExpanded = value;
    this.invalidate();
  }

  toggle(): void {
    this.expanded = !this.isExpanded;
  }

  draw(width: number): string[] {
    if (this.quiet) return [];
    const keys = keymap();
    const lines = [fg("accent", "hx", { bold: true }) + fg("dim", ` v${this.versionText}`)];
    if (!this.isExpanded) {
      // Prose, so wrapped between words on a narrow terminal.
      const inner = Math.max(1, width - 2);
      lines.push(
        ...wrap(fg("muted", "An agent harness. Ask a question, or start with /help."), inner),
      );
      lines.push(...wrap(fg("dim", `${keys.primary("app.tools.expand")} shows every key.`), inner));
    } else {
      const rows = [...keys.bindings.values()]
        // An action with no key has nothing to show but a blank.
        .filter((binding) => keys.keysFor(binding.id).length)
        .map((binding) => [fg("dim", keys.text(binding.id)), fg("muted", binding.description)]);
      lines.push(...columns(rows));
      lines.push(fg("dim", "/ commands  ! bash  @ files"));
    }
    return new Lines(lines).render(width);
  }
}

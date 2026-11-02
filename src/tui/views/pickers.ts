/**
 * Filtered lists: slash commands, models, effort, sessions, rewind points.
 *
 * All of them are the same object - a title, a line to type on, and a list -
 * so they are all the same shape. Rows are laid out by measuring the data
 * rather than padding to a guessed width.
 */

import type { RewindPoint, SessionMeta } from "../../core/session.ts";
import { formatTokens } from "../../core/usage.ts";
import { describe } from "../../net.ts";
import { resolveEffort } from "../../providers/codex_catalogue.ts";
import { type ModelInfo, matchModels } from "../../providers/models.ts";
import { formatFixed, lstrip, strftime, strip } from "../../py.ts";
import { inverse } from "../../term/ansi.ts";
import type { Component } from "../../term/component.ts";
import { Lines, Rule, Spacer, Text } from "../../term/primitives.ts";
import { CURSOR_MARKER } from "../../term/screen.ts";
import { count } from "../../text.ts";
import { columns, oneLine } from "../format.ts";
import { filterItems } from "../fuzzy.ts";
import { CURRENT, CURSOR, GUTTER } from "../glyphs.ts";
import { LIST_MINIMUM, LIST_VISIBLE, RECORD_WIDTH } from "../limits.ts";
import { fg, rule } from "../paint.ts";
import { Framed, type Hint, hint, hintsLine } from "./dialog.ts";

export type Row = [string, string[]];

/**
 * A filter box over a list. Typing narrows, Enter picks, Escape cancels.
 *
 * The visible window is centred on the selection, so the row Enter will take
 * is always on screen. A subclass sets its own fields and then calls `open`.
 */
export abstract class Picker extends Framed {
  title = "";
  placeholder = "Filter…";
  /** Lines under the list, about the list: why something is missing from it. */
  notes: readonly string[] = [];
  /** `[role, text]` under the notes: what the last thing asked of the picker
   * itself came to. One line, replaced rather than stacked. */
  status: [string, string] | null = null;

  /** Lines the frame spends on itself: two rules, four blank lines, the
   * title, the filter line and the hint line. */
  static readonly CHROME = 9;

  query = "";
  selected = 0;
  result: string | null = null;
  done = false;
  protected rowList: Row[] = [];
  /** Rows `notes` took at the last draw, wrapped to its width. */
  private notesHeight = 0;
  /** Rows the overlay may use, supplied by whoever put this on screen. */
  private rowsAvailable: (() => number) | null = null;

  /** Fill the list and open on what is already in force, so Enter alone
   * keeps it and the arrow and the ✓ do not point at different rows. */
  protected open(initial = ""): void {
    // Pre-filled so a query typed as `/model opus` stays visible and editable.
    this.query = initial;
    this.refresh();
    const current = this.currentValue();
    const index = this.rowList.findIndex(([value]) => value === current);
    this.selected = Math.max(0, index);
  }

  /** How many rows the overlay has; called at draw time, so a resize is
   * picked up without re-opening anything. */
  setRowsAvailable(rows: () => number): void {
    this.rowsAvailable = rows;
    this.invalidate();
  }

  /** How many list rows fit, given the room and what the frame costs. */
  visibleRows(): number {
    if (this.rowsAvailable === null) return LIST_VISIBLE;
    let room: number;
    try {
      room = Math.trunc(this.rowsAvailable());
    } catch {
      return LIST_VISIBLE;
    }
    let chrome = this.docked ? Picker.CHROME - 2 : Picker.CHROME;
    // The (n/total) counter's line is always reserved rather than resolved,
    // which would not terminate.
    if (this.rowList.length > 1) chrome += 1;
    return Math.max(LIST_MINIMUM, room - chrome - this.notesHeight);
  }

  /** `[value, cells]` pairs matching `query`. Cells become columns. */
  abstract rows(query: string): Row[];

  /** The value already in force, marked separately from the selection. */
  currentValue(): string | null {
    return null;
  }

  protected refresh(): void {
    this.rowList = this.rows(this.query);
    this.selected = Math.min(this.selected, Math.max(0, this.rowList.length - 1));
    this.invalidate();
  }

  draw(width: number): string[] {
    const laidOut = columns(this.rowList.map(([, cells]) => cells));
    const current = this.currentValue();
    const notes: Component[] = this.notes.map((note) => new Text(fg("warning", note), 1, 0));
    if (this.status !== null) notes.push(new Text(fg(this.status[0], this.status[1]), 1, 0));
    if (notes.length) notes.unshift(new Spacer(1));
    // Measured before the window is chosen: a note wrapped to three lines
    // costs the list three rows, or the frame runs off the bottom.
    this.notesHeight = notes.reduce((sum, part) => sum + part.render(width).length, 0);

    const body: string[] = [];
    for (const index of this.window()) {
      const [value] = this.rowList[index] as Row;
      const chosen = index === this.selected;
      const cursor = chosen ? fg("accent", CURSOR) : GUTTER;
      const marker = value === current ? fg("accent", CURRENT) : GUTTER;
      body.push(cursor + marker + laidOut[index]);
    }
    if (!this.rowList.length)
      // "no matches" over an empty filter blames a query nobody typed.
      body.push(fg("muted", this.query ? "no matches" : "nothing to list"));
    else if (this.rowList.length > this.visibleRows())
      body.push(fg("muted", `(${this.selected + 1}/${this.rowList.length})`));
    // The list keeps all the room it is given, however short it is, so a
    // picker is always a whole page and does not shrink as a filter narrows it.
    if (this.rowsAvailable !== null) {
      const room = this.visibleRows() + (this.rowList.length > 1 ? 1 : 0);
      while (body.length < room) body.push("");
    }

    // An empty filter shows a hint, with the cursor before it.
    const filterLine = this.query
      ? fg("text", this.query) + cursorCell()
      : cursorCell() + fg("dim", this.placeholder);
    const parts: Component[] = [
      new Rule(rule("border")),
      new Spacer(1),
      new Text(fg("accent", this.title, { bold: true }), 1, 0),
      new Text(filterLine, 1, 0),
      new Spacer(1),
      new Lines(body),
      ...notes,
      new Spacer(1),
      new Text(hintsLine(this.hints()), 1, 0),
      ...this.closing(),
    ];
    return parts.flatMap((part) => part.render(width));
  }

  /** The keys this picker answers to, for the line under it. */
  hints(): Hint[] {
    return [hint("↑↓", "move"), hint("enter", "select"), hint("esc", "cancel")];
  }

  /** The visible slice, centred on the selection where it can be. */
  private window(): number[] {
    const total = this.rowList.length;
    const visible = this.visibleRows();
    const range = (start: number, stop: number) =>
      Array.from({ length: stop - start }, (_, i) => start + i);
    if (total <= visible) return range(0, total);
    const half = Math.floor(visible / 2);
    const start = Math.max(0, Math.min(this.selected - half, total - visible));
    return range(start, start + visible);
  }

  handleInput(key: string, data: string): boolean {
    if (this.done) return false;
    if (key === "text") {
      this.query += data;
      this.selected = 0;
      this.refresh();
      return true;
    }
    if (key === "backspace") {
      this.query = Array.from(this.query).slice(0, -1).join("");
      this.selected = 0;
      this.refresh();
      return true;
    }
    if ((key === "up" || key === "down") && this.rowList.length) {
      const length = this.rowList.length;
      this.selected = (((this.selected + (key === "down" ? 1 : -1)) % length) + length) % length;
      this.invalidate();
      return true;
    }
    if ((key === "pageup" || key === "pagedown") && this.rowList.length) {
      const visible = this.visibleRows();
      const step = key === "pagedown" ? visible : -visible;
      this.selected = Math.max(0, Math.min(this.rowList.length - 1, this.selected + step));
      this.invalidate();
      return true;
    }
    if (key === "enter") {
      this.result = this.rowList.length ? (this.rowList[this.selected] as Row)[0] : null;
      this.done = true;
      return true;
    }
    if (key === "escape") {
      this.result = null;
      this.done = true;
      return true;
    }
    return false;
  }
}

const cursorCell = () => CURSOR_MARKER + inverse(" ");

export interface PaletteCommand {
  readonly name: string;
  readonly summary: string;
}

export class CommandPalette extends Picker {
  override title = "Commands";
  override placeholder = "Filter commands…";

  constructor(
    readonly commands: readonly PaletteCommand[],
    initial = "",
  ) {
    super();
    this.open(initial);
  }

  rows(query: string): Row[] {
    // Fuzzy over name *and* summary: in a palette the user is often looking
    // for a capability ("cost") rather than a name they already know.
    return filterItems(
      this.commands,
      lstrip(strip(query), "/"),
      (command) => `${command.name} ${command.summary}`,
    ).map((command) => [
      command.name,
      [fg("text", `/${command.name}`), fg("muted", command.summary)],
    ]);
  }
}

/** Fetches the catalogue again: the models to list, and the notes under them. */
export type Reload = () => Promise<[ModelInfo[], string[]]>;

/**
 * Model chooser: id, context window, what pays for it, prompt caching and
 * image support. ctrl+r fetches the catalogue again in place.
 */
export class ModelPicker extends Picker {
  override title = "Select model";
  override placeholder = "Filter models…";
  static readonly LIMIT = 200;
  private reloading = false;
  /** Asks for a frame. A refresh lands between keystrokes. */
  private redraw: () => void = () => {};

  constructor(
    public models: ModelInfo[],
    readonly current: string,
    initial = "",
    notes: readonly string[] | null = null,
    private readonly reload: Reload | null = null,
  ) {
    super();
    this.notes = [...(notes ?? [])];
    this.open(initial);
  }

  setRedraw(redraw: () => void): void {
    this.redraw = redraw;
  }

  override currentValue(): string | null {
    return this.current;
  }

  override hints(): Hint[] {
    const hints = super.hints();
    if (this.reload !== null) hints.splice(2, 0, hint("ctrl+r", "refresh"));
    return hints;
  }

  override handleInput(key: string, data: string): boolean {
    if (key === "ctrl+r" && this.reload !== null && !this.done) {
      this.startReload();
      return true;
    }
    return super.handleInput(key, data);
  }

  private startReload(): void {
    // One at a time: a second press would only ask the network again.
    if (this.reloading) return;
    this.reloading = true;
    this.status = ["muted", "Refreshing the catalogue…"];
    this.invalidate();
    void this.reloadNow().finally(() => {
      this.reloading = false;
    });
  }

  private async reloadNow(): Promise<void> {
    try {
      const [models, notes] = await (this.reload as Reload)();
      // Kept on the model it was on rather than the row number, or else on
      // the model in force.
      const selected = this.rowList.length ? (this.rowList[this.selected] as Row)[0] : null;
      this.models = models;
      this.notes = [...notes];
      this.rowList = this.rows(this.query);
      const values = this.rowList.map(([value]) => value);
      const kept = [selected, this.current].find(
        (value) => value !== null && values.includes(value),
      );
      this.selected =
        kept !== undefined && kept !== null
          ? values.indexOf(kept)
          : Math.min(this.selected, Math.max(0, values.length - 1));
      this.status = ["success", `Refreshed ${count(models.length, "model")}.`];
    } catch (error) {
      // The list already on screen stays.
      this.status = ["error", `Refresh failed: ${describe(error)}`];
    }
    this.invalidate();
    this.redraw();
  }

  rows(query: string): Row[] {
    // The same matcher `/model <query>` uses.
    const matches = matchModels(this.models, query);
    const shown = matches.slice(0, ModelPicker.LIMIT);
    // The model in force stays listed however far down it sorts.
    shown.push(...matches.slice(ModelPicker.LIMIT).filter((model) => model.id === this.current));
    return shown.map((model) => [model.id, this.cells(model)]);
  }

  private cells(model: ModelInfo): string[] {
    const mode = String(model.cacheMode);
    const cache = (
      { explicit: "cache✓", implicit: "cache~", none: "cache✗" } as Record<string, string>
    )[mode] as string;
    const cacheRole = (
      { explicit: "success", implicit: "warning", none: "dim" } as Record<string, string>
    )[mode] as string;
    return [
      fg("text", model.id, { bold: model.id === this.current }),
      fg("muted", formatTokens(model.contextWindow)),
      this.billing(model),
      fg(cacheRole, cache),
      // Blank rather than "no", so the yeses stand out.
      model.supportsImages ? fg("muted", "images") : "",
    ];
  }

  /** What pays for this model. A subscription has no per-token price. */
  private billing(model: ModelInfo): string {
    if (model.isSubscription) return fg("success", "subscription");
    const prompt = model.pricing.prompt * 1_000_000;
    const completion = model.pricing.completion * 1_000_000;
    return fg("dim", `$${formatFixed(prompt, 2)}/$${formatFixed(completion, 2)}`);
  }
}

/** Value of the row that clears the setting. Never a level name - `none` is
 * a real effort, so it cannot double as "unset". */
export const DEFAULT_EFFORT_ROW = "default";

/** Reasoning-depth chooser for the model in force, offering only the levels
 * that model advertises. */
export class EffortPicker extends Picker {
  override placeholder = "Filter levels…";

  constructor(
    readonly info: ModelInfo,
    readonly current: string | null,
  ) {
    super();
    this.title = `Reasoning effort for ${info.id.slice(info.id.lastIndexOf("/") + 1)}`;
    this.open();
  }

  override currentValue(): string | null {
    if (this.current === null) return DEFAULT_EFFORT_ROW;
    if (this.info.reasoningLevels.includes(this.current)) return this.current;
    // Clamped for this model: the level it actually runs at is in force.
    return resolveEffort(this.current, this.info);
  }

  rows(query: string): Row[] {
    const defaultLevel = this.info.defaultReasoningLevel || "the model's default";
    const options: [string, string][] = [
      [DEFAULT_EFFORT_ROW, `leave it to the model - runs at ${defaultLevel}`],
      ...this.info.reasoningLevels.map((level): [string, string] => [level, ""]),
    ];
    const needle = strip(query).toLowerCase();
    const rows: Row[] = [];
    for (const [value, given] of options) {
      if (needle && !value.includes(needle)) continue;
      const note =
        !given && value === this.info.defaultReasoningLevel ? "this model's default" : given;
      rows.push([value, [fg("text", value), fg("muted", note)]]);
    }
    return rows;
  }
}

/** Resume a previous session from this directory. */
export class SessionPicker extends Picker {
  override title = "Resume session";
  override placeholder = "Filter sessions…";

  constructor(readonly sessions: readonly SessionMeta[]) {
    super();
    this.open();
  }

  rows(query: string): Row[] {
    const plain = (meta: SessionMeta) =>
      `${when(meta.updatedAt)} ${size(meta)} ${meta.title || meta.sessionId}`;
    return filterItems(this.sessions, query, plain).map((meta) => [
      meta.sessionId,
      [
        fg("dim", when(meta.updatedAt)),
        fg("muted", size(meta)),
        // A session title is written by the model.
        fg("text", oneLine(meta.title || meta.sessionId, RECORD_WIDTH)),
      ],
    ]);
  }
}

/** Pick the prompt to take the session back to. Newest first. */
export class RewindPicker extends Picker {
  override title = "Rewind to";
  override placeholder = "Filter prompts…";
  readonly points: RewindPoint[];

  constructor(points: readonly RewindPoint[]) {
    super();
    this.points = [...points].reverse();
    this.open();
  }

  rows(query: string): Row[] {
    const plain = (point: RewindPoint) => `${oneLine(point.text, RECORD_WIDTH)} ${point.index}`;
    return filterItems(this.points, query, plain).map((point) => {
      const cells = [
        fg("dim", strftime("%H:%M", point.timestamp)),
        fg("text", oneLine(point.text, RECORD_WIDTH) || "(empty prompt)"),
      ];
      if (point.compacted) cells.push(fg("muted", "compacted"));
      return [String(point.index), cells];
    });
  }
}

const when = (timestamp: number) => strftime("%Y-%m-%d %H:%M", timestamp);

/** How big a session is, led by the number the user recognises: prompts. */
function size(meta: SessionMeta): string {
  const prompts = meta.promptCount || 0;
  if (!prompts) return `${meta.messageCount} msgs`;
  return `${count(prompts, "prompt")} · ${meta.messageCount} msgs`;
}

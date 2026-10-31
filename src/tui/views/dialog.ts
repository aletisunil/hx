/**
 * The shape every dialog in HX has, taken from pi: a rule above and below,
 * never a rectangle; a title, a subtitle, a body, options and hints.
 *
 * Options carry two markers in a two-column gutter: where Enter will land,
 * and what is already in force.
 */

import { type Component, Widget } from "../../term/component.ts";
import { Lines, Rule, Spacer, Text } from "../../term/primitives.ts";
import { CURRENT, CURSOR, GUTTER } from "../glyphs.ts";
import { fg, rule } from "../paint.ts";

/** One row of a dialog's list. */
export interface Option {
  /** A single key that answers immediately, for a dialog that offers one. */
  readonly key: string;
  readonly label: string;
  /** Consequence worth stating - what this option will actually do. */
  readonly note: string;
  readonly value: unknown;
  /** Already in force. Marked separately from being selected. */
  readonly current: boolean;
}

export function option(fields: Partial<Option>): Option {
  return { key: "", label: "", note: "", value: null, current: false, ...fields };
}

export interface Hint {
  readonly key: string;
  readonly description: string;
}

export const hint = (key: string, description: string): Hint => ({ key, description });

/** `esc deny  enter confirm` - dim key, muted description, two spaces. */
export function hintsLine(hints: readonly Hint[]): string {
  return hints.map((h) => fg("dim", h.key) + fg("muted", ` ${h.description}`)).join("  ");
}

/**
 * The rule above, the rule below, and the one case that drops the lower one:
 * directly above the dock, whose own rule closes the frame, so the seam does
 * not draw rule, blank, rule.
 */
export abstract class Framed extends Widget {
  /** Drawn directly above the dock, which supplies the closing rule. */
  docked = false;

  setDocked(docked: boolean): void {
    if (docked === this.docked) return;
    this.docked = docked;
    this.invalidate();
  }

  /** The bottom of the frame: nothing at all when the dock is under it. */
  closing(border = "border"): Component[] {
    if (this.docked) return [];
    return [new Spacer(1), new Rule(rule(border))];
  }
}

export interface DialogFields {
  title: string;
  subtitle: string;
  body: string[];
  options: Option[];
  hints: Hint[];
  selected: number;
  border: string;
}

/** A framed block of title, body, options and hints. */
export class Dialog extends Framed implements DialogFields {
  title: string;
  subtitle: string;
  body: string[];
  options: Option[];
  hints: Hint[];
  selected: number;
  border: string;

  constructor(fields: Partial<DialogFields> = {}) {
    super();
    this.title = fields.title ?? "";
    this.subtitle = fields.subtitle ?? "";
    this.body = fields.body ?? [];
    this.options = fields.options ?? [];
    this.hints = fields.hints ?? [];
    this.selected = fields.selected ?? 0;
    this.border = fields.border ?? "border";
  }

  draw(width: number): string[] {
    const parts: Component[] = [new Rule(rule(this.border)), new Spacer(1)];
    if (this.title) parts.push(new Text(this.title, 1, 0));
    if (this.subtitle) parts.push(new Text(this.subtitle, 1, 0));
    if (this.title || this.subtitle) parts.push(new Spacer(1));
    if (this.body.length) parts.push(new Lines(this.body), new Spacer(1));
    if (this.options.length) parts.push(...this.rows(), new Spacer(1));
    if (this.hints.length) parts.push(new Text(hintsLine(this.hints), 1, 0));
    parts.push(...this.closing(this.border));
    return parts.flatMap((part) => part.render(width));
  }

  private rows(): Text[] {
    const anyCurrent = this.options.some((o) => o.current);
    return this.options.map((option, index) => {
      const chosen = index === this.selected;
      const cursor = chosen ? fg("accent", CURSOR) : GUTTER;
      const current = option.current ? fg("accent", CURRENT) : anyCurrent ? GUTTER : "";
      // Weight follows selection, never meaning: an option is not emphasised
      // for being the dangerous one, because then the eye learns to go there.
      const key = option.key ? `${fg(chosen ? "accent" : "dim", option.key)}  ` : "";
      const label = fg(chosen ? "accent" : "text", option.label);
      const note = option.note ? fg("dim", ` · ${option.note}`) : "";
      return new Text(`${cursor}${current}${key}${label}${note}`, 1, 0);
    });
  }

  move(delta: number): void {
    if (!this.options.length) return;
    const length = this.options.length;
    this.selected = (((this.selected + delta) % length) + length) % length;
    this.invalidate();
  }

  select(index: number): void {
    this.selected = Math.max(0, Math.min(this.options.length - 1, index));
    this.invalidate();
  }

  get choice(): Option | null {
    return this.options[this.selected] ?? null;
  }

  byKey(key: string): Option | null {
    return this.options.find((o) => o.key && o.key === key) ?? null;
  }
}

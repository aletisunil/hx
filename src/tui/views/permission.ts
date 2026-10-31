/**
 * Asking permission.
 *
 * An approval is part of the conversation, not an interruption to it: a
 * transcript block that takes the keyboard, drawn in the dialog grammar.
 * Answering does not remove the block. It collapses in place into a one-line
 * record of what was granted and how widely.
 */

import { keymap } from "../../keys.ts";
import type { GrantScope, PermissionAnswer, PermissionRequest } from "../../permissions/engine.ts";
import { pyLen, strip } from "../../py.ts";
import { Text } from "../../term/primitives.ts";
import { plainText } from "../../term/sanitize.ts";
import { oneLine } from "../format.ts";
import { NOTICE, TODO_DONE, TOOL_FAILED } from "../glyphs.ts";
import { PREVIEW_LINES, RECORD_WIDTH } from "../limits.ts";
import { fg } from "../paint.ts";
import {
  displayPath,
  expandNote,
  looksLikeDiff,
  renderDiff,
  rendererFor,
  sanitizedCall,
  toolCall,
} from "../renderers.ts";
import { Dialog, Framed, type Hint, hint, option } from "./dialog.ts";

/** How each grant is described after the fact, in the words the user would
 * use to undo it. */
export const SCOPE_LABELS: Record<GrantScope, string> = {
  once: "allowed once",
  session: "allowed for this session",
  always: "always allowed",
};

/** The four answers, in the order they are offered. `always` carries its
 * consequence in words rather than in colour. */
export const CHOICES: readonly [string, GrantScope | null, string, string][] = [
  ["y", "once", "allow once", ""],
  ["s", "session", "allow for this session", ""],
  ["a", "always", "always allow", "saves a rule for this project in ~/.hx"],
  ["n", null, "deny", ""],
];

const answerFor = (scope: GrantScope | null): PermissionAnswer => ({
  allowed: scope !== null,
  scope: scope ?? "once",
});

/** One pending approval, rendered in the transcript. */
export class PermissionPrompt extends Framed {
  /** So a path in the detail is shown the way the transcript shows it. */
  readonly cwd: string;
  readonly origin: string | null;
  answer: PermissionAnswer | null = null;
  expanded = false;
  /** Set when the turn was interrupted before anyone answered. */
  abandoned = false;
  selected = 0;
  /** Another approval is ahead of this one in the queue, and has the keyboard. */
  waiting = false;

  constructor(
    readonly request: PermissionRequest,
    /** Called once with the answer. Absent when the block is drawn on its own. */
    private readonly settle: ((answer: PermissionAnswer) => void) | null = null,
    origin: string | null = null,
    cwd: string | null = null,
  ) {
    super();
    this.cwd = cwd ?? process.cwd();
    this.origin = origin || request.origin || null;
  }

  get answered(): boolean {
    return this.answer !== null || this.abandoned;
  }

  setWaiting(waiting: boolean): void {
    if (waiting !== this.waiting) {
      this.waiting = waiting;
      this.invalidate();
    }
  }

  /** Record the outcome and collapse. Idempotent, so a late second key press
   * cannot overwrite the decision already acted on. */
  resolve(answer: PermissionAnswer): void {
    if (this.answered) return;
    this.answer = answer;
    this.invalidate();
    this.settle?.(answer);
  }

  /** The turn was interrupted. Whoever waits is released with the one answer
   * safe to give for a user who never answered: no. */
  abandon(): void {
    if (this.answered) return;
    this.abandoned = true;
    this.invalidate();
    this.settle?.({ allowed: false, scope: "once" });
  }

  draw(width: number): string[] {
    // The same one-column indent the pending block has.
    if (this.answered) return new Text(this.record(), 1, 0).render(width);
    // Queued behind another approval: the same question, without the options.
    const options = this.waiting
      ? []
      : CHOICES.map(([key, , label, note]) => option({ key, label, note }));
    const dialog = new Dialog({
      title: fg("text", "Permission needed", { bold: true }) + this.who(),
      subtitle: fg("warning", plainText(this.request.toolName), { bold: true }),
      body: this.detail(),
      options,
      hints: this.hints(),
      selected: this.selected,
      border: this.waiting ? "border_muted" : "warning",
    });
    // Usually the last thing said, directly above the dock.
    dialog.setDocked(this.docked);
    return dialog.render(width);
  }

  private who(): string {
    if (!this.origin) return "";
    return fg("dim", `  (requested by ${plainText(this.origin)})`);
  }

  /** What is actually about to happen, drawn by the tool's own renderer. */
  private detail(): string[] {
    const lines = this.detailLines();
    if (this.expanded || lines.length <= PREVIEW_LINES) return lines;
    return [...lines.slice(0, PREVIEW_LINES), expandNote(lines.length - PREVIEW_LINES)];
  }

  private detailLines(): string[] {
    const link = this.link();
    if (link) return [fg("warning", `${link[0]} is a link to ${link[1]}`), "", ...this.bodyLines()];
    return this.bodyLines();
  }

  /** The path an edit names and the file it really changes, when they differ. */
  private link(): [string, string] | null {
    const target = this.request.linkTarget;
    const path = this.request.params.file_path;
    if (!target || !path) return null;
    return [plainText(displayPath(path, this.cwd)), plainText(displayPath(target, this.cwd))];
  }

  private bodyLines(): string[] {
    // Sanitized before anything reads it: the command shown has to be the
    // command that runs.
    const detail = strip(plainText(this.request.detail || ""), "\n");
    const kind = this.request.detailKind ?? "text";
    if (!detail) return [fg("text", this.target())];
    if (kind === "diff" || (kind !== "command" && looksLikeDiff(detail))) return renderDiff(detail);
    const call = sanitizedCall(
      toolCall({
        name: this.request.toolName,
        params: { ...(this.request.params ?? {}) },
        cwd: this.cwd,
      }),
    );
    if (kind === "command") return rendererFor(this.request.toolName).forApproval().detail(call);
    return detail.split("\n").map((line) => fg("text", line));
  }

  /** What the approval is about, as one safe string. */
  private target(): string {
    const specifier = this.request.specifier;
    const link = this.link();
    if (link) return `${link[0]} → ${link[1]}`;
    if (specifier && specifier === this.request.params.file_path)
      return plainText(displayPath(specifier, this.cwd));
    return plainText(String(specifier || this.request.description));
  }

  /** Every key that does something. */
  private hints(): Hint[] {
    // No key here does anything yet, and saying so is the point.
    if (this.waiting) return [hint("waiting", "- answer the approval above first")];
    return [
      hint("y/s/a/n", "answer"),
      hint("↑↓", "select"),
      hint("enter", "confirm"),
      hint("esc", "deny"),
      hint(keymap().primary("app.tools.expand"), "expand"),
    ];
  }

  /** The one line this collapses to once it has been answered. */
  private record(): string {
    const target = oneLine(this.target(), RECORD_WIDTH);
    if (this.abandoned)
      return (
        fg("muted", NOTICE.info as string) +
        fg("muted", "not answered - the turn was interrupted") +
        fg("dim", ` · ${target}`)
      );
    const answer = this.answer as PermissionAnswer;
    const head = answer.allowed
      ? fg("success", `${TODO_DONE} `, { bold: true }) + fg("success", SCOPE_LABELS[answer.scope])
      : fg("error", `${TOOL_FAILED} `, { bold: true }) + fg("error", "denied");
    return head + fg("dim", ` · ${target}`);
  }

  handleInput(key: string, data: string): boolean {
    if (this.answered) return false;
    // A printable key arrives as "text" with the character in data.
    const pressed = key === "text" && pyLen(data) === 1 ? data : key;
    const index = CHOICES.findIndex(([choice]) => choice === pressed);
    if (index >= 0) {
      this.selected = index;
      this.resolve(answerFor((CHOICES[index] as (typeof CHOICES)[number])[1]));
      return true;
    }
    if (pressed === "up" || pressed === "down") {
      const length = CHOICES.length;
      this.selected =
        (((this.selected + (pressed === "down" ? 1 : -1)) % length) + length) % length;
      this.invalidate();
      return true;
    }
    if (pressed === "enter") {
      this.resolve(answerFor((CHOICES[this.selected] as (typeof CHOICES)[number])[1]));
      return true;
    }
    if (pressed === "escape") {
      this.resolve({ allowed: false, scope: "once" });
      return true;
    }
    if (keymap().keysFor("app.tools.expand").includes(pressed) || pressed === "v") {
      this.expanded = !this.expanded;
      this.invalidate();
      return true;
    }
    return false;
  }
}

/**
 * Signing in, and configuring a key, on the shared dialog grammar.
 *
 * A flow can talk before its dialog is on screen, so every method of the
 * flow's protocol records state and repaints rather than touching anything.
 */

import type { LoginInteraction } from "../../auth/oauth/browser.ts";
import { pyLen, strip } from "../../py.ts";
import { inverse } from "../../term/ansi.ts";
import type { Component } from "../../term/component.ts";
import { Lines, Rule, Spacer, Text } from "../../term/primitives.ts";
import { CURSOR_MARKER } from "../../term/screen.ts";
import { columns } from "../format.ts";
import { CURRENT, CURSOR, GUTTER } from "../glyphs.ts";
import { fg, link, rule } from "../paint.ts";
import { Framed, hint, hintsLine } from "./dialog.ts";

const cursorCell = () => CURSOR_MARKER + inverse(" ");
const dropLast = (text: string) => Array.from(text).slice(0, -1).join("");

/** `[value, label, hasCredential]` for one account. */
export type ProviderRow = [string, string, boolean];

/** Which account to sign in to. */
export class ProviderDialog extends Framed {
  selected = 0;
  result: string | null = null;
  done = false;

  constructor(readonly providers: ProviderRow[]) {
    super();
  }

  draw(width: number): string[] {
    const rows = this.providers.map(([, label, present], index) => {
      const chosen = index === this.selected;
      const cursor = chosen ? fg("accent", CURSOR) : GUTTER;
      const marker = present ? fg("accent", CURRENT) : GUTTER;
      return cursor + marker + fg(chosen ? "accent" : "text", label);
    });
    const parts: Component[] = [
      new Rule(rule("border")),
      new Spacer(1),
      new Text(fg("accent", "Sign in", { bold: true }), 1, 0),
      new Spacer(1),
      new Lines(rows),
      new Spacer(1),
      new Text(
        hintsLine([hint("↑↓", "move"), hint("enter", "select"), hint("esc", "cancel")]),
        1,
        0,
      ),
      ...this.closing(),
    ];
    return parts.flatMap((part) => part.render(width));
  }

  handleInput(key: string, _data: string): boolean {
    if (this.done || !this.providers.length) return false;
    if (key === "up" || key === "down") {
      const length = this.providers.length;
      this.selected = (((this.selected + (key === "down" ? 1 : -1)) % length) + length) % length;
      this.invalidate();
      return true;
    }
    if (key === "enter") {
      this.result = (this.providers[this.selected] as ProviderRow)[0];
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

/** The error a cancelled paste prompt rejects with. */
export class LoginCancelled extends Error {
  override name = "AbortError";
}

/** A sign-in flow in progress, implementing the flow's interaction protocol. */
export class LoginDialog extends Framed implements LoginInteraction {
  /** What to do, from the flow. Kept for the whole sign-in. */
  private instructions = "Starting…";
  /** The latest progress line - a busy port, the token exchange. */
  private note = "";
  private highlight = "";
  private highlightUrl = "";
  private pasteLabel = "";
  private pasteable = false;
  private typed = "";
  private pending: { resolve: (text: string) => void; reject: (error: Error) => void } | null =
    null;
  cancelled = false;

  constructor(
    readonly title = "Sign in",
    /** Asks for a frame: the flow updates this from a background task. */
    private readonly onChange: (() => void) | null = null,
  ) {
    super();
  }

  private changed(): void {
    this.invalidate();
    this.onChange?.();
  }

  showUrl(url: string, instructions: string): void {
    this.highlight = url;
    this.highlightUrl = url;
    this.instructions = instructions;
    // Whatever the flow said before it had a URL is finished by now.
    this.note = "";
    this.changed();
  }

  showDeviceCode(userCode: string, verificationUri: string): void {
    this.instructions = `Open ${verificationUri} and enter this code:`;
    this.highlight = userCode;
    this.highlightUrl = verificationUri;
    // Nothing to paste in this flow; the poll decides when it is done.
    this.pasteable = false;
    this.changed();
  }

  progress(message: string): void {
    this.note = message;
    this.changed();
  }

  /** Resolve when the user submits, never otherwise. The browser callback
   * usually wins, and the signal then withdraws the question. */
  promptPaste(message: string, signal: AbortSignal): Promise<string> {
    this.pasteLabel = message;
    this.pasteable = true;
    this.typed = "";
    this.changed();
    return new Promise((resolve, reject) => {
      this.pending = { resolve, reject };
      signal.addEventListener(
        "abort",
        () => {
          if (this.pending?.resolve === resolve) this.pending = null;
        },
        { once: true },
      );
    });
  }

  draw(width: number): string[] {
    // Prose wraps; the URL is a link and is cut to the width instead.
    const parts: Component[] = [
      new Rule(rule("border")),
      new Spacer(1),
      new Text(fg("accent", this.title, { bold: true }), 1, 0),
      new Spacer(1),
      new Text(fg("muted", this.instructions), 1, 0),
    ];
    if (this.highlight)
      parts.push(
        new Lines([
          this.highlightUrl
            ? link(this.highlightUrl, this.highlight)
            : fg("accent", this.highlight, { bold: true }),
        ]),
      );
    if (this.note) parts.push(new Spacer(1), new Text(fg("text", this.note), 1, 0));
    if (this.pasteable)
      parts.push(
        new Spacer(1),
        new Text(fg("muted", this.pasteLabel), 1, 0),
        new Lines([fg("text", this.typed) + cursorCell()]),
      );
    const hints = [hint("esc", "cancel")];
    if (this.pasteable) hints.unshift(hint("enter", "submit"));
    parts.push(new Spacer(1), new Text(hintsLine(hints), 1, 0), ...this.closing());
    return parts.flatMap((part) => part.render(width));
  }

  handleInput(key: string, data: string): boolean {
    if (key === "escape") {
      this.cancelled = true;
      const pending = this.pending;
      this.pending = null;
      pending?.reject(new LoginCancelled("cancelled"));
      return true;
    }
    if (!this.pasteable) return false;
    if (key === "text" || key === "paste") {
      this.typed += strip(data.replaceAll("\n", ""));
      this.invalidate();
      return true;
    }
    if (key === "backspace") {
      this.typed = dropLast(this.typed);
      this.invalidate();
      return true;
    }
    if (key === "enter" && this.pending !== null) {
      const pending = this.pending;
      this.pending = null;
      pending.resolve(strip(this.typed));
      this.pasteable = false;
      this.invalidate();
      return true;
    }
    return false;
  }
}

/** Set the API key for a provider. */
export class ConfigureDialog extends Framed {
  private typed = "";
  result: string | null = null;
  done = false;

  constructor(
    readonly title: string,
    readonly summary: [string, string][],
    readonly warning = "",
  ) {
    super();
  }

  draw(width: number): string[] {
    const body = columns(
      this.summary.map(([label, value]) => [fg("muted", label), fg("text", value)]),
    );
    if (this.warning) body.push(fg("warning", this.warning));
    body.push("");
    body.push(fg("muted", "API key: ") + fg("text", "•".repeat(pyLen(this.typed))) + cursorCell());
    const parts: Component[] = [
      new Rule(rule("border")),
      new Spacer(1),
      new Text(fg("accent", this.title, { bold: true }), 1, 0),
      new Spacer(1),
      new Lines(body),
      new Spacer(1),
      new Text(hintsLine([hint("enter", "save"), hint("esc", "cancel")]), 1, 0),
      ...this.closing(),
    ];
    return parts.flatMap((part) => part.render(width));
  }

  handleInput(key: string, data: string): boolean {
    if (this.done) return false;
    if (key === "text" || key === "paste") {
      this.typed += strip(data.replaceAll("\n", ""));
      this.invalidate();
      return true;
    }
    if (key === "backspace") {
      this.typed = dropLast(this.typed);
      this.invalidate();
      return true;
    }
    if (key === "enter") {
      this.result = strip(this.typed) || null;
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

/**
 * An emacs kill ring for the prompt: ctrl+w, ctrl+u, ctrl+k push here, ctrl+y
 * pastes the newest back, and alt+y walks further down. Separate from the
 * system clipboard, which it would otherwise trample.
 */

export const MAX_ENTRIES = 32;

/** Newest kill first. Bounded, because this is a scratch buffer. */
export class KillRing {
  private entries: string[] = [];
  private index = 0;

  constructor(private readonly maxEntries = MAX_ENTRIES) {}

  get length(): number {
    return this.entries.length;
  }

  /** Record killed text. Empty kills are dropped. */
  kill(text: string): void {
    if (!text) return;
    this.entries.unshift(text);
    if (this.entries.length > this.maxEntries) this.entries.length = this.maxEntries;
    this.index = 0;
  }

  /** The newest kill, and the start of a yank-pop sequence. */
  yank(): string | null {
    if (this.entries.length === 0) return null;
    this.index = 0;
    return this.entries[0] as string;
  }

  /** The next older kill, wrapping around at the end of the ring. */
  yankPop(): string | null {
    if (this.entries.length < 2) return null;
    this.index = (this.index + 1) % this.entries.length;
    return this.entries[this.index] as string;
  }
}

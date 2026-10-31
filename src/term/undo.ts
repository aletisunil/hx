/**
 * Undo, as a stack of whole snapshots. A draft in a prompt is small, so storing
 * the entire text per step costs nothing. Consecutive typing is coalesced, so
 * undo steps back a word or a line, not a character.
 */

export const MAX_DEPTH = 200;

export interface Snapshot {
  readonly text: string;
  readonly cursor: number;
}

export class UndoStack {
  private past: Snapshot[] = [];
  private future: Snapshot[] = [];
  private coalescing = false;

  /** Remember a state before it is changed. `coalesce` merges this into the
   * previous step, so a run of typed characters is one undo. */
  record(text: string, cursor: number, coalesce = false): void {
    if (this.past.length && (this.past.at(-1) as Snapshot).text === text) return;
    if (coalesce && this.coalescing && this.past.length) return;
    this.past.push({ text, cursor });
    if (this.past.length > MAX_DEPTH) this.past.splice(0, this.past.length - MAX_DEPTH);
    this.future = [];
    this.coalescing = coalesce;
  }

  /** End a coalescing run, so the next change starts its own step. */
  breakRun(): void {
    this.coalescing = false;
  }

  undo(text: string, cursor: number): Snapshot | null {
    const snapshot = this.past.pop();
    if (snapshot === undefined) return null;
    this.future.push({ text, cursor });
    this.coalescing = false;
    return snapshot;
  }

  redo(text: string, cursor: number): Snapshot | null {
    const snapshot = this.future.pop();
    if (snapshot === undefined) return null;
    this.past.push({ text, cursor });
    this.coalescing = false;
    return snapshot;
  }
}

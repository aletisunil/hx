/**
 * The text being edited, as a string and a cursor offset. A single offset
 * removes the whole class of bugs where a row and a column disagree about where
 * the cursor is; rows and columns are derived where they are needed.
 *
 * Offsets are UTF-16 units, and every motion steps a whole code point, so the
 * cursor never sits inside an emoji.
 */

import { stepBack, stepForward, wordLeft, wordRight } from "./word_nav.ts";

/** Editable text with one cursor. */
export class TextBuffer {
  private value: string;
  private offset: number;

  constructor(text = "") {
    this.value = text;
    this.offset = text.length;
  }

  get text(): string {
    return this.value;
  }

  set text(value: string) {
    this.value = value;
    this.offset = Math.min(this.offset, value.length);
  }

  get cursor(): number {
    return this.offset;
  }

  set cursor(value: number) {
    this.offset = Math.max(0, Math.min(value, this.value.length));
  }

  get lines(): string[] {
    return this.value.split("\n");
  }

  get row(): number {
    return this.value.slice(0, this.offset).split("\n").length - 1;
  }

  get column(): number {
    return this.offset - this.lineStart();
  }

  get lineCount(): number {
    return this.value.split("\n").length;
  }

  lineStart(offset: number = this.offset): number {
    return offset > 0 ? this.value.lastIndexOf("\n", offset - 1) + 1 : 0;
  }

  lineEnd(offset: number = this.offset): number {
    const found = this.value.indexOf("\n", offset);
    return found === -1 ? this.value.length : found;
  }

  set(text: string, cursor: number | null = null): void {
    this.value = text;
    this.offset = cursor === null ? text.length : Math.max(0, Math.min(cursor, text.length));
  }

  clear(): void {
    this.set("");
  }

  insert(text: string): void {
    this.value = this.value.slice(0, this.offset) + text + this.value.slice(this.offset);
    this.offset += text.length;
  }

  /** Remove `[start, end)` and return what was removed. */
  deleteRange(from: number, to: number): string {
    let start = Math.max(0, from);
    let end = Math.min(this.value.length, to);
    if (start > end) [start, end] = [end, start];
    const removed = this.value.slice(start, end);
    if (!removed) return "";
    this.value = this.value.slice(0, start) + this.value.slice(end);
    this.offset = start;
    return removed;
  }

  backspace(): string {
    if (this.offset === 0) return "";
    return this.deleteRange(this.offset - stepBack(this.value, this.offset), this.offset);
  }

  deleteForward(): string {
    if (this.offset >= this.value.length) return "";
    return this.deleteRange(this.offset, this.offset + stepForward(this.value, this.offset));
  }

  getRange(from: number, to: number): string {
    let start = Math.max(0, from);
    let end = Math.min(this.value.length, to);
    if (start > end) [start, end] = [end, start];
    return this.value.slice(start, end);
  }

  left(): void {
    this.cursor = this.offset - stepBack(this.value, this.offset);
  }

  right(): void {
    this.cursor = this.offset + stepForward(this.value, this.offset);
  }

  wordLeft(): number {
    return wordLeft(this.value, this.offset);
  }

  wordRight(): number {
    return wordRight(this.value, this.offset);
  }

  home(): void {
    this.cursor = this.lineStart();
  }

  end(): void {
    this.cursor = this.lineEnd();
  }

  /** Move to the previous line, keeping the column. False at the top. */
  up(): boolean {
    const start = this.lineStart();
    if (start === 0) return false;
    const column = this.offset - start;
    const previousStart = this.lineStart(start - 1);
    this.cursor = Math.min(previousStart + column, start - 1);
    return true;
  }

  /** Move to the next line, keeping the column. False at the bottom. */
  down(): boolean {
    const end = this.lineEnd();
    if (end >= this.value.length) return false;
    const column = this.offset - this.lineStart();
    const nextStart = end + 1;
    this.cursor = Math.min(nextStart + column, this.lineEnd(nextStart));
    return true;
  }
}

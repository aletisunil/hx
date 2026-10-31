/**
 * Where a word starts and ends, for readline-style motion: a word is a run of
 * alphanumerics, so moving back a word from the end of `foo.bar()` lands on
 * `bar`, not on `foo`.
 */

import { isAlnum } from "../py.ts";

const isWord = (char: string) => char === "_" || isAlnum(char);

/** The code point ending at `index`, and its length in UTF-16 units. */
function before(text: string, index: number): [string, number] {
  const low = text.charCodeAt(index - 1);
  if (index >= 2 && low >= 0xdc00 && low <= 0xdfff) {
    const high = text.charCodeAt(index - 2);
    if (high >= 0xd800 && high <= 0xdbff) return [text.slice(index - 2, index), 2];
  }
  return [text.slice(index - 1, index), 1];
}

function after(text: string, index: number): [string, number] {
  const char = String.fromCodePoint(text.codePointAt(index) as number);
  return [char, char.length];
}

/** Start of the word at or before `index`. */
export function wordLeft(text: string, start: number): number {
  let index = Math.max(0, Math.min(start, text.length));
  while (index > 0 && !isWord(before(text, index)[0])) index -= before(text, index)[1];
  while (index > 0 && isWord(before(text, index)[0])) index -= before(text, index)[1];
  return index;
}

/** End of the word at or after `index`. */
export function wordRight(text: string, start: number): number {
  let index = Math.max(0, Math.min(start, text.length));
  while (index < text.length && !isWord(after(text, index)[0])) index += after(text, index)[1];
  while (index < text.length && isWord(after(text, index)[0])) index += after(text, index)[1];
  return index;
}

/** UTF-16 length of the code point that ends at `index`. */
export const stepBack = (text: string, index: number) => (index > 0 ? before(text, index)[1] : 0);
/** UTF-16 length of the code point that starts at `index`. */
export const stepForward = (text: string, index: number) =>
  index < text.length ? after(text, index)[1] : 0;

/**
 * Fuzzy matching for the command palette, pickers and prompt completion,
 * ported from pi's `fuzzy.ts`. A query matches when its characters appear in
 * order; the score rewards runs, word boundaries and early positions. Lower is
 * better.
 */

import { strip } from "../py.ts";

const BOUNDARY = /[\s\-_./:]/;
const TOKENS = /[\s/]+/;
const LETTERS_DIGITS = /^([a-z]+)([0-9]+)$/;
const DIGITS_LETTERS = /^([0-9]+)([a-z]+)$/;

/** Score `query` against `text`, or null when it does not match. */
function score(query: string, text: string): number | null {
  if (!query) return 0;
  const q = [...query];
  const t = [...text];
  if (q.length > t.length) return null;
  let total = 0;
  let queryIndex = 0;
  let lastMatch = -1;
  let consecutive = 0;
  for (let i = 0; i < t.length; i++) {
    if (queryIndex >= q.length) break;
    if (t[i] !== q[queryIndex]) continue;
    const atBoundary = i === 0 || BOUNDARY.test(t[i - 1] as string);
    if (lastMatch === i - 1) {
      consecutive++;
      total -= consecutive * 5;
    } else {
      consecutive = 0;
      if (lastMatch >= 0) total += (i - lastMatch - 1) * 2;
    }
    if (atBoundary) total -= 10;
    total += i * 0.1;
    lastMatch = i;
    queryIndex++;
  }
  if (queryIndex < q.length) return null;
  if (query === text) total -= 100;
  return total;
}

/** Score one query against one string, retrying with digits and letters
 * swapped: `4sonnet` for `sonnet-4`. */
export function match(query: string, text: string): number | null {
  const queryLower = query.toLowerCase();
  const textLower = text.toLowerCase();
  const direct = score(queryLower, textLower);
  if (direct !== null) return direct;
  const found = LETTERS_DIGITS.exec(queryLower) ?? DIGITS_LETTERS.exec(queryLower);
  if (!found) return null;
  const swapped = score(`${found[2]}${found[1]}`, textLower);
  return swapped === null ? null : swapped + 5;
}

/** Items that match every whitespace- or slash-separated token, best first;
 * equal matches keep their original order. */
export function filterItems<T>(items: Iterable<T>, query: string, key: (item: T) => string): T[] {
  const entries = [...items];
  if (!strip(query)) return entries;
  const tokens = strip(query).split(TOKENS).filter(Boolean);
  if (tokens.length === 0) return entries;
  const scored: [number, number, T][] = [];
  entries.forEach((item, position) => {
    const text = key(item);
    let total = 0;
    for (const token of tokens) {
      const result = match(token, text);
      if (result === null) return;
      total += result;
    }
    scored.push([total, position, item]);
  });
  return scored.sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(([, , item]) => item);
}

/** Wording shared by the tools, the TUI and the CLI. */

/**
 * `1 line`, `2 lines`: a number with its noun agreeing.
 *
 * `plural` is for the nouns an `s` does not make plural - `match`.
 */
export function count(number: number, singular: string, plural?: string): string {
  const noun = number === 1 ? singular : (plural ?? `${singular}s`);
  return `${number} ${noun}`;
}

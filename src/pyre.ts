/**
 * Regular expressions written for Python's `re`, run as JavaScript RegExps.
 *
 * Users wrote hook matchers and search patterns against Python's syntax. The
 * spellings JavaScript lacks - named groups and back-references with `P`,
 * `\A` and `\Z` - are translated; everything else is the common subset.
 */

export function translatePattern(pattern: string): string {
  return pattern
    .replace(/\(\?P<([A-Za-z_]\w*)>/g, "(?<$1>")
    .replace(/\(\?P=([A-Za-z_]\w*)\)/g, "\\k<$1>")
    .replace(/\\A/g, "^")
    .replace(/\\Z/g, "$(?![\\s\\S])");
}

/** `re.compile(pattern, flags)`. Throws on a pattern neither language accepts. */
export function compile(pattern: string, flags = ""): RegExp {
  return new RegExp(translatePattern(pattern), flags);
}

/** `re.fullmatch(pattern, text) is not None`. */
export function fullmatch(pattern: string, text: string): boolean {
  return new RegExp(`^(?:${translatePattern(pattern)})$`).test(text);
}

/** The reason a pattern does not compile, without JavaScript's restatement of the pattern. */
export function compileError(error: unknown): string {
  return (error as Error).message.replace(/^Invalid regular expression: \/.*\/[a-z]*: /s, "");
}

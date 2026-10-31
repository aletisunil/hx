/**
 * Syntax highlighting, emitted as escape sequences.
 *
 * Tokens come from highlight.js and are coloured with the palette's `syntax_*`
 * roles, so a code block follows the user's theme. Language detection is
 * deliberately absent: guessing reliably mistakes English prose for some
 * obscure dialect, which looks like a rendering bug and is worse than plain text.
 */

import hljs from "highlight.js";
import { type ColorMode, fg } from "./ansi.ts";
import { cellWidth } from "./width.ts";

/** highlight.js scopes, by the palette role they are drawn in. The innermost
 * scope that has a role wins, the way a token's parent lends it a colour. */
const SCOPE_ROLES: Record<string, string> = {
  comment: "syntax_comment",
  quote: "syntax_comment",
  doctag: "syntax_comment",
  keyword: "syntax_keyword",
  literal: "syntax_keyword",
  "title.function": "syntax_function",
  "title.function.invoke": "syntax_function",
  meta: "syntax_function",
  variable: "syntax_variable",
  "variable.language": "syntax_keyword",
  attr: "syntax_variable",
  property: "syntax_variable",
  params: "syntax_variable",
  string: "syntax_string",
  regexp: "syntax_string",
  subst: "syntax_variable",
  number: "syntax_number",
  type: "syntax_type",
  built_in: "syntax_type",
  "title.class": "syntax_type",
  "title.class.inherited": "syntax_type",
  operator: "syntax_operator",
  punctuation: "syntax_punctuation",
};

/** The language highlight.js knows by `language`, or by the file's extension. */
function languageFor(language: string | null, filename: string | null): string | null {
  if (language && hljs.getLanguage(language)) return language;
  if (filename) {
    const dot = filename.lastIndexOf(".");
    const extension = dot >= 0 ? filename.slice(dot + 1) : filename;
    if (extension && hljs.getLanguage(extension)) return extension;
  }
  return null;
}

const ENTITIES: Record<string, string> = {
  "&lt;": "<",
  "&gt;": ">",
  "&amp;": "&",
  "&quot;": '"',
  "&#x27;": "'",
};
const unescapeHtml = (html: string) =>
  html.replace(/&(?:lt|gt|amp|quot|#x27);/g, (entity) => ENTITIES[entity] as string);

/** The role for a span's class list: `hljs-title function_` is `title.function`. */
function roleOf(classes: string): string | null {
  const scope = classes
    .split(" ")
    .map((name) => name.replace(/^hljs-/, "").replace(/_+$/, ""))
    .join(".");
  return SCOPE_ROLES[scope] ?? SCOPE_ROLES[scope.split(".")[0] as string] ?? null;
}

/**
 * Colour `code`, one string per line. Falls back to the text unchanged when
 * the language is unknown, which is the right answer far more often than a
 * guess would be. `color` resolves a role to the palette's colour for it.
 */
export function highlight(
  code: string,
  language: string | null,
  color: (role: string) => string,
  mode: ColorMode = "truecolor",
  filename: string | null = null,
): string[] {
  const name = languageFor(language, filename);
  if (name === null) return code.split("\n");
  let html: string;
  try {
    html = hljs.highlight(code, { language: name, ignoreIllegals: true }).value;
  } catch {
    return code.split("\n");
  }
  const lines = [""];
  const stack: (string | null)[] = [];
  for (const match of html.matchAll(/<span class="([^"]+)">|<\/span>|([^<]+)/g)) {
    if (match[1] !== undefined) stack.push(roleOf(match[1]) ?? stack.at(-1) ?? null);
    else if (match[2] === undefined) stack.pop();
    else {
      const role = stack.at(-1) ?? null;
      const colour = role ? color(role) : "";
      // `ansi_*` slots are the terminal's own palette, which code does not use.
      const usable = colour && !colour.startsWith("ansi") ? colour : "";
      unescapeHtml(match[2])
        .split("\n")
        .forEach((piece, index) => {
          if (index) lines.push("");
          if (piece) lines[lines.length - 1] += usable ? fg(usable, piece, mode) : piece;
        });
    }
  }
  if (lines.length && !cellWidth(lines.at(-1) as string)) lines.pop();
  return lines.length ? lines : [""];
}

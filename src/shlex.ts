/**
 * `shlex.split` and `shlex.quote`, POSIX mode, as Python implements them.
 *
 * Permission rules, the Bash tool's command framing and dragged-in file paths
 * are all split and quoted this way. The npm shell-quote packages differ on
 * edge cases - a backslash inside double quotes, an unterminated quote - and
 * here an edge case decides whether a command needs the user's approval.
 */

const WHITESPACE = " \t\r\n";

/** `shlex.split(text)`: posix, no comments. Throws on an unclosed quote or a trailing backslash. */
export function split(text: string): string[] {
  const tokens: string[] = [];
  let state: "space" | "word" | "'" | '"' | "escape" = "space";
  let escaped: "word" | '"' = "word";
  let token = "";
  let quoted = false;

  const emit = () => {
    if (token || quoted) tokens.push(token);
    token = "";
    quoted = false;
  };

  for (const char of text) {
    switch (state) {
      case "space":
        if (WHITESPACE.includes(char)) continue;
        if (char === "\\") {
          escaped = "word";
          state = "escape";
        } else if (char === "'" || char === '"') {
          state = char;
        } else {
          token = char;
          state = "word";
        }
        break;
      case "'":
      case '"':
        quoted = true;
        if (char === state) state = "word";
        else if (char === "\\" && state === '"') {
          escaped = '"';
          state = "escape";
        } else token += char;
        break;
      case "escape":
        // Within double quotes only the quote itself or the backslash may be
        // escaped; before anything else the backslash stays.
        if (escaped === '"' && char !== '"' && char !== "\\") token += "\\";
        token += char;
        state = escaped;
        break;
      case "word":
        if (WHITESPACE.includes(char)) {
          state = "space";
          emit();
        } else if (char === "'" || char === '"') {
          state = char;
        } else if (char === "\\") {
          escaped = "word";
          state = "escape";
        } else token += char;
        break;
    }
  }
  if (state === "'" || state === '"') throw new Error("No closing quotation");
  if (state === "escape") throw new Error("No escaped character");
  if (state === "word") emit();
  return tokens;
}

const SAFE = /^[%+,\-./0-9:=@A-Z_a-z]+$/;

/** `shlex.quote(text)`: single quotes, with each `'` spelled `'"'"'`. */
export function quote(text: string): string {
  if (!text) return "''";
  if (SAFE.test(text)) return text;
  return `'${text.replaceAll("'", `'"'"'`)}'`;
}

/** `shlex.join(parts)`. */
export const join = (parts: readonly string[]) => parts.map(quote).join(" ");

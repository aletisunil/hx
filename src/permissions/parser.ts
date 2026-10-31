/**
 * Shell command decomposition for permission matching.
 *
 * Matching a rule against the whole command string is unsafe: `git status &&
 * rm -rf /` would match an `allow: Bash(git status:*)` rule. Every command is
 * split into its individual executed segments and each segment must pass on
 * its own.
 *
 * This is a permission-matching aid, not a shell. When it cannot decompose a
 * command with confidence it says so, and the caller must treat that as "ask".
 */

import { rstrip, strip } from "../py.ts";
import { split } from "../shlex.ts";

const OPERATORS = ["&&", "||", ";;", ";", "|&", "|", "\n", "&"] as const;

/** Commands whose real payload is decided at runtime. They cannot be
 * decomposed, so a command invoking one is reported unparseable. `env`
 * qualifies only when it is launching something - see `isDynamic`. */
export const DYNAMIC_EXECUTABLES = new Set(["eval", "exec", "source", ".", "env"]);

/** `2>&1`, `>&2`, `>&-` - duplicating a descriptor, never a file write. */
const FD_DUP = /^\d*>&\d*-?$/;
const OUT_OP = /^(?:\d*>>?|&>>?)$/;
const IN_OP = /^\d*<<?-?$/;
const OUT_ATTACHED = /^(?:\d*>>?|&>>?)(?<target>[\s\S]+)$/;
const IN_ATTACHED = /^\d*<<?-?(?<target>[\s\S]+)$/;

/** Redirect targets that discard or re-route output rather than writing a
 * file. Prompting for `cmd 2>/dev/null` teaches users to approve without reading. */
export const DISCARD_TARGETS = new Set(["/dev/null", "/dev/stdout", "/dev/stderr"]);

/**
 * Commands that do not mutate state on their own. Used only to relax prompting.
 * Several of these write when given the right flag, which is what
 * `MUTATING_FLAGS` covers. Deliberately absent are the wrappers whose payload is
 * another command - `xargs`, `time`, `watch`, `nohup`, `command`.
 */
export const READ_ONLY_COMMANDS = new Set([
  // Inspecting the filesystem
  "ls",
  "cat",
  "head",
  "tail",
  "wc",
  "file",
  "stat",
  "du",
  "df",
  "pwd",
  "cd",
  "tree",
  "basename",
  "dirname",
  "realpath",
  "readlink",
  // Searching
  "grep",
  "rg",
  "find",
  "fd",
  "diff",
  "cmp",
  "comm",
  // Text through a pipe
  "echo",
  "printf",
  "sort",
  "uniq",
  "cut",
  "tr",
  "nl",
  "rev",
  "tac",
  "fold",
  "paste",
  "join",
  "seq",
  "column",
  "awk",
  "sed",
  "jq",
  "yq",
  "od",
  "xxd",
  "hexdump",
  "strings",
  // Digests
  "md5sum",
  "sha1sum",
  "sha256sum",
  "shasum",
  "cksum",
  // Asking the machine about itself
  "date",
  "whoami",
  "id",
  "groups",
  "hostname",
  "uname",
  "uptime",
  "ps",
  "locale",
  "which",
  "type",
  "env",
  "printenv",
  // Shell scaffolding that produces no effect of its own
  "test",
  "[",
  "true",
  "false",
  "sleep",
]);

/** Flags that turn a read-only command into a writing or executing one. */
export const MUTATING_FLAGS: Record<string, Set<string>> = {
  sed: new Set(["-i", "--in-place"]),
  awk: new Set(["-i", "--include"]),
  find: new Set(["-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"]),
  fd: new Set(["-x", "--exec", "-X", "--exec-batch"]),
  sort: new Set(["-o", "--output", "--compress-program"]),
  tree: new Set(["-o"]),
  date: new Set(["-s", "--set"]),
  rg: new Set(["--pre"]),
  yq: new Set(["-i", "--inplace"]),
  // Any word starting -r reverts: xxd reads `-r`, `-rp` and `-revert` alike.
  xxd: new Set(["-r"]),
};

/** Commands that write a file named by their second operand, with no flag at
 * all. The value is the options that take a separate argument. */
const OUTPUT_OPERAND_COMMANDS: Record<string, Set<string>> = {
  // uniq [OPTION]... [INPUT [OUTPUT]]
  uniq: new Set(["-f", "--skip-fields", "-s", "--skip-chars", "-w", "--check-chars"]),
  // xxd [options] [infile [outfile]]
  xxd: new Set([
    "-c",
    "-cols",
    "-g",
    "-groupsize",
    "-l",
    "-len",
    "-n",
    "-name",
    "-o",
    "-offset",
    "-s",
    "-seek",
    "-R",
  ]),
};

/** Git subcommands that read whatever arguments they are given. */
const GIT_READ_ONLY_SUBCOMMANDS = new Set([
  "status",
  "log",
  "diff",
  "diff-files",
  "diff-index",
  "diff-tree",
  "show",
  "blame",
  "describe",
  "rev-parse",
  "rev-list",
  "ls-files",
  "ls-tree",
  "ls-remote",
  "cat-file",
  "for-each-ref",
  "show-ref",
  "shortlog",
  "whatchanged",
  "grep",
  "merge-base",
  "name-rev",
  "check-ignore",
  "count-objects",
  "verify-commit",
  "var",
]);

/** Flags that make an otherwise read-only git subcommand write or execute. */
const GIT_UNSAFE_SUBCOMMAND_FLAGS = new Set(["--output", "-O", "--open-files-in-pager"]);

/** Git subcommands that list when bare and mutate when steered, with the flags
 * that make the difference. */
const GIT_LISTING_SUBCOMMANDS: Record<string, Set<string>> = {
  branch: new Set([
    "-d",
    "-D",
    "--delete",
    "-m",
    "-M",
    "--move",
    "-c",
    "-C",
    "--copy",
    "-u",
    "--set-upstream-to",
    "--unset-upstream",
    "--edit-description",
    "-f",
    "--force",
  ]),
  tag: new Set(["-d", "--delete", "-a", "--annotate", "-s", "--sign", "-f", "--force", "-m", "-F"]),
  config: new Set([
    "--add",
    "--unset",
    "--unset-all",
    "--replace-all",
    "--rename-section",
    "--remove-section",
    "-e",
    "--edit",
  ]),
  remote: new Set(),
  worktree: new Set(),
  submodule: new Set(),
  notes: new Set(),
  stash: new Set(),
  reflog: new Set(),
  bisect: new Set(),
};

/** The nested subcommand each listing accepts and stays read-only. */
const GIT_LISTING_OPERANDS: Record<string, Set<string>> = {
  remote: new Set(["show", "get-url"]),
  worktree: new Set(["list"]),
  submodule: new Set(["status"]),
  notes: new Set(["list", "show"]),
  stash: new Set(["list", "show"]),
  reflog: new Set(["show"]),
  bisect: new Set(["log"]),
};

/** Flags that read explicitly, so their operands are a query rather than a value to write. */
const GIT_LISTING_READ_FLAGS: Record<string, Set<string>> = {
  config: new Set(["--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list"]),
  branch: new Set([
    "-l",
    "--list",
    "--contains",
    "--no-contains",
    "--points-at",
    "--merged",
    "--no-merged",
  ]),
  tag: new Set(["-l", "--list", "-n", "--contains", "--no-contains", "--points-at", "--merged"]),
};

/** Bare `git stash` stashes the working tree, so it only reads when it says `list` or `show`. */
const GIT_LISTING_REQUIRES_OPERAND = new Set(["stash"]);

/** Options before the subcommand that swallow the next word. */
const GIT_GLOBAL_FLAGS_WITH_VALUE = new Set([
  "-C",
  "-c",
  "--namespace",
  "--config-env",
  "--exec-path",
]);

/** Global options that decide what a read-only subcommand actually runs, so
 * their presence withdraws the read-only reading entirely. */
const GIT_UNSAFE_GLOBAL_FLAGS = new Set([
  "-c",
  "--config-env",
  "--exec-path",
  "--git-dir",
  "--work-tree",
]);

/** `env` options taking a separate value, which is therefore not a command. */
const ENV_FLAGS_WITH_VALUE = new Set(["-u", "--unset", "-C", "--chdir"]);

/** One command that will actually execute. */
export interface CommandSegment {
  readonly raw: string;
  readonly executable: string;
  readonly args: readonly string[];
  /** True for `$(...)` / backtick content, which executes just as readily. */
  readonly inSubstitution: boolean;
}

export interface ParsedCommand {
  readonly raw: string;
  readonly segments: readonly CommandSegment[];
  readonly hasRedirectOut: boolean;
  readonly redirectTargets: readonly string[];
  /** Set when the command could not be decomposed with confidence. Callers
   * must treat this as "ask", never as "allow". */
  readonly unparseable: boolean;
}

/**
 * Split on `&&`, `||`, `;`, `|`, newlines, and command substitution, respecting
 * quoting. Anything not understood sets `unparseable` rather than degrading to
 * a naive split - a wrong split here is a real escape.
 */
export function parse(command: string): ParsedCommand {
  const [pieces, substitutions, ok] = scan(command);
  const segments: CommandSegment[] = [];
  const redirects: string[] = [];
  let unparseable = !ok;
  for (const [text, inSub] of [
    ...pieces.map((p) => [p, false] as const),
    ...substitutions.map((s) => [s, true] as const),
  ]) {
    const [segment, targets, segmentOk] = toSegment(text, inSub);
    redirects.push(...targets);
    if (!segmentOk) unparseable = true;
    if (segment !== null) {
      segments.push(segment);
      if (isDynamic(segment)) unparseable = true;
    }
  }
  return {
    raw: command,
    segments,
    hasRedirectOut: redirects.some((target) => !DISCARD_TARGETS.has(target)),
    redirectTargets: redirects,
    unparseable: unparseable || segments.length === 0,
  };
}

/** True when the segment's real payload is only known at runtime. Bare `env`
 * prints the environment and is not dynamic; `env FOO=1 <command>` is. */
function isDynamic(segment: CommandSegment): boolean {
  if (segment.executable === "env") return envRunsACommand(segment.args);
  return DYNAMIC_EXECUTABLES.has(segment.executable);
}

function toSegment(text: string, inSub: boolean): [CommandSegment | null, string[], boolean] {
  const stripped = strip(text);
  if (!stripped) return [null, [], true];
  let tokens: string[];
  try {
    tokens = split(stripped);
  } catch {
    return [null, [], false];
  }
  if (tokens.length === 0) return [null, [], true];
  const [words, redirects] = splitRedirections(tokens);
  if (words.length === 0) return [null, redirects, true];
  const executable = words[0] as string;
  // A name produced by expansion is only known at runtime.
  const ok = !executable.includes("$") && !executable.includes("`");
  return [
    { raw: stripped, executable, args: words.slice(1), inSubstitution: inSub },
    redirects,
    ok,
  ];
}

/**
 * Separate the words that make up the command from its redirections. A
 * redirection contributes no word, and only an *output* redirection is
 * reported: `2>&1` duplicates a descriptor and `< in` reads.
 */
function splitRedirections(tokens: string[]): [string[], string[]] {
  const words: string[] = [];
  const targets: string[] = [];
  let index = 0;
  while (index < tokens.length) {
    const token = tokens[index] as string;
    index++;
    if (FD_DUP.test(token)) continue;
    if (OUT_OP.test(token) || IN_OP.test(token)) {
      // `2> err`: the target is the next token, and only `>` forms write.
      if (index < tokens.length) {
        if (OUT_OP.test(token)) targets.push(tokens[index] as string);
        index++;
      }
      continue;
    }
    const attached = OUT_ATTACHED.exec(token);
    if (attached) {
      targets.push(attached.groups?.target as string);
      continue;
    }
    if (IN_ATTACHED.test(token)) continue;
    const embedded = embeddedRedirect(token);
    if (embedded !== null) {
      const [head, target, writes] = embedded;
      words.push(head);
      if (writes) targets.push(target);
      continue;
    }
    words.push(token);
  }
  return [words.filter((word) => word), targets];
}

/** `echo hi>out` - a redirection glued to the end of a word. */
function embeddedRedirect(token: string): [string, string, boolean] | null {
  const positions = ["<", ">"]
    .filter((char) => token.includes(char))
    .map((char) => token.indexOf(char));
  const position = positions.length ? Math.min(...positions) : -1;
  if (position <= 0) return null;
  const head = token.slice(0, position);
  const rest = token.slice(position);
  const target = rest.replace(/^[<>]+/, "");
  if (!target) return null;
  return [head, target, rest.startsWith(">")];
}

/**
 * Split into top-level pieces and extracted substitution bodies. `ok` is false
 * when quoting is unbalanced or a substitution never closes.
 */
function scan(text: string): [string[], string[], boolean] {
  const pieces: string[] = [];
  const substitutions: string[] = [];
  let buffer = "";
  let ok = true;
  let index = 0;
  let quote: string | null = null;

  while (index < text.length) {
    const char = text[index] as string;
    if (char === "\\" && quote !== "'") {
      buffer += text.slice(index, index + 2);
      index += 2;
      continue;
    }
    if (quote) {
      if (char === quote) quote = null;
      // Substitutions are live inside double quotes.
      else if (quote === '"' && (text.startsWith("$(", index) || char === "`")) {
        const [body, consumed, closed] = readSubstitution(text, index);
        substitutions.push(...recurse(body, substitutions));
        ok = ok && closed;
        index += consumed;
        continue;
      }
      buffer += char;
      index++;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      buffer += char;
      index++;
      continue;
    }
    if (text.startsWith("$(", index) || char === "`") {
      const [body, consumed, closed] = readSubstitution(text, index);
      substitutions.push(...recurse(body, substitutions));
      ok = ok && closed;
      index += consumed;
      continue;
    }
    if (char === "(" || char === ")") {
      // Subshell grouping: the contents run at this level.
      index++;
      continue;
    }
    const operator = OPERATORS.find((op) => text.startsWith(op, index));
    if (operator === "&" && isRedirectAmpersand(text, index, buffer)) {
      buffer += char;
      index++;
      continue;
    }
    if (operator) {
      pieces.push(buffer);
      buffer = "";
      index += operator.length;
      continue;
    }
    buffer += char;
    index++;
  }
  if (quote) ok = false;
  pieces.push(buffer);
  return [pieces.filter((piece) => strip(piece)), substitutions, ok];
}

/** True for the `&` in `2>&1`, `>&2` or `&>log`: splitting there would invent a
 * phantom command (`1`) out of a descriptor number. */
function isRedirectAmpersand(text: string, index: number, buffer: string): boolean {
  if (rstrip(buffer).endsWith(">")) return true;
  return text.startsWith("&>", index);
}

/** Substitution bodies are themselves commands; flatten them one level deep. */
function recurse(body: string, seen: string[]): string[] {
  const [innerPieces, innerSubs] = scan(body);
  return [...innerPieces, ...innerSubs.filter((s) => !seen.includes(s))];
}

/** Read a `$(...)` or backtick substitution starting at `index`. */
function readSubstitution(text: string, index: number): [string, number, boolean] {
  if (text[index] === "`") {
    const end = text.indexOf("`", index + 1);
    if (end === -1) return [text.slice(index + 1), text.length - index, false];
    return [text.slice(index + 1, end), end - index + 1, true];
  }
  let depth = 0;
  for (let cursor = index + 1; cursor < text.length; cursor++) {
    if (text[cursor] === "(") depth++;
    else if (text[cursor] === ")") {
      depth--;
      if (depth === 0) return [text.slice(index + 2, cursor), cursor - index + 1, true];
    }
  }
  return [text.slice(index + 2), text.length - index, false];
}

/**
 * Match a segment against a rule specifier such as `git commit:*` or `ls`. `:*`
 * is a prefix match on the whole command line; a bare executable name matches
 * any invocation of it.
 */
export function matchSpecifier(segment: CommandSegment, pattern: string): boolean {
  if (pattern === "*" || pattern === ":*") return true;
  const commandLine = [segment.executable, ...segment.args].join(" ");
  if (pattern.endsWith(":*")) {
    const prefix = strip(pattern.slice(0, -2));
    return commandLine === prefix || commandLine.startsWith(`${prefix} `);
  }
  if (pattern.includes(" ")) return commandLine === pattern;
  return segment.executable === pattern;
}

/**
 * Heuristic allowlist of non-mutating commands (`ls`, `cat`, `git status`). The
 * executable name alone does not settle it: `sed` reads and `sed -i` rewrites.
 * Used only to relax prompting, never to grant something a deny rule covers.
 */
export function isReadOnly(segment: CommandSegment): boolean {
  if (segment.executable === "git") return gitIsReadOnly(segment.args);
  if (!READ_ONLY_COMMANDS.has(segment.executable)) return false;
  if (segment.executable === "env") return !envRunsACommand(segment.args);
  if (PROGRAM_TEXT_COMMANDS.has(segment.executable) && programTextWrites(segment)) return false;
  if (segment.executable in OUTPUT_OPERAND_COMMANDS && namesAnOutput(segment)) return false;
  return !hasFlag(
    segment.args,
    MUTATING_FLAGS[segment.executable] ?? new Set(),
    ATTACHED_VALUE_COMMANDS.has(segment.executable),
  );
}

/** Commands whose payload is a little program, which has to be read before
 * the command counts as read-only. */
const PROGRAM_TEXT_COMMANDS = new Set(["awk", "sed"]);

/** An `awk` program reaching outside itself: a subshell, an output redirection
 * on a `print`, or a pipeline to a command. `$1 > 5` is a comparison. */
const AWK_WRITES = /system\s*\(|\|\s*["']|["']\s*\||print[f]?[^;}\n]*>/;

/** Options whose value is more of the program. */
const PROGRAM_TEXT_OPTIONS: Record<string, string[]> = {
  awk: ["-e", "--source"],
  sed: ["-e", "--expression"],
};

/** Options that load the program from a file, which cannot be judged from here. */
const PROGRAM_FILE_OPTIONS: Record<string, string[]> = {
  awk: ["-f", "--file", "-E", "--exec"],
  sed: ["-f", "--file"],
};

/** True when the command was given an output file as its second operand.
 * Counting errs towards two, which prompts - never the reverse. */
function namesAnOutput(segment: CommandSegment): boolean {
  const takesValue = OUTPUT_OPERAND_COMMANDS[segment.executable] as Set<string>;
  let operands = 0;
  let optionsDone = false;
  for (let index = 0; index < segment.args.length; index++) {
    const arg = segment.args[index] as string;
    if (!optionsDone && arg === "--") optionsDone = true;
    else if (!optionsDone && arg.startsWith("-") && arg !== "-") {
      if (takesValue.has(arg)) index++;
    } else {
      operands++;
      optionsDone = true;
    }
  }
  return operands >= 2;
}

/** True when the program handed to `awk`/`sed` can write or execute. */
function programTextWrites(segment: CommandSegment): boolean {
  if (hasOption(segment.args, PROGRAM_FILE_OPTIONS[segment.executable] as string[])) return true;
  const operands = segment.args.filter((arg) => !arg.startsWith("-"));
  const inline = optionValues(segment.args, PROGRAM_TEXT_OPTIONS[segment.executable] as string[]);
  if (segment.executable === "awk") {
    // Every operand: with `-F ,` the separator is an operand too.
    return [...operands, ...inline].some((text) => AWK_WRITES.test(text));
  }
  // sed's script is the value of each -e, or - with no -e at all - the first
  // operand. Not both: once -e carries the script, the operands are files.
  const scripts = inline.length ? inline : operands.slice(0, 1);
  return scripts.some(sedScriptWrites);
}

const isDigit = (char: string) => /^\p{Nd}$/u.test(char);
const isAlpha = (char: string) => /^\p{L}$/u.test(char);

/**
 * True when a `sed` script contains a `w`/`W` write or an `e` execute. Walked
 * rather than searched, because the letters are only commands in command
 * position: `s/e/x/` replaces the letter e and writes nothing.
 */
function sedScriptWrites(script: string): boolean {
  let index = 0;
  const end = script.length;
  while (index < end) {
    const char = script[index] as string;
    if (" \t\n;{}".includes(char)) index++;
    else if (char === "/") index = skipToDelim(script, index + 1, "/");
    else if (char === "\\" && index + 1 < end)
      index = skipToDelim(script, index + 2, script[index + 1] as string);
    else if (isDigit(char) || ",$!+~".includes(char)) index++;
    else if ("wWe".includes(char)) return true;
    else if ("sy".includes(char) && index + 1 < end) {
      const delim = script[index + 1] as string;
      index = skipToDelim(script, index + 2, delim); // past the pattern
      index = skipToDelim(script, index, delim); // past the replacement
      let flagsEnd = index;
      while (flagsEnd < end && !";}\n".includes(script[flagsEnd] as string)) flagsEnd++;
      const flags = script.slice(index, flagsEnd);
      if (flags.includes("w") || flags.includes("e")) return true;
      index = flagsEnd;
    } else {
      // Any other command: skip to the end of the statement.
      while (index < end && !";}\n".includes(script[index] as string)) index++;
    }
  }
  return false;
}

/** Scan forward to the closing `delim`, honouring backslash escapes. */
function skipToDelim(text: string, start: number, delim: string): number {
  let index = start;
  while (index < text.length) {
    if (text[index] === "\\") {
      index += 2;
      continue;
    }
    if (text[index] === delim) return index + 1;
    index++;
  }
  return text.length;
}

/**
 * Match `arg` against `options`, returning it and any value packed into it:
 * separate (`-e` then the script), `=`-attached (`--expression=…`), packed into
 * the same word (`-e'w out'`), or a long option cut short (`--expr=…`).
 */
function splitOption(arg: string, options: string[]): [string, string | null] | null {
  if (options.includes(arg)) return [arg, null];
  const at = arg.indexOf("=");
  const name = at < 0 ? arg : arg.slice(0, at);
  const option = options.find((o) => name === o || abbreviates(name, o));
  if (option !== undefined) return [option, at < 0 ? null : arg.slice(at + 1)];
  if (
    arg.length > 2 &&
    arg.startsWith("-") &&
    !arg.startsWith("--") &&
    options.includes(arg.slice(0, 2))
  ) {
    return [arg.slice(0, 2), arg.slice(2)];
  }
  return null;
}

const hasOption = (args: readonly string[], options: string[]) =>
  args.some((arg) => splitOption(arg, options) !== null);

/** The values given to `options`, in any of their spellings. */
function optionValues(args: readonly string[], options: string[]): string[] {
  const values: string[] = [];
  let index = 0;
  while (index < args.length) {
    const matched = splitOption(args[index] as string, options);
    index++;
    if (matched === null) continue;
    const [, attached] = matched;
    if (attached !== null) values.push(attached);
    else if (index < args.length) {
      values.push(args[index] as string);
      index++;
    }
  }
  return values;
}

/** Commands whose short options swallow the rest of their word as a value, so
 * only the first letter may be read as a flag: `date -Iseconds` is one option. */
const ATTACHED_VALUE_COMMANDS = new Set(["date", "xxd"]);

/**
 * True when any argument is one of `flags`, in any spelling a shell accepts:
 * separate (`-i`), attached (`-i.bak`, `--in-place=x`) and clustered
 * (`sed -ni`). Only two-character flags take part in the attached and
 * clustered readings. A long flag also matches when cut short.
 */
function hasFlag(args: readonly string[], flags: Set<string>, clusterFirstOnly = false): boolean {
  if (flags.size === 0) return false;
  for (const arg of args) {
    if (arg === "--") break; // everything after this is an operand
    const name = arg.split("=")[0] as string;
    if (flags.has(name) || [...flags].some((flag) => abbreviates(name, flag))) return true;
    if (!arg.startsWith("-") || arg.startsWith("--")) continue;
    let cluster = leadingLetters(arg.slice(1));
    if (clusterFirstOnly) cluster = [...cluster].slice(0, 1).join("");
    if ([...flags].some((flag) => flag.length === 2 && cluster.includes(flag[1] as string)))
      return true;
  }
  return false;
}

/** True when `name` is the long `option` cut short: `--compress` for
 * `--compress-program`. getopt_long takes any unambiguous prefix. */
function abbreviates(name: string, option: string): boolean {
  return name.length > 2 && name.startsWith("--") && option.startsWith(name) && name !== option;
}

/** The run of option letters at the head of a cluster - `i` of `-i.bak`. */
function leadingLetters(text: string): string {
  let out = "";
  for (const char of text) {
    if (!isAlpha(char)) break;
    out += char;
  }
  return out;
}

/** True when `env` is being used to launch something rather than to print. */
function envRunsACommand(args: readonly string[]): boolean {
  let index = 0;
  while (index < args.length) {
    const arg = args[index] as string;
    index++;
    if (arg === "--") return index < args.length;
    if (arg === "-S" || arg === "--split-string" || arg.startsWith("--split-string=")) return true;
    if (ENV_FLAGS_WITH_VALUE.has(arg)) {
      index++;
      continue;
    }
    if (arg.startsWith("-")) continue;
    if (arg.includes("=") && !arg.startsWith("=")) continue; // a NAME=value assignment
    return true;
  }
  return false;
}

function gitIsReadOnly(args: readonly string[]): boolean {
  const [subcommand, rest] = splitGitSubcommand(args);
  if (subcommand === null) return false;
  if (GIT_READ_ONLY_SUBCOMMANDS.has(subcommand)) return !hasFlag(rest, GIT_UNSAFE_SUBCOMMAND_FLAGS);
  const flags = GIT_LISTING_SUBCOMMANDS[subcommand];
  if (flags === undefined) return false;
  if (hasFlag(rest, flags)) return false;
  const readFlags = GIT_LISTING_READ_FLAGS[subcommand] ?? new Set<string>();
  if (rest.some((arg) => readFlags.has(arg.split("=")[0] as string))) return true;
  const operands = rest.filter((arg) => !arg.startsWith("-"));
  if (operands.length === 0) return !GIT_LISTING_REQUIRES_OPERAND.has(subcommand);
  return (GIT_LISTING_OPERANDS[subcommand] ?? new Set()).has(operands[0] as string);
}

/** Step over git's global options to reach the subcommand and its arguments.
 * No subcommand at all when an unsafe global option is in the way. */
function splitGitSubcommand(args: readonly string[]): [string | null, readonly string[]] {
  let index = 0;
  while (index < args.length) {
    const arg = args[index] as string;
    if (GIT_UNSAFE_GLOBAL_FLAGS.has(arg.split("=")[0] as string)) return [null, []];
    if (GIT_GLOBAL_FLAGS_WITH_VALUE.has(arg)) {
      index += 2;
      continue;
    }
    if (arg.startsWith("-")) {
      index++;
      continue;
    }
    return [arg, args.slice(index + 1)];
  }
  return [null, []];
}

/**
 * Command-line entry point.
 *
 * `hx`                 launch the TUI in the current directory
 * `hx -p "..."`        print mode: run one prompt headless, stream to stdout
 * `hx resume [id]`     resume a session
 * `hx prompt`          print the resolved system prompt
 * `hx trace [id]`      write a session to a self-contained HTML page
 * `hx mcp ...`         manage MCP servers
 * `hx docs [section]`  print the shipped manual
 * `hx changelog [ver]` print what shipped in each version
 * `hx upgrade`         self-update
 *
 * Print mode exists so the harness is scriptable and E2E-testable without
 * driving a terminal UI.
 */

import { type Layer, loadSettings } from "./config.ts";
import { loadInstructions, resolveSystemPrompt } from "./core/context.ts";
import { changelog, DocsUnavailable, docText, manual, README } from "./docs.ts";
import { absolute, expandUser, readText } from "./pathlib.ts";
import { osErrorText, purePath, repr, strip } from "./py.ts";
import { VERSION } from "./version.ts";

export const USAGE = `\
hx - an agent harness for the terminal

Usage:
  hx                        Start the interactive TUI
  hx -p, --print PROMPT     Run one prompt headlessly and print the result
  hx resume [SESSION_ID]    Resume a previous session
  hx prompt                 Print the system prompt this directory would use
  hx trace [SESSION_ID] [PATH]
                            Write a session to a self-contained HTML trace
  hx mcp list|add|remove|login|logout
                            Manage MCP servers, and sign in to remote ones
  hx auth [set|clear]       Show or change the OpenRouter API key
  hx docs [SECTION|--all]   Print the manual, or one section of it
  hx changelog [VERSION]    Print what shipped in each version
  hx upgrade                Update hx to the latest version
  hx --version              Show version
  hx --help                 Show this message

Options:
  --model MODEL             Override the model for this run
  --mode MODE               plan | default | acceptEdits | bypass
  --cwd PATH                Run against a different project directory
  --no-sandbox              Disable OS sandboxing (permission rules still apply)
  --system-prompt TEXT      Replace the system prompt (@path reads a file)
  --append-system-prompt TEXT
                            Append to the system prompt; repeatable (@path reads a file)
  --image PATH              Attach an image to the -p prompt; repeatable
`;

/** Commands that print a shipped document and exit, taking no run options. */
const DOC_COMMANDS = new Set(["docs", "changelog"]);

/**
 * Commands with flags of their own, so their arguments are taken as typed.
 *
 * `hx mcp add jira --url URL` and `hx mcp add fs npx -y server` are the
 * documented forms; read as run options, `--url` and `-y` would be refused as
 * unknown before the command ever saw them.
 */
const VERBATIM_COMMANDS = new Set([...DOC_COMMANDS, "mcp", "auth"]);

export interface ParsedArgs {
  command: string;
  prompt: string;
  sessionId: string | null;
  rest: string[];
  overrides: Layer | null;
  cwd: string | null;
  images: string[];
}

export class UsageError extends Error {
  override name = "UsageError";
}

/** `print()`: the text and a newline. */
function print(text: string, stream: NodeJS.WriteStream = process.stdout): void {
  stream.write(`${text}\n`);
}

/** Parse arguments and dispatch. Returns a process exit code. */
export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  const args = [...argv];
  if (args.length && VERBATIM_COMMANDS.has(args[0] as string)) {
    // `hx mcp add fs npx server --help` is a server's argument, not a
    // request for HX's usage.
    return dispatch(args);
  }

  if (args.includes("--version") || args.includes("-V")) {
    print(`hx ${VERSION}`);
    return 0;
  }
  if (args.includes("--help") || args.includes("-h")) {
    print(USAGE);
    return 0;
  }

  return dispatch(args);
}

async function dispatch(args: string[]): Promise<number> {
  let parsed: ParsedArgs;
  try {
    parsed = parseArgs(args);
  } catch (error) {
    if (!(error instanceof UsageError)) throw error;
    print(`error: ${error.message}\n\n${USAGE}`, process.stderr);
    return 2;
  }

  switch (parsed.command) {
    case "prompt":
      return runPromptCommand(parsed);
    case "docs":
      return runDocsCommand(parsed.rest);
    case "changelog":
      return runChangelogCommand(parsed.rest);
    default:
      return notPorted(parsed.command);
  }
}

/**
 * The commands still to come across from the Python build. Said plainly, so a
 * half-finished build can never be mistaken for a working one.
 */
function notPorted(command: string): number {
  print(`error: \`hx ${command}\` is not ported to the TypeScript build yet`, process.stderr);
  return 70;
}

export function parseArgs(args: string[]): ParsedArgs {
  const parsed: ParsedArgs = {
    command: "tui",
    prompt: "",
    sessionId: null,
    rest: [],
    overrides: null,
    cwd: null,
    images: [],
  };

  // The documentation and management commands take their arguments verbatim:
  // `hx docs --all` is a request for a section of the manual, and
  // `hx mcp add jira --url URL` is for `hx mcp`, not a run of HX with an
  // unknown option.
  if (args.length && VERBATIM_COMMANDS.has(args[0] as string)) {
    return { ...parsed, command: args[0] as string, rest: args.slice(1) };
  }

  const overrides: Record<string, Record<string, unknown>> = {};
  const group = (name: string) => {
    overrides[name] ??= {};
    return overrides[name];
  };
  const positional: string[] = [];

  for (let index = 0; index < args.length; index++) {
    const arg = args[index] as string;
    const value = (message: string): string => {
      index += 1;
      if (index >= args.length) throw new UsageError(message);
      return args[index] as string;
    };
    if (arg === "-p" || arg === "--print") {
      parsed.command = "print";
      parsed.prompt = value("-p requires a prompt");
    } else if (arg === "--model") {
      group("models").model = value("--model requires a value");
    } else if (arg === "--mode") {
      group("permissions").mode = value("--mode requires a value");
    } else if (arg === "--cwd") {
      parsed.cwd = purePath(expandUser(value("--cwd requires a path")));
    } else if (arg === "--no-sandbox") {
      group("permissions").sandbox = false;
    } else if (arg === "--system-prompt") {
      group("prompt").system = promptValue(value("--system-prompt requires a value"));
    } else if (arg === "--append-system-prompt") {
      const raw = value("--append-system-prompt requires a value");
      const prompt = group("prompt");
      prompt.append ??= [];
      (prompt.append as string[]).push(promptValue(raw));
    } else if (arg === "--image") {
      // Against where the command was typed, not --cwd: that is where the
      // shell completed the path.
      parsed.images.push(absolute(expandUser(value("--image requires a path"))));
    } else if (arg.startsWith("-")) {
      throw new UsageError(`unknown option ${arg}`);
    } else {
      positional.push(arg);
    }
  }

  if (positional.length) {
    const [head, ...tail] = positional as [string, ...string[]];
    if (["resume", "prompt", "trace", "mcp", "upgrade", "auth"].includes(head)) {
      parsed.command = head;
      parsed.rest = tail;
      if (head === "resume" && tail.length) parsed.sessionId = tail[0] as string;
    } else if (parsed.command !== "print") {
      throw new UsageError(`unknown command ${repr(head)}`);
    }
  }

  if (parsed.images.length && parsed.command !== "print") {
    throw new UsageError("--image goes with -p; in the TUI, paste or drag the image in");
  }
  parsed.overrides = Object.keys(overrides).length ? overrides : null;
  return parsed;
}

/**
 * A prompt flag's value. `@path` reads the file, anything else is literal.
 *
 * A missing file is a usage error, not an empty prompt: silently running with
 * the built-in prompt after the user asked for theirs is the worse failure.
 */
function promptValue(raw: string): string {
  if (!raw.startsWith("@")) return raw;
  const path = purePath(expandUser(raw.slice(1)));
  try {
    return readText(path);
  } catch (error) {
    throw new UsageError(`cannot read prompt file ${path}: ${osErrorText(error)}`);
  }
}

/**
 * `hx prompt` - print the system prompt this directory resolves to.
 *
 * The prompt goes to stdout so it can be piped or diffed; where it came from,
 * and which AGENTS.md files ride alongside it, goes to stderr so it never
 * contaminates that output.
 */
function runPromptCommand(parsed: ParsedArgs): number {
  const settings = loadSettings(parsed.cwd, parsed.overrides);
  const resolved = resolveSystemPrompt(settings.cwd, settings.prompt);
  const instructions = loadInstructions(settings.cwd);

  print(resolved.text);
  print(`[source] ${resolved.source}`, process.stderr);
  for (const append of resolved.appends) print(`[append] ${append}`, process.stderr);
  for (const loaded of instructions) print(`[instructions] ${loaded.path}`, process.stderr);
  return 0;
}

/**
 * `hx docs [SECTION|--all]` - the manual that shipped with this version.
 *
 * Bare `hx docs` lists the sections rather than printing the whole manual: it
 * is thousands of tokens, and the caller is usually a session answering one
 * question about HX itself.
 */
function runDocsCommand(args: string[]): number {
  try {
    if (args.length && (args[0] === "--all" || args[0] === "-a")) print(strip(docText(README)));
    else print(manual(args.length ? args.join(" ") : null));
  } catch (error) {
    if (!(error instanceof DocsUnavailable)) throw error;
    print(`error: ${error.message}`, process.stderr);
    return 1;
  }
  return 0;
}

/**
 * `hx changelog [VERSION]` - what shipped in each version.
 *
 * `VERSION` accepts `0.1.5`, `v0.1.5`, `unreleased` or `latest`.
 */
function runChangelogCommand(args: string[]): number {
  try {
    print(changelog(args[0] ?? null));
  } catch (error) {
    if (!(error instanceof DocsUnavailable)) throw error;
    print(`error: ${error.message}`, process.stderr);
    return 1;
  }
  return 0;
}

/**
 * Symbols tool: AST-accurate outline, definition and reference lookup.
 *
 * Tree-sitter rather than a language server: it parses one file in
 * microseconds, which is the right trade for the questions an agent actually
 * asks - *where is this defined* and *who calls it*. It cannot resolve types,
 * so `references` matches by name; that is still far tighter than a grep,
 * which cannot tell a call from the same word in a comment or a string.
 *
 * The grammars come from `@vscode/tree-sitter-wasm`, which is not installed
 * with HX. The tool registers only when it resolves - see `available` - so its
 * schema never sits in the cached prefix advertising something that cannot run.
 */

import { readFileSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname } from "node:path";
import { glob, parts } from "../glob.ts";
import { compareCodePoints } from "../json.ts";
import { expandUser, isDir, isFile, join } from "../pathlib.ts";
import { osErrorText, purePath, repr, strip, toInt } from "../py.ts";
import { count } from "../text.ts";
import {
  type Params,
  Tool,
  type ToolContext,
  ToolError,
  type ToolResult,
  toolResult,
} from "./base.ts";
import { IGNORED_DIRS } from "./glob.ts";

const DESCRIPTION = `Find code structure without reading whole files.

- \`outline\` - the shape of one file: classes, functions, methods and their
  line spans. Cheaper than reading the file when you only need its map.
- \`definition\` - where a symbol is defined, across the project.
- \`references\` - where a symbol is used. Matches by name against the parse
  tree, so comments and strings never match, but an unrelated symbol with the
  same name in another file does.

Prefer this over Grep for anything named. Fall back to Grep for free text.`;

/** Node types that count as a use of a name: Python's `identifier`, and the
 * property, field and type identifiers the other grammars name uses with. */
const REFERENCE_NODES = new Set([
  "identifier",
  "property_identifier",
  "field_identifier",
  "type_identifier",
  "shorthand_property_identifier",
  "shorthand_property_identifier_pattern",
]);

/** Deepest AST level walked: a long `a + b + ...` chain nests once per term. */
const MAX_DEPTH = 300;
const DEFAULT_LIMIT = 100;
/** Files above this are skipped. A generated bundle is not worth parsing. */
const MAX_FILE_BYTES = 2_000_000;

/** Everything language-specific, so a new language is one table entry. */
interface LanguageSpec {
  readonly grammar: string;
  /** Node types that introduce a name worth listing. */
  readonly definitions: ReadonlySet<string>;
}

const PY: LanguageSpec = {
  grammar: "python",
  definitions: new Set(["function_definition", "class_definition"]),
};
const TS: LanguageSpec = {
  grammar: "typescript",
  definitions: new Set([
    "function_declaration",
    "class_declaration",
    "method_definition",
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
  ]),
};
const JS: LanguageSpec = {
  grammar: "javascript",
  definitions: new Set(["function_declaration", "class_declaration", "method_definition"]),
};
// `type_spec`, not `type_declaration`: the name lives on the spec, and a
// grouped `type ( A ...; B ... )` has one declaration and two specs.
const GO: LanguageSpec = {
  grammar: "go",
  definitions: new Set(["function_declaration", "method_declaration", "type_spec"]),
};
const RUST: LanguageSpec = {
  grammar: "rust",
  definitions: new Set([
    "function_item",
    "struct_item",
    "enum_item",
    "trait_item",
    "impl_item",
    "mod_item",
    "type_item",
  ]),
};

export const LANGUAGES: Record<string, LanguageSpec> = {
  ".py": PY,
  ".pyi": PY,
  ".ts": TS,
  ".tsx": { grammar: "tsx", definitions: TS.definitions },
  ".js": JS,
  ".jsx": JS,
  ".mjs": JS,
  ".cjs": JS,
  ".go": GO,
  ".rs": RUST,
};

const PACKAGE = "@vscode/tree-sitter-wasm";

/** The parts of web-tree-sitter this tool uses. */
interface TsNode {
  readonly type: string;
  readonly text: string;
  readonly startIndex: number;
  readonly endIndex: number;
  readonly startPosition: { row: number };
  readonly endPosition: { row: number };
  readonly children: (TsNode | null)[];
  childForFieldName(name: string): TsNode | null;
}
interface TsParser {
  setLanguage(language: unknown): void;
  parse(source: string): { rootNode: TsNode } | null;
}
interface TreeSitter {
  Parser: { init(options?: object): Promise<void>; new (): TsParser };
  Language: { load(path: string): Promise<unknown> };
}

const load = createRequire(import.meta.url);

/** Where the package lives, or `null` when it is not installed. */
function packageDir(): string | null {
  try {
    return dirname(load.resolve(PACKAGE));
  } catch {
    return null;
  }
}

/** Whether the optional grammars can be loaded. */
export const available = () => packageDir() !== null;

let runtime: Promise<TreeSitter> | null = null;
const parsers = new Map<string, Promise<TsParser>>();

function parserFor(grammar: string): Promise<TsParser> {
  let parser = parsers.get(grammar);
  if (parser === undefined) {
    parser = (async () => {
      const directory = packageDir();
      if (directory === null)
        throw new ToolError(`tree-sitter is not installed: npm install -g ${PACKAGE}`);
      runtime ??= (async () => {
        const module = load(PACKAGE) as TreeSitter;
        await module.Parser.init({ locateFile: (file: string) => join(directory, file) });
        return module;
      })();
      const treeSitter = await runtime;
      const language = await treeSitter.Language.load(
        join(directory, `tree-sitter-${grammar}.wasm`),
      );
      const instance = new treeSitter.Parser();
      instance.setLanguage(language);
      return instance;
    })();
    parsers.set(grammar, parser);
  }
  return parser;
}

export class SymbolsTool extends Tool {
  readonly name = "Symbols";
  readonly description = DESCRIPTION;

  schema() {
    return {
      type: "object",
      properties: {
        mode: { type: "string", enum: ["outline", "definition", "references"] },
        symbol: {
          type: "string",
          description: "Name to look up. Required for definition and references.",
        },
        file_path: {
          type: "string",
          description: "File to outline, or one file to restrict the search to",
        },
        path: { type: "string", description: "Directory to search (default cwd)" },
        limit: { type: "integer", default: DEFAULT_LIMIT },
      },
      required: ["mode"],
    };
  }

  /** The file or folder read, so a path rule covers it. */
  override permissionSpecifier(params: Params): string | null {
    for (const key of ["file_path", "path"])
      if (typeof params[key] === "string" && params[key]) return params[key] as string;
    return null;
  }

  async run(params: Params, ctx: ToolContext): Promise<ToolResult> {
    const mode = String(params.mode);
    const limit = toInt(params.limit || DEFAULT_LIMIT);
    if (mode === "outline") {
      const raw = params.file_path;
      if (!raw) throw new ToolError("outline needs file_path");
      return outline(resolveFile(String(raw), ctx.cwd), limit);
    }
    const symbol = String(params.symbol || "");
    if (!symbol) throw new ToolError(`${mode} needs symbol`);
    return search(mode, symbol, params, ctx, limit);
  }
}

async function outline(path: string, limit: number): Promise<ToolResult> {
  const spec = specFor(path);
  const root = await parse(path, spec);
  const rows: string[] = [];
  // `depth` is how far to indent, `level` how far down the tree.
  const walk = (node: TsNode, depth: number, level: number) => {
    if (rows.length >= limit || level > MAX_DEPTH) return;
    let childDepth = depth;
    if (spec.definitions.has(node.type)) {
      const name = nameOf(node);
      if (name !== null) {
        const [start, end] = [node.startPosition.row + 1, node.endPosition.row + 1];
        rows.push(`${"  ".repeat(depth)}${kind(node.type)} ${name}  [${start}-${end}]`);
        childDepth = depth + 1;
      }
    }
    for (const child of node.children) if (child !== null) walk(child, childDepth, level + 1);
  };
  walk(root, 0, 0);
  if (!rows.length) return toolResult(`${path}: no definitions found`, { summary: "0 symbols" });
  return toolResult(`${path}\n${rows.join("\n")}`, { summary: count(rows.length, "symbol") });
}

async function search(
  mode: string,
  symbol: string,
  params: Params,
  ctx: ToolContext,
  limit: number,
): Promise<ToolResult> {
  const hits: string[] = [];
  let truncated = false;
  for (const path of candidates(params, ctx)) {
    if (hits.length >= limit) {
      truncated = true;
      break;
    }
    let rows: [number, string][];
    try {
      const spec = specFor(path);
      const root = await parse(path, spec);
      rows = matches(root, spec, symbol, mode, ctx.signal);
    } catch (error) {
      // An unparseable or unsupported file is not a failure of the search -
      // the other files still have the answer.
      if (error instanceof ToolError) continue;
      throw error;
    }
    for (const [line, text] of rows) {
      hits.push(`${path}:${line}: ${strip(text)}`);
      if (hits.length >= limit) {
        truncated = true;
        break;
      }
    }
  }
  if (!hits.length) {
    const where = mode === "definition" ? "definition" : "reference";
    return toolResult(`No ${where} of ${repr(symbol)} found.`, { summary: "0 hits" });
  }
  let body = hits.join("\n");
  if (truncated) body += `\n\n... stopped at ${count(limit, "hit")} (raise limit to see more)`;
  return toolResult(body, { summary: count(hits.length, "hit") });
}

/**
 * Walk once, collecting the name nodes that match. `definition` wants the
 * name node of a definition; `references` every other identifier with that
 * text - a mention in a comment or a string is not an identifier node.
 */
function matches(
  root: TsNode,
  spec: LanguageSpec,
  symbol: string,
  mode: string,
  signal: AbortSignal,
): [number, string][] {
  if (signal.aborted) throw signal.reason;
  const lines = sourceOf(root).split("\n");
  const found: [number, string][] = [];
  const row = (index: number): [number, string] => [index + 1, lines[index] ?? ""];
  const visit = (node: TsNode, level: number) => {
    if (level > MAX_DEPTH) return;
    if (spec.definitions.has(node.type)) {
      const nameNode = node.childForFieldName("name");
      if (nameNode !== null && nameNode.text === symbol) {
        if (mode === "definition") found.push(row(node.startPosition.row));
        // Everything except the name itself, so a definition never counts as
        // a reference to itself. Compared by span: each access is a new wrapper.
        for (const child of node.children)
          if (
            child !== null &&
            (child.startIndex !== nameNode.startIndex || child.endIndex !== nameNode.endIndex)
          )
            visit(child, level + 1);
        return;
      }
    } else if (mode === "references" && REFERENCE_NODES.has(node.type) && node.text === symbol) {
      found.push(row(node.startPosition.row));
    }
    for (const child of node.children) if (child !== null) visit(child, level + 1);
  };
  visit(root, 0);
  return found;
}

const sourceOf = (root: TsNode) => root.text;

/** Files to search, newest first, reusing Glob's ignore set. */
function candidates(params: Params, ctx: ToolContext): string[] {
  if (params.file_path) return [resolveFile(String(params.file_path), ctx.cwd)];
  let root = purePath(expandUser(String(params.path || ctx.cwd)));
  if (!root.startsWith("/")) root = purePath(join(ctx.cwd, root));
  if (!isDir(root)) throw new ToolError(`${root}: not a directory`);
  const found = glob(root, "**/*").filter(
    (path) =>
      LANGUAGES[suffix(path)] !== undefined &&
      isFile(path) &&
      !parts(path).some((part) => IGNORED_DIRS.has(part)),
  );
  const times = new Map(found.map((path) => [path, mtime(path)]));
  return found.sort((a, b) => (times.get(b) as number) - (times.get(a) as number));
}

function mtime(path: string): number {
  try {
    return statSync(path).mtimeMs;
  } catch {
    return 0;
  }
}

function resolveFile(raw: string, cwd: string): string {
  const path = purePath(expandUser(raw));
  const resolved = path.startsWith("/") ? path : purePath(join(cwd, path));
  if (!isFile(resolved)) throw new ToolError(`${resolved}: no such file`);
  return resolved;
}

/** `Path(path).suffix`. */
function suffix(path: string): string {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const index = name.lastIndexOf(".");
  return index > 0 && index < name.length - 1 ? name.slice(index) : "";
}

function specFor(path: string): LanguageSpec {
  const spec = LANGUAGES[suffix(path)];
  if (spec === undefined) {
    const known = Object.keys(LANGUAGES).sort(compareCodePoints).join(", ");
    throw new ToolError(
      `${suffix(path) || path.slice(path.lastIndexOf("/") + 1)}: unsupported language. Known: ${known}`,
    );
  }
  return spec;
}

async function parse(path: string, spec: LanguageSpec): Promise<TsNode> {
  let size: number;
  try {
    size = statSync(path).size;
  } catch (error) {
    throw new ToolError(`${path}: ${osErrorText(error)}`);
  }
  if (size > MAX_FILE_BYTES) throw new ToolError(`${path}: ${size} bytes, too large to parse`);
  // Undecodable bytes read as U+FFFD, as Python's errors="replace" shows them.
  const source = new TextDecoder("utf-8", { ignoreBOM: true }).decode(readFileSync(path));
  const parser = await parserFor(spec.grammar);
  const tree = parser.parse(source);
  if (tree === null) throw new ToolError(`${path}: could not be parsed`);
  return tree.rootNode;
}

function nameOf(node: TsNode): string | null {
  // Rust `impl` blocks name a type rather than a symbol; the type field keeps
  // the block in the outline instead of dropping it.
  const nameNode = node.childForFieldName("name") ?? node.childForFieldName("type");
  return nameNode === null ? null : nameNode.text;
}

/** `function_definition` -> `function`. Good enough across grammars. */
function kind(nodeType: string): string {
  for (const end of ["_definition", "_declaration", "_item", "_spec"])
    if (nodeType.endsWith(end)) return nodeType.slice(0, -end.length);
  return nodeType;
}

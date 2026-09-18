"""Symbols tool: AST-accurate outline, definition and reference lookup.

Tree-sitter rather than a language server. A server answers richer questions -
types, cross-file renames through re-exports - but it has to be installed per
language, supervised per session, and indexed before it can answer anything.
Tree-sitter parses one file in microseconds with a grammar that ships as a
wheel, which is the right trade for the questions an agent actually asks:
*where is this defined* and *who calls it*.

What it cannot do is resolve types, so ``references`` matches by name. That is
still far tighter than a grep, which cannot tell a call from the same word in a
comment or a string.

The tool registers only when the optional dependency imports - see
:func:`available`. Its schema would otherwise sit in the cached prefix
advertising something that cannot run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.glob import IGNORED_DIRS

if TYPE_CHECKING:
    from tree_sitter import Node

DESCRIPTION = """Find code structure without reading whole files.

- `outline` - the shape of one file: classes, functions, methods and their
  line spans. Cheaper than reading the file when you only need its map.
- `definition` - where a symbol is defined, across the project.
- `references` - where a symbol is used. Matches by name against the parse
  tree, so comments and strings never match, but an unrelated symbol with the
  same name in another file does.

Prefer this over Grep for anything named. Fall back to Grep for free text."""

REFERENCE_NODES = frozenset(
    {
        "identifier",
        "property_identifier",
        "field_identifier",
        "type_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
    }
)
"""Node types that count as a use of a name.

Only Python spells every usage site ``identifier``. TS/JS put a method call's
name in ``property_identifier``, Go and Rust use ``field_identifier`` for fields
and methods and ``type_identifier`` wherever a type is named. Matching only
``identifier`` answered "no references" for `client.connect()`, which is the one
answer worse than a grep's false positives."""

MAX_DEPTH = 300
"""Deepest AST level walked.

A tree-sitter tree nests once per term of an expression, so a long `a + b + ...`
chain is deep enough to exhaust CPython's stack. ``RecursionError`` is not a
``ToolError``, so one such file would abort a whole project search instead of
being skipped; stopping short is the smaller loss."""

DEFAULT_LIMIT = 100
MAX_FILE_BYTES = 2_000_000
"""Files above this are skipped. A generated bundle is not worth parsing."""


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """Everything language-specific, so a new language is one table entry."""

    grammar: str
    definitions: frozenset[str]
    """Node types that introduce a name worth listing."""


_PY = LanguageSpec("python", frozenset({"function_definition", "class_definition"}))
_TS = LanguageSpec(
    "typescript",
    frozenset(
        {
            "function_declaration",
            "class_declaration",
            "method_definition",
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
        }
    ),
)
_JS = LanguageSpec(
    "javascript",
    frozenset({"function_declaration", "class_declaration", "method_definition"}),
)
_GO = LanguageSpec(
    "go",
    # ``type_spec``, not ``type_declaration``: the declaration is the ``type``
    # keyword and its parenthesised group, and the name lives on the spec inside
    # it. A grouped `type ( A ...; B ... )` has one declaration and two specs.
    frozenset({"function_declaration", "method_declaration", "type_spec"}),
)
_RUST = LanguageSpec(
    "rust",
    frozenset(
        {
            "function_item",
            "struct_item",
            "enum_item",
            "trait_item",
            "impl_item",
            "mod_item",
            "type_item",
        }
    ),
)

LANGUAGES: dict[str, LanguageSpec] = {
    ".py": _PY,
    ".pyi": _PY,
    ".ts": _TS,
    ".tsx": LanguageSpec("tsx", _TS.definitions),
    ".js": _JS,
    ".jsx": _JS,
    ".mjs": _JS,
    ".cjs": _JS,
    ".go": _GO,
    ".rs": _RUST,
}


def available() -> bool:
    """Whether the optional tree-sitter dependency is importable."""
    try:
        import tree_sitter_language_pack  # noqa: F401
    except ImportError:
        return False
    return True


class SymbolsTool(Tool):
    name = "Symbols"
    description = DESCRIPTION
    mutating = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["outline", "definition", "references"],
                },
                "symbol": {
                    "type": "string",
                    "description": "Name to look up. Required for definition and references.",
                },
                "file_path": {
                    "type": "string",
                    "description": "File to outline, or one file to restrict the search to",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search (default cwd)",
                },
                "limit": {"type": "integer", "default": DEFAULT_LIMIT},
            },
            "required": ["mode"],
        }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Parsing a tree is CPU work on a thread rather than the event loop, for
        # the same reason every other filesystem tool does it: the TUI is mid-render.
        return await asyncio.to_thread(self._run, params, ctx)

    def _run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        mode = str(params["mode"])
        limit = int(params.get("limit") or DEFAULT_LIMIT)

        if mode == "outline":
            raw = params.get("file_path")
            if not raw:
                raise ToolError("outline needs file_path")
            return self._outline(_resolve(str(raw), ctx.cwd), limit)

        symbol = str(params.get("symbol") or "")
        if not symbol:
            raise ToolError(f"{mode} needs symbol")
        return self._search(mode, symbol, params, ctx, limit)

    def _outline(self, path: Path, limit: int) -> ToolResult:
        spec = _spec_for(path)
        tree, source = _parse(path, spec)
        rows: list[str] = []

        def walk(node: Node, depth: int, level: int) -> None:
            # ``depth`` is how far to indent, ``level`` how far down the tree we
            # actually are - only the second one can exhaust the stack.
            if len(rows) >= limit or level > MAX_DEPTH:
                return
            child_depth = depth
            if node.type in spec.definitions:
                name = _name_of(node, source)
                if name is not None:
                    start, end = node.start_point[0] + 1, node.end_point[0] + 1
                    kind = _kind(node.type)
                    rows.append(f"{'  ' * depth}{kind} {name}  [{start}-{end}]")
                    child_depth = depth + 1
            for child in node.children:
                walk(child, child_depth, level + 1)

        walk(tree.root_node, 0, 0)
        if not rows:
            return ToolResult(content=f"{path}: no definitions found", summary="0 symbols")
        body = f"{path}\n" + "\n".join(rows)
        return ToolResult(content=body, summary=f"{len(rows)} symbols")

    def _search(
        self, mode: str, symbol: str, params: dict[str, Any], ctx: ToolContext, limit: int
    ) -> ToolResult:
        hits: list[str] = []
        truncated = False

        for path in _candidates(params, ctx):
            if len(hits) >= limit:
                truncated = True
                break
            try:
                spec = _spec_for(path)
                tree, source = _parse(path, spec)
                rows = _matches(tree.root_node, source, spec, symbol, mode)
            except (ToolError, RecursionError):
                # An unparseable or unsupported file is not a failure of the
                # search - the other files still have the answer. A tree too deep
                # to walk is the same kind of nothing.
                continue
            for line, text in rows:
                hits.append(f"{path}:{line}: {text.strip()}")
                if len(hits) >= limit:
                    truncated = True
                    break

        if not hits:
            where = "definition" if mode == "definition" else "reference"
            return ToolResult(content=f"No {where} of {symbol!r} found.", summary="0 hits")

        body = "\n".join(hits)
        if truncated:
            body += f"\n\n... stopped at {limit} hits (raise limit to see more)"
        return ToolResult(content=body, summary=f"{len(hits)} hits")


def _matches(
    root: Node, source: bytes, spec: LanguageSpec, symbol: str, mode: str
) -> list[tuple[int, str]]:
    """Walk once, collecting the name nodes that match.

    ``definition`` wants the name node of a definition; ``references`` wants
    every other identifier with that text, which is what separates this from a
    grep - a mention inside a comment or a string literal is not an identifier
    node and never appears here.
    """
    lines = source.split(b"\n")
    found: list[tuple[int, str]] = []
    wanted = symbol.encode("utf-8")

    def text_of(node: Node) -> bytes:
        return source[node.start_byte : node.end_byte]

    def visit(node: Node, level: int) -> None:
        if level > MAX_DEPTH:
            return
        if node.type in spec.definitions:
            name_node = node.child_by_field_name("name")
            if name_node is not None and text_of(name_node) == wanted:
                if mode == "definition":
                    found.append(_row(node.start_point[0], lines))
                # Descend into everything except the name itself, so a
                # definition never counts as a reference to itself. Compare by
                # byte span: tree-sitter hands back a fresh wrapper object on
                # every access, so ``is`` compares two wrappers, not two nodes.
                for child in node.children:
                    if _span(child) != _span(name_node):
                        visit(child, level + 1)
                return
        elif mode == "references" and node.type in REFERENCE_NODES and text_of(node) == wanted:
            found.append(_row(node.start_point[0], lines))

        for child in node.children:
            visit(child, level + 1)

    visit(root, 0)
    return found


def _span(node: Node) -> tuple[int, int]:
    """Identity for a node. See the comment in :func:`_matches`."""
    return node.start_byte, node.end_byte


def _row(index: int, lines: list[bytes]) -> tuple[int, str]:
    raw = lines[index] if index < len(lines) else b""
    return index + 1, raw.decode("utf-8", errors="replace")


def _candidates(params: dict[str, Any], ctx: ToolContext) -> list[Path]:
    """Files to search, newest first, reusing Glob's ignore set."""
    single = params.get("file_path")
    if single:
        return [_resolve(str(single), ctx.cwd)]

    root = Path(params.get("path") or ctx.cwd).expanduser()
    if not root.is_absolute():
        root = ctx.cwd / root
    if not root.is_dir():
        raise ToolError(f"{root}: not a directory")

    found = [
        path
        for path in root.rglob("*")
        if path.suffix in LANGUAGES
        and path.is_file()
        and not any(part in IGNORED_DIRS for part in path.parts)
    ]
    found.sort(key=lambda p: _mtime(p), reverse=True)
    return found


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _resolve(raw: str, cwd: Path) -> Path:
    path = Path(raw).expanduser()
    resolved = path if path.is_absolute() else (cwd / path)
    if not resolved.is_file():
        raise ToolError(f"{resolved}: no such file")
    return resolved


def _spec_for(path: Path) -> LanguageSpec:
    spec = LANGUAGES.get(path.suffix)
    if spec is None:
        known = ", ".join(sorted(LANGUAGES))
        raise ToolError(f"{path.suffix or path.name}: unsupported language. Known: {known}")
    return spec


def _parse(path: Path, spec: LanguageSpec) -> tuple[Any, bytes]:
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:  # pragma: no cover - guarded by available()
        raise ToolError("tree-sitter is not installed: pip install 'hx-cli[symbols]'") from exc

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ToolError(f"{path}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise ToolError(f"{path}: {size} bytes, too large to parse")

    source = path.read_bytes()
    parser = get_parser(spec.grammar)
    return parser.parse(source), source


def _name_of(node: Node, source: bytes) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        # Rust `impl` blocks name a type rather than a symbol; fall back to the
        # type field so the outline shows the block instead of dropping it.
        name_node = node.child_by_field_name("type")
    if name_node is None:
        return None
    return source[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")


def _kind(node_type: str) -> str:
    """``function_definition`` -> ``function``. Good enough across grammars."""
    for suffix in ("_definition", "_declaration", "_item", "_spec"):
        if node_type.endswith(suffix):
            return node_type[: -len(suffix)]
    return node_type

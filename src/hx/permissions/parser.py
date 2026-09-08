"""Shell command decomposition for permission matching.

Matching a rule against the whole command string is unsafe: ``git status && rm
-rf /`` would match an ``allow: Bash(git status:*)`` rule. Every command is
split into its individual executed segments and each segment must pass on its
own.

This is a permission-matching aid, not a shell. When it cannot decompose a
command with confidence it says so, and the caller must treat that as "ask".
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

OPERATORS = ("&&", "||", ";;", ";", "|&", "|", "\n", "&")

DYNAMIC_EXECUTABLES = frozenset({"eval", "exec", "source", ".", "env"})
"""Commands whose real payload is decided at runtime. They cannot be decomposed,
so a command invoking one is reported unparseable rather than guessed at."""

REDIRECT_TOKENS = (">>", ">", "<")

READ_ONLY_COMMANDS = frozenset(
    {
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
        "echo",
        "printf",
        "date",
        "whoami",
        "uname",
        "which",
        "type",
        "grep",
        "rg",
        "find",
        "fd",
        "diff",
        "tree",
        "basename",
        "dirname",
        "sort",
        "uniq",
        "cut",
        "awk",
        "sed",
        "jq",
        "column",
    }
)
"""Commands that do not mutate state on their own. Used only to relax prompting."""

READ_ONLY_SUBCOMMANDS = {
    "git": frozenset({"status", "log", "diff", "show", "branch", "remote", "blame", "describe"}),
}


@dataclass(slots=True)
class CommandSegment:
    """One command that will actually execute."""

    raw: str
    executable: str
    args: tuple[str, ...]
    in_substitution: bool = False
    """True for ``$(...)`` / backtick content, which executes just as readily."""


@dataclass(slots=True)
class ParsedCommand:
    raw: str
    segments: tuple[CommandSegment, ...]
    has_redirect_out: bool = False
    redirect_targets: tuple[str, ...] = ()
    unparseable: bool = False
    """Set when the command could not be decomposed with confidence. Callers must
    treat this as "ask", never as "allow"."""


def parse(command: str) -> ParsedCommand:
    """Split on ``&&``, ``||``, ``;``, ``|``, newlines, and command substitution,
    respecting quoting.

    Anything not understood sets ``unparseable`` rather than degrading to a
    naive split - a wrong split here is a real escape.
    """
    try:
        pieces, substitutions, ok = _scan(command)
    except _ScanError:
        return ParsedCommand(raw=command, segments=(), unparseable=True)

    segments: list[CommandSegment] = []
    redirects: list[str] = []
    unparseable = not ok

    for text, in_sub in [*[(p, False) for p in pieces], *[(s, True) for s in substitutions]]:
        segment, targets, segment_ok = _to_segment(text, in_sub)
        redirects.extend(targets)
        if not segment_ok:
            unparseable = True
        if segment is not None:
            segments.append(segment)
            if segment.executable in DYNAMIC_EXECUTABLES:
                unparseable = True

    return ParsedCommand(
        raw=command,
        segments=tuple(segments),
        has_redirect_out=bool(redirects),
        redirect_targets=tuple(redirects),
        unparseable=unparseable or not segments,
    )


def _to_segment(text: str, in_sub: bool) -> tuple[CommandSegment | None, list[str], bool]:
    stripped = text.strip()
    if not stripped:
        return None, [], True

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return None, [], False
    if not tokens:
        return None, [], True

    words: list[str] = []
    redirects: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in REDIRECT_TOKENS or (token[:1].isdigit() and token[1:] in REDIRECT_TOKENS):
            index += 1
            if index < len(tokens):
                redirects.append(tokens[index])
            index += 1
            continue
        for marker in REDIRECT_TOKENS:
            if marker in token and not token.startswith(marker):
                head, _, tail = token.partition(marker)
                words.append(head)
                if tail:
                    redirects.append(tail)
                break
        else:
            words.append(token)
        index += 1

    words = [word for word in words if word]
    if not words:
        return None, redirects, True

    executable = words[0]
    # A name produced by expansion is only known at runtime.
    ok = "$" not in executable and "`" not in executable
    return (
        CommandSegment(
            raw=stripped,
            executable=executable,
            args=tuple(words[1:]),
            in_substitution=in_sub,
        ),
        redirects,
        ok,
    )


class _ScanError(Exception):
    pass


def _scan(text: str) -> tuple[list[str], list[str], bool]:
    """Split into top-level pieces and extracted substitution bodies.

    Returns ``(pieces, substitutions, ok)``. ``ok`` is False when quoting is
    unbalanced or a substitution never closes.
    """
    pieces: list[str] = []
    substitutions: list[str] = []
    buffer: list[str] = []
    ok = True

    index = 0
    length = len(text)
    quote: str | None = None

    while index < length:
        char = text[index]

        if char == "\\" and quote != "'":
            buffer.append(text[index : index + 2])
            index += 2
            continue

        if quote:
            if char == quote:
                quote = None
            # Substitutions are live inside double quotes.
            elif quote == '"' and (text.startswith("$(", index) or char == "`"):
                body, consumed, closed = _read_substitution(text, index)
                substitutions.extend(_recurse(body, substitutions))
                ok = ok and closed
                index += consumed
                continue
            buffer.append(char)
            index += 1
            continue

        if char in "'\"":
            quote = char
            buffer.append(char)
            index += 1
            continue

        if text.startswith("$(", index) or char == "`":
            body, consumed, closed = _read_substitution(text, index)
            substitutions.extend(_recurse(body, substitutions))
            ok = ok and closed
            index += consumed
            continue

        if char in "()":
            # Subshell grouping: the contents run at this level.
            index += 1
            continue

        operator = next((op for op in OPERATORS if text.startswith(op, index)), None)
        if operator:
            pieces.append("".join(buffer))
            buffer.clear()
            index += len(operator)
            continue

        buffer.append(char)
        index += 1

    if quote:
        ok = False
    pieces.append("".join(buffer))
    return [piece for piece in pieces if piece.strip()], substitutions, ok


def _recurse(body: str, seen: list[str]) -> list[str]:
    """Substitution bodies are themselves commands; flatten them one level deep."""
    inner_pieces, inner_subs, _ = _scan(body)
    return [*inner_pieces, *[s for s in inner_subs if s not in seen]]


def _read_substitution(text: str, index: int) -> tuple[str, int, bool]:
    """Read a ``$(...)`` or backtick substitution starting at ``index``."""
    if text[index] == "`":
        end = text.find("`", index + 1)
        if end == -1:
            return text[index + 1 :], len(text) - index, False
        return text[index + 1 : end], end - index + 1, True

    depth = 0
    cursor = index + 1  # points at "("
    while cursor < len(text):
        if text[cursor] == "(":
            depth += 1
        elif text[cursor] == ")":
            depth -= 1
            if depth == 0:
                return text[index + 2 : cursor], cursor - index + 1, True
        cursor += 1
    return text[index + 2 :], len(text) - index, False


def match_specifier(segment: CommandSegment, pattern: str) -> bool:
    """Match a segment against a rule specifier such as ``git commit:*`` or ``ls``.

    ``:*`` is a prefix match on the whole command line; a bare executable name
    matches any invocation of it.
    """
    if pattern in {"*", ":*"}:
        return True

    command_line = " ".join([segment.executable, *segment.args])
    if pattern.endswith(":*"):
        prefix = pattern[:-2].strip()
        return command_line == prefix or command_line.startswith(prefix + " ")
    if " " in pattern:
        return command_line == pattern
    return segment.executable == pattern


def is_read_only(segment: CommandSegment) -> bool:
    """Heuristic allowlist of non-mutating commands (``ls``, ``cat``, ``git status``).

    Used only to relax prompting, never to grant something an explicit deny rule
    covers.
    """
    if subcommands := READ_ONLY_SUBCOMMANDS.get(segment.executable):
        return bool(segment.args) and segment.args[0] in subcommands
    return segment.executable in READ_ONLY_COMMANDS

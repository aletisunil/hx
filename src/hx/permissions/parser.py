"""Shell command decomposition for permission matching.

Matching a rule against the whole command string is unsafe: ``git status && rm
-rf /`` would match an ``allow: Bash(git status:*)`` rule. Every command is
split into its individual executed segments and each segment must pass on its
own.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    raise NotImplementedError


def match_specifier(segment: CommandSegment, pattern: str) -> bool:
    """Match a segment against a rule specifier such as ``git commit:*`` or ``ls``.

    ``:*`` is a prefix match on the argument vector; a bare executable name
    matches any invocation of it.
    """
    raise NotImplementedError


def is_read_only(segment: CommandSegment) -> bool:
    """Heuristic allowlist of non-mutating commands (``ls``, ``cat``, ``git status``).

    Used only to relax prompting, never to grant something an explicit deny rule
    covers.
    """
    raise NotImplementedError

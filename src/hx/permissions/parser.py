"""Shell command decomposition for permission matching.

Matching a rule against the whole command string is unsafe: ``git status && rm
-rf /`` would match an ``allow: Bash(git status:*)`` rule. Every command is
split into its individual executed segments and each segment must pass on its
own.

This is a permission-matching aid, not a shell. When it cannot decompose a
command with confidence it says so, and the caller must treat that as "ask".
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

OPERATORS = ("&&", "||", ";;", ";", "|&", "|", "\n", "&")

DYNAMIC_EXECUTABLES = frozenset({"eval", "exec", "source", ".", "env"})
"""Commands whose real payload is decided at runtime. They cannot be decomposed,
so a command invoking one is reported unparseable rather than guessed at.

``env`` qualifies only when it is launching something - see :func:`_is_dynamic`.
"""

REDIRECT_TOKENS = (">>", ">", "<")

_FD_DUP_RE = re.compile(r"^\d*>&\d*-?$")
"""``2>&1``, ``>&2``, ``>&-`` - duplicating a descriptor, never a file write."""

_OUT_OP_RE = re.compile(r"^(?:\d*>>?|&>>?)$")
_IN_OP_RE = re.compile(r"^\d*<<?-?$")
_OUT_ATTACHED_RE = re.compile(r"^(?:\d*>>?|&>>?)(?P<target>.+)$")
_IN_ATTACHED_RE = re.compile(r"^\d*<<?-?(?P<target>.+)$")

DISCARD_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
"""Redirect targets that discard or re-route output rather than writing a file.
``cmd 2>/dev/null`` is the most common shape an agent emits; prompting for it
teaches users to approve without reading."""

READ_ONLY_COMMANDS = frozenset(
    {
        # Inspecting the filesystem
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
        # Searching
        "grep",
        "rg",
        "find",
        "fd",
        "diff",
        "cmp",
        "comm",
        # Text through a pipe
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
        # Digests
        "md5sum",
        "sha1sum",
        "sha256sum",
        "shasum",
        "cksum",
        # Asking the machine about itself
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
        # Shell scaffolding that produces no effect of its own
        "test",
        "[",
        "true",
        "false",
        "sleep",
    }
)
"""Commands that do not mutate state on their own. Used only to relax prompting.

Membership is not the whole answer: several of these write when given the right
flag, which is what :data:`MUTATING_FLAGS` covers. Deliberately absent are the
wrappers whose payload is another command - ``xargs``, ``time``, ``watch``,
``nohup``, ``command`` - since their safety is the payload's, not their own.
"""

MUTATING_FLAGS: dict[str, frozenset[str]] = {
    "sed": frozenset({"-i", "--in-place"}),
    "awk": frozenset({"-i", "--include"}),
    "find": frozenset(
        {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
    ),
    "fd": frozenset({"-x", "--exec", "-X", "--exec-batch"}),
    "sort": frozenset({"-o", "--output"}),
    "tree": frozenset({"-o"}),
    "date": frozenset({"-s", "--set"}),
    "rg": frozenset({"--pre"}),
    "yq": frozenset({"-i", "--inplace"}),
}
"""Flags that turn a read-only command into a writing or executing one.

``sed`` edits in place under ``-i``, ``find`` runs arbitrary programs under
``-exec`` and unlinks under ``-delete``, ``sort -o`` overwrites its own input.
Treating the executable name alone as read-only allowed all three through
unprompted.
"""

GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {
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
    }
)
"""Git subcommands that read whatever arguments they are given."""

GIT_UNSAFE_SUBCOMMAND_FLAGS = frozenset({"--output", "-O", "--open-files-in-pager"})
"""Flags that make an otherwise read-only git subcommand write or execute.

``git diff --output=FILE`` (also on ``log`` and ``show``) writes that file, and
``git grep -O<cmd>`` / ``--open-files-in-pager=<cmd>`` runs that program over
the matches. Reading the subcommand name alone let both through unprompted.
"""

GIT_LISTING_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "branch": frozenset(
        {
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
        }
    ),
    "tag": frozenset(
        {"-d", "--delete", "-a", "--annotate", "-s", "--sign", "-f", "--force", "-m", "-F"}
    ),
    "config": frozenset(
        {
            "--add",
            "--unset",
            "--unset-all",
            "--replace-all",
            "--rename-section",
            "--remove-section",
            "-e",
            "--edit",
        }
    ),
    "remote": frozenset(),
    "worktree": frozenset(),
    "submodule": frozenset(),
    "notes": frozenset(),
    "stash": frozenset(),
    "reflog": frozenset(),
    "bisect": frozenset(),
}
"""Git subcommands that list when bare and mutate when steered.

``git branch`` prints branches; ``git branch -D old`` deletes one. The value is
the set of flags that make the difference, so the read-only reading is withdrawn
the moment one appears.
"""

GIT_LISTING_OPERANDS: dict[str, frozenset[str]] = {
    "remote": frozenset({"show", "get-url"}),
    "worktree": frozenset({"list"}),
    "submodule": frozenset({"status"}),
    "notes": frozenset({"list", "show"}),
    "stash": frozenset({"list", "show"}),
    "reflog": frozenset({"show"}),
    "bisect": frozenset({"log"}),
}
"""The nested subcommand each listing accepts and stays read-only - the rest of
them (``remote add``, ``stash drop``) are mutations wearing the same prefix."""

GIT_LISTING_READ_FLAGS: dict[str, frozenset[str]] = {
    "config": frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list"}),
    "branch": frozenset(
        {"-l", "--list", "--contains", "--no-contains", "--points-at", "--merged", "--no-merged"}
    ),
    "tag": frozenset(
        {"-l", "--list", "-n", "--contains", "--no-contains", "--points-at", "--merged"}
    ),
}
"""Flags that read explicitly, so their operands are a query rather than a value
to write: ``git config --get user.name`` reads, ``git config user.name x``
writes; ``git tag -l 'v*'`` filters, ``git tag v1`` creates."""

GIT_LISTING_REQUIRES_OPERAND = frozenset({"stash"})
"""Bare ``git stash`` stashes the working tree. Unlike its siblings it is not a
listing at all, so it only reads when it says ``list`` or ``show``."""

GIT_GLOBAL_FLAGS_WITH_VALUE = frozenset({"-C", "-c", "--namespace", "--config-env", "--exec-path"})
"""Options that sit before the subcommand and swallow the next word, so that
``git -C /repo status`` is still recognised as ``status``."""

GIT_UNSAFE_GLOBAL_FLAGS = frozenset(
    {"-c", "--config-env", "--exec-path", "--git-dir", "--work-tree"}
)
"""Global options that decide what a read-only subcommand actually runs.

``git -c core.pager=<anything> log`` executes that string; ``-c alias.x='!cmd'``
and ``--exec-path`` do the same by other routes, and ``--git-dir`` /
``--work-tree`` move the repository being read out from under the rule that
allowed it. None of them can be stepped over, so their presence withdraws the
read-only reading entirely rather than being skipped to reach the subcommand.
"""

ENV_FLAGS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir"})
"""``env`` options taking a separate value, which is therefore not a command."""


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
            if _is_dynamic(segment):
                unparseable = True

    return ParsedCommand(
        raw=command,
        segments=tuple(segments),
        has_redirect_out=any(target not in DISCARD_TARGETS for target in redirects),
        redirect_targets=tuple(redirects),
        unparseable=unparseable or not segments,
    )


def _is_dynamic(segment: CommandSegment) -> bool:
    """True when the segment's real payload is only known at runtime.

    ``env`` is in :data:`DYNAMIC_EXECUTABLES` for the shape that deserves it -
    ``env FOO=1 <command>`` - and not for the far more common bare ``env``,
    which prints the environment and used to be reported unparseable, so it
    prompted every single time.
    """
    if segment.executable == "env":
        return _env_runs_a_command(segment.args)
    return segment.executable in DYNAMIC_EXECUTABLES


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

    words, redirects = _split_redirections(tokens)
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


def _split_redirections(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Separate the words that make up the command from its redirections.

    A redirection contributes no word - ``ls -la 2>err`` is ``ls -la``, not
    ``ls -la 2`` - and only an *output* redirection is reported: ``2>&1``
    duplicates a descriptor and ``< in`` reads, so neither writes anything.

    Returns ``(words, output_targets)``.
    """
    words: list[str] = []
    targets: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1

        if _FD_DUP_RE.match(token):
            continue

        if _OUT_OP_RE.match(token) or _IN_OP_RE.match(token):
            # `2> err`: the target is the next token, and only `>` forms write.
            if index < len(tokens):
                if _OUT_OP_RE.match(token):
                    targets.append(tokens[index])
                index += 1
            continue

        if attached := _OUT_ATTACHED_RE.match(token):
            targets.append(attached.group("target"))
            continue

        if _IN_ATTACHED_RE.match(token):
            continue

        if (embedded := _embedded_redirect(token)) is not None:
            head, target, writes = embedded
            words.append(head)
            if writes:
                targets.append(target)
            continue

        words.append(token)

    return [word for word in words if word], targets


def _embedded_redirect(token: str) -> tuple[str, str, bool] | None:
    """``echo hi>out`` - a redirection glued to the end of a word.

    Returns ``(word, target, writes)``, or ``None`` when the token holds no
    redirection. Leading forms such as ``2>err`` are matched earlier and never
    reach here.
    """
    position = min((token.find(char) for char in "<>" if char in token), default=-1)
    if position <= 0:
        return None
    head, rest = token[:position], token[position:]
    target = rest.lstrip("<>")
    if not target:
        return None
    return head, target, rest.startswith(">")


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
        if operator == "&" and _is_redirect_ampersand(text, index, buffer):
            buffer.append(char)
            index += 1
            continue
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


def _is_redirect_ampersand(text: str, index: int, buffer: list[str]) -> bool:
    """True for the ``&`` in ``2>&1``, ``>&2`` or ``&>log``.

    Splitting there would invent a phantom command (``1``) out of a descriptor
    number and make an obviously read-only command look unrecognisable.
    """
    if "".join(buffer).rstrip().endswith(">"):
        return True
    return text.startswith("&>", index)


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

    The executable name alone does not settle it. ``sed`` reads and ``sed -i``
    rewrites; ``git branch`` lists and ``git branch -D`` deletes. Arguments are
    inspected wherever the same name covers both.

    Used only to relax prompting, never to grant something an explicit deny rule
    covers.
    """
    if segment.executable == "git":
        return _git_is_read_only(segment.args)
    if segment.executable not in READ_ONLY_COMMANDS:
        return False
    if segment.executable == "env":
        return not _env_runs_a_command(segment.args)
    if segment.executable in PROGRAM_TEXT_COMMANDS and _program_text_writes(segment):
        return False
    return not _has_flag(
        segment.args,
        MUTATING_FLAGS.get(segment.executable, frozenset()),
        cluster_first_only=segment.executable in ATTACHED_VALUE_COMMANDS,
    )


PROGRAM_TEXT_COMMANDS = frozenset({"awk", "sed"})
"""Commands whose payload is a little program, not just flags and paths.

Their flags are only half the question: ``awk 'BEGIN{system("rm -rf x")}'`` and
``sed -n 'w /tmp/out' f`` write and execute with no flag involved at all, so the
program itself has to be read before the command counts as read-only.
"""

AWK_WRITES = re.compile(r"""system\s*\(|\|\s*["']|["']\s*\||print[f]?[^;}\n]*>""")
"""An ``awk`` program reaching outside itself: a subshell, an output redirection
on a ``print``, or a pipeline to a command. ``$1 > 5`` is a comparison and is
deliberately not matched - only a ``>`` in the same statement as a ``print``."""

PROGRAM_TEXT_OPTIONS: dict[str, tuple[str, ...]] = {
    "awk": ("-e", "--source"),
    "sed": ("-e", "--expression"),
}
"""Options whose value is more of the program, so it has to be read as one."""

PROGRAM_FILE_OPTIONS: dict[str, tuple[str, ...]] = {
    "awk": ("-f", "--file", "-E", "--exec"),
    "sed": ("-f", "--file"),
}
"""Options that load the program from a file.

Its text is not on the command line, so there is nothing here to read and no
grounds to call the command read-only: ``awk -f prog.awk`` runs whatever
``prog.awk`` says, ``system()`` included.
"""


def _program_text_writes(segment: CommandSegment) -> bool:
    """True when the program handed to ``awk``/``sed`` can write or execute."""
    if _has_option(segment.args, PROGRAM_FILE_OPTIONS[segment.executable]):
        return True  # the program is in a file, and cannot be judged from here
    operands = tuple(arg for arg in segment.args if not arg.startswith("-"))
    inline = tuple(_option_values(segment.args, PROGRAM_TEXT_OPTIONS[segment.executable]))
    if segment.executable == "awk":
        # Every operand: with ``-F ,`` the separator is an operand too, so the
        # program is not reliably the first one.
        return any(AWK_WRITES.search(text) for text in operands + inline)
    # sed's script is the value of each -e, or - with no -e at all - the first
    # operand. Not both: once -e carries the script, the operands are files.
    scripts = inline or operands[:1]
    return any(_sed_script_writes(script) for script in scripts)


def _sed_script_writes(script: str) -> bool:
    """True when a ``sed`` script contains a ``w``/``W`` write or an ``e`` execute.

    Walked rather than searched, because the letters are only commands in
    command position: ``s/e/x/`` replaces the letter e and writes nothing, while
    ``s/a/b/w out`` and ``/pat/w out`` both write.
    """
    index, end = 0, len(script)
    while index < end:
        char = script[index]
        if char in " \t\n;{}":
            index += 1
        elif char == "/":  # an address regex
            index = _skip_to_delim(script, index + 1, "/")
        elif char == "\\" and index + 1 < end:  # an address with a custom delimiter
            index = _skip_to_delim(script, index + 2, script[index + 1])
        elif char.isdigit() or char in ",$!+~":  # the rest of an address
            index += 1
        elif char in "wWe":
            return True
        elif char in "sy" and index + 1 < end:
            delim = script[index + 1]
            index = _skip_to_delim(script, index + 2, delim)  # past the pattern
            index = _skip_to_delim(script, index, delim)  # past the replacement
            flags_end = index
            while flags_end < end and script[flags_end] not in ";}\n":
                flags_end += 1
            if set(script[index:flags_end]) & {"w", "e"}:
                return True
            index = flags_end
        else:  # any other command: skip to the end of the statement
            while index < end and script[index] not in ";}\n":
                index += 1
    return False


def _skip_to_delim(text: str, index: int, delim: str) -> int:
    """Scan forward to the closing ``delim``, honouring backslash escapes."""
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == delim:
            return index + 1
        index += 1
    return len(text)


def _split_option(arg: str, options: tuple[str, ...]) -> tuple[str, str | None] | None:
    """Match ``arg`` against ``options``, returning it and any value packed into it.

    Three spellings reach the same option and a shell accepts all three:
    separate (``-e`` then the script), ``=``-attached (``--expression=…``) and,
    for short options, packed into the same word (``-e'w out'``). Missing the
    last one let a writing script through unread.
    """
    if arg in options:
        return arg, None
    name, sep, value = arg.partition("=")
    if sep and name in options:
        return name, value
    if len(arg) > 2 and arg.startswith("-") and not arg.startswith("--") and arg[:2] in options:
        return arg[:2], arg[2:]
    return None


def _has_option(args: tuple[str, ...], options: tuple[str, ...]) -> bool:
    """True when any of ``options`` is present, in any of its spellings."""
    return any(_split_option(arg, options) is not None for arg in args)


def _option_values(args: tuple[str, ...], options: tuple[str, ...]) -> list[str]:
    """The values given to ``options``, in any of their spellings."""
    values: list[str] = []
    index = 0
    while index < len(args):
        matched = _split_option(args[index], options)
        index += 1
        if matched is None:
            continue
        _, attached = matched
        if attached is not None:
            values.append(attached)
        elif index < len(args):
            values.append(args[index])
            index += 1
    return values


ATTACHED_VALUE_COMMANDS = frozenset({"date"})
"""Commands whose short options swallow the rest of their word as a value.

``date -Iseconds`` is one option and its argument, not a cluster of five, so
only its first letter may be read as a flag - otherwise the ``s`` of
``seconds`` matches ``date -s`` and a pure formatting call prompts every time.
"""


def _has_flag(
    args: tuple[str, ...], flags: frozenset[str], *, cluster_first_only: bool = False
) -> bool:
    """True when any argument is one of ``flags``.

    Three spellings have to land on the same answer, because a shell accepts all
    three: separate (``-i``), attached (``-i.bak``, ``--in-place=x``) and
    clustered (``sed -ni``). Only two-character flags take part in the attached
    and clustered readings - ``find``'s options are single-dash words, and
    scanning ``-depth`` for the letters of ``-delete`` would refuse commands that
    write nothing.

    ``cluster_first_only`` narrows the clustered reading to the first letter, for
    commands where the rest of the word is that option's value rather than more
    options - see :data:`ATTACHED_VALUE_COMMANDS`.
    """
    if not flags:
        return False
    for arg in args:
        if arg == "--":
            break  # everything after this is an operand, not an option
        if arg.split("=", 1)[0] in flags:
            return True
        if not arg.startswith("-") or arg.startswith("--"):
            continue
        cluster = _leading_letters(arg[1:])
        if cluster_first_only:
            cluster = cluster[:1]
        if any(len(flag) == 2 and flag[1] in cluster for flag in flags):
            return True
    return False


def _leading_letters(text: str) -> str:
    """The run of option letters at the head of a cluster - ``i`` of ``-i.bak``."""
    end = 0
    while end < len(text) and text[end].isalpha():
        end += 1
    return text[:end]


def _env_runs_a_command(args: tuple[str, ...]) -> bool:
    """True when ``env`` is being used to launch something rather than to print.

    ``env`` and ``env | grep PATH`` dump the environment and are as harmless as
    ``printenv``; ``env FOO=1 rm -rf /`` is an ``rm``. Only the second reading is
    undecomposable, so only the second one is withheld.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            return index < len(args)
        if arg in {"-S", "--split-string"} or arg.startswith("--split-string="):
            return True  # the payload is a command packed into one word
        if arg in ENV_FLAGS_WITH_VALUE:
            index += 1
            continue
        if arg.startswith("-"):
            continue
        if "=" in arg and not arg.startswith("="):
            continue  # a NAME=value assignment, not the command
        return True
    return False


def _git_is_read_only(args: tuple[str, ...]) -> bool:
    subcommand, rest = _split_git_subcommand(args)
    if subcommand is None:
        return False
    if subcommand in GIT_READ_ONLY_SUBCOMMANDS:
        return not _has_flag(rest, GIT_UNSAFE_SUBCOMMAND_FLAGS)

    flags = GIT_LISTING_SUBCOMMANDS.get(subcommand)
    if flags is None:
        return False
    if _has_flag(rest, flags):
        return False

    read_flags = GIT_LISTING_READ_FLAGS.get(subcommand, frozenset())
    if any(arg.split("=", 1)[0] in read_flags for arg in rest):
        return True

    operands = [arg for arg in rest if not arg.startswith("-")]
    if not operands:
        return subcommand not in GIT_LISTING_REQUIRES_OPERAND
    return operands[0] in GIT_LISTING_OPERANDS.get(subcommand, frozenset())


def _split_git_subcommand(args: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    """Step over git's global options to reach the subcommand and its arguments.

    Returns no subcommand at all when one of :data:`GIT_UNSAFE_GLOBAL_FLAGS` is
    in the way: those decide what the subcommand runs, so there is nothing safe
    left to recognise.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.split("=", 1)[0] in GIT_UNSAFE_GLOBAL_FLAGS:
            return None, ()
        if arg in GIT_GLOBAL_FLAGS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg, args[index + 1 :]
    return None, ()

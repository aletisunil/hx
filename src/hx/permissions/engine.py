"""Permission rule engine.

Rules are ``Tool(specifier)`` strings - ``Bash(git commit:*)``, ``Edit(src/**)``,
``Read(~/.ssh/**)``. Evaluation order is deny, then ask, then allow: an explicit
deny can never be overridden by a broader allow, including in bypass mode.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from hx.config import PermissionMode
from hx.permissions.parser import CommandSegment, is_read_only, match_specifier, parse


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class GrantScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    """Persisted to ``.hx/settings.local.json``."""


DEFAULT_MUTATING_TOOLS = frozenset({"Bash", "Write", "Edit", "MultiEdit", "Task", "NotebookEdit"})
"""Fallback when the caller does not supply the registry's own metadata."""


@dataclass(slots=True)
class Rule:
    tool: str
    specifier: str | None
    decision: Decision
    source: str
    """Where the rule came from, so ``/permissions`` can show it."""


@dataclass(slots=True)
class PermissionRequest:
    tool_name: str
    specifier: str | None
    params: dict[str, Any]
    mutating: bool
    description: str
    detail: str = ""
    """Rendered diff or command text shown in the approval modal."""
    origin: str | None = None
    """Which subagent asked, when it was not the main conversation."""


@dataclass(slots=True)
class PermissionOutcome:
    decision: Decision
    reason: str
    matched_rule: Rule | None = None


@dataclass(slots=True)
class PermissionAnswer:
    allowed: bool
    scope: GrantScope = GrantScope.ONCE


Asker = Callable[[PermissionRequest], Awaitable[PermissionAnswer]]
"""Supplied by the frontend. Absent means nothing can answer an ASK."""


class PermissionEngine:
    #: Modes that never prompt for a file edit.
    EDIT_TOOLS: ClassVar[frozenset[str]] = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

    def __init__(
        self,
        mode: PermissionMode,
        rules: list[Rule],
        cwd: Path,
        asker: Asker | None = None,
    ) -> None:
        self.mode = mode
        self.rules = list(rules)
        self.cwd = cwd
        self.asker = asker
        self._session_grants: set[tuple[str, str | None]] = set()

    # --- decision ---------------------------------------------------------

    def evaluate(self, request: PermissionRequest) -> PermissionOutcome:
        """Pure decision - no prompting, no I/O. Ordering: deny, ask, allow, then
        the mode default.

        For Bash the specifier is decomposed by ``hx.permissions.parser`` and every
        segment must pass; an unparseable command degrades to ASK.
        """
        if denial := self._match(request, Decision.DENY):
            return PermissionOutcome(Decision.DENY, "matched a deny rule", denial)

        # Before the decomposition check, not after: an exact grant is the only
        # thing that can cover a command we cannot decompose, and it is safe
        # precisely because the approved string is byte-identical to what runs.
        if (request.tool_name, request.specifier) in self._session_grants:
            return PermissionOutcome(Decision.ALLOW, "granted for this session")

        if request.tool_name == "Bash" and request.specifier:
            parsed = parse(request.specifier)
            if parsed.unparseable:
                return PermissionOutcome(
                    Decision.ASK,
                    "command could not be decomposed; approving it blind is not safe",
                )

        if ask_rule := self._match(request, Decision.ASK):
            return PermissionOutcome(Decision.ASK, "matched an ask rule", ask_rule)

        if allow_rule := self._match_allow(request):
            return PermissionOutcome(Decision.ALLOW, "matched an allow rule", allow_rule)

        return self._mode_default(request)

    def _mode_default(self, request: PermissionRequest) -> PermissionOutcome:
        if self.mode is PermissionMode.BYPASS:
            return PermissionOutcome(Decision.ALLOW, "bypass mode")

        if self.mode is PermissionMode.PLAN:
            if request.mutating:
                return PermissionOutcome(
                    Decision.DENY, "plan mode is read-only; no writes or commands"
                )
            return PermissionOutcome(Decision.ALLOW, "read-only tool in plan mode")

        if not request.mutating:
            return PermissionOutcome(Decision.ALLOW, "read-only tool")

        if self.mode is PermissionMode.ACCEPT_EDITS and request.tool_name in self.EDIT_TOOLS:
            return PermissionOutcome(Decision.ALLOW, "acceptEdits mode")

        if request.tool_name == "Bash" and request.specifier:
            parsed = parse(request.specifier)
            if parsed.segments and all(is_read_only(s) for s in parsed.segments):
                if parsed.has_redirect_out:
                    return PermissionOutcome(Decision.ASK, "command redirects output to a file")
                return PermissionOutcome(Decision.ALLOW, "read-only command")

        return PermissionOutcome(Decision.ASK, "mutating tool")

    def _match(self, request: PermissionRequest, decision: Decision) -> Rule | None:
        for rule in self.rules:
            if rule.decision is not decision or rule.tool not in {request.tool_name, "*"}:
                continue
            if rule.specifier is None or self._specifier_matches(request, rule.specifier):
                return rule
        return None

    def _match_allow(self, request: PermissionRequest) -> Rule | None:
        """Allow requires *every* segment to be covered.

        Any-segment matching is right for deny (``safe && danger`` must be
        caught) but catastrophic for allow: an ``allow: Bash(git status:*)``
        rule would green-light ``git status && rm -rf /``. Each segment must
        find its own allow rule, substitutions included.
        """
        candidates = [
            rule
            for rule in self.rules
            if rule.decision is Decision.ALLOW and rule.tool in {request.tool_name, "*"}
        ]
        if not candidates:
            return None

        if request.tool_name != "Bash" or not request.specifier:
            for rule in candidates:
                if rule.specifier is None or self._specifier_matches(request, rule.specifier):
                    return rule
            return None

        # A rule holding the whole command verbatim: exact, so it grants no more
        # than the string it names. This is what covers a command that cannot be
        # decomposed, and what keeps hand-written full-command rules working.
        for rule in candidates:
            if rule.specifier == request.specifier:
                return rule

        parsed = parse(request.specifier)
        if not parsed.segments or parsed.unparseable:
            return None

        first: Rule | None = None
        for segment in parsed.segments:
            covering = next(
                (
                    rule
                    for rule in candidates
                    if rule.specifier is None or match_specifier(segment, rule.specifier)
                ),
                None,
            )
            if covering is None:
                return None
            first = first or covering
        return first

    def _specifier_matches(self, request: PermissionRequest, pattern: str) -> bool:
        if not request.specifier:
            return False

        if request.tool_name == "Bash":
            # Deny and ask: one dangerous segment is enough to trigger.
            parsed = parse(request.specifier)
            return any(match_specifier(segment, pattern) for segment in parsed.segments)

        return match_path(request.specifier, pattern, self.cwd)

    # --- prompting --------------------------------------------------------

    async def request(
        self,
        request: PermissionRequest,
        on_ask: Callable[[], None] | None = None,
    ) -> tuple[bool, str]:
        """Evaluate and, on ASK, prompt through :attr:`asker`.

        ``on_ask`` fires only when the call actually stops for approval, so a
        frontend can announce the prompt without announcing every auto-allowed
        call as well.

        Returns ``(allowed, reason)``. The reason is handed to the model on a
        denial so it can adapt, instead of guessing why a tool went silent.
        """
        outcome = self.evaluate(request)

        if outcome.decision is Decision.ALLOW:
            return True, outcome.reason
        if outcome.decision is Decision.DENY:
            return False, outcome.reason
        if self.asker is None:
            # Nothing can answer, so the only safe reading of ASK is "no".
            return False, (
                "this tool call needs approval, but this session has no way to ask "
                "(non-interactive). Run hx interactively, or add a permission rule."
            )

        if on_ask is not None:
            on_ask()
        answer = await self.asker(request)
        if answer.allowed:
            self.grant(request, answer.scope)
            return True, "approved by the user"
        return False, "the user declined"

    def grant(self, request: PermissionRequest, scope: GrantScope) -> None:
        """Record an approval.

        ``SESSION`` keeps the rules in memory; ``ALWAYS`` also writes them to
        the project's local settings. Both record the exact request as well, so a command
        that could not be decomposed is still covered for the rest of the
        session.
        """
        if scope is GrantScope.ONCE:
            return

        self._session_grants.add((request.tool_name, request.specifier))
        source = "session grant" if scope is GrantScope.SESSION else "local settings"
        for rule_text in persistable_rules(request.tool_name, request.specifier):
            self.rules.append(parse_rule(rule_text, source, Decision.ALLOW))
            if scope is GrantScope.ALWAYS:
                persist_allow_rule(rule_text, self.cwd)

    # --- modes ------------------------------------------------------------

    def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode

    def allowed_tools(self, all_tools: list[str], mutating: set[str] | None = None) -> set[str]:
        """Tools exposed in the current mode - plan mode hides mutating tools
        entirely rather than letting the model call them and be refused."""
        mutating_set = mutating if mutating is not None else set(DEFAULT_MUTATING_TOOLS)
        if self.mode is not PermissionMode.PLAN:
            return set(all_tools)
        return {name for name in all_tools if name not in mutating_set}


def persistable_rules(tool_name: str, specifier: str | None) -> list[str]:
    """Generalise one approval into the rule texts worth keeping.

    A Bash approval becomes one prefix rule per executed segment, never the
    exact argument vector: a whole-command specifier is matched against each
    segment on its own, so ``cd /repo && pytest -q`` stored verbatim would only
    ever match again as that same string - an "always allow" that allows almost
    nothing. Each segment therefore contributes ``executable subcommand:*``.

    Widening is only safe where there is a subcommand to anchor it to. A
    command that cannot be decomposed, and one whose segments carry nothing but
    flags, keep their exact text; guessing a prefix for either would grant more
    than the user saw.
    """
    if not specifier:
        return [tool_name]
    if tool_name != "Bash":
        return [f"{tool_name}({specifier})"]

    parsed = parse(specifier)
    if parsed.unparseable or not parsed.segments:
        return [f"Bash({specifier})"]

    rules: list[str] = []
    for segment in parsed.segments:
        prefix = _rule_prefix(segment)
        if prefix is None:
            return [f"Bash({specifier})"]
        text = f"Bash({prefix}:*)"
        if text not in rules:
            rules.append(text)
    return rules


def _rule_prefix(segment: CommandSegment) -> str | None:
    """``git commit -m x`` -> ``git commit``; ``rm -rf build`` -> ``None``.

    The subcommand is what keeps a prefix rule narrow. Falling back to the bare
    executable when there is none looks harmless on ``ls -la`` and is not:
    approving ``rm -rf build`` would write ``Bash(rm:*)`` and quietly cover
    every ``rm`` the user never saw. Those segments are not widened at all.
    """
    if segment.args and not segment.args[0].startswith("-"):
        return f"{segment.executable} {segment.args[0]}"
    return None


_RULE_RE = re.compile(r"^(?P<tool>[A-Za-z_][\w-]*|\*)(?:\((?P<spec>.*)\))?$", re.DOTALL)


def parse_rule(text: str, source: str, decision: Decision) -> Rule:
    """Parse ``Tool(specifier)`` or bare ``Tool``.

    Raises:
        InvalidRule: on malformed syntax - a rule that silently fails to parse is
            a rule that silently does not protect anything.
    """
    match = _RULE_RE.match(text.strip())
    if match is None:
        raise InvalidRule(f"malformed permission rule: {text!r}")
    specifier = match.group("spec")
    return Rule(
        tool=match.group("tool"),
        specifier=specifier.strip() if specifier else None,
        decision=decision,
        source=source,
    )


def match_path(candidate: str, pattern: str, cwd: Path) -> bool:
    """Glob-match a path specifier, with ``**`` spanning directory separators."""
    path = Path(candidate).expanduser()
    absolute = path if path.is_absolute() else (cwd / path)
    expanded = Path(pattern).expanduser()
    target = expanded if expanded.is_absolute() else (cwd / expanded)

    regex = _glob_regex(str(target))
    if regex.match(str(absolute)):
        return True

    # Also match against the cwd-relative form, so `Edit(src/**)` works.
    try:
        relative = absolute.relative_to(cwd)
    except ValueError:
        return False
    return bool(_glob_regex(pattern).match(str(relative)))


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob to a regex.

    ``**`` crosses directory separators and ``*`` does not, and crucially
    ``**/`` also matches *zero* directories - so ``**/.ssh/**`` protects
    ``.ssh/id_rsa`` sitting directly in the project, not only nested copies of
    it. ``fnmatch`` cannot express either rule.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:[\\s\\S]*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append("[\\s\\S]*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            close = pattern.find("]", index + 1)
            if close == -1:
                out.append(re.escape("["))
                index += 1
            else:
                out.append(pattern[index : close + 1])
                index = close + 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def load_rules(cwd: Path) -> list[Rule]:
    """Collect rules from every settings layer, narrowest last.

    Order only decides which source a rule is *reported* as when two layers say
    the same thing; evaluation is deny-then-ask-then-allow across the lot, so a
    local allow can never quietly override a project deny.
    """
    from hx.config import read_settings_file
    from hx.paths import project_local_settings_file, project_settings_file, user_settings_file

    rules: list[Rule] = []
    for path, source in (
        (user_settings_file(), "user settings"),
        (project_settings_file(cwd), "project settings"),
        (project_local_settings_file(cwd), "local settings"),
    ):
        permissions = read_settings_file(path).get("permissions") or {}
        for key, decision in (
            ("deny", Decision.DENY),
            ("ask", Decision.ASK),
            ("allow", Decision.ALLOW),
        ):
            for text in permissions.get(key) or []:
                rules.append(parse_rule(str(text), source, decision))
    return rules


def migrate_legacy_rules(cwd: Path) -> list[str]:
    """Widen allow rules that only ever match one exact command string.

    Until grants were generalised, "always allow" on a compound command stored
    the whole command as the specifier - ``Bash(cd /repo && pytest -x -q)``.
    Such a rule matches that string and nothing else, so changing a single flag
    prompts again, which is not what the user was offered when they pressed
    "always". Those rules are rewritten in place, once, into the per-segment
    prefix rules the same approval produces today.

    Single-segment rules are left alone: they are already usable, and widening
    a hand-written ``Bash(git status)`` would grant more than its author asked.

    Only the two files HX owns are rewritten - the user's and this machine's
    project layer, both under ``$HX_HOME``. The project's shared
    ``settings.json`` is read and reported on, never edited: it belongs to the
    repository, and a tool that quietly rewrites a checked-in file hands its
    user an unexplained diff.

    Returns one line per rule rewritten, naming the rule before and after.
    Editing a user's permission file is not something to do behind their back,
    so every change is spelled out rather than summarised as a count: the whole
    point of the file is that its owner can see what it grants.
    """
    from hx.config import ConfigError, read_settings_file, write_settings_file
    from hx.paths import project_local_settings_file, project_settings_file, user_settings_file

    shared = project_settings_file(cwd)
    notices: list[str] = []
    for path in (
        user_settings_file(),
        shared,
        project_local_settings_file(cwd),
    ):
        try:
            data = read_settings_file(path)
        except ConfigError:
            continue  # a broken settings file is reported elsewhere, not repaired here
        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            continue
        allow = permissions.get("allow")
        if not isinstance(allow, list):
            continue

        widened: list[str] = []
        changes: list[str] = []
        for entry in allow:
            replacements = _widen_rule(str(entry))
            if replacements is None:
                replacements = [str(entry)]
            else:
                changes.append(f"{entry} -> {', '.join(replacements)}")
            widened.extend(text for text in replacements if text not in widened)

        if not changes:
            continue
        listed = "\n".join(f"  {change}" for change in changes)

        if path == shared:
            notices.append(
                f"{path} has whole-command permission rules, which match one exact "
                f"command and nothing else:\n{listed}\n"
                "  It is your repository's file, so HX has not touched it - rewrite "
                "them yourself, or remove them and grant again."
            )
            continue

        permissions["allow"] = widened
        try:
            write_settings_file(path, data)
        except OSError:
            continue  # read-only settings are not worth failing startup over
        notices.append(f"Rewrote whole-command permission rules in {path}:\n{listed}")
    return notices


def _widen_rule(text: str) -> list[str] | None:
    """The per-segment rewrite for a whole-command rule, or ``None`` to keep it."""
    try:
        rule = parse_rule(text, "settings", Decision.ALLOW)
    except InvalidRule:
        return None
    if rule.tool != "Bash" or not rule.specifier:
        return None
    parsed = parse(rule.specifier)
    if parsed.unparseable or len(parsed.segments) < 2:
        return None
    replacements = persistable_rules("Bash", rule.specifier)
    # A rule with an unwidenable segment comes back as itself. That is not a
    # change, and reporting it as one would describe a rewrite that never
    # happened.
    return None if replacements == [text] else replacements


def persist_allow_rule(rule_text: str, cwd: Path) -> None:
    """Append an allow rule to this machine's settings for this project.

    Not the project's ``settings.json``: that file is shared configuration, and
    a grant is one person's decision on one machine - frequently spelling out
    absolute paths from their home directory. Writing there committed those
    decisions to everyone who cloned the repo.

    And not inside the repository at all. The local layer lives under
    ``$HX_HOME/projects/<slug>`` (see
    :func:`hx.paths.project_local_settings_file`), so answering a permission
    prompt never puts a file in the user's checkout for them to notice in a
    diff and wonder about - which also means HX no longer has any reason to
    write a ``.gitignore`` on their behalf.
    """
    from hx.config import read_settings_file, write_settings_file
    from hx.paths import project_local_settings_file

    path = project_local_settings_file(cwd)
    data = read_settings_file(path)
    permissions = data.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    if rule_text not in allow:
        allow.append(rule_text)
    write_settings_file(path, data)


class InvalidRule(Exception):
    pass

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
    """Persisted to project settings.json."""


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

        if request.tool_name == "Bash" and request.specifier:
            parsed = parse(request.specifier)
            if parsed.unparseable:
                return PermissionOutcome(
                    Decision.ASK,
                    "command could not be decomposed; approving it blind is not safe",
                )

        if (request.tool_name, request.specifier) in self._session_grants:
            return PermissionOutcome(Decision.ALLOW, "granted for this session")

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

    async def request(self, request: PermissionRequest) -> tuple[bool, str]:
        """Evaluate and, on ASK, prompt through :attr:`asker`.

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

        answer = await self.asker(request)
        if answer.allowed:
            self.grant(request, answer.scope)
            return True, "approved by the user"
        return False, "the user declined"

    def grant(self, request: PermissionRequest, scope: GrantScope) -> None:
        """Record an approval. ``ALWAYS`` writes a rule into project settings."""
        if scope is GrantScope.ONCE:
            return

        self._session_grants.add((request.tool_name, request.specifier))
        if scope is not GrantScope.ALWAYS:
            return

        specifier = self._persistable_specifier(request)
        rule_text = f"{request.tool_name}({specifier})" if specifier else request.tool_name
        self.rules.append(parse_rule(rule_text, "project settings", Decision.ALLOW))
        persist_allow_rule(rule_text, self.cwd)

    def _persistable_specifier(self, request: PermissionRequest) -> str | None:
        """Generalise a one-off approval into a rule worth keeping.

        A Bash approval becomes a prefix rule for that executable and
        subcommand, never the exact argument vector - otherwise "always allow"
        would be useless on the very next invocation.
        """
        if not request.specifier:
            return None
        if request.tool_name != "Bash":
            return request.specifier

        parsed = parse(request.specifier)
        if len(parsed.segments) != 1:
            return request.specifier
        segment = parsed.segments[0]
        head = _rule_prefix(segment)
        return f"{head}:*"

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


def _rule_prefix(segment: CommandSegment) -> str:
    """``git commit -m x`` -> ``git commit``; ``ls -la`` -> ``ls``."""
    if segment.args and not segment.args[0].startswith("-"):
        return f"{segment.executable} {segment.args[0]}"
    return segment.executable


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
    """Collect rules from user and project settings, project last."""
    from hx.config import read_settings_file
    from hx.paths import project_settings_file, user_settings_file

    rules: list[Rule] = []
    for path, source in (
        (user_settings_file(), "user settings"),
        (project_settings_file(cwd), "project settings"),
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


def persist_allow_rule(rule_text: str, cwd: Path) -> None:
    """Append an allow rule to the project settings file."""
    from hx.config import read_settings_file, write_settings_file
    from hx.paths import project_settings_file

    path = project_settings_file(cwd)
    data = read_settings_file(path)
    permissions = data.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    if rule_text not in allow:
        allow.append(rule_text)
    write_settings_file(path, data)


class InvalidRule(Exception):
    pass

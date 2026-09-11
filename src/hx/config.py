"""Layered configuration.

Precedence, lowest to highest::

    defaults
      < ~/.hx/settings.json
      < <cwd>/.hx/settings.json          (shared, check it in)
      < <cwd>/.hx/settings.local.json    (this machine only, never checked in)
      < environment
      < CLI flags

Settings are a plain dataclass so the whole config is hashable/serialisable and
can be diffed in tests. Permission rules are kept as raw strings here; parsing
into matchers is ``hx.permissions.engine``'s job.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from dataclasses import fields as dataclasses_fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from hx.paths import (
    PROJECT_DIR_NAME,
    legacy_project_gitignore_file,
    legacy_project_local_settings_file,
    project_local_settings_file,
    project_migrations_file,
    project_settings_file,
    user_settings_file,
)


class PermissionMode(StrEnum):
    """How aggressively HX asks before acting."""

    PLAN = "plan"
    """Read-only. No writes, no command execution."""

    DEFAULT = "default"
    """Ask before writes and command execution."""

    ACCEPT_EDITS = "acceptEdits"
    """Auto-approve file edits; still ask for command execution."""

    BYPASS = "bypass"
    """Approve everything. Sandbox still applies."""


@dataclass(frozen=True, slots=True)
class PermissionSettings:
    mode: PermissionMode = PermissionMode.DEFAULT
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    sandbox: bool = True
    """Use the OS sandbox when available (seatbelt / bubblewrap)."""
    allow_network: bool = False
    """Permit outbound network from sandboxed commands."""


@dataclass(frozen=True, slots=True)
class ContextSettings:
    compact_at: float = 0.80
    """Fraction of the model context window that triggers auto-compaction."""
    keep_recent_turns: int = 6
    """Turns kept verbatim across a compaction."""
    tool_output_char_cap: int = 25_000
    tool_output_line_cap: int = 2_000
    git_notices: bool = True
    """Late-inject the branch and files that changed on disk outside the session."""


@dataclass(frozen=True, slots=True)
class ModelSettings:
    model: str = "anthropic/claude-sonnet-4.5"
    subagent_model: str | None = None
    """Model used by subagents. Falls back to ``model``."""
    title_model: str | None = None
    """Model used to name a session. Falls back to ``model``."""
    max_tokens: int = 8192
    temperature: float | None = None
    reasoning_effort: str | None = None
    """How hard a reasoning model should think, for models that offer a choice.

    ``None`` - the default - leaves each model at the depth its vendor picked
    for it, which the Codex catalogue publishes per model. A value is clamped
    to what the chosen model advertises, so ``max`` runs at ``xhigh`` on a
    model that stops there rather than being refused.
    """
    codex_models: tuple[str, ...] = ()
    """Extra Codex model ids to offer, bare (``gpt-5.6-terra``).

    The account's real list comes from the Codex backend at sign-in, so this is
    an escape hatch rather than the usual way in: an id that account can call
    but the catalogue does not advertise. The ids are added to the ``/model``
    picker alongside the fetched ones.
    """


@dataclass(frozen=True, slots=True)
class PromptSettings:
    """System prompt overrides supplied as text.

    ``system`` replaces the built-in prompt outright; ``append`` is added after
    whichever prompt is in force. Overrides that live in files on disk are
    resolved in :mod:`hx.core.context` - only typed text reaches here, so a
    settings layer never carries a file's contents.
    """

    system: str | None = None
    append: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BashSettings:
    timeout_seconds: int = 120
    max_timeout_seconds: int = 600
    shell: str | None = None
    """Override the login shell used for the persistent Bash session."""


class EnterWhileBusy(StrEnum):
    """What Enter means while a turn is running."""

    QUEUE = "queue"
    """Hold the message until the turn ends. Alt+Enter steers instead."""

    STEER = "steer"
    """Put it into the running turn now. Alt+Enter queues instead."""


@dataclass(frozen=True, slots=True)
class TuiSettings:
    enter_while_busy: EnterWhileBusy = EnterWhileBusy.QUEUE
    """Queueing is the default because it cannot surprise anyone: a steer cuts
    off the model mid-sentence, which is worth asking for deliberately."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Fully resolved configuration for one HX session."""

    cwd: Path = field(default_factory=Path.cwd)
    models: ModelSettings = field(default_factory=ModelSettings)
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    bash: BashSettings = field(default_factory=BashSettings)
    prompt: PromptSettings = field(default_factory=PromptSettings)
    tui: TuiSettings = field(default_factory=TuiSettings)
    theme: str = "dark"
    quiet_startup: bool = False
    """Skip the startup header. For anyone who has read it already."""
    telemetry: bool = False


def load_settings(
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Resolve the settings layers into a single :class:`Settings`.

    Args:
        cwd: Project root. Defaults to the process working directory.
        overrides: CLI-flag layer, applied last.
    """
    root = (cwd or Path.cwd()).resolve()
    migrate_local_settings(root)
    layers = [
        read_settings_file(user_settings_file()),
        read_settings_file(project_settings_file(root)),
        read_settings_file(project_local_settings_file(root)),
        settings_from_env(dict(os.environ)),
        overrides or {},
    ]
    merged = merge_layers(layers)
    return _build_settings(merged, root)


def migrate_local_settings(root: Path) -> Path | None:
    """Move a repository's ``.hx/settings.local.json`` under the user's home.

    Earlier versions wrote permission grants into the checkout, along with a
    ``.hx/.gitignore`` whose only job was to hide them. Both are HX's files in
    somebody else's repository, so they are moved out on the next run rather
    than left for the user to find in a diff.

    Returns the new path when something moved, ``None`` otherwise. Best effort
    throughout: a read-only checkout is a reason to keep reading the old file,
    not to refuse to start.
    """
    legacy = legacy_project_local_settings_file(root)
    if not legacy.is_file():
        return None

    destination = project_local_settings_file(root)
    try:
        moving = read_settings_file(legacy)
    except ConfigError:
        # Unparseable. Leave it exactly where it is: deleting a file we cannot
        # read loses whatever the user had in it.
        return None

    try:
        # The destination wins where both exist - it is the newer location, and
        # anything already granted there was granted more recently.
        merged = merge_layers([moving, read_settings_file(destination)])
        write_settings_file(destination, merged)
    except (ConfigError, OSError):
        return None

    with contextlib.suppress(OSError):
        legacy.unlink()
    _drop_legacy_gitignore(root)
    _prune_empty_project_dir(root)
    return destination


#: Key in the migrations file for the one-time lift below.
LIFTED_PROJECT_GRANTS = "lifted_project_grants"


def lift_project_grants(root: Path) -> list[str] | None:
    """Move auto-written permission grants out of the project's shared settings.

    Versions before the local layer existed appended every "always allow" to
    ``./.hx/settings.json`` - the file a project checks in - so a developer's
    machine-local decisions, absolute home paths and all, ended up in the
    repository. Current HX never writes there, but the residue does not clear
    itself.

    Only ``permissions.allow`` moves. ``deny`` and ``ask`` stay exactly where
    they are: HX never wrote those, and they are the rules a project genuinely
    shares - quietly relocating a deny into one machine's settings would weaken
    a guardrail everyone else is relying on.

    Runs once, recorded in :func:`~hx.paths.project_migrations_file`, so a grant
    the user later writes into the shared file by hand is left alone.

    Returns the rules moved, or ``None`` when there was nothing to do.
    """
    if _migration_done(root, LIFTED_PROJECT_GRANTS):
        return None

    shared_path = project_settings_file(root)
    try:
        shared = read_settings_file(shared_path)
    except ConfigError:
        return None

    permissions = shared.get("permissions")
    if not isinstance(permissions, dict):
        _record_migration(root, LIFTED_PROJECT_GRANTS)
        return None

    grants = permissions.get("allow")
    if not isinstance(grants, list) or not grants:
        # Nothing to move, but the question is settled either way - and asking
        # it again on every start would reopen it the moment a rule is added.
        _record_migration(root, LIFTED_PROJECT_GRANTS)
        return None

    moved = [str(rule) for rule in grants]
    destination = project_local_settings_file(root)
    try:
        local = read_settings_file(destination)
    except ConfigError:
        return None
    local_permissions = local.setdefault("permissions", {})
    if not isinstance(local_permissions, dict):
        return None
    existing = local_permissions.get("allow")
    allow = list(existing) if isinstance(existing, list) else []
    allow.extend(rule for rule in moved if rule not in allow)
    local_permissions["allow"] = allow

    del permissions["allow"]
    if not permissions:
        del shared["permissions"]

    try:
        write_settings_file(destination, local)
        # The shared file is rewritten without the grants, or removed when they
        # were all it held - an empty settings file is clutter, not content.
        if shared:
            write_settings_file(shared_path, shared)
        else:
            shared_path.unlink()
    except OSError:
        return None

    _prune_empty_project_dir(root)
    _record_migration(root, LIFTED_PROJECT_GRANTS)
    return moved


def _prune_empty_project_dir(root: Path) -> None:
    """Remove ``./.hx`` once nothing is left in it.

    Only HX's own files were ever in there for most projects, so after they
    move out the directory is an empty folder the user did not create and now
    has no use for. A project that keeps its own ``settings.json``, skills,
    agents or prompts still has them, and the directory stays.
    """
    with contextlib.suppress(OSError):
        directory = root / PROJECT_DIR_NAME
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


def _migration_done(root: Path, name: str) -> bool:
    path = project_migrations_file(root)
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(recorded, dict) and recorded.get(name))


def _record_migration(root: Path, name: str) -> None:
    path = project_migrations_file(root)
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        recorded = {}
    if not isinstance(recorded, dict):
        recorded = {}
    recorded[name] = True
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _drop_legacy_gitignore(root: Path) -> None:
    """Remove the ``settings.local.json`` line HX added to ``.hx/.gitignore``.

    The file is deleted outright once that line was all it held; a user who
    added rules of their own keeps the file, minus the one HX put there.
    """
    path = legacy_project_gitignore_file(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    kept = [line for line in lines if line.strip() != "settings.local.json"]
    if len(kept) == len(lines):
        return
    with contextlib.suppress(OSError):
        if any(line.strip() for line in kept):
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            path.unlink()


def _default(cls: type, name: str) -> Any:
    """Default value of a dataclass field.

    ``slots=True`` replaces class attributes with descriptors, so
    ``ModelSettings.max_tokens`` is not the default - it is a member_descriptor.
    """
    for f in dataclasses_fields(cls):
        if f.name == name:
            return f.default
    raise KeyError(name)


def _build_settings(data: dict[str, Any], cwd: Path) -> Settings:
    models = data.get("models", {})
    permissions = data.get("permissions", {})
    context = data.get("context", {})
    bash = data.get("bash", {})
    prompt = data.get("prompt", {})
    tui = data.get("tui", {})

    mode_raw = permissions.get("mode", PermissionMode.DEFAULT)
    try:
        mode = PermissionMode(mode_raw)
    except ValueError as exc:
        raise ConfigError(f"unknown permission mode: {mode_raw!r}") from exc

    effort_raw = models.get("reasoning_effort")
    if effort_raw is not None:
        from hx.providers.codex_catalogue import EFFORT_ORDER

        if effort_raw not in EFFORT_ORDER:
            known = ", ".join(EFFORT_ORDER)
            raise ConfigError(f"unknown models.reasoning_effort: {effort_raw!r}. Known: {known}")

    busy_raw = tui.get("enterWhileBusy", tui.get("enter_while_busy", EnterWhileBusy.QUEUE))
    try:
        enter_while_busy = EnterWhileBusy(busy_raw)
    except ValueError as exc:
        raise ConfigError(f"unknown tui.enterWhileBusy: {busy_raw!r}") from exc

    return Settings(
        cwd=cwd,
        models=ModelSettings(
            model=models.get("model", _default(ModelSettings, "model")),
            subagent_model=models.get("subagent_model"),
            title_model=models.get("title_model"),
            max_tokens=int(models.get("max_tokens", _default(ModelSettings, "max_tokens"))),
            temperature=models.get("temperature"),
            reasoning_effort=effort_raw,
            codex_models=tuple(str(entry) for entry in models.get("codex_models", ())),
        ),
        permissions=PermissionSettings(
            mode=mode,
            allow=tuple(permissions.get("allow", ())),
            ask=tuple(permissions.get("ask", ())),
            deny=tuple(permissions.get("deny", ())),
            sandbox=bool(permissions.get("sandbox", True)),
            allow_network=bool(permissions.get("allow_network", False)),
        ),
        context=ContextSettings(
            compact_at=float(context.get("compact_at", _default(ContextSettings, "compact_at"))),
            keep_recent_turns=int(
                context.get("keep_recent_turns", _default(ContextSettings, "keep_recent_turns"))
            ),
            tool_output_char_cap=int(
                context.get(
                    "tool_output_char_cap", _default(ContextSettings, "tool_output_char_cap")
                )
            ),
            tool_output_line_cap=int(
                context.get(
                    "tool_output_line_cap", _default(ContextSettings, "tool_output_line_cap")
                )
            ),
            git_notices=bool(context.get("git_notices", _default(ContextSettings, "git_notices"))),
        ),
        bash=BashSettings(
            timeout_seconds=int(
                bash.get("timeout_seconds", _default(BashSettings, "timeout_seconds"))
            ),
            max_timeout_seconds=int(
                bash.get("max_timeout_seconds", _default(BashSettings, "max_timeout_seconds"))
            ),
            shell=bash.get("shell"),
        ),
        prompt=PromptSettings(
            system=prompt.get("system"),
            append=tuple(_as_list(prompt.get("append", ()))),
        ),
        tui=TuiSettings(enter_while_busy=enter_while_busy),
        theme=data.get("theme", "dark"),
        quiet_startup=bool(data.get("quietStartup", data.get("quiet_startup", False))),
        telemetry=bool(data.get("telemetry", False)),
    )


def _as_list(value: Any) -> list[str]:
    """Accept either one string or a list of them - ``prompt.append`` reads
    naturally both ways and a bare string is the common case."""
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def read_settings_file(path: Path) -> dict[str, Any]:
    """Read one ``settings.json`` layer. Missing file -> empty dict.

    Raises:
        ConfigError: if the file exists but is not valid JSON.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    return data


def write_settings_file(path: Path, data: dict[str, Any]) -> None:
    """Write a settings layer atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


#: Permission rule lists are unioned across layers rather than replaced - a
#: project must be able to add a deny rule without discarding the user's.
#: ``prompt.append`` unions for the same reason: a project appending to the
#: system prompt must not silently discard what the user appended.
_UNIONED_KEYS = frozenset({"allow", "ask", "deny", "append"})


def merge_layers(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Deep-merge settings layers. Later layers win; lists are replaced, not concatenated,
    except permission rule lists which are unioned."""
    result: dict[str, Any] = {}
    for layer in layers:
        _merge_into(result, layer)
    return result


def _merge_into(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _merge_into(existing, value)
        elif key in _UNIONED_KEYS and isinstance(existing, list) and isinstance(value, list):
            target[key] = existing + [item for item in value if item not in existing]
        else:
            target[key] = value


_ENV_MAP: dict[str, tuple[str, ...]] = {
    "HX_MODEL": ("models", "model"),
    "HX_SUBAGENT_MODEL": ("models", "subagent_model"),
    "HX_MAX_TOKENS": ("models", "max_tokens"),
    "HX_PERMISSION_MODE": ("permissions", "mode"),
    "HX_SANDBOX": ("permissions", "sandbox"),
    "HX_COMPACT_AT": ("context", "compact_at"),
    "HX_GIT_NOTICES": ("context", "git_notices"),
    "HX_THEME": ("theme",),
    "HX_QUIET_STARTUP": ("quietStartup",),
}


def settings_from_env(env: dict[str, str]) -> dict[str, Any]:
    """Extract the ``HX_*`` environment layer (e.g. ``HX_MODEL``, ``HX_PERMISSION_MODE``)."""
    layer: dict[str, Any] = {}
    for name, path in _ENV_MAP.items():
        if name not in env:
            continue
        cursor = layer
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = _coerce(env[name])
    return layer


def _coerce(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


class ConfigError(Exception):
    """Raised for malformed configuration files."""

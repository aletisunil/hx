"""Layered configuration.

Precedence, lowest to highest::

    defaults < ~/.hx/settings.json < <cwd>/.hx/settings.json < environment < CLI flags

Settings are a plain dataclass so the whole config is hashable/serialisable and
can be diffed in tests. Permission rules are kept as raw strings here; parsing
into matchers is ``hx.permissions.engine``'s job.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from dataclasses import fields as dataclasses_fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from hx.paths import project_settings_file, user_settings_file


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


@dataclass(frozen=True, slots=True)
class ModelSettings:
    model: str = "anthropic/claude-sonnet-4.5"
    subagent_model: str | None = None
    """Model used by subagents. Falls back to ``model``."""
    max_tokens: int = 8192
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class BashSettings:
    timeout_seconds: int = 120
    max_timeout_seconds: int = 600
    shell: str | None = None
    """Override the login shell used for the persistent Bash session."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Fully resolved configuration for one HX session."""

    cwd: Path = field(default_factory=Path.cwd)
    models: ModelSettings = field(default_factory=ModelSettings)
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    bash: BashSettings = field(default_factory=BashSettings)
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
    layers = [
        read_settings_file(user_settings_file()),
        read_settings_file(project_settings_file(root)),
        settings_from_env(dict(os.environ)),
        overrides or {},
    ]
    merged = merge_layers(layers)
    return _build_settings(merged, root)


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

    mode_raw = permissions.get("mode", PermissionMode.DEFAULT)
    try:
        mode = PermissionMode(mode_raw)
    except ValueError as exc:
        raise ConfigError(f"unknown permission mode: {mode_raw!r}") from exc

    return Settings(
        cwd=cwd,
        models=ModelSettings(
            model=models.get("model", _default(ModelSettings, "model")),
            subagent_model=models.get("subagent_model"),
            max_tokens=int(models.get("max_tokens", _default(ModelSettings, "max_tokens"))),
            temperature=models.get("temperature"),
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
        theme=data.get("theme", "dark"),
        quiet_startup=bool(data.get("quietStartup", data.get("quiet_startup", False))),
        telemetry=bool(data.get("telemetry", False)),
    )


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
_UNIONED_KEYS = frozenset({"allow", "ask", "deny"})


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

"""Filesystem layout for HX state and configuration.

User-level state lives under ``$HX_HOME`` (default ``~/.hx``). Project-level
overrides live in ``<cwd>/.hx``. Nothing here touches the network or mutates
state on import; call :func:`ensure_user_dirs` explicitly at startup.
"""

from __future__ import annotations

import os
from pathlib import Path

HX_HOME_ENV = "HX_HOME"
PROJECT_DIR_NAME = ".hx"


def user_home() -> Path:
    """Root of user-level HX state (``$HX_HOME`` or ``~/.hx``)."""
    override = os.environ.get(HX_HOME_ENV)
    return Path(override).expanduser() if override else Path.home() / ".hx"


def user_settings_file() -> Path:
    return user_home() / "settings.json"


def auth_file() -> Path:
    """Credential store. Created with mode 0600 by the auth layer."""
    return user_home() / "auth.json"


def models_cache_file() -> Path:
    """Cached OpenRouter model catalogue."""
    return user_home() / "models.json"


def sessions_dir() -> Path:
    return user_home() / "sessions"


def session_dir(session_id: str) -> Path:
    return sessions_dir() / session_id


def session_transcript_file(session_id: str) -> Path:
    return session_dir(session_id) / "transcript.jsonl"


def session_outputs_dir(session_id: str) -> Path:
    """Where oversized tool outputs are spilled (see ``hx.tools.output``)."""
    return session_dir(session_id) / "outputs"


def session_checkpoints_dir(session_id: str) -> Path:
    """Content-addressed pre-images of the files HX changed (see
    :mod:`hx.core.checkpoints`)."""
    return session_dir(session_id) / "checkpoints"


def user_themes_dir() -> Path:
    """User-supplied theme files (``*.json``, see :mod:`hx.tui.theme_json`)."""
    return user_home() / "themes"


def user_skills_dir() -> Path:
    return user_home() / "skills"


def user_agents_dir() -> Path:
    return user_home() / "agents"


def keybindings_file() -> Path:
    """User keybinding overrides (see :mod:`hx.keys`)."""
    return user_home() / "keybindings.json"


def logs_dir() -> Path:
    return user_home() / "logs"


def project_dir(cwd: Path | None = None) -> Path:
    """Project-level config directory (``<cwd>/.hx``)."""
    return (cwd or Path.cwd()) / PROJECT_DIR_NAME


def project_settings_file(cwd: Path | None = None) -> Path:
    return project_dir(cwd) / "settings.json"


def project_mcp_file(cwd: Path | None = None) -> Path:
    return project_dir(cwd) / "mcp.json"


def project_skills_dir(cwd: Path | None = None) -> Path:
    return project_dir(cwd) / "skills"


def project_agents_dir(cwd: Path | None = None) -> Path:
    return project_dir(cwd) / "agents"


def user_system_prompt_file() -> Path:
    """User-level replacement for the built-in system prompt."""
    return user_home() / "system-prompt.md"


def user_system_prompt_append_file() -> Path:
    """User-level text appended to whichever system prompt is in force."""
    return user_home() / "system-prompt-append.md"


def project_system_prompt_file(cwd: Path | None = None) -> Path:
    return project_dir(cwd) / "system-prompt.md"


def project_system_prompt_append_file(cwd: Path | None = None) -> Path:
    return project_dir(cwd) / "system-prompt-append.md"


def ensure_user_dirs() -> None:
    """Create the user-level directory skeleton if it does not exist."""
    for path in (user_home(), sessions_dir(), user_skills_dir(), user_agents_dir(), logs_dir()):
        path.mkdir(parents=True, exist_ok=True)

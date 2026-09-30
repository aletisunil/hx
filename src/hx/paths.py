"""Filesystem layout for HX state and configuration.

User-level state lives under ``$HX_HOME`` (default ``~/.hx``). A project's
shared, checked-in settings live in ``<cwd>/.hx/settings.json``, written by
hand. Anything HX writes for itself - the permission grants a user accumulates
by answering prompts - lives under ``$HX_HOME/projects/<slug>``, so HX never
leaves a file inside somebody's checkout.

Nothing here touches the network or mutates state on import; call
:func:`ensure_user_dirs` explicitly at startup.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

HX_HOME_ENV = "HX_HOME"
PROJECT_DIR_NAME = ".hx"

_SLUG_MAX = 48
"""Longest readable half of a project slug. A deep path is trimmed from the
left: the last few components are the ones that identify a checkout."""


def user_home() -> Path:
    """Root of user-level HX state (``$HX_HOME`` or ``~/.hx``)."""
    override = os.environ.get(HX_HOME_ENV)
    return Path(override).expanduser() if override else Path.home() / ".hx"


def tilde(path: str | Path) -> str:
    """``path`` with the home directory written as ``~``.

    Compared as text, not resolved: a session records the path it resolved
    when it was made, and the directory may be gone by the time it is drawn.
    """
    text, home = str(path), str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home.rstrip("/") + "/"):
        return "~" + text[len(home.rstrip("/")) :]
    return text


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
    """Shared project settings. Written by hand, and safe to check in."""
    return project_dir(cwd) / "settings.json"


def project_state_dir(cwd: Path | None = None) -> Path:
    """Where HX keeps *its own* state for a project: ``$HX_HOME/projects/<slug>``.

    Under the user's home rather than inside the repository. What HX writes on
    the user's behalf is machine-local - an "always allow" grant is one
    person's decision, on one machine, frequently spelling out absolute paths
    from their home directory - and a tool that drops such a file into a
    checkout makes it the user's problem: it shows up in `git status`, invites
    a stray commit, and has to be excluded by a `.gitignore` entry HX had no
    business writing either.

    Still keyed by project, because the grants are genuinely project-scoped:
    allowing ``pytest`` in one repository must not allow it everywhere.
    """
    return user_home() / "projects" / project_slug(cwd)


def project_slug(cwd: Path | None = None) -> str:
    """A filesystem-safe, collision-free name for a project directory.

    The readable path, punctuation flattened, plus a short digest of the real
    absolute path. The readable half is for a human browsing ``~/.hx/projects``
    and wondering which directory a settings file belongs to; the digest is
    what keeps ``~/work/api`` and ``~/personal/api`` apart after flattening.
    """
    resolved = (cwd or Path.cwd()).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    readable = "-".join(part for part in str(resolved).split(os.sep) if part)
    safe = "".join(char if char.isalnum() or char in "-_." else "-" for char in readable)
    return f"{safe[-_SLUG_MAX:].strip('-')}-{digest}" if safe else digest


def project_local_settings_file(cwd: Path | None = None) -> Path:
    """This machine's settings for this project - never checked in.

    Everything HX writes on the user's behalf lands here. It lives under
    :func:`project_state_dir` rather than in the repository; see
    :func:`legacy_project_local_settings_file` for where it used to be and
    :func:`hx.config.migrate_local_settings` for how one moves.
    """
    return project_state_dir(cwd) / "settings.local.json"


def project_migrations_file(cwd: Path | None = None) -> Path:
    """Records the one-time cleanups already applied for this project.

    A migration that reruns is not a migration but a policy: without this, the
    lift of permission grants out of the repository's shared settings would
    fight the user every time they deliberately put one back.
    """
    return project_state_dir(cwd) / "migrations.json"


def legacy_project_local_settings_file(cwd: Path | None = None) -> Path:
    """Where the local layer lived when it was written into the repository.

    Read once, to move it, and then no longer written.
    """
    return project_dir(cwd) / "settings.local.json"


def legacy_project_gitignore_file(cwd: Path | None = None) -> Path:
    """``.hx/.gitignore``, which existed only to hide the file above.

    Removed with it when nothing else in ``.hx`` needs excluding.
    """
    return project_dir(cwd) / ".gitignore"


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


def user_instructions_file() -> Path:
    """Your ``AGENTS.md``: instructions that follow you into every project."""
    return user_home() / "AGENTS.md"


def project_root(cwd: Path | None = None) -> Path | None:
    """The repository ``cwd`` sits in: the nearest directory holding ``.git``.

    ``.git`` is a directory in a clone and a file in a worktree or submodule.
    A submodule is part of the repository around it, so the walk carries on
    past one to the superproject. ``None`` outside a repository.
    """
    start = (cwd or Path.cwd()).resolve()
    submodule: Path | None = None
    for directory in (start, *start.parents):
        marker = directory / ".git"
        if marker.is_dir() or (marker.is_file() and not _is_submodule(marker)):
            return directory
        if marker.is_file() and submodule is None:
            submodule = directory
    return submodule


def _is_submodule(marker: Path) -> bool:
    """Whether a ``.git`` file points into a superproject's ``.git/modules``."""
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return text.startswith("gitdir:") and "/modules/" in text.replace("\\", "/")


def project_instructions_files(cwd: Path, root: Path | None) -> list[Path]:
    """Every ``AGENTS.md`` that governs ``cwd``, least specific first.

    From the repository root down to ``cwd``, one candidate per directory, so
    starting HX in ``src/app`` still finds the root's file - and ``src/app``'s
    own, which comes last and so has the last word. Outside a repository only
    ``cwd`` itself is looked at: walking up from an arbitrary directory would
    pick up whatever happens to sit in its parents.

    ``root`` is :func:`project_root` of ``cwd``, passed in because the caller
    needs it too and finding it walks the file system.
    """
    here = cwd.resolve()
    if root is None:
        return [here / "AGENTS.md"]
    chain = [here, *here.parents]
    return [directory / "AGENTS.md" for directory in reversed(chain[: chain.index(root) + 1])]


def ensure_user_dirs() -> None:
    """Create the user-level skeleton if it does not exist.

    That includes an empty ``AGENTS.md``, so there is a file to find and fill
    in; empty, it adds nothing to the prompt. An existing one - including a
    symlink, dangling or not - is never touched, and failing to create it never
    stops a session: the file is optional.
    """
    for path in (user_home(), sessions_dir(), user_skills_dir(), user_agents_dir(), logs_dir()):
        path.mkdir(parents=True, exist_ok=True)
    try:
        with user_instructions_file().open("x"):
            pass
    except OSError:
        pass

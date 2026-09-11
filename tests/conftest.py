"""Shared fixtures.

Tests are offline by default. Anything hitting the real OpenRouter API is
marked ``live`` and excluded from the default run.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def hx_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated ``$HX_HOME`` so tests never touch the developer's real state."""
    home = tmp_path / "hxhome"
    home.mkdir()
    monkeypatch.setenv("HX_HOME", str(home))
    return home


@pytest.fixture()
def project(tmp_path: Path, hx_home: Path) -> Path:
    """An empty project directory with a ``.hx`` config dir.

    Depends on ``hx_home`` because everything HX writes for a project - the
    permission grants, the record of which migrations have run - now lives
    under ``$HX_HOME/projects/<slug>``. Without the isolated home, a test that
    grants a permission writes into the developer's real ``~/.hx``.
    """
    root = tmp_path / "project"
    (root / ".hx").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _reset_theme() -> object:
    """Restore the global palette after every test.

    THEME is a module-level singleton, so a test that switches to light leaves
    every later colour assertion comparing against the wrong palette - which
    shows up as an unrelated test failing only when run with others.
    """
    from hx.tui.theme import DEFAULT_THEME, THEME

    previous = THEME.palette.name
    yield None
    THEME.use(previous if previous in {"dark", "light", "ansi"} else DEFAULT_THEME)


unimplemented = pytest.mark.xfail(
    raises=NotImplementedError,
    reason="M0 scaffold: behaviour not implemented yet",
)

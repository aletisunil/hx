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
def project(tmp_path: Path) -> Path:
    """An empty project directory with a ``.hx`` config dir."""
    root = tmp_path / "project"
    (root / ".hx").mkdir(parents=True)
    return root


unimplemented = pytest.mark.xfail(
    raises=NotImplementedError,
    reason="M0 scaffold: behaviour not implemented yet",
)

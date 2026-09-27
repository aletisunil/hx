"""Shared fixtures.

The suite is end-to-end (``tests/e2e``) and offline. Anything hitting a real
API is marked ``live`` and excluded from the default run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.paths import auth_file

REAL_AUTH_FILE = auth_file()
"""The developer's own credential file, captured at import - before ``hx_home``
points ``$HX_HOME`` at a temporary directory. Live tests sign in with it; read
lazily instead, it is always the empty isolated file and every live test skips.
"""


@pytest.fixture()
def hx_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated ``$HX_HOME`` so tests never touch the developer's real state."""
    home = tmp_path / "hxhome"
    home.mkdir()
    monkeypatch.setenv("HX_HOME", str(home))
    return home

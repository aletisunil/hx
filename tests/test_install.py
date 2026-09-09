"""Installer presentation and smoke tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_installer_shows_the_hx_banner_features_and_support_contact(
    tmp_path: Path,
) -> None:
    """A successful install should summarize HX and leave a way to get help."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        "  '--version ') echo 'uv 0.test' ;;\n"
        "  'tool install') exit 0 ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)

    hx = fake_bin / "hx"
    hx.write_text("#!/bin/sh\nexit 0\n")
    hx.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "|_| |_|/_/  \\_\\" in result.stdout
    assert "HX is a terminal coding agent that works inside your project." in result.stdout
    assert "handles larger tasks with persistent sessions, skills, tools, and subagents" in (
        result.stdout
    )
    assert "see context, cost, and usage for every turn" in result.stdout
    assert "project.\n  It reads and edits files, runs commands in a sandbox" in result.stdout
    assert "subagents.\n  Connect to models through OpenRouter" in result.stdout
    assert "HX Installer" not in result.stdout
    assert "Questions or issues? Contact \x1b[1;33miam@sunilaleti.dev\x1b[0m" in (result.stdout)
    assert "Done. Run 'hx' inside a project directory." in result.stdout

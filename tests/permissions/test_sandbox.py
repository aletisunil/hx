"""Sandbox profile generation and real enforcement."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hx.permissions.sandbox import (
    Sandbox,
    SandboxBackend,
    build_seatbelt_profile,
    default_policy,
    detect_backend,
)
from tests.conftest import unimplemented


@unimplemented
def test_seatbelt_profile_denies_by_default(project: Path) -> None:
    profile = build_seatbelt_profile(default_policy(project))
    assert "(deny default)" in profile


@unimplemented
def test_paths_with_regex_metacharacters_are_escaped(tmp_path: Path) -> None:
    """A directory named with regex syntax must not break out of the profile."""
    weird = tmp_path / "proj (1)"
    weird.mkdir()
    profile = build_seatbelt_profile(default_policy(weird))
    assert "proj \\(1\\)" in profile or "proj (1)" not in profile.replace("\\(", "")


@unimplemented
def test_degrades_visibly_when_no_backend(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert detect_backend() is SandboxBackend.NONE
    assert not Sandbox(default_policy(project)).active


@pytest.mark.sandbox
@unimplemented
def test_write_outside_project_is_blocked(project: Path, tmp_path: Path) -> None:
    """End-to-end: the sandbox actually stops the write, not just the rule engine."""
    outside = tmp_path / "outside.txt"
    sandbox = Sandbox(default_policy(project))
    argv = sandbox.wrap(["/bin/sh", "-c", f"echo pwned > {outside}"])
    subprocess.run(argv, check=False, capture_output=True)
    assert not outside.exists()


@pytest.mark.sandbox
@unimplemented
def test_network_is_blocked_by_default(project: Path) -> None:
    sandbox = Sandbox(default_policy(project, allow_network=False))
    argv = sandbox.wrap(["/bin/sh", "-c", "curl -sS -m 5 https://example.com"])
    assert subprocess.run(argv, check=False, capture_output=True).returncode != 0

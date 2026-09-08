"""Sandbox profile generation and real enforcement.

The enforcement tests are the point: a profile that looks right but does not
actually stop a write is worse than no sandbox, because the status bar would
claim protection that is not there.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hx.permissions.sandbox import (
    Sandbox,
    SandboxBackend,
    SandboxPolicy,
    build_bwrap_argv,
    build_seatbelt_profile,
    default_policy,
    detect_backend,
)

pytestmark_sandbox = pytest.mark.sandbox


def _run(sandbox: Sandbox, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        sandbox.wrap(["/bin/sh", "-c", script]), capture_output=True, text=True, check=False
    )


def test_seatbelt_profile_denies_by_default(project: Path) -> None:
    profile = build_seatbelt_profile(default_policy(project))
    assert "(deny default)" in profile
    assert "(deny network*)" in profile


def test_network_allowance_is_opt_in(project: Path) -> None:
    assert "(allow network*)" in build_seatbelt_profile(default_policy(project, allow_network=True))


def test_credential_denials_come_after_the_read_allowance(project: Path) -> None:
    """SBPL takes the last matching rule, so ordering is the whole protection."""
    profile = build_seatbelt_profile(default_policy(project))
    assert profile.index("(allow file-read* (subpath") < profile.index("(deny file-read* (subpath")


def test_paths_are_emitted_as_quoted_literals_not_regexes(tmp_path: Path) -> None:
    """A directory named `proj (1)` must not be able to break the profile syntax."""
    weird = tmp_path / "proj (1) [x]"
    weird.mkdir()
    profile = build_seatbelt_profile(SandboxPolicy(writable_paths=(weird,)))
    assert f'(subpath "{weird.resolve()}")' in profile


def test_quotes_in_a_path_are_escaped(tmp_path: Path) -> None:
    nasty = tmp_path / 'we"ird'
    nasty.mkdir()
    profile = build_seatbelt_profile(SandboxPolicy(writable_paths=(nasty,)))
    assert '\\"' in profile
    assert f'"{nasty.resolve()}"' not in profile


def test_deny_paths_are_resolved_before_emission(tmp_path: Path) -> None:
    """Seatbelt matches real paths: an unresolved /var deny never covers
    /private/var, which is where the files actually live on macOS."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    profile = build_seatbelt_profile(SandboxPolicy(deny_paths=(link,)))
    assert str(real.resolve()) in profile


def test_bwrap_argv_unshares_the_network_by_default(project: Path) -> None:
    argv = build_bwrap_argv(default_policy(project), ["/bin/sh", "-c", "true"])
    assert "--unshare-net" in argv
    assert argv[:3] == ["bwrap", "--ro-bind", "/"]
    assert "--unshare-net" not in build_bwrap_argv(
        default_policy(project, allow_network=True), ["true"]
    )


def test_degrades_visibly_when_no_backend(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox that silently does nothing is worse than a visible absence."""
    monkeypatch.setattr("platform.system", lambda: "Haiku")
    monkeypatch.setattr(shutil, "which", lambda _: None)

    assert detect_backend() is SandboxBackend.NONE
    sandbox = Sandbox(default_policy(project), backend=SandboxBackend.NONE)
    assert not sandbox.active
    assert sandbox.wrap(["echo", "hi"]) == ["echo", "hi"]


@pytest.mark.sandbox
@pytest.mark.skipif(detect_backend() is SandboxBackend.NONE, reason="no sandbox backend")
def test_write_inside_the_project_still_works(project: Path) -> None:
    sandbox = Sandbox(default_policy(project))
    _run(sandbox, f"echo inside > {project}/ok.txt")
    assert (project / "ok.txt").exists()


@pytest.mark.sandbox
@pytest.mark.skipif(detect_backend() is SandboxBackend.NONE, reason="no sandbox backend")
def test_write_outside_project_is_blocked(project: Path) -> None:
    """End-to-end: the sandbox actually stops the write, not just the rule engine.

    The target must sit outside the temp dir too - the policy makes $TMPDIR
    writable, so a naive tmp_path probe would pass while proving nothing.
    """
    outside = Path.home() / ".hx-sandbox-test-probe"
    outside.unlink(missing_ok=True)
    try:
        _run(Sandbox(default_policy(project)), f"echo pwned > {outside}")
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.sandbox
@pytest.mark.skipif(detect_backend() is SandboxBackend.NONE, reason="no sandbox backend")
def test_denied_paths_cannot_be_read(project: Path) -> None:
    secrets = Path(tempfile.mkdtemp(prefix="hx-secret-"))
    (secrets / "id_rsa").write_text("PRIVATE KEY MATERIAL")

    base = default_policy(project)
    policy = SandboxPolicy(
        writable_paths=base.writable_paths,
        readable_paths=base.readable_paths,
        deny_paths=(*base.deny_paths, secrets),
    )
    result = _run(Sandbox(policy), f"cat {secrets}/id_rsa")
    assert "PRIVATE" not in result.stdout


@pytest.mark.sandbox
@pytest.mark.skipif(detect_backend() is SandboxBackend.NONE, reason="no sandbox backend")
def test_network_is_blocked_by_default(project: Path) -> None:
    sandbox = Sandbox(default_policy(project, allow_network=False))
    assert _run(sandbox, "curl -sS -m 5 https://example.com").returncode != 0


@pytest.mark.sandbox
@pytest.mark.skipif(detect_backend() is SandboxBackend.NONE, reason="no sandbox backend")
def test_ordinary_work_is_not_broken(project: Path) -> None:
    """A sandbox that blocks real work gets turned off, which protects nothing."""
    result = _run(Sandbox(default_policy(project)), "ls / >/dev/null && python3 -c 'print(1+1)'")
    assert result.stdout.strip() == "2"

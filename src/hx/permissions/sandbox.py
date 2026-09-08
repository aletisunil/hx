"""OS-level sandboxing for executed commands.

macOS uses ``sandbox-exec`` with a generated Seatbelt profile; Linux uses
``bubblewrap``. Both default to: read the filesystem, write only inside the
project and the temp dir, no outbound network.

If neither backend exists the sandbox degrades to a no-op - and says so in the
status bar. Silently pretending to be sandboxed would be worse than being
visibly unsandboxed.
"""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class SandboxBackend(StrEnum):
    SEATBELT = "seatbelt"
    BUBBLEWRAP = "bubblewrap"
    NONE = "none"


CREDENTIAL_PATHS = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.kube",
    "~/.docker/config.json",
    "~/.config/gh",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.hx/auth.json",
)
"""Denied even inside readable roots. A coding agent has no business reading
these, and one prompt-injected `cat ~/.ssh/id_rsa` is all it takes."""


@dataclass(slots=True)
class SandboxPolicy:
    writable_paths: tuple[Path, ...] = ()
    readable_paths: tuple[Path, ...] = ()
    deny_paths: tuple[Path, ...] = field(default_factory=tuple)
    """Explicitly unreadable even inside readable roots - credential stores,
    ``~/.ssh``, ``~/.aws``, and HX's own ``auth.json``."""
    allow_network: bool = False
    allow_subprocess: bool = True


class Sandbox:
    """Wraps a command so it runs under the platform's sandbox."""

    def __init__(self, policy: SandboxPolicy, backend: SandboxBackend | None = None) -> None:
        self.policy = policy
        self._backend = backend if backend is not None else detect_backend()
        self._profile_path: Path | None = None

    @property
    def backend(self) -> SandboxBackend:
        return self._backend

    @property
    def active(self) -> bool:
        """False when degraded to :attr:`SandboxBackend.NONE`."""
        return self._backend is not SandboxBackend.NONE

    def wrap(self, argv: list[str]) -> list[str]:
        """Return the argv to actually exec."""
        if self._backend is SandboxBackend.SEATBELT:
            return ["/usr/bin/sandbox-exec", "-f", str(self._write_profile()), *argv]
        if self._backend is SandboxBackend.BUBBLEWRAP:
            return build_bwrap_argv(self.policy, argv)
        return list(argv)

    def profile_text(self) -> str:
        """The generated Seatbelt profile - exposed so tests can assert on it and
        ``/permissions`` can show it."""
        return build_seatbelt_profile(self.policy)

    def _write_profile(self) -> Path:
        if self._profile_path is None or not self._profile_path.exists():
            with tempfile.NamedTemporaryFile(
                "w", suffix=".sb", prefix="hx-sandbox-", delete=False
            ) as handle:
                handle.write(self.profile_text())
            self._profile_path = Path(handle.name)
        return self._profile_path

    def cleanup(self) -> None:
        if self._profile_path is not None:
            self._profile_path.unlink(missing_ok=True)
            self._profile_path = None


def detect_backend() -> SandboxBackend:
    """Probe for ``sandbox-exec`` / ``bwrap`` on PATH."""
    system = platform.system()
    if system == "Darwin" and Path("/usr/bin/sandbox-exec").exists():
        return SandboxBackend.SEATBELT
    if system == "Linux" and shutil.which("bwrap"):
        return SandboxBackend.BUBBLEWRAP
    return SandboxBackend.NONE


def default_policy(cwd: Path, allow_network: bool = False) -> SandboxPolicy:
    """Writable: cwd and ``$TMPDIR``. Denied: credential paths. Network off."""
    # gettempdir() resolves through /private on macOS; bind the resolved form,
    # not the whole /var/folders tree, which would hand over every process's temp.
    temp = Path(tempfile.gettempdir()).resolve()
    writable = [cwd.resolve(), temp]

    # Tool caches that live outside the project but must stay writable, or
    # ordinary commands fail in confusing ways.
    for extra in ("~/.cache", "~/Library/Caches"):
        candidate = Path(extra).expanduser()
        if candidate.is_dir():
            writable.append(candidate.resolve())

    return SandboxPolicy(
        writable_paths=tuple(dict.fromkeys(writable)),
        readable_paths=(Path("/"),),
        deny_paths=tuple(_real(Path(p).expanduser()) for p in CREDENTIAL_PATHS),
        allow_network=allow_network,
    )


def build_seatbelt_profile(policy: SandboxPolicy) -> str:
    """Generate a Seatbelt profile.

    Starts from ``(deny default)`` and allows explicitly. SBPL evaluates rules
    in order with the last match winning, so the credential denials come after
    the broad read allowance.

    Paths are emitted as quoted ``subpath`` literals, not regexes, so a
    directory named ``proj (1)`` cannot break out of the profile syntax.
    """
    lines = [
        "(version 1)",
        "(deny default)",
        "",
        ";; Process management",
        "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
    ]
    lines.append("(allow process-exec*)" if policy.allow_subprocess else "(deny process-exec*)")

    lines += ["", ";; Reads"]
    for path in policy.readable_paths or (Path("/"),):
        lines.append(f"(allow file-read* (subpath {_sbpl_string(_real(path))}))")

    lines += ["", ";; Writes"]
    for path in policy.writable_paths:
        lines.append(f"(allow file-write* (subpath {_sbpl_string(_real(path))}))")
    for device in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/urandom"):
        lines.append(f"(allow file-write-data file-read-data (literal {_sbpl_string(device)}))")
    lines.append('(allow file-write* (subpath "/private/var/folders"))')

    lines += ["", ";; Network"]
    lines.append("(allow network*)" if policy.allow_network else "(deny network*)")

    if policy.deny_paths:
        lines += ["", ";; Credentials - last match wins, so these override the read allowance"]
        for path in policy.deny_paths:
            lines.append(f"(deny file-read* (subpath {_sbpl_string(_real(path))}))")

    return "\n".join(lines) + "\n"


def _real(path: Path) -> Path:
    """Resolve symlinks before emitting a path into a profile.

    Seatbelt matches against the resolved real path, so an unresolved
    ``/var/folders/...`` deny silently fails to cover the very files it names -
    on macOS that path really lives under ``/private``.
    """
    try:
        return path.resolve()
    except OSError:
        return path


def _sbpl_string(path: Path | str) -> str:
    """Quote a path as an SBPL string literal.

    Only backslashes and double quotes are special inside a quoted literal, so
    escaping those is sufficient - and unlike a regex, parentheses and dots in
    the path carry no meaning.
    """
    text = str(path)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_bwrap_argv(policy: SandboxPolicy, argv: list[str]) -> list[str]:
    """Generate the ``bwrap`` invocation: ``--ro-bind / /``, project bound
    read-write, ``--unshare-net`` unless network is allowed."""
    command = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--die-with-parent",
    ]
    for path in policy.writable_paths:
        command += ["--bind", str(_real(path)), str(_real(path))]
    for path in policy.deny_paths:
        if path.exists():
            # There is no "deny read" in bwrap; shadow the path with an empty dir.
            command += ["--tmpfs", str(path)]
    if not policy.allow_network:
        command.append("--unshare-net")
    if cwd := os.environ.get("PWD"):
        command += ["--chdir", cwd]
    return [*command, "--", *argv]

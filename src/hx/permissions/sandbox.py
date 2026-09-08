"""OS-level sandboxing for executed commands.

macOS uses ``sandbox-exec`` with a generated Seatbelt profile; Linux uses
``bubblewrap``. Both default to: read the filesystem, write only inside the
project and the temp dir, no outbound network.

If neither backend exists the sandbox degrades to a no-op - and says so in the
status bar. Silently pretending to be sandboxed would be worse than being
visibly unsandboxed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class SandboxBackend(StrEnum):
    SEATBELT = "seatbelt"
    BUBBLEWRAP = "bubblewrap"
    NONE = "none"


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
        raise NotImplementedError

    @property
    def backend(self) -> SandboxBackend:
        raise NotImplementedError

    @property
    def active(self) -> bool:
        """False when degraded to :attr:`SandboxBackend.NONE`."""
        raise NotImplementedError

    def wrap(self, argv: list[str]) -> list[str]:
        """Return the argv to actually exec."""
        raise NotImplementedError

    def profile_text(self) -> str:
        """The generated Seatbelt profile - exposed so tests can assert on it and
        ``/permissions`` can show it."""
        raise NotImplementedError


def detect_backend() -> SandboxBackend:
    """Probe for ``sandbox-exec`` / ``bwrap`` on PATH."""
    raise NotImplementedError


def default_policy(cwd: Path, allow_network: bool = False) -> SandboxPolicy:
    """Writable: cwd and ``$TMPDIR``. Denied: credential paths. Network off."""
    raise NotImplementedError


def build_seatbelt_profile(policy: SandboxPolicy) -> str:
    """Generate a Seatbelt profile.

    Starts from ``(deny default)`` and allows explicitly. Paths are emitted as
    resolved realpaths with regex metacharacters escaped, so a directory named
    with a ``(`` cannot break out of the profile syntax.
    """
    raise NotImplementedError


def build_bwrap_argv(policy: SandboxPolicy, argv: list[str]) -> list[str]:
    """Generate the ``bwrap`` invocation: ``--ro-bind / /``, project bound
    read-write, ``--unshare-net`` unless network is allowed."""
    raise NotImplementedError


class SandboxUnavailable(Exception):
    """Raised only when a sandbox was explicitly required and none is available."""

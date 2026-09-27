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
    "~/.ssh/",
    "~/.aws/",
    "~/.gnupg/",
    "~/.kube/",
    "~/.docker/config.json",
    "~/.config/gh/",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.hx/auth.json",
)
"""Denied even inside readable and writable roots. A coding agent has no
business reading these, and one prompt-injected `cat ~/.ssh/id_rsa` is all it
takes - or writing them, where an `authorized_keys` line is a way back in.

A trailing slash marks a directory. That matters only for one that does not
exist yet: see :func:`build_bwrap_argv`."""


@dataclass(frozen=True, slots=True)
class DeniedPath:
    path: Path
    is_dir: bool
    """What the path is meant to be, which is what it gets created as if it has
    to be created - ``~/.ssh`` made as a file would break ssh as surely as
    ``~/.netrc`` made as a directory would break curl."""


@dataclass(slots=True)
class SandboxPolicy:
    writable_paths: tuple[Path, ...] = ()
    readable_paths: tuple[Path, ...] = ()
    deny_paths: tuple[DeniedPath, ...] = field(default_factory=tuple)
    """Unreadable and unwritable even inside allowed roots - credential stores,
    ``~/.ssh``, ``~/.aws``, and HX's own ``auth.json``."""
    allow_network: bool = False
    allow_subprocess: bool = True
    cwd: Path | None = None
    """Directory the wrapped command starts in. ``None`` leaves it to bwrap."""


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
    writable = [cwd.resolve(), *_temp_container(temp)]

    # Tool caches that live outside the project but must stay writable, or
    # ordinary commands fail in confusing ways.
    for extra in ("~/.cache", "~/Library/Caches"):
        candidate = Path(extra).expanduser()
        if candidate.is_dir():
            writable.append(candidate.resolve())

    return SandboxPolicy(
        writable_paths=tuple(dict.fromkeys(writable)),
        readable_paths=(Path("/"),),
        deny_paths=tuple(
            DeniedPath(_real(Path(p).expanduser()), is_dir=p.endswith("/"))
            for p in CREDENTIAL_PATHS
        ),
        allow_network=allow_network,
        cwd=cwd.resolve(),
    )


_DARWIN_TEMP_ROOT = Path("/private/var/folders")


def _temp_container(temp: Path) -> list[Path]:
    """The temp directory, plus this user's darwin container when there is one.

    macOS gives each user a confined directory under ``/private/var/folders``
    holding both ``T`` (``$TMPDIR``) and ``C`` (per-user caches). Ordinary
    tooling writes to the cache side, so binding ``T`` alone makes commands
    fail in ways that look nothing like a sandbox denial.

    The container, though - never ``/private/var/folders`` itself, which is
    every user's and every process's. That distinction is the whole point:
    granting the tree wholesale was how the profile quietly handed back what
    resolving ``$TMPDIR`` had just been careful to narrow.
    """
    paths = [temp]
    if temp.is_relative_to(_DARWIN_TEMP_ROOT):
        container = temp.parent
        # /private/var/folders/<xx>/<hash> - two levels down, and no higher.
        if container.is_relative_to(_DARWIN_TEMP_ROOT) and container != _DARWIN_TEMP_ROOT:
            paths.append(container)
    return paths


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

    lines += ["", ";; Network"]
    lines.append("(allow network*)" if policy.allow_network else "(deny network*)")

    if policy.deny_paths:
        # Writes too: a project opened in $HOME makes ~/.ssh writable, and a
        # planted authorized_keys is worse than a leaked key.
        lines += ["", ";; Credentials - last match wins, so these override both allowances"]
        for denied in policy.deny_paths:
            lines.append(
                f"(deny file-read* file-write* (subpath {_sbpl_string(_real(denied.path))}))"
            )

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
    read-write, credential paths shadowed, ``--unshare-net`` unless network is
    allowed.

    Not quite side-effect free: a credential path that does not exist but sits
    inside a writable root is created on the host first - see
    :func:`_ensure_mountpoint`.
    """
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
    writable = [_real(path) for path in policy.writable_paths]
    for path in writable:
        command += ["--bind", str(path), str(path)]
    for denied in policy.deny_paths:
        path = _real(denied.path)
        if not path.exists() and not _ensure_mountpoint(denied, writable):
            continue
        # There is no "deny" in bwrap, so the path is shadowed instead - and
        # what it is shadowed *with* has to match what is there. `--tmpfs`
        # mounts a directory, and aiming it at a regular file aborts bwrap
        # outright: the sandbox never starts, and the command it was wrapping
        # dies with it. Six of the credential paths are files, `~/.hx/auth.json`
        # among them - the one HX writes itself - so this was every Linux user
        # who had ever logged in. Read-only either way, so a write fails the
        # way it does under Seatbelt instead of vanishing into a tmpfs.
        if path.is_dir():
            command += ["--tmpfs", str(path), "--remount-ro", str(path)]
        else:
            command += ["--ro-bind", os.devnull, str(path)]
    if not policy.allow_network:
        command.append("--unshare-net")
    # The policy's own directory, not `$PWD`: the environment variable is
    # whatever the parent shell last exported, which after a `cd` inside a
    # script is somewhere else entirely - and is simply absent under `env -i`.
    if policy.cwd is not None:
        command += ["--chdir", str(_real(policy.cwd))]
    return [*command, "--", *argv]


def _ensure_mountpoint(denied: DeniedPath, writable: list[Path]) -> bool:
    """Create a missing credential path on the host, so it can be shadowed.

    bwrap can only deny a path by mounting over it, and a mount needs something
    to mount on. Outside the writable roots, skipping a missing path is safe:
    under ``--ro-bind / /`` the sandbox cannot create it either. Inside one it
    is not - a session opened in ``$HOME`` with no ``~/.ssh`` yet could
    ``mkdir ~/.ssh`` and plant an ``authorized_keys`` on the host.

    So the path is created, empty and private, as what it is meant to be. It
    stays: removing it after the command would race any other sandbox still
    shadowing it, and an empty ``~/.ssh`` is what ssh itself would have made.
    Left to bwrap, it would appear too - but a file as ``0444``, which the
    user's own tools could not then write.

    Returns whether the path now exists. A failure means the parent refuses
    this user, and the sandbox runs as the same user, so it cannot create the
    path either.
    """
    path = _real(denied.path)
    if not any(path.is_relative_to(root) for root in writable):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if denied.is_dir:
            path.mkdir(mode=0o700, exist_ok=True)
        else:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    except OSError:
        return False
    return path.exists()

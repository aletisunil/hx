"""Working-tree awareness, carried by late injection.

The branch and the set of changed files move constantly, so they belong below
the rolling cache breakpoint alongside the todo list, never in the system
prompt (see :mod:`hx.core.lateinject`). Putting either in the prefix would
invalidate the KV cache on every turn that touched a file.

One ``git status --porcelain=v2 --branch -z`` per provider call answers both
questions at once. It runs without a shell, is bounded by a timeout, and
reports paths NUL-separated so a filename with a space, a quote or a newline in
it parses like any other. Outside a repository - or with no git on ``PATH`` -
the watcher disables itself permanently rather than paying for a failing
subprocess every time.

:meth:`GitWatcher.poll` blocks - a subprocess, plus a stat per dirty file - and
is called once per provider call rather than once per user turn, so a turn with
ten tool calls polls ten times. :meth:`~hx.core.lateinject.InjectionRegistry.apply`
runs it in a worker thread for that reason; nothing here may be called directly
from a coroutine.

Only files HX has *not* read are reported. A file HX read and still holds the
current bytes for is not news, and one it read that has since moved on disk is
already the ``stale_files`` injector's to report - so each file has exactly one
reporter and the model never sees the same path twice under two headings.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hx.core.lateinject import Injection

STATUS_COMMAND: tuple[str, ...] = ("git", "status", "--porcelain=v2", "--branch", "-z")
ROOT_COMMAND: tuple[str, ...] = ("git", "rev-parse", "--show-toplevel")

TIMEOUT_SECONDS = 2.0
"""A turn must not wait on git. A repository slow enough to blow this budget
would cost more in latency than the notice is worth."""

MAX_LISTED = 20
"""A bulk checkout can change thousands of files; the list is a hint, not an
inventory."""

MAX_TRANSIENT_FAILURES = 3
"""Consecutive timeouts before the watcher gives up for the session."""

DETACHED = "(detached)"

#: git's status letters, spelled the way the notice reads them out.
LABELS: dict[str, str] = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "typechange",
    "U": "conflicted",
    "?": "untracked",
}


class GitUnavailable(Exception):
    """Raised by the runner when git could not answer.

    ``permanent`` separates "this will never work" - no git, not a repository -
    from "not this time", which is worth another attempt next turn.
    """

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class Change:
    path: str
    """Repository-relative, as git prints it - what the model should see."""
    absolute: Path
    label: str


@dataclass(frozen=True, slots=True)
class Status:
    branch: str
    changes: tuple[Change, ...]
    """Files whose status or contents moved since the previous poll. Empty on
    the first poll, which only establishes the baseline: a working tree that
    was already dirty when the session opened did not change *during* it."""


Runner = Callable[[tuple[str, ...]], str]
"""Runs a git command in the session directory and returns stdout."""


class GitWatcher:
    """Polls the working tree once per turn and reports what moved."""

    def __init__(self, cwd: Path, *, runner: Runner | None = None) -> None:
        self.cwd = cwd
        self._run: Runner = runner or _runner(cwd)
        self._enabled = True
        self._failures = 0
        self._root: Path | None = None
        self._last: dict[str, tuple[str, tuple[int, int] | None]] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def poll(self) -> Status | None:
        """Current branch plus what changed since the last call, or ``None``
        when git has nothing to say."""
        if not self._enabled:
            return None
        try:
            if self._root is None:
                self._root = Path(self._run(ROOT_COMMAND).strip())
            raw = self._run(STATUS_COMMAND)
        except GitUnavailable as exc:
            self._note_failure(exc)
            return None

        self._failures = 0
        branch, entries = parse_status(raw)
        snapshot = {path: (code, _signature(self._root / path)) for path, code in entries.items()}
        previous, self._last = self._last, snapshot
        if previous is None:
            return Status(branch=branch, changes=())

        changes = tuple(
            Change(
                path=path,
                absolute=self._root / path,
                label=LABELS.get(entries[path], entries[path]),
            )
            for path, state in snapshot.items()
            if previous.get(path) != state
        )
        return Status(branch=branch, changes=changes)

    def _note_failure(self, exc: GitUnavailable) -> None:
        if exc.permanent:
            self._enabled = False
            return
        self._failures += 1
        if self._failures >= MAX_TRANSIENT_FAILURES:
            self._enabled = False


def parse_status(raw: str) -> tuple[str, dict[str, str]]:
    """Parse ``--porcelain=v2 --branch -z`` into a branch and path -> code.

    Entry kinds: ``1`` ordinary change, ``2`` rename or copy (its original path
    follows as the next NUL-separated field, which is consumed here), ``u``
    unmerged, ``?`` untracked. ``!`` ignored entries only appear with
    ``--ignored`` and are skipped.
    """
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()

    branch = DETACHED
    entries: dict[str, str] = {}
    index = 0
    while index < len(fields):
        line = fields[index]
        index += 1
        if not line:
            continue
        kind = line[0]
        if kind == "#":
            if line.startswith("# branch.head "):
                head = line.removeprefix("# branch.head ").strip()
                branch = DETACHED if head in {"", DETACHED} else head
            continue
        if kind == "?":
            entries[line[2:]] = "?"
            continue
        if kind == "!":
            continue
        splits = {"1": 8, "2": 9, "u": 10}.get(kind)
        if splits is None:
            continue
        parts = line.split(" ", splits)
        if len(parts) <= splits:
            continue
        entries[parts[splits]] = _status_code(parts[1])
        if kind == "2":
            index += 1  # the rename's original path
    return branch, entries


def git_injector(
    watcher: GitWatcher,
    *,
    seen: Callable[[Path], bool] | None = None,
    priority: int = 15,
) -> Callable[[], Injection | None]:
    """Late injector for branch and working-tree movement.

    ``seen`` reports whether HX already knows a path (``FileTracker.was_read``);
    those files are left to the ``stale_files`` injector.
    """

    def inject() -> Injection | None:
        status = watcher.poll()
        if status is None:
            return None

        lines = [f"Git branch: {status.branch}"]
        unknown = [c for c in status.changes if seen is None or not seen(c.absolute)]
        if unknown:
            listed = [f"- {c.label}: {c.path}" for c in unknown[:MAX_LISTED]]
            if len(unknown) > MAX_LISTED:
                listed.append(f"- … and {len(unknown) - MAX_LISTED} more")
            lines.append(
                "These files changed on disk since the last turn and have not been "
                "read in this session. Read one before editing it:\n" + "\n".join(listed)
            )
        return Injection(source="git", text="\n\n".join(lines), priority=priority)

    return inject


def _runner(cwd: Path) -> Runner:
    def run(command: tuple[str, ...]) -> str:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitUnavailable("git is not installed", permanent=True) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitUnavailable("git timed out", permanent=False) from exc
        except OSError as exc:
            raise GitUnavailable(str(exc), permanent=False) from exc
        if result.returncode != 0:
            # 128 is git's "not a repository" and friends: nothing to retry.
            raise GitUnavailable(
                result.stderr.strip() or f"git exited {result.returncode}",
                permanent=result.returncode == 128,
            )
        return result.stdout

    return run


def _status_code(xy: str) -> str:
    """The meaningful half of git's two-character status field.

    The worktree column wins when it says anything: a file staged as added and
    since modified is, to a model about to edit it, modified.
    """
    worktree = xy[1] if len(xy) > 1 else "."
    index = xy[0] if xy else "."
    return worktree if worktree != "." else index


def _signature(path: Path) -> tuple[int, int] | None:
    """Cheap stand-in for a content hash.

    Hashing every dirty file on every turn is a per-turn cost proportional to
    the size of the working tree; mtime and size answer "did this move again"
    for the price of a stat.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)

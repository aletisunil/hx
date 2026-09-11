"""Working-tree notices: what the model is told changed, and what it costs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hx.git import (
    DETACHED,
    MAX_LISTED,
    GitUnavailable,
    GitWatcher,
    git_injector,
    parse_status,
)


def _status(*entries: str) -> str:
    return "\0".join(entries) + "\0"


def test_parse_reads_the_branch() -> None:
    branch, _ = parse_status(_status("# branch.oid abc123", "# branch.head feature/x"))
    assert branch == "feature/x"


def test_parse_reports_a_detached_head() -> None:
    branch, _ = parse_status(_status("# branch.head (detached)"))
    assert branch == DETACHED


def test_parse_reads_ordinary_changes_and_untracked_files() -> None:
    _, entries = parse_status(
        _status(
            "1 .M N... 100644 100644 100644 aaa bbb src/hx/git.py",
            "? notes.txt",
        )
    )
    assert entries == {"src/hx/git.py": "M", "notes.txt": "?"}


def test_parse_consumes_the_original_path_of_a_rename() -> None:
    """The original path is its own NUL field; read as an entry it would be
    reported as a phantom change with a garbage status code."""
    _, entries = parse_status(
        _status(
            "2 R. N... 100644 100644 100644 aaa bbb R100 new.py",
            "old.py",
            "? after.txt",
        )
    )
    assert entries == {"new.py": "R", "after.txt": "?"}


def test_parse_handles_paths_with_spaces_and_newlines() -> None:
    """Why the -z form is used: neither character terminates a field."""
    _, entries = parse_status(_status("1 .M N... 100644 100644 100644 aaa bbb my notes/a\nb.md"))
    assert entries == {"my notes/a\nb.md": "M"}


def test_parse_prefers_the_worktree_column() -> None:
    """Staged as added, since modified: to a model about to edit it, modified."""
    _, entries = parse_status(
        _status(
            "1 AM N... 100644 100644 100644 aaa bbb x.py",
            "1 A. N... 100644 100644 100644 aaa bbb y.py",
        )
    )
    assert entries == {"x.py": "M", "y.py": "A"}


def test_parse_reads_unmerged_entries() -> None:
    _, entries = parse_status(
        _status("u UU N... 100644 100644 100644 100644 aaa bbb ccc conflict.py")
    )
    assert entries == {"conflict.py": "U"}


class FakeGit:
    """A scripted git. Each status string is one turn."""

    def __init__(self, root: Path, *statuses: str) -> None:
        self.root = root
        self.statuses = list(statuses)
        self.calls: list[tuple[str, ...]] = []
        self.error: GitUnavailable | None = None

    def __call__(self, command: tuple[str, ...]) -> str:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        if command[1] == "rev-parse":
            return f"{self.root}\n"
        return self.statuses.pop(0) if self.statuses else ""


def test_the_first_poll_only_establishes_a_baseline(tmp_path: Path) -> None:
    """A tree that was already dirty when the session opened did not change
    during it - reporting it would spend context on old news every session."""
    (tmp_path / "a.py").write_text("x")
    git = FakeGit(tmp_path, _status("# branch.head main", "1 .M N... 1 1 1 a b a.py"))
    watcher = GitWatcher(tmp_path, runner=git)

    status = watcher.poll()

    assert status is not None
    assert status.branch == "main"
    assert status.changes == ()


def test_a_new_change_is_reported_once(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    entry = "1 .M N... 1 1 1 a b a.py"
    git = FakeGit(
        tmp_path,
        _status("# branch.head main"),
        _status("# branch.head main", entry),
        _status("# branch.head main", entry),
    )
    watcher = GitWatcher(tmp_path, runner=git)

    watcher.poll()
    first = watcher.poll()
    second = watcher.poll()

    assert first is not None and [c.path for c in first.changes] == ["a.py"]
    assert first.changes[0].label == "modified"
    assert second is not None and second.changes == ()


def test_a_second_edit_to_the_same_file_is_reported_again(tmp_path: Path) -> None:
    """The status code does not move on the second edit, so only the file
    signature can tell the model its copy is stale again."""
    target = tmp_path / "a.py"
    target.write_text("one")
    entry = "1 .M N... 1 1 1 a b a.py"
    git = FakeGit(tmp_path, _status(entry), _status(entry), _status(entry))
    watcher = GitWatcher(tmp_path, runner=git)

    watcher.poll()
    target.write_text("two longer contents")
    again = watcher.poll()

    assert again is not None and [c.path for c in again.changes] == ["a.py"]


def test_a_permanent_failure_disables_the_watcher(tmp_path: Path) -> None:
    """Outside a repository the subprocess must be paid for once, not per turn."""
    git = FakeGit(tmp_path)
    git.error = GitUnavailable("not a git repository", permanent=True)
    watcher = GitWatcher(tmp_path, runner=git)

    assert watcher.poll() is None
    assert watcher.poll() is None
    assert not watcher.enabled
    assert len(git.calls) == 1


def test_transient_failures_are_retried_then_given_up_on(tmp_path: Path) -> None:
    git = FakeGit(tmp_path)
    git.error = GitUnavailable("timed out", permanent=False)
    watcher = GitWatcher(tmp_path, runner=git)

    for _ in range(3):
        assert watcher.poll() is None
    assert not watcher.enabled
    assert len(git.calls) == 3


def test_the_injection_carries_the_branch_even_with_no_changes(tmp_path: Path) -> None:
    git = FakeGit(tmp_path, _status("# branch.head main"))
    inject = git_injector(GitWatcher(tmp_path, runner=git))

    injection = inject()

    assert injection is not None
    assert injection.text == "Git branch: main"


def test_files_hx_has_read_are_left_to_the_stale_file_injector(tmp_path: Path) -> None:
    """One reporter per file: the same path under two headings is context spent
    twice to say one thing."""
    (tmp_path / "known.py").write_text("x")
    (tmp_path / "fresh.py").write_text("x")
    entries = ("1 .M N... 1 1 1 a b known.py", "1 .M N... 1 1 1 a b fresh.py")
    git = FakeGit(tmp_path, _status(), _status(*entries))
    watcher = GitWatcher(tmp_path, runner=git)
    inject = git_injector(watcher, seen=lambda path: path.name == "known.py")

    inject()
    injection = inject()

    assert injection is not None
    assert "fresh.py" in injection.text
    assert "known.py" not in injection.text


def test_a_bulk_checkout_does_not_flood_the_context(tmp_path: Path) -> None:
    entries = []
    for index in range(MAX_LISTED + 5):
        (tmp_path / f"f{index}.py").write_text("x")
        entries.append(f"1 .M N... 1 1 1 a b f{index}.py")
    git = FakeGit(tmp_path, _status(), _status(*entries))
    inject = git_injector(GitWatcher(tmp_path, runner=git))

    inject()
    injection = inject()

    assert injection is not None
    assert injection.text.count("- modified:") == MAX_LISTED
    assert "and 5 more" in injection.text


def test_a_disabled_watcher_injects_nothing(tmp_path: Path) -> None:
    git = FakeGit(tmp_path)
    git.error = GitUnavailable("not a git repository", permanent=True)
    inject = git_injector(GitWatcher(tmp_path, runner=git))

    assert inject() is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_against_a_real_repository(tmp_path: Path) -> None:
    """The parser is only worth as much as the format it was written against."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.py").write_text("one\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    watcher = GitWatcher(tmp_path)

    baseline = watcher.poll()
    (tmp_path / "tracked.py").write_text("two\n")
    (tmp_path / "fresh.txt").write_text("new\n")
    after = watcher.poll()

    assert baseline is not None and baseline.changes == ()
    assert baseline.branch not in {"", DETACHED}
    assert after is not None
    assert {(c.path, c.label) for c in after.changes} == {
        ("tracked.py", "modified"),
        ("fresh.txt", "untracked"),
    }
    assert after.changes[0].absolute.parent == tmp_path


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_outside_a_repository_the_watcher_stays_quiet(tmp_path: Path) -> None:
    watcher = GitWatcher(tmp_path)

    assert watcher.poll() is None
    assert not watcher.enabled


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "file.txt").write_text("one\n")
    subprocess.run(["git", "add", "file.txt"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_branch_watcher_follows_a_checkout_made_elsewhere(tmp_path: Path) -> None:
    """The status bar read the branch once at startup, so checking out in
    another terminal left it naming a branch the user had long since left."""
    from hx.git import BranchWatcher

    _init_repo(tmp_path)
    watcher = BranchWatcher(tmp_path)
    assert watcher.poll() == "main"

    subprocess.run(["git", "checkout", "-q", "-b", "feature/x"], cwd=tmp_path, check=True)

    assert watcher.poll() == "feature/x"
    assert watcher.branch == "feature/x"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_branch_watcher_reports_a_detached_head(tmp_path: Path) -> None:
    from hx.git import BranchWatcher

    _init_repo(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", head], cwd=tmp_path, check=True)

    assert BranchWatcher(tmp_path).poll() == DETACHED


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_branch_watcher_works_from_a_subdirectory(tmp_path: Path) -> None:
    """A session started below the root still has a branch to show."""
    from hx.git import BranchWatcher

    _init_repo(tmp_path)
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)

    assert BranchWatcher(nested).poll() == "main"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_branch_watcher_follows_a_worktree(tmp_path: Path) -> None:
    """``.git`` is a file there, not a directory - and a worktree is exactly
    where branch switching happens most."""
    from hx.git import BranchWatcher

    main = tmp_path / "main"
    _init_repo(main)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "side", str(linked)], cwd=main, check=True
    )

    assert (linked / ".git").is_file()
    assert BranchWatcher(linked).poll() == "side"


def test_the_branch_watcher_stays_quiet_outside_a_repository(tmp_path: Path) -> None:
    from hx.git import BranchWatcher

    watcher = BranchWatcher(tmp_path)
    assert watcher.poll() is None
    assert watcher.branch is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_repository_that_disappears_keeps_the_last_branch(tmp_path: Path) -> None:
    """A transient stat failure must not blank the status bar."""
    from hx.git import BranchWatcher

    _init_repo(tmp_path)
    watcher = BranchWatcher(tmp_path)
    assert watcher.poll() == "main"

    shutil.rmtree(tmp_path / ".git")

    assert watcher.poll() == "main"

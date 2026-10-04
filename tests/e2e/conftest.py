"""Fixtures for the end-to-end suite.

Every test gets:

* ``stub`` - the OpenRouter stand-in, already serving;
* ``project`` - an empty project directory, the cwd ``hx`` runs in;
* ``hx`` - launches the installed ``hx`` entry point, either on a pty
  (:meth:`HX.tui`) or as a plain subprocess (:meth:`HX.run`).

The process sees a private ``$HOME`` and ``$HX_HOME``, a fake API key and
clipboard tools that write to a file, so a run cannot read or change anything
of the developer's - their credentials, their sessions, their clipboard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hx.permissions.sandbox import SandboxBackend, detect_backend
from tests.e2e.report import REPO, Report, Step, TestRecord
from tests.e2e.stub import RELEASES_PATH, SONNET, Stub
from tests.e2e.terminal import Terminal

HX_BIN = Path(sys.executable).parent / "hx"

SANDBOX = detect_backend()
SANDBOX_STATUS = "no-sandbox" if SANDBOX is SandboxBackend.NONE else f"sandbox {SANDBOX.value}"
"""How the status bar names the sandbox on this machine: seatbelt on macOS,
bubblewrap on Linux with ``bwrap`` installed."""
requires_sandbox = pytest.mark.skipif(
    SANDBOX is SandboxBackend.NONE, reason="no OS sandbox (sandbox-exec or bwrap) here"
)
SANDBOX_REFUSALS = ("Operation not permitted", "Read-only file system", "Permission denied")
"""How a confined command fails: seatbelt says the first, bwrap's read-only
bind one of the others."""
API_KEY = "sk-or-v1-e2e0000000000000000000000000000000000000000000000000000cafe"

_report = Report(Path(os.environ.get("HX_E2E_REPORT") or REPO / "e2e-report"))

_SHIMS = {
    # The macOS and Linux clipboard tools, writing to and reading from a file.
    "pbcopy": 'cat > "$E2E_CLIPBOARD"',
    "pbpaste": 'cat "$E2E_CLIPBOARD" 2>/dev/null',
    "wl-copy": 'cat > "$E2E_CLIPBOARD"',
    "xclip": 'cat > "$E2E_CLIPBOARD"',
    # webbrowser on macOS opens URLs through osascript; so does the image
    # clipboard reader. Neither may reach the real desktop.
    "osascript": 'printf "%s\\n" "$*" >> "$E2E_OSASCRIPT"',
    "open": 'printf "%s\\n" "$*" >> "$E2E_OPENED"',
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e/" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[Any]:
    outcome: Any = yield
    report = outcome.get_result()
    record: TestRecord | None = getattr(item, "_e2e_record", None)
    if record is None:
        return
    if report.when == "call" or (report.when == "setup" and not report.passed):
        record.outcome = report.outcome
        record.duration = report.duration
        if report.failed:
            record.failure = str(report.longrepr)[-6000:]
    elif report.when == "teardown" and report.failed and record.outcome == "passed":
        record.outcome = "failed"
        record.failure = str(report.longrepr)[-6000:]


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _report.tests:
        index = _report.write()
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"E2E report: {index}")


@pytest.fixture()
def record(request: pytest.FixtureRequest) -> TestRecord:
    doc = (request.function.__doc__ or "").strip()
    rec = _report.begin(request.node.nodeid, " ".join(doc.split()))
    request.node._e2e_record = rec
    return rec


@pytest.fixture()
def stub(record: TestRecord) -> Iterator[Stub]:
    server = Stub().start()
    record.replacements.append((server.url, "http://stub/api/v1"))
    yield server
    record.requests = server.log()
    server.close()
    assert not server.unexpected, "unscripted model calls:\n" + "\n".join(server.unexpected)


@pytest.fixture()
def sandbox_root(tmp_path: Path, record: TestRecord) -> Path:
    # Resolved: on macOS /var is a symlink to /private/var and HX prints the real path.
    root = tmp_path.resolve()
    record.replacements.append((str(root), "<tmp>"))
    record.replacements.append((str(tmp_path), "<tmp>"))
    return root


@pytest.fixture()
def project(sandbox_root: Path) -> Path:
    path = sandbox_root / "project"
    path.mkdir()
    return path


class HX:
    """Launches ``hx`` the way a user does, into the test's private world."""

    def __init__(self, root: Path, project: Path, stub: Stub, record: TestRecord) -> None:
        self.root = root
        self.project = project
        self.stub = stub
        self.record = record
        self.home = root / "home"
        self.hx_home = root / "hxhome"
        self.bin = root / "bin"
        self.clipboard = root / "clipboard"
        self.osascript_log = root / "osascript.log"
        self.opened_log = root / "opened.log"
        for folder in (self.home, self.hx_home, self.bin):
            folder.mkdir(exist_ok=True)
        for name, body in _SHIMS.items():
            shim = self.bin / name
            shim.write_text(f"#!/bin/sh\n{body}\n")
            shim.chmod(0o755)
        self.extra_env: dict[str, str] = {}
        self._terminals: list[Terminal] = []
        self._screens = 0

    def env(self, **overrides: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{HX_BIN.parent}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "HX_HOME": str(self.hx_home),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "SHELL": "/bin/bash",
            "USER": os.environ.get("USER", "e2e"),
            "TMPDIR": str(self.root),
            "HX_OPENROUTER_API_KEY": API_KEY,
            "HX_OPENROUTER_BASE_URL": self.stub.url,
            # Never the real npm registry: which Claude Code is current is the stub's to say.
            "HX_CLAUDE_CODE_RELEASES_URL": self.stub.url + RELEASES_PATH,
            "HX_MODEL": SONNET,
            "HX_TRUE_COLOR": "1",
            # Keys here are written to the pty, not typed: whatever the
            # developer's own keyboard holds must not turn an enter into a newline.
            "HX_MODIFIER_PROBE": "0",
            "BROWSER": str(self.bin / "open"),
            "E2E_CLIPBOARD": str(self.clipboard),
            # On Linux HX picks a clipboard tool by session type; this points it
            # at the wl-copy shim. macOS always uses pbcopy.
            "WAYLAND_DISPLAY": "e2e",
            "E2E_OSASCRIPT": str(self.osascript_log),
            "E2E_OPENED": str(self.opened_log),
            "GIT_AUTHOR_NAME": "E2E",
            "GIT_AUTHOR_EMAIL": "e2e@example.com",
            "GIT_COMMITTER_NAME": "E2E",
            "GIT_COMMITTER_EMAIL": "e2e@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        env.update(self.extra_env)
        env.update(overrides)
        return env

    # -- the TUI -----------------------------------------------------------

    def tui(
        self,
        *args: str,
        columns: int = 100,
        rows: int = 30,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        ready: str | None = "Ask HX",
        keyboard: str = "kitty",
    ) -> Terminal:
        """Start ``hx`` on a pty and wait for the prompt to be drawn.

        ``keyboard`` is what the emulated terminal can do with modified keys -
        see :data:`tests.e2e.terminal.KEYBOARDS`.
        """
        term = Terminal(
            [str(HX_BIN), *args],
            env=self.env(**(env or {})),
            cwd=cwd or self.project,
            columns=columns,
            rows=rows,
            record=self._snapshot,
            keyboard=keyboard,
        )
        self._terminals.append(term)
        if ready is not None:
            term.wait_for(ready, timeout=20)
            term.settle()
        return term

    def _snapshot(self, label: str, term: Terminal) -> None:
        self._screens += 1
        self.record.steps.append(Step("screen", label, text=term.text(), html=term.html()))

    # -- one-shot commands -------------------------------------------------

    def run(
        self,
        *args: str,
        input: str | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``hx`` without a terminal - a pipe on both ends, as a script would."""
        started = time.monotonic()
        result = subprocess.run(
            [str(HX_BIN), *args],
            input=input,
            capture_output=True,
            text=True,
            cwd=cwd or self.project,
            env=self.env(**(env or {})),
            timeout=timeout,
            stdin=None if input is not None else subprocess.DEVNULL,
        )
        self.record.steps.append(
            Step(
                "command",
                "hx " + " ".join(args),
                text=f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
                extra={
                    "argv": ["hx", *args],
                    "exit": result.returncode,
                    "seconds": round(time.monotonic() - started, 2),
                },
            )
        )
        if check:
            assert result.returncode == 0, result.stderr
        return result

    def note(self, text: str) -> None:
        self.record.steps.append(Step("note", text))

    # -- state HX leaves behind ---------------------------------------------

    def sessions(self) -> list[dict[str, Any]]:
        """Every session's ``meta.json``, oldest first."""
        metas = sorted(
            self.hx_home.glob("**/sessions/*/meta.json"), key=lambda p: p.stat().st_mtime
        )
        return [json.loads(p.read_text()) for p in metas]

    def close(self) -> None:
        for term in self._terminals:
            if term.alive:
                term.snapshot("screen at the end of the test")
            term.close()


@pytest.fixture()
def hx(sandbox_root: Path, project: Path, stub: Stub, record: TestRecord) -> Iterator[HX]:
    runner = HX(sandbox_root, project, stub, record)
    yield runner
    runner.close()


FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


class FakeClaude:
    """The ``claude`` CLI the claude-code route drives, scripted.

    Installed as ``claude`` on the test's ``PATH``. Every process HX starts
    reads the same queue of model turns and writes what it saw to one log.
    """

    def __init__(self, hx: HX) -> None:
        self.hx = hx
        self.state = hx.root / "claude"
        self.state.mkdir(exist_ok=True)
        self._write_shim(hx.bin / "claude")
        hx.extra_env["E2E_CLAUDE_DIR"] = str(self.state)

    @staticmethod
    def _write_shim(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n')
        path.chmod(0o755)

    def install(
        self, version: str, models: list[dict[str, Any]] | None = None, *, cask: str | None = None
    ) -> None:
        """Make the ``claude`` on PATH Claude Code ``version``, listing ``models``
        (the fake's default list when ``None``).

        ``cask`` lays it out the way Homebrew does - the binary under
        ``Caskroom/<cask>/<version>/``, linked from PATH - and installing again
        is then an upgrade: the old version stays, the link moves.
        """
        (self.state / "version").write_text(version)
        listed = self.state / "models.json"
        if models is None:
            listed.unlink(missing_ok=True)
        else:
            listed.write_text(json.dumps(models))
        on_path = self.hx.bin / "claude"
        on_path.unlink(missing_ok=True)
        if cask is None:
            self._write_shim(on_path)
            return
        binary = self.hx.root / "Caskroom" / cask / version / "claude"
        self._write_shim(binary)
        on_path.symlink_to(binary)

    def login(self, plan: str = "max", method: str = "claude.ai") -> None:
        (self.state / "auth.json").write_text(
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": method,
                    "email": "e2e@example.com",
                    "orgId": "org-e2e",
                    "orgName": "E2E Org",
                    "subscriptionType": plan,
                }
            )
        )

    def slow_status(self, seconds: float) -> None:
        """Make ``claude auth status`` take ``seconds`` to answer."""
        (self.state / "status_delay").write_text(str(seconds))

    def limits(self, model: str, context_window: int, max_output_tokens: int) -> None:
        """What a turn on ``model`` (a resolved name) reports as its limits."""
        path = self.state / "limits.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current[model] = [context_window, max_output_tokens]
        path.write_text(json.dumps(current))

    def use(self, model: str = "claude-code/claude-sonnet-5") -> None:
        self.hx.extra_env["HX_MODEL"] = model

    def script(self, *turns: dict[str, Any]) -> None:
        path = self.state / "script.json"
        queued = json.loads(path.read_text()) if path.exists() else []
        path.write_text(json.dumps([*queued, *turns]))

    def uninstall(self) -> None:
        (self.hx.bin / "claude").unlink()

    def log(self, kind: str | None = None) -> list[dict[str, Any]]:
        path = self.state / "log.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [e for e in entries if kind is None or e["kind"] == kind]

    def spawns(self, *, tools: bool | None = None) -> list[dict[str, Any]]:
        """Every ``claude -p`` HX started; ``tools`` keeps only working sessions
        (True) or only one-offs such as the naming call (False)."""
        found = self.log("spawn")
        if tools is None:
            return found
        return [s for s in found if ("--mcp-config" in s["argv"]) is tools]

    def note_log(self) -> None:
        self.hx.record.steps.append(
            Step("note", "claude CLI log", text="\n".join(json.dumps(e)[:400] for e in self.log()))
        )


@pytest.fixture()
def claude(hx: HX) -> Iterator[FakeClaude]:
    fake = FakeClaude(hx)
    yield fake
    fake.note_log()
    unexpected = fake.log("unexpected")
    assert not unexpected, f"unscripted Claude Code turns: {unexpected}"


def git(cwd: Path, *args: str) -> str:
    """Run git in a test's project, for setting one up."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "E2E",
            "GIT_AUTHOR_EMAIL": "e2e@example.com",
            "GIT_COMMITTER_NAME": "E2E",
            "GIT_COMMITTER_EMAIL": "e2e@example.com",
        },
    ).stdout

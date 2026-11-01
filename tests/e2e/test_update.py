"""Keeping HX up to date: `hx update` on demand, and a background update once a day.

Each test lays HX out the way `npm install --global` does - the bundle under
`lib/node_modules/@sunilaleti/hx` - and puts a fake `npm` on PATH that records
what it is asked to install. The stub server answers for npm's registry.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import HX, HX_BIN, VERSION
from tests.e2e.report import REPO
from tests.e2e.stub import Stub, say

PACKAGE = "@sunilaleti/hx"
NEWER = f"{int(VERSION.split('.')[0]) + 1}.0.0"


def install_from_npm(hx: HX) -> Path:
    """Put `hx` on PATH as a global npm install of this build, and `npm` as a fake
    that logs its arguments, prints `$E2E_NPM_STDERR` and exits `$E2E_NPM_EXIT`.
    Returns the log."""
    if HX_BIN.suffix != ".js":
        pytest.skip("needs the bundle, to lay it out as npm would")
    package = hx.root / "npm-global" / "lib" / "node_modules" / "@sunilaleti" / "hx"
    (package / "dist").mkdir(parents=True)
    shutil.copy(HX_BIN, package / "dist" / "hx.js")
    shutil.copy(REPO / "package.json", package / "package.json")
    (package / "node_modules").symlink_to(REPO / "node_modules")
    node = shutil.which("node")
    shim = hx.bin / "hx"
    shim.write_text(f'#!/bin/sh\nexec "{node}" "{package / "dist" / "hx.js"}" "$@"\n')
    log = hx.root / "npm.log"
    npm = hx.bin / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        '[ -n "$E2E_NPM_STDERR" ] && printf "%s\\n" "$E2E_NPM_STDERR" >&2\n'
        'sleep "${E2E_NPM_SECONDS:-0}"\n'
        f'printf "done\\n" >> "{log}.done"\n'
        'exit "${E2E_NPM_EXIT:-0}"\n'
    )
    npm.chmod(0o755)
    return log


def npm_calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# -- hx update ---------------------------------------------------------------------


def test_update_installs_the_newest_release(hx: HX, stub: Stub) -> None:
    """`hx update` asks npm's registry for the newest release and installs that exact
    version with the npm on PATH."""
    log = install_from_npm(hx)
    stub.hx_releases = {"latest": NEWER}
    result = hx.run("update", check=True)

    assert npm_calls(log) == [f"install --global {PACKAGE}@{NEWER}"]
    assert f"Updating hx {VERSION} to {NEWER}" in result.stdout, result.stdout
    assert f"Updated hx to {NEWER}." in result.stdout, result.stdout


def test_update_says_when_hx_is_current(hx: HX, stub: Stub) -> None:
    """On the newest release - or ahead of it - there is nothing to install. `hx upgrade`
    is the same command."""
    log = install_from_npm(hx)
    stub.hx_releases = {"latest": VERSION}
    for command in ("update", "upgrade"):
        result = hx.run(command, check=True)
        assert result.stdout.strip() == f"hx {VERSION} is up to date.", result.stdout
    stub.hx_releases = {"latest": "0.0.1"}
    assert "is up to date" in hx.run("update", check=True).stdout
    assert npm_calls(log) == []


def test_update_reports_what_npm_said_when_it_fails(hx: HX, stub: Stub) -> None:
    """A global prefix the user cannot write is npm's error to report; HX passes it
    through and says which install failed."""
    log = install_from_npm(hx)
    stub.hx_releases = {"latest": NEWER}
    result = hx.run(
        "update", env={"E2E_NPM_EXIT": "243", "E2E_NPM_STDERR": "npm error code EACCES"}
    )

    assert result.returncode == 1
    assert npm_calls(log) == [f"install --global {PACKAGE}@{NEWER}"]
    assert "npm error code EACCES" in result.stderr
    assert f"npm could not install {PACKAGE}@{NEWER} (exit 243)" in result.stderr


def test_update_from_a_checkout_does_not_touch_npm(hx: HX, stub: Stub) -> None:
    """Run from a clone's dist/, there is no npm install to update: HX says so and how
    to get the release, and leaves npm alone."""
    log = hx.root / "npm.log"
    stub.hx_releases = {"latest": NEWER}
    result = hx.run("update")

    assert result.returncode == 1
    assert "not from an npm install" in result.stderr, result.stderr
    assert f"npm install --global {PACKAGE}" in result.stderr
    assert not log.exists()


def test_update_with_the_registry_unreachable(hx: HX, stub: Stub) -> None:
    log = install_from_npm(hx)
    stub.hx_releases = None
    result = hx.run("update")

    assert result.returncode == 1
    assert "Could not reach the npm registry" in result.stderr, result.stderr
    assert npm_calls(log) == []


# -- the background updater ------------------------------------------------------------


def test_the_tui_updates_hx_in_the_background_once_a_day(hx: HX, stub: Stub) -> None:
    """With a newer release out, a session installs it without being asked and says to
    restart. The next session the same day does not ask the registry again."""
    log = install_from_npm(hx)
    stub.hx_releases = {"latest": NEWER}
    term = hx.tui(env={"HX_DISABLE_AUTOUPDATER": ""})
    term.wait_for(f"Updated hx to {NEWER}. Restart hx to use it.")
    term.settle()
    term.snapshot("updated in the background")
    assert npm_calls(log) == [f"install --global {PACKAGE}@{NEWER}"]
    term.press("ctrl+d")
    assert term.wait_exit() == 0

    stub.script(say("Still here."))
    term = hx.tui(env={"HX_DISABLE_AUTOUPDATER": ""})
    term.submit("hello")
    term.wait_for("Still here.")
    assert stub.hx_release_fetches == 1
    assert len(npm_calls(log)) == 1


def test_a_failed_background_update_says_how_to_see_why(hx: HX, stub: Stub) -> None:
    log = install_from_npm(hx)
    stub.hx_releases = {"latest": NEWER}
    term = hx.tui(env={"HX_DISABLE_AUTOUPDATER": "", "E2E_NPM_EXIT": "1"})
    term.wait_for(f"Could not update hx to {NEWER}")
    term.settle()
    term.snapshot("background update failed")
    assert "hx update" in term.text()
    assert npm_calls(log) == [f"install --global {PACKAGE}@{NEWER}"]


def test_quitting_does_not_wait_for_the_update(hx: HX, stub: Stub) -> None:
    """Quitting never waits on the updater: not on a registry slow to answer, and not on
    an install still running. npm carries on after HX exits and finishes the install, so
    nothing is left half-written."""
    log = install_from_npm(hx)
    stub.hx_releases = {"latest": NEWER}
    stub.hx_releases_hold = threading.Event()
    term = hx.tui(env={"HX_DISABLE_AUTOUPDATER": ""})
    term.wait_for("Ask HX")
    started = time.monotonic()
    term.press("ctrl+d")
    assert term.wait_exit(timeout=5) == 0
    assert time.monotonic() - started < 2, "quitting waited for the registry"
    stub.hx_releases_hold.set()

    # A day later, with an install that takes a while.
    (hx.hx_home / "update.json").write_text(json.dumps({"checkedAt": 0}))
    stub.hx_releases_hold = None
    term = hx.tui(env={"HX_DISABLE_AUTOUPDATER": "", "E2E_NPM_SECONDS": "3"})
    deadline = time.monotonic() + 10
    while not npm_calls(log) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert npm_calls(log) == [f"install --global {PACKAGE}@{NEWER}"]
    started = time.monotonic()
    term.press("ctrl+d")
    assert term.wait_exit(timeout=5) == 0
    assert time.monotonic() - started < 2, "quitting waited for npm"
    done = Path(f"{log}.done")
    assert not done.exists(), "npm finished before HX quit; the test proves nothing"
    deadline = time.monotonic() + 10
    while not done.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert done.exists(), "npm was killed with HX"


@pytest.mark.parametrize("off", ["setting", "environment"])
def test_background_updates_can_be_turned_off(hx: HX, stub: Stub, off: str) -> None:
    """`"autoUpdates": false` in settings, or `HX_DISABLE_AUTOUPDATER=1`: no registry
    check and no install, however new the release."""
    log = install_from_npm(hx)
    stub.hx_releases = {"latest": NEWER}
    env = {"HX_DISABLE_AUTOUPDATER": "1" if off == "environment" else ""}
    if off == "setting":
        (hx.hx_home / "settings.json").write_text(json.dumps({"autoUpdates": False}))
    stub.script(say("No update."))
    term = hx.tui(env=env)
    term.submit("hello")
    term.wait_for("No update.")
    time.sleep(1)
    assert stub.hx_release_fetches == 0
    assert npm_calls(log) == []


def test_print_mode_never_updates(hx: HX, stub: Stub) -> None:
    """`hx -p` is for scripts: it never reaches for the registry or npm."""
    log = install_from_npm(hx)
    stub.hx_releases = {"latest": NEWER}
    stub.script(say("Done."))
    hx.run("-p", "hello", env={"HX_DISABLE_AUTOUPDATER": ""}, check=True)
    assert stub.hx_release_fetches == 0
    assert npm_calls(log) == []

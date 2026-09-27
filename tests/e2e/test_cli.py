"""The command line outside the TUI: help, docs, credentials, MCP config, prompt."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from hx import __version__
from tests.e2e.conftest import HX
from tests.e2e.report import REPO

ECHO_SERVER = Path(__file__).parent / "fixtures" / "echo_server.py"


def test_version_and_help(hx: HX) -> None:
    """`hx --version` names the installed version; `hx --help` prints usage and exits 0."""
    version = hx.run("--version", check=True)
    assert version.stdout.strip() == f"hx {__version__}"

    usage = hx.run("--help", check=True)
    assert "an agent harness for the terminal" in usage.stdout
    for flag in ("--model MODEL", "--mode MODE", "--cwd PATH", "--image PATH"):
        assert flag in usage.stdout


def test_unknown_option_is_a_usage_error(hx: HX) -> None:
    """A typo in a flag exits 2 with the reason first, then the usage, all on stderr."""
    result = hx.run("--modle", "gpt-5")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error: unknown option --modle")
    assert "Usage:" in result.stderr


def test_image_flag_needs_print_mode(hx: HX) -> None:
    """--image only goes with -p; the TUI takes pasted images instead."""
    result = hx.run("--image", "shot.png")
    assert result.returncode == 2
    assert "--image goes with -p" in result.stderr


def test_docs_ship_with_the_binary(hx: HX) -> None:
    """`hx docs` lists the manual's sections and prints any one of them whole."""
    listing = hx.run("docs", check=True)
    assert "Sections (print one with `hx docs <section>`" in listing.stdout
    assert "Credentials" in listing.stdout

    section = hx.run("docs", "credentials", check=True)
    assert "HX_OPENROUTER_API_KEY" in section.stdout
    assert "Sections (print one" not in section.stdout

    missing = hx.run("docs", "no-such-section")
    assert missing.returncode != 0
    assert "no-such-section" in missing.stderr


def test_changelog_prints_this_release(hx: HX) -> None:
    """`hx changelog <version>` prints the section for the installed version - so bumping the
    version without writing its CHANGELOG.md section fails the suite before a tag exists."""
    result = hx.run("changelog", __version__, check=True)
    assert re.search(rf"^## \[?{re.escape(__version__)}\]?", result.stdout, re.MULTILINE)


def test_every_tag_has_a_changelog_section(hx: HX) -> None:
    """Every released tag in the repository has a section `hx changelog` can print."""
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout.split()
    for tag in tags:
        result = hx.run("changelog", tag.removeprefix("v"))
        assert result.returncode == 0, f"{tag} has no CHANGELOG.md section: {result.stderr}"


def test_auth_reports_where_the_key_comes_from(hx: HX) -> None:
    """`hx auth` masks the key and says the environment overrides the saved file."""
    result = hx.run("auth", check=True)
    first = result.stdout.splitlines()[0]
    assert first.startswith("openrouter")
    assert "sk-or-…cafe" in first
    assert "environment (HX_OPENROUTER_API_KEY)" in first
    assert "e2e0000" not in result.stdout, "the key must never be printed whole"
    assert "tavily" in result.stdout


def test_auth_set_and_clear_round_trip(hx: HX) -> None:
    """A key typed into `hx auth set` is hidden, stored 0600 and used; `hx auth clear` removes it."""
    env = {"HX_OPENROUTER_API_KEY": ""}
    # The key is read without echo, which needs a terminal: typed, not piped.
    term = hx.tui("auth", "set", env=env, ready="(input hidden)")
    term.type("sk-or-v1-saved000000000000000000000000beef")
    assert "beef" not in term.text(), "the key must not echo"
    term.press("enter")
    assert term.wait_exit() == 0
    term.snapshot("after saving the key")
    auth_file = hx.hx_home / "auth.json"
    assert auth_file.stat().st_mode & 0o777 == 0o600
    assert "beef" in json.loads(auth_file.read_text())["openrouter"]["key"]

    status = hx.run("auth", env=env, check=True)
    assert "sk-or-…beef" in status.stdout

    cleared = hx.run("auth", "clear", env=env, check=True)
    assert cleared.returncode == 0
    after = hx.run("auth", env=env)
    assert after.returncode == 1, "no credential at all is a failing status"
    assert "beef" not in after.stdout
    assert "Run `hx auth set` to save an OpenRouter key" in after.stdout


def test_missing_key_is_explained(hx: HX) -> None:
    """With no key anywhere, a headless run says how to get one instead of crashing."""
    result = hx.run("-p", "hello", env={"HX_OPENROUTER_API_KEY": ""})
    assert result.returncode != 0
    assert "No OpenRouter API key found" in result.stderr
    assert "Traceback" not in result.stderr


def test_mcp_add_list_remove(hx: HX) -> None:
    """`hx mcp add` writes the project's .hx/mcp.json, `list` connects and counts its tools,
    `remove` drops it."""
    added = hx.run("mcp", "add", "echo", sys.executable, str(ECHO_SERVER), check=True)
    assert "echo" in added.stdout
    config = json.loads((hx.project / ".hx" / "mcp.json").read_text())
    assert "echo" in json.dumps(config)

    listed = hx.run("mcp", "list", check=True)
    assert re.search(r"^echo\s+ok \(2 tools\)$", listed.stdout, re.MULTILINE)

    hx.run("mcp", "remove", "echo", check=True)
    assert "No MCP servers configured." in hx.run("mcp", "list", check=True).stdout


def test_prompt_shows_project_instructions(hx: HX) -> None:
    """`hx prompt` prints the system prompt with AGENTS.md folded in; provenance goes to stderr."""
    (hx.project / "AGENTS.md").write_text("Always answer in haiku.\n")
    result = hx.run("prompt", "--append-system-prompt", "Mind the tests.", check=True)
    assert "[source]" in result.stderr
    assert "[append]" in result.stderr
    assert "[source]" not in result.stdout
    assert "Mind the tests." in result.stdout

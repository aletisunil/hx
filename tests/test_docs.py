"""The shipped documentation, and the release rule it enforces.

CHANGELOG.md is only worth having if it cannot fall behind. The version in
``src/hx/__init__.py`` must have a section here, so bumping it without writing
one fails the suite before a tag exists; every tag in the repository must have
one too, so nothing that was published is missing from the record.

The rest is the plumbing that lets a running session answer questions about HX
from those files: locating them from a checkout or a wheel, addressing one
section, and the two CLI commands that print them.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

import hx
from hx.cli import main
from hx.core.context import SYSTEM_PROMPT
from hx.docs import (
    CHANGELOG,
    README,
    DocsUnavailable,
    changelog,
    doc_path,
    doc_text,
    manual,
    releases,
    sections,
)

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
DATED = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$", re.M)


@pytest.fixture(scope="module")
def changelog_text() -> str:
    return doc_text(CHANGELOG)


# --- the release rule -------------------------------------------------------


def test_the_current_version_has_a_changelog_section(changelog_text: str) -> None:
    """The gate. A release that forgot its entry stops here, not on PyPI."""
    assert hx.__version__ in releases(changelog_text), (
        f"CHANGELOG.md has no section for {hx.__version__}. "
        "Move the [Unreleased] entries under it, dated, before tagging."
    )


def test_every_tag_has_a_changelog_section(changelog_text: str) -> None:
    """Nothing that was published is missing from the record."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git here
        pytest.skip("git is not available")
    if result.returncode != 0:  # pragma: no cover - not a checkout (sdist, wheel)
        pytest.skip("not a git checkout")

    tagged = {line.lstrip("v") for line in result.stdout.split() if line}
    documented = set(releases(changelog_text))
    assert tagged - documented == set(), "these released tags have no CHANGELOG.md section"


def test_unreleased_section_exists(changelog_text: str) -> None:
    """Where landed-but-untagged work goes; renamed at release time."""
    assert "Unreleased" in releases(changelog_text)


def test_releases_are_dated_and_ordered_newest_first(changelog_text: str) -> None:
    entries = DATED.findall(changelog_text)
    assert entries, "no dated release headings"

    versions = [tuple(int(part) for part in version.split(".")) for version, _ in entries]
    assert versions == sorted(versions, reverse=True), "releases are not newest-first"

    dates = [date for _, date in entries]
    assert dates == sorted(dates, reverse=True), "release dates are not newest-first"

    for version, _ in entries:
        assert VERSION.match(version)


def test_the_changelog_is_packaged() -> None:
    """It has to be in the wheel, or `hx changelog` answers nothing once installed."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    included = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included == {README: f"hx/_docs/{README}", CHANGELOG: f"hx/_docs/{CHANGELOG}"}


def test_the_system_prompt_points_at_the_docs() -> None:
    """The whole point of shipping them: a session answers from the docs."""
    assert "hx docs" in SYSTEM_PROMPT
    assert "hx changelog" in SYSTEM_PROMPT


# --- locating and slicing ---------------------------------------------------


def test_docs_resolve_from_the_checkout() -> None:
    assert doc_path(README) == ROOT / README
    assert doc_path(CHANGELOG) == ROOT / CHANGELOG


def test_a_missing_document_is_reported_not_guessed() -> None:
    with pytest.raises(DocsUnavailable):
        doc_path("NOPE.md")


def test_sections_split_on_headings() -> None:
    found = sections("intro\n\n## One\n\na\n\n## Two\n\nb\n")
    assert found[""] == "intro"
    assert found["One"] == "## One\n\na"
    assert found["Two"] == "## Two\n\nb"


def test_bare_manual_lists_sections_rather_than_printing_everything() -> None:
    """The manual is thousands of tokens; a question deserves one section."""
    listed = manual()
    assert "Credentials" in listed or "Configuration" in listed
    assert len(listed) < len(doc_text(README)) / 2


def test_a_section_is_matched_case_insensitively() -> None:
    assert manual("configuration").startswith("## Configuration")


def test_an_unknown_section_names_the_ones_that_exist() -> None:
    with pytest.raises(DocsUnavailable, match="Sections:"):
        manual("quantum tunnelling")


def test_release_sections_drop_the_link_definitions(changelog_text: str) -> None:
    """The reference links at the foot of the file belong to no release."""
    oldest = releases(changelog_text)["0.1.0"]
    assert "]: https://github.com" not in oldest
    assert not oldest.endswith("-")


def test_changelog_addresses_one_release(changelog_text: str) -> None:
    assert changelog("0.1.5").startswith("## [0.1.5]")
    assert changelog("v0.1.5") == changelog("0.1.5")
    assert changelog("unreleased").startswith("## [Unreleased]")
    assert changelog("latest") == changelog(sorted(DATED.findall(changelog_text))[-1][0])


def test_an_unknown_version_names_the_ones_that_exist() -> None:
    with pytest.raises(DocsUnavailable, match="Versions:"):
        changelog("9.9.9")


# --- the commands -----------------------------------------------------------


def test_hx_docs_lists_sections(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["docs"]) == 0
    assert "hx docs <section>" in capsys.readouterr().out


def test_hx_docs_prints_one_section(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["docs", "credentials"]) == 0
    out = capsys.readouterr().out
    assert "hx auth set" in out
    assert "## Safety" not in out


def test_hx_docs_all_prints_the_manual(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["docs", "--all"]) == 0
    assert "## Safety" in capsys.readouterr().out


def test_hx_docs_flags_are_not_run_options() -> None:
    """`--all` must reach the docs command, not the argument parser's `-` branch."""
    from hx.cli import parse_args

    parsed = parse_args(["docs", "--all"])
    assert (parsed.command, parsed.rest) == ("docs", ("--all",))


def test_hx_changelog_prints_the_record(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["changelog"]) == 0
    assert f"[{hx.__version__}]" in capsys.readouterr().out

    assert main(["changelog", hx.__version__]) == 0
    assert capsys.readouterr().out.startswith(f"## [{hx.__version__}]")


def test_an_unresolvable_request_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["changelog", "9.9.9"]) == 1
    assert "no changelog entry" in capsys.readouterr().err

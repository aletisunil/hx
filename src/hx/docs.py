"""The shipped documentation: the manual and the release record.

``README.md`` and ``CHANGELOG.md`` live at the repository root - one copy,
rendered by GitHub and PyPI - and are force-included into the wheel under
``hx/_docs/`` (see ``pyproject.toml``). This module finds whichever of the two
locations exists, so ``hx docs`` and ``hx changelog`` behave the same from a
checkout and from an installed tool.

They are part of the product, not just the repo. The system prompt points a
session at these two commands, so "how do I set the API key?" or "what changed
in 0.1.5?" is answered from the documentation that shipped with the running
version rather than from the model's memory of some other release. Nothing is
read at import time or injected into the prefix: the text costs tokens only in
the turn that asks for it.
"""

from __future__ import annotations

import re
from pathlib import Path

README = "README.md"
CHANGELOG = "CHANGELOG.md"

_PACKAGED = Path(__file__).resolve().parent / "_docs"
_REPO_ROOT = Path(__file__).resolve().parents[2]

_HEADING = re.compile(r"^(#{2,4}) +(.+?)\s*$", re.M)
_RELEASE = re.compile(r"^## +\[([^\]]+)\]", re.M)
_LINK_DEF = re.compile(r"^\[[^\]]+\]: \S+$", re.M)


class DocsUnavailable(RuntimeError):
    """The document is not next to the code. A broken build, not a user error."""


def doc_path(name: str) -> Path:
    """Locate a shipped document: the packaged copy first, then the checkout."""
    for candidate in (_PACKAGED / name, _REPO_ROOT / name):
        if candidate.is_file():
            return candidate
    raise DocsUnavailable(f"{name} is not bundled with this installation of hx")


def doc_text(name: str) -> str:
    return doc_path(name).read_text()


def sections(text: str) -> dict[str, str]:
    """Split a document on its headings, title to section text.

    Subsections are addressable in their own right and are also part of their
    parent, so ``hx docs credentials`` prints just that and ``hx docs install``
    prints it in context - the question is usually about a subsection, and
    "Credentials" only reads as a heading of "Install" in the table of
    contents. The first heading of a given title wins, so an outer section is
    never shadowed by something nested deeper.

    The text above the first heading is kept under ``""`` so nothing is lost;
    callers that only want addressable sections can ignore that key.
    """
    matches = list(_HEADING.finditer(text))
    found: dict[str, str] = {"": (text[: matches[0].start()] if matches else text).strip()}

    for index, match in enumerate(matches):
        level = len(match.group(1))
        end = len(text)
        for later in matches[index + 1 :]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        found.setdefault(match.group(2), text[match.start() : end].strip())
    return found


def _match(query: str, titles: list[str]) -> list[str]:
    """Titles matching ``query``: exact (case-insensitive) wins outright,
    otherwise every substring match, so an ambiguous query can be reported."""
    wanted = query.strip().lower()
    exact = [title for title in titles if title.lower() == wanted]
    if exact:
        return exact
    return [title for title in titles if wanted in title.lower()]


def manual(section: str | None = None) -> str:
    """The README, whole or by section.

    Without a section this lists the section titles rather than printing the
    entire manual: the full text is thousands of tokens, and a session asking
    "how do I set the API key" should pay for one section, not all of them.
    """
    text = doc_text(README)
    found = sections(text)
    titles = [title for title in found if title]

    if section is None:
        # Indented by heading depth, so the listing reads as a table of
        # contents rather than a flat pile of names.
        listed = "\n".join(
            f"{'  ' * (len(match.group(1)) - 1)}{match.group(2)}"
            for match in _HEADING.finditer(text)
        )
        return (
            f"{found['']}\n\nSections (print one with `hx docs <section>`, "
            f"or all of it with `hx docs --all`):\n{listed}"
        )

    matches = _match(section, titles)
    if not matches:
        listed = ", ".join(titles)
        raise DocsUnavailable(f"no section matching {section!r}. Sections: {listed}")
    if len(matches) > 1:
        listed = ", ".join(matches)
        raise DocsUnavailable(f"{section!r} matches several sections: {listed}")
    return found[matches[0]]


def releases(text: str | None = None) -> dict[str, str]:
    """Every ``## [version]`` section of the changelog, newest first."""
    body = doc_text(CHANGELOG) if text is None else text
    found: dict[str, str] = {}
    matches = list(_RELEASE.finditer(body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        # The trailing `---` rules and the link definitions at the foot of the
        # file are page furniture, not part of the release they follow.
        section = _LINK_DEF.sub("", body[match.start() : end]).strip()
        found[match.group(1)] = section.rstrip("-").strip()
    return found


def changelog(version: str | None = None) -> str:
    """The changelog, whole or for one release.

    ``version`` accepts ``0.1.5``, ``v0.1.5``, ``unreleased`` or ``latest``.
    """
    text = doc_text(CHANGELOG)
    if version is None:
        return text.strip()

    found = releases(text)
    wanted = version.strip().lstrip("vV").lower()
    if wanted == "latest":
        released = [name for name in found if name.lower() != "unreleased"]
        if not released:
            raise DocsUnavailable("the changelog has no released versions yet")
        return found[released[0]]
    for name, section in found.items():
        if name.lower().lstrip("v") == wanted:
            return section
    listed = ", ".join(found)
    raise DocsUnavailable(f"no changelog entry for {version!r}. Versions: {listed}")

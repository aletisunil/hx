"""YAML frontmatter parsing, shared by skills and agent definitions.

Both are Markdown files whose first block is ``---``-delimited YAML. Keeping
one parser means a malformed file fails the same way in both places.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DELIMITER = "---"


@dataclass(slots=True)
class Document:
    metadata: dict[str, Any]
    body: str
    path: Path


def parse(text: str, path: Path) -> Document:
    """Split frontmatter from body.

    Raises:
        FrontmatterError: when the block is unterminated or is not a mapping.
            A file that silently parses as "no metadata" would be discovered
            and then quietly ignored, which is harder to debug than a failure.
    """
    stripped = text.lstrip("﻿")
    if not stripped.startswith(DELIMITER):
        return Document(metadata={}, body=stripped.strip(), path=path)

    lines = stripped.splitlines()
    closing = next(
        (i for i, line in enumerate(lines[1:], start=1) if line.strip() == DELIMITER), None
    )
    if closing is None:
        raise FrontmatterError(f"{path}: frontmatter opened with --- but never closed")

    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"{path}: invalid YAML frontmatter ({exc})") from exc

    if not isinstance(metadata, dict):
        raise FrontmatterError(f"{path}: frontmatter must be a mapping")

    return Document(metadata=metadata, body="\n".join(lines[closing + 1 :]).strip(), path=path)


def read(path: Path) -> Document:
    try:
        return parse(path.read_text(encoding="utf-8"), path)
    except OSError as exc:
        raise FrontmatterError(f"{path}: {exc}") from exc


def require(document: Document, *keys: str) -> None:
    missing = [key for key in keys if not str(document.metadata.get(key, "")).strip()]
    if missing:
        raise FrontmatterError(f"{document.path}: missing required field(s): {', '.join(missing)}")


def string_tuple(value: Any) -> tuple[str, ...]:
    """Accept either a YAML list or a comma-separated string."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


class FrontmatterError(Exception):
    pass

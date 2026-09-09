"""Session persistence around naming.

A session with no name shows up in ``/resume`` as a timestamped id, which is
what these tests exist to prevent regressing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from hx.core.session import list_sessions, load_session, new_session
from hx.paths import session_dir


def test_set_title_persists_to_meta(hx_home: Path, tmp_path: Path) -> None:
    session = new_session(tmp_path, "m")
    session.set_title("  Fix the parser  ")

    meta = json.loads((session_dir(session.meta.session_id) / "meta.json").read_text())
    assert meta["title"] == "Fix the parser"
    assert load_session(session.meta.session_id).meta.title == "Fix the parser"


def test_an_empty_title_leaves_the_session_unnamed(hx_home: Path, tmp_path: Path) -> None:
    session = new_session(tmp_path, "m")
    session.set_title("   ")
    assert session.meta.title is None


def test_naming_does_not_reorder_the_resume_list(hx_home: Path, tmp_path: Path) -> None:
    """``updated_at`` orders the picker, so it must track work, not naming."""
    from hx.core.messages import user_message

    older = new_session(tmp_path, "m")
    older.append(user_message("first"))
    time.sleep(0.01)
    newer = new_session(tmp_path, "m")
    newer.append(user_message("second"))

    before = older.meta.updated_at
    older.set_title("named later")

    assert older.meta.updated_at == before
    assert next(m.session_id for m in list_sessions(tmp_path)) == newer.meta.session_id


def test_the_title_survives_a_resume_with_messages(hx_home: Path, tmp_path: Path) -> None:
    from hx.core.messages import user_message

    session = new_session(tmp_path, "m")
    session.append(user_message("hello"))
    session.set_title("greeting")

    resumed = load_session(session.meta.session_id)
    assert resumed.meta.title == "greeting"
    assert resumed.messages[0].text() == "hello"


def test_empty_sessions_stay_out_of_the_resume_list(hx_home: Path, tmp_path: Path) -> None:
    """A session nobody spoke in is noise in the picker."""
    from hx.core.messages import user_message

    new_session(tmp_path, "m")
    used = new_session(tmp_path, "m")
    used.append(user_message("hello"))

    assert [m.session_id for m in list_sessions(tmp_path)] == [used.meta.session_id]

"""Tool output capping."""

from __future__ import annotations

from pathlib import Path

from hx.tools.output import cap_output


def test_small_output_passes_through() -> None:
    result = cap_output("hello", session_id="s", tool_use_id="t")
    assert result.text == "hello"
    assert not result.truncated
    assert result.spilled_path is None


def test_spill_path_cannot_escape_the_outputs_directory(hx_home: Path) -> None:
    """Tool ids come from the model and must not be trusted as filenames."""
    from hx.paths import session_outputs_dir
    from hx.tools.output import spill

    path = spill("body", "sess", "../../escape")
    assert path.parent == session_outputs_dir("sess")


def test_summary_reports_the_first_line_and_a_count() -> None:
    from hx.tools.output import summarize_for_ui

    assert summarize_for_ui("only line") == "only line"
    assert summarize_for_ui("first\nsecond\nthird") == "first (+2 lines)"
    assert summarize_for_ui("   ") == "no output"

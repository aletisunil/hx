"""Tool output capping."""

from __future__ import annotations

from pathlib import Path

from hx.tools.output import cap_output


def test_small_output_passes_through() -> None:
    result = cap_output("hello", session_id="s", tool_use_id="t")
    assert result.text == "hello"
    assert not result.truncated
    assert result.spilled_path is None


def test_large_output_keeps_head_and_tail_and_spills() -> None:
    text = "\n".join(f"line {i}" for i in range(50_000))
    result = cap_output(text, session_id="s", tool_use_id="t", char_cap=1000)
    assert result.truncated
    assert result.spilled_path is not None
    assert "line 0" in result.text
    assert "line 49999" in result.text
    assert len(result.text) < len(text)


def test_capped_output_names_the_spill_path_for_the_model() -> None:
    text = "x" * 100_000
    result = cap_output(text, session_id="s", tool_use_id="t", char_cap=100)
    assert result.spilled_path is not None
    assert result.spilled_path in result.text


def test_capped_text_respects_the_cap_including_the_notice() -> None:
    """The caller asked for a bound on what enters the context, not on the body."""
    text = "\n".join(f"line {i}" for i in range(50_000))
    for cap in (1_000, 5_000, 25_000):
        assert len(cap_output(text, session_id="s", tool_use_id="t", char_cap=cap).text) <= cap


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

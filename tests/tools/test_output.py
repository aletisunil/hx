"""Tool output capping."""

from __future__ import annotations

from hx.tools.output import cap_output
from tests.conftest import unimplemented


@unimplemented
def test_small_output_passes_through() -> None:
    result = cap_output("hello", session_id="s", tool_use_id="t")
    assert result.text == "hello"
    assert not result.truncated
    assert result.spilled_path is None


@unimplemented
def test_large_output_keeps_head_and_tail_and_spills() -> None:
    text = "\n".join(f"line {i}" for i in range(50_000))
    result = cap_output(text, session_id="s", tool_use_id="t", char_cap=1000)
    assert result.truncated
    assert result.spilled_path is not None
    assert "line 0" in result.text
    assert "line 49999" in result.text
    assert len(result.text) < len(text)


@unimplemented
def test_capped_output_names_the_spill_path_for_the_model() -> None:
    text = "x" * 100_000
    result = cap_output(text, session_id="s", tool_use_id="t", char_cap=100)
    assert result.spilled_path in result.text

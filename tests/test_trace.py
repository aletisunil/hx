"""Session traces.

A trace is the answer to "what actually went through", so the tests are mostly
about completeness: what a trace must not quietly leave out, and what a page
must not need the network for. The escaping test is the one that matters most -
a tool result is arbitrary text from the machine, and it lands inside a script
element.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hx.core.checkpoints import Checkpoint
from hx.core.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    tool_result_message,
    user_message,
)
from hx.core.session import Environment, load_session, new_session
from hx.core.usage import TurnUsage
from hx.trace import (
    TRACE_FILENAME,
    build_trace,
    default_trace_path,
    render_html,
    write_trace,
)


def _session(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A session exercising every block kind and every message flag."""
    session = new_session(tmp_path, "anthropic/claude-sonnet-4.5")
    session.append(user_message("why is the cache cold?"))
    session.append(
        assistant_message(
            [
                ThinkingBlock(text="the prefix moved", signature="sig"),
                TextBlock(text="Checking the breakpoints."),
                ToolUseBlock(id="t1", name="Read", input={"file_path": "a.py"}),
            ],
            model="anthropic/claude-sonnet-4.5",
        )
    )
    session.append(
        tool_result_message([ToolResultBlock(tool_use_id="t1", content="1 a3f9\tx = 1")])
    )
    session.append(Message(role="user", content=[TextBlock(text="branch: main")], ephemeral=True))
    session.append(Message(role="user", content=[TextBlock(text="an older turn")], compacted=True))
    session.record_usage(TurnUsage(input_tokens=10, output_tokens=5, cost_usd=0.5, latency_ms=12))
    session.record_checkpoint(Checkpoint(index=2, path="a.py", existed=True))
    return session


# --- completeness -----------------------------------------------------------


def test_every_message_is_in_the_trace(hx_home: Path, tmp_path: Path) -> None:
    """Including the two kinds a reader would most suspect of being dropped."""
    trace = build_trace(_session(tmp_path))

    assert len(trace["messages"]) == 5
    assert [m["index"] for m in trace["messages"]] == [0, 1, 2, 3, 4]
    assert any(m["ephemeral"] for m in trace["messages"]), "late-injected message dropped"
    assert any(m["compacted"] for m in trace["messages"]), "compacted message dropped"


def test_thinking_and_tool_blocks_survive(hx_home: Path, tmp_path: Path) -> None:
    trace = build_trace(_session(tmp_path))
    kinds = {block["type"] for message in trace["messages"] for block in message["content"]}
    assert {"text", "thinking", "tool_use", "tool_result"} <= kinds

    thinking = next(
        block
        for message in trace["messages"]
        for block in message["content"]
        if block["type"] == "thinking"
    )
    assert thinking["text"] == "the prefix moved"
    assert thinking["signature"] == "sig"


def test_usage_and_checkpoints_are_carried(hx_home: Path, tmp_path: Path) -> None:
    trace = build_trace(_session(tmp_path))
    assert trace["usage"]["turns"][0]["cost_usd"] == 0.5
    assert trace["checkpoints"][0]["path"] == "a.py"


def test_the_live_flag_is_recorded(hx_home: Path, tmp_path: Path) -> None:
    assert build_trace(_session(tmp_path), live=True)["live"] is True
    assert build_trace(_session(tmp_path))["live"] is False


# --- the environment --------------------------------------------------------


def test_the_recorded_environment_reaches_the_trace(hx_home: Path, tmp_path: Path) -> None:
    """A session traced from disk still knows what the model was told."""
    session = _session(tmp_path)
    session.record_environment(
        Environment(
            system_prompt="you are hx",
            tools=[{"name": "Read", "description": "read", "input_schema": {}}],
            project_context="# AGENTS.md",
            skills_index="deploy - ship it",
        )
    )

    trace = build_trace(load_session(session.meta.session_id))
    assert trace["system_prompt"] == "you are hx"
    assert trace["tools"][0]["name"] == "Read"
    assert trace["project_context"] == "# AGENTS.md"
    assert trace["skills_index"] == "deploy - ship it"


def test_an_explicit_prompt_wins_over_the_recorded_one(hx_home: Path, tmp_path: Path) -> None:
    """``/trace`` holds the loop, so it can answer before the first call has."""
    session = _session(tmp_path)
    session.record_environment(Environment(system_prompt="recorded"))
    assert build_trace(session, system_prompt="live")["system_prompt"] == "live"


def test_a_session_with_no_environment_traces_anyway(hx_home: Path, tmp_path: Path) -> None:
    """Sessions recorded before the environment was written down still trace."""
    trace = build_trace(_session(tmp_path))
    assert trace["system_prompt"] is None
    assert trace["tools"] == []


def test_the_environment_is_recorded_once(hx_home: Path, tmp_path: Path) -> None:
    session = new_session(tmp_path, "m")
    session.record_environment(Environment(system_prompt="first"))
    session.record_environment(Environment(system_prompt="second"))

    replayed = load_session(session.meta.session_id)
    assert replayed.environment is not None
    assert replayed.environment.system_prompt == "first"


def test_an_environment_record_from_a_newer_hx_still_loads(hx_home: Path, tmp_path: Path) -> None:
    """Unknown keys are dropped, the same way ``meta.json`` drops them."""
    from hx.paths import session_transcript_file

    session = new_session(tmp_path, "m")
    session.append(user_message("hi"))
    path = session_transcript_file(session.meta.session_id)
    with path.open("a", encoding="utf-8") as handle:
        record = {"kind": "environment", "data": {"system_prompt": "p", "invented_later": 1}}
        handle.write(json.dumps(record) + "\n")

    replayed = load_session(session.meta.session_id)
    assert replayed.environment is not None
    assert replayed.environment.system_prompt == "p"


# --- the page ---------------------------------------------------------------


def test_a_tool_result_cannot_close_the_script_element(hx_home: Path, tmp_path: Path) -> None:
    """The one that matters: tool output is arbitrary text from the machine.

    Unescaped, a result containing ``</script>`` ends the data element early and
    the rest of the session is parsed as markup.
    """
    session = new_session(tmp_path, "m")
    session.append(
        tool_result_message(
            [
                ToolResultBlock(
                    tool_use_id="t1",
                    content="</script><img src=x onerror=alert(1)> <!-- and a comment",
                )
            ]
        )
    )

    page = render_html(build_trace(session))
    body = page.split('<script id="trace-data" type="application/json">')[1].split("</script>")[0]
    assert "<" not in body and ">" not in body and "&" not in body
    assert "onerror" not in page.replace("\\u003e", "")[: page.index("trace-data")]
    # And it is still the payload: escaped JSON parses back to the same text.
    assert "</script>" in json.loads(body)["messages"][0]["content"][0]["content"]


def test_the_payload_round_trips_out_of_the_page(hx_home: Path, tmp_path: Path) -> None:
    """The page is also the machine-readable trace."""
    page = render_html(build_trace(_session(tmp_path)))
    body = page.split('<script id="trace-data" type="application/json">')[1].split("</script>")[0]
    assert len(json.loads(body)["messages"]) == 5


def test_the_page_fetches_nothing(hx_home: Path, tmp_path: Path) -> None:
    """A trace of a private repository must not phone anywhere, and must render
    on a machine with no network a year from now."""
    page = render_html(build_trace(_session(tmp_path)))
    for pattern in (r"https?://", r"<img\b", r"<link\b", r"\bsrc=", r"@import"):
        assert not re.search(pattern, page), f"the page references {pattern}"


def test_the_page_offers_dark_and_light(hx_home: Path, tmp_path: Path) -> None:
    page = render_html(build_trace(_session(tmp_path)))
    assert "prefers-color-scheme: dark" in page, "no system default"
    assert '[data-theme="dark"]' in page and '[data-theme="light"]' in page, "no manual override"
    assert 'id="theme"' in page, "no toggle"


def test_a_title_cannot_be_filled_with_the_payload(hx_home: Path, tmp_path: Path) -> None:
    """The template has two placeholders, and the title is user-controlled.

    Replacing them one after another lets the title's text be rescanned by the
    second pass: a session named after the data placeholder used to have the
    whole session spliced into its ``<title>``.
    """
    session = new_session(tmp_path, "m")
    session.set_title("__HX_TRACE_DATA__")
    page = render_html(build_trace(session))

    assert "<title>hx trace - __HX_TRACE_DATA__</title>" in page
    assert page.count('<script id="trace-data"') == 1
    body = page.split('<script id="trace-data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(body)["session"]["title"] == "__HX_TRACE_DATA__"


def test_a_payload_is_substituted_literally(hx_home: Path, tmp_path: Path) -> None:
    """A backslash in the payload is a backslash, not a group reference."""
    session = new_session(tmp_path, "m")
    session.append(user_message(r"\1 \g<0> back\slashes"))
    page = render_html(build_trace(session))

    body = page.split('<script id="trace-data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(body)["messages"][0]["content"][0]["text"] == r"\1 \g<0> back\slashes"


def test_the_title_is_escaped(hx_home: Path, tmp_path: Path) -> None:
    session = new_session(tmp_path, "m")
    session.set_title("<script>alert(1)</script>")
    page = render_html(build_trace(session))
    assert "<title>hx trace - &lt;script&gt;" in page


# --- writing ----------------------------------------------------------------


def test_write_trace_creates_the_file(hx_home: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    target = tmp_path / "out" / "trace.html"
    written = write_trace(build_trace(session), target)

    assert written == target
    assert written.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_the_default_path_is_beside_the_transcript(hx_home: Path, tmp_path: Path) -> None:
    """Not the working directory: a trace holds the whole conversation, and HX
    does not leave that inside somebody's checkout."""
    from hx.paths import session_dir

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    session = _session(checkout)

    path = default_trace_path(session)
    assert path == session_dir(session.meta.session_id) / TRACE_FILENAME
    assert hx_home in path.parents
    assert checkout not in path.parents


# --- the CLI ----------------------------------------------------------------


def test_hx_trace_writes_the_latest_session(
    hx_home: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from hx.cli import main

    session = _session(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["trace"]) == 0
    printed = Path(capsys.readouterr().out.strip())
    assert printed == default_trace_path(session)
    assert printed.exists()


def test_hx_trace_takes_a_session_id_and_a_path(hx_home: Path, tmp_path: Path, capsys) -> None:
    from hx.cli import main

    session = _session(tmp_path)
    target = tmp_path / "somewhere" / "t.html"

    assert main(["trace", session.meta.session_id, str(target)]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert target.exists()


def test_hx_trace_names_a_directory_target(hx_home: Path, tmp_path: Path, capsys) -> None:
    from hx.cli import main

    session = _session(tmp_path)
    (tmp_path / "into").mkdir()

    assert main(["trace", session.meta.session_id, str(tmp_path / "into")]) == 0
    assert (tmp_path / "into" / TRACE_FILENAME).exists()


def test_hx_trace_reports_an_unknown_session(hx_home: Path, tmp_path: Path, capsys) -> None:
    from hx.cli import main

    assert main(["trace", "no-such-session"]) == 1
    assert "no session" in capsys.readouterr().err


def test_hx_trace_reports_a_directory_with_no_sessions(
    hx_home: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from hx.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["trace"]) == 1
    assert "no sessions recorded" in capsys.readouterr().err


# --- reading the page -------------------------------------------------------


def test_an_encrypted_reasoning_block_says_so(hx_home: Path, tmp_path: Path) -> None:
    """A one-line thinking block is not a trace that dropped the rest.

    The Responses API returns a short summary in plaintext and the reasoning
    itself as ``encrypted_content``, which HX stores to replay and cannot read
    either. Labelling that plain "thinking" invites exactly the wrong
    conclusion, so the page says which of the two is on it.
    """
    page = render_html(build_trace(_session(tmp_path)))
    assert "function thinkingLabel(block)" in page
    assert "summary only, reasoning encrypted" in page
    assert "encrypted_content" in page, "the page never inspects the signature"


def test_a_thinking_block_with_no_summary_is_not_a_blank_box(hx_home: Path, tmp_path: Path) -> None:
    """One block in a real session came back signed and empty. Rendered as a
    label over nothing, it reads as a bug in the page."""
    page = render_html(build_trace(_session(tmp_path)))
    assert "The provider returned no summary for this block." in page


def test_expand_all_reaches_the_panels_and_not_only_the_transcript(
    hx_home: Path, tmp_path: Path
) -> None:
    """The prompt and the schemas sit in ``<details>`` panels.

    A reader who clicks "Expand all" and still has to open six bars by hand was
    told the button does something it does not.
    """
    page = render_html(build_trace(_session(tmp_path)))
    assert 'querySelectorAll("details")' in page, "expansion is still transcript-scoped"
    assert "Collapse all" in page, "expansion does not come back"


def test_a_body_built_after_expand_all_is_born_expanded(hx_home: Path, tmp_path: Path) -> None:
    """Switching the hidden kinds on builds nodes that did not exist at the
    click, and they have to arrive in the state the reader chose."""
    page = render_html(build_trace(_session(tmp_path)))
    assert "applyExpansion(row.node)" in page, "a new node is not brought into line"
    assert "setFolded(node, !EXPANDED)" in page, "a new fold ignores the chosen state"


def test_a_session_with_nothing_to_read_opens_its_panels(hx_home: Path, tmp_path: Path) -> None:
    """With no turns the panels are the whole page, and a page of shut bars
    reads as a page with nothing on it - which is what it was reported as."""
    empty = render_html(build_trace(new_session(tmp_path, "m")))
    assert "OPEN_PANELS" in empty and "messages.length === 0" in empty


def test_the_prose_panels_are_not_dressed_as_code(hx_home: Path, tmp_path: Path) -> None:
    """The prompt and the project context are written for a person. JSON keeps
    the monospace box; markdown does not need one."""
    session = new_session(tmp_path, "m")
    session.record_environment(Environment(system_prompt="hello", tools=[]))
    page = render_html(build_trace(session))
    assert 'el("pre", "body prose", text)' in page
    assert "pre.body.prose" in page, "the prose class has no styling"


def test_an_unpriced_session_shows_no_cost(hx_home: Path, tmp_path: Path) -> None:
    """A subscription route prices every call at zero. ``$0.0000`` beside a
    million cache-read tokens reads as a rounding artefact rather than free."""
    page = render_html(build_trace(_session(tmp_path)))
    assert "function totalCost()" in page
    assert 'priced ? money(total) : "-"' in page
    assert 'return "$0";' in page, "a real zero still renders four decimals"


# --- the CLI's one argument -------------------------------------------------


def test_hx_trace_takes_a_path_on_its_own(hx_home: Path, tmp_path: Path, monkeypatch) -> None:
    """``/trace <path>`` takes a path in one argument, so ``hx trace`` does too.

    Reading it as a session id produced ``no session '/Users/you/bug.html'`` -
    an error about the wrong thing entirely.
    """
    from hx.cli import main

    _session(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "bug.html"

    assert main(["trace", str(target)]) == 0
    assert target.exists()


def test_a_lone_session_id_is_still_a_session_id(hx_home: Path, tmp_path: Path, capsys) -> None:
    """The path sniff must not swallow the id it shares an argument slot with."""
    from hx.cli import main

    session = _session(tmp_path)
    assert main(["trace", session.meta.session_id]) == 0
    assert capsys.readouterr().out.strip() == str(default_trace_path(session))


def test_a_lone_relative_path_is_a_path(hx_home: Path, tmp_path: Path, monkeypatch) -> None:
    """Relative to the shell, not to ``--cwd``: that flag picks the project to
    trace, and a shell user typing ``out.html`` means where they are standing."""
    from hx.cli import main

    _session(tmp_path)
    elsewhere = tmp_path / "shell"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(["trace", "--cwd", str(tmp_path), "out.html"]) == 0
    assert (elsewhere / "out.html").exists()


# --- opening the trace must not take the terminal with it ------------------


def test_a_terminal_browser_is_not_launched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``webbrowser`` falls back to lynx/w3m when nothing graphical is
    registered, which launches into the terminal HX is drawing on.

    The user asked for a file, and lost the session display to a text browser
    they did not ask for. On a headless box the right answer is the path, which
    the caller prints either way.
    """
    import webbrowser

    from hx.trace import open_in_browser

    opened: list[str] = []

    class TerminalBrowser(webbrowser.GenericBrowser):
        def __init__(self) -> None:
            self.name = "lynx"

        def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
            opened.append(url)
            return True

    monkeypatch.setattr(webbrowser, "get", lambda *_a: TerminalBrowser())
    target = tmp_path / "trace.html"
    target.write_text("<html></html>")

    assert open_in_browser(target) is False
    assert opened == []


def test_a_graphical_browser_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import webbrowser

    from hx.trace import open_in_browser

    opened: list[str] = []

    class Graphical:
        name = "firefox"

        def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
            opened.append(url)
            return True

    monkeypatch.setattr(webbrowser, "get", lambda *_a: Graphical())
    target = tmp_path / "trace.html"
    target.write_text("<html></html>")

    assert open_in_browser(target) is True
    assert opened == [target.resolve().as_uri()]


def test_no_browser_at_all_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Over SSH this is the normal case, not a failure."""
    import webbrowser

    from hx.trace import open_in_browser

    def no_browser(*_args: object) -> object:
        raise webbrowser.Error("no runnable browser")

    monkeypatch.setattr(webbrowser, "get", no_browser)
    target = tmp_path / "trace.html"
    target.write_text("<html></html>")

    assert open_in_browser(target) is False

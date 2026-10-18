"""The persistent shell: what survives between commands, and what may not reach them."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.e2e.conftest import API_KEY, HX
from tests.e2e.stub import Stub, call, say


def test_a_command_reading_stdin_does_not_wedge_the_shell(hx: HX, stub: Stub) -> None:
    """A command that reads stdin gets end-of-file at once, and the commands after it still
    run - with the session's cd, exports and exit codes intact, heredocs working, and a
    command with an unclosed quote failing on its own instead of swallowing the next one."""
    heredoc_command = "cat <<'EOF'\nfirst (line\nsecond 'line\nEOF"
    unclosed_command = 'echo "unterminated'
    # Neither can be decomposed, so even bypass asks for them; exact rules cover them.
    settings = hx.project / ".hx" / "settings.json"
    settings.parent.mkdir()
    allow = [f"Bash({heredoc_command})", f"Bash({unclosed_command})"]
    settings.write_text(json.dumps({"permissions": {"allow": allow}}))
    stub.script(
        call("Bash", "Peek at stdin.", command="cat"),
        call("Bash", command='read -r line; echo "line=[$line] status=$?"'),
        call("Bash", command="mkdir -p sub && cd sub && export STAGE=two"),
        call("Bash", command='pwd; echo "stage=$STAGE"; touch marker.txt'),
        call("Bash", command=heredoc_command),
        call("Bash", command=unclosed_command),
        call("Bash", command="false"),
        call("Bash", command="echo still-alive"),
        say("Done."),
    )
    started = time.monotonic()
    hx.run("-p", "work in the shell", "--mode", "bypass", check=True)
    elapsed = time.monotonic() - started

    results = [request.tool_results()[-1] for request in stub.requests[1:]]
    hx.note("tool results:\n" + "\n---\n".join(results))
    cat, read, _cd, where, heredoc, quote, false, alive = results

    assert elapsed < 30, "a command waiting on stdin ran into the timeout"
    assert cat == "(no output)"
    assert "line=[] status=1" in read
    assert where.splitlines()[0] == str(hx.project / "sub")
    assert "stage=two" in where
    assert (hx.project / "sub" / "marker.txt").exists()
    assert heredoc == "first (line\nsecond 'line"
    assert "[exit code" in quote and "still-alive" not in quote
    assert "[exit code 1]" in false
    assert alive == "still-alive"


def test_hx_credentials_stay_out_of_the_shell(hx: HX, stub: Stub) -> None:
    """The keys HX itself signs in with are not handed to the commands it runs - not to the
    persistent shell, not to a background job - while the rest of the environment is."""
    stub.script(
        call(
            "Bash", command='env; echo "unprefixed=[$OPENROUTER_API_KEY] tavily=[$TAVILY_API_KEY]"'
        ),
        call("Bash", command="env > background-env.txt", run_in_background=True),
        say("Looked."),
    )
    hx.run(
        "-p",
        "show me the environment",
        "--mode",
        "bypass",
        env={
            "OPENROUTER_API_KEY": API_KEY,
            "TAVILY_API_KEY": "tvly-e2e-secret",
            "E2E_PROJECT_SETTING": "kept",
        },
        check=True,
    )

    shell = stub.requests[1].tool_results()[0]
    assert API_KEY not in shell
    assert "tvly-e2e-secret" not in shell
    assert "unprefixed=[] tavily=[]" in shell
    assert "E2E_PROJECT_SETTING=kept" in shell

    background = hx.project / "background-env.txt"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "E2E_PROJECT_SETTING" not in _read(background):
        time.sleep(0.1)
    written = _read(background)
    assert "E2E_PROJECT_SETTING=kept" in written
    assert API_KEY not in written
    assert "tvly-e2e-secret" not in written


def test_a_background_job_does_not_read_the_terminal(hx: HX, stub: Stub) -> None:
    """A background job gets no terminal on stdin, so a dev server's keyboard shortcuts
    cannot take keystrokes meant for HX's prompt."""
    probe = hx.project / "probe.txt"
    stub.script(
        call(
            "Bash",
            command=(
                "if [ -t 0 ]; then echo tty; else echo no-tty; fi > probe.tmp; "
                "cat >> probe.tmp; mv probe.tmp probe.txt"
            ),
            run_in_background=True,
        ),
        say("Started."),
    )
    term = hx.tui("--mode", "bypass")
    term.submit("start the probe")
    term.wait_for("Started.")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not probe.exists():
        time.sleep(0.1)
    term.snapshot("after the background job")
    assert probe.exists(), "the job blocked reading stdin"
    assert probe.read_text() == "no-tty\n"


def test_output_split_mid_character_keeps_its_characters(hx: HX, stub: Stub) -> None:
    """Non-ASCII output arrives intact even when a read ends in the middle of a character."""
    stub.script(
        call("Bash", command="python -c \"import sys; sys.stdout.write('a' + 'é' * 6000)\""),
        say("Printed."),
    )
    hx.run("-p", "print accents", "--mode", "bypass", check=True)

    output = stub.requests[1].tool_results()[0]
    assert "�" not in output
    assert output.count("é") > 1000


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""

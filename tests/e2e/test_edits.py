"""File edits land exactly as asked: line endings, modes, links, and what the user approves."""

from __future__ import annotations

import stat

from hx.tools import anchors
from tests.e2e.conftest import HX, git
from tests.e2e.stub import Stub, ToolCall, call, calls, say


def _anchor(text: str, line: int) -> str:
    """The anchor Read prints beside 1-based ``line`` of ``text``."""
    labels, _ = anchors.compute(anchors.split(text)[0])
    return labels[line - 1]


def test_a_crlf_file_takes_an_edit_written_with_plain_newlines(hx: HX, stub: Stub) -> None:
    """Read shows a CRLF file without its carriage returns, so the model's old_string has
    plain newlines. The edit still matches, the approval shows the real change, and the
    file keeps CRLF on every line - the edited ones included."""
    source = hx.project / "greet.py"
    source.write_bytes(b"def greet():\r\n    return 'hello'\r\n\r\nprint(greet())\r\n")
    stub.script(
        call("Read", file_path=str(source)),
        call(
            "Edit",
            file_path=str(source),
            old_string="def greet():\n    return 'hello'\n",
            new_string="def greet(name):\n    return f'hello, {name}'\n",
        ),
        say("Updated."),
    )
    term = hx.tui()
    term.submit("greet by name")
    term.wait_for("Permission needed")
    term.settle()
    term.snapshot("approval for an edit to a CRLF file")
    screen = term.text()
    assert "-    1 def greet():" in screen
    assert "+    1 def greet(name):" in screen

    term.press("enter")
    term.wait_for("Updated.")
    assert source.read_bytes() == (
        b"def greet(name):\r\n    return f'hello, {name}'\r\n\r\nprint(greet())\r\n"
    )
    assert not stub.requests[2].tool_results()[-1].startswith("edit 1:")


def test_hashline_replacements_ending_in_a_newline(hx: HX, stub: Stub) -> None:
    """A replacement that ends with a newline replaces the span and adds no blank line; a
    lone newline still means one blank line; a CRLF file stays CRLF."""
    lf = hx.project / "steps.txt"
    lf_text = "one\ntwo\nthree\nfour\nfive\nsix\n"
    lf.write_text(lf_text)
    crlf = hx.project / "steps.bat"
    crlf_text = "@echo off\r\necho old\r\nexit /b 0\r\n"
    crlf.write_bytes(crlf_text.encode())
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(lf)}, id="read_lf"),
            ToolCall("Read", {"file_path": str(crlf)}, id="read_crlf"),
        ),
        call(
            "Edit",
            file_path=str(lf),
            hashline=[
                {"start": _anchor(lf_text, 2), "new_string": "TWO\n"},
                {"start": _anchor(lf_text, 5), "new_string": "\n"},
            ],
        ),
        call(
            "Edit",
            file_path=str(crlf),
            hashline=[{"start": _anchor(crlf_text, 2), "new_string": "echo new\n"}],
        ),
        say("Edited."),
    )
    hx.run("-p", "edit both", "--mode", "acceptEdits", check=True)

    assert lf.read_text() == "one\nTWO\nthree\nfour\n\nsix\n"
    assert crlf.read_bytes() == b"@echo off\r\necho new\r\nexit /b 0\r\n"


def test_an_edit_keeps_the_file_mode_and_the_link(hx: HX, stub: Stub) -> None:
    """Editing a script leaves it executable, editing through a symlink changes the file it
    points at and leaves the link a link, and no temporary file is left behind."""
    script = hx.project / "deploy.sh"
    script.write_text("#!/bin/sh\necho staging\n")
    script.chmod(0o755)
    shared = hx.project / "shared"
    shared.mkdir()
    real = shared / "settings.ini"
    real.write_text("[app]\nlevel = info\n")
    link = hx.project / "settings.ini"
    link.symlink_to(real)
    fresh = hx.project / "notes.txt"
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(script)}, id="read_script"),
            ToolCall("Read", {"file_path": str(link)}, id="read_link"),
        ),
        call("Edit", file_path=str(script), old_string="staging", new_string="production"),
        call("Edit", file_path=str(link), old_string="info", new_string="debug"),
        call("Write", file_path=str(script), content="#!/bin/sh\necho production now\n"),
        call("Write", file_path=str(fresh), content="new\n"),
        call("Bash", command="./deploy.sh"),
        say("Done."),
    )
    hx.run("-p", "ship it", "--mode", "bypass", check=True)

    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert stub.requests[6].tool_results()[-1] == "production now"
    assert link.is_symlink()
    assert real.read_text() == "[app]\nlevel = debug\n"
    assert stat.S_IMODE(fresh.stat().st_mode) & 0o111 == 0
    leftovers = sorted(p.name for p in hx.project.rglob("*hx-tmp*"))
    assert leftovers == []


def test_the_approval_previews_the_file_under_cwd(hx: HX, stub: Stub) -> None:
    """Started with --cwd from another directory, an edit to a relative path is previewed
    against the project's file - not a same-named file where the shell happens to be."""
    (hx.project / "src").mkdir()
    (hx.project / "src" / "app.py").write_text("LEVEL = 'info'\n")
    (hx.root / "src").mkdir()
    (hx.root / "src" / "app.py").write_text("DECOY = True\n")
    stub.script(
        call("Read", file_path="src/app.py"),
        call("Edit", file_path="src/app.py", old_string="'info'", new_string="'debug'"),
        say("Raised the level."),
    )
    term = hx.tui("--cwd", str(hx.project), cwd=hx.root)
    term.submit("raise the log level")
    term.wait_for("Permission needed")
    term.settle()
    term.snapshot("approval under --cwd")
    screen = term.text()
    assert "-    1 LEVEL = 'info'" in screen
    assert "+    1 LEVEL = 'debug'" in screen
    assert "DECOY" not in screen and "no change" not in screen

    term.press("enter")
    term.wait_for("Raised the level.")
    assert (hx.project / "src" / "app.py").read_text() == "LEVEL = 'debug'\n"
    assert (hx.root / "src" / "app.py").read_text() == "DECOY = True\n"


def test_edit_counts_lines_that_start_with_dashes(hx: HX, stub: Stub) -> None:
    """The edit header counts every changed line, a SQL comment or ``++i`` included, and the
    diff under it shows each of them."""
    query = hx.project / "report.sql"
    query.write_text("-- monthly totals\nSELECT 1;\n")
    loop = hx.project / "loop.c"
    loop.write_text("int main() {\n  i++;\n}\n")
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(query)}, id="read_sql"),
            ToolCall("Read", {"file_path": str(loop)}, id="read_c"),
        ),
        call("Edit", file_path=str(query), old_string="-- monthly", new_string="-- weekly"),
        call("Edit", file_path=str(loop), old_string="  i++;", new_string="  ++i;\n  ++j;"),
        say("Edited."),
    )
    term = hx.tui("--mode", "acceptEdits")
    term.submit("tweak them")
    term.wait_for("Edited.")
    term.settle()
    term.snapshot("edit headers")
    screen = term.scrollback()
    assert "report.sql  +1 -1" in screen
    assert "-    1 -- monthly totals" in screen
    assert "+    1 -- weekly totals" in screen
    assert "loop.c  +2 -1" in screen
    assert "+    2   ++i;" in screen


def test_commands_that_write_through_an_operand_ask_first(hx: HX, stub: Stub) -> None:
    """``xxd`` and ``uniq`` write when given an output file, and ``xxd -r`` and ``sort
    --compress-program`` write or execute through a flag. Those need approval - also when
    the flag comes after the file, which xxd and BSD uniq read as the output's name, and
    when a long option is cut short, which getopt_long and git accept. The reading forms
    of the same commands still run without one."""
    (hx.project / "names.txt").write_text("ada\nada\nlin\n")
    (hx.project / "dump.hex").write_text("6869\n")
    git(hx.project, "init", "-q", "-b", "main")
    git(hx.project, "add", "names.txt")
    git(hx.project, "commit", "-q", "-m", "names")
    git(hx.project, "checkout", "-q", "-b", "topic")
    commands = [
        ("xxd -l 4 names.txt", True),
        ("uniq names.txt", True),
        ("uniq -c names.txt", True),
        ("sort -r names.txt", True),
        ("sort --reverse names.txt", True),
        ("git grep --line-number ada", True),
        ("xxd -r -p dump.hex restored.bin", False),
        ("xxd names.txt names.hex", False),
        ("uniq names.txt deduped.txt", False),
        ("uniq -f 1 names.txt deduped.txt", False),
        ("sort --compress-program=sh names.txt", False),
        ("sort --compress=sh names.txt", False),
        ("sort --out=sorted.txt names.txt", False),
        ("xxd names.txt -p", False),
        ("uniq names.txt -c", False),
        ("sed --expr='w sed-out.txt' names.txt", False),
        ("git grep --open-files-in=true ada", False),
        ("git branch --del main", False),
    ]
    stub.script(*(call("Bash", command=command) for command, _ in commands), say("Tried."))
    hx.run("-p", "try the commands", check=True)

    for index, (command, runs) in enumerate(commands, start=1):
        result = stub.requests[index].tool_results()[-1]
        refused = "needs approval" in result
        assert refused != runs, f"{command!r}: {result}"
    for written in ("restored.bin", "names.hex", "deduped.txt", "sorted.txt", "-p", "-c"):
        assert not (hx.project / written).exists(), written
    assert not (hx.project / "sed-out.txt").exists()
    assert git(hx.project, "branch", "--list", "main").strip(), "the branch was deleted"

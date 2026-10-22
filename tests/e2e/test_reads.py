"""What counts as read: Edit and Write take only the files this session's transcript showed.

The record belongs to the session, so it follows the session - across a quit and
``hx resume``, a ``/clear``, a ``/resume`` and a ``/rewind`` - and a subagent's reads stay
in the subagent's session.
"""

from __future__ import annotations

from tests.e2e.conftest import HX
from tests.e2e.stub import Stub, call, say

NOT_READ = "has not been read in this session"
CHANGED = "changed on disk since it was read"


def _edit(path: object, old: str, new: str) -> object:
    return call("Edit", file_path=str(path), old_string=old, new_string=new)


def test_a_resumed_session_keeps_what_it_read(hx: HX, stub: Stub) -> None:
    """The model reads two files and the user quits. Before `hx resume`, the user changes one
    of them by hand. The resumed model edits the untouched file at once, is told the other
    changed since it read it, and edits that one after reading it again."""
    config = hx.project / "config.toml"
    config.write_text('port = 8080\nhost = "localhost"\n')
    notes = hx.project / "notes.md"
    notes.write_text("# Notes\n\n- deploy on fridays\n")

    stub.script(
        call("Read", file_path=str(config)),
        call("Read", file_path=str(notes)),
        say("Port 8080; the notes say to deploy on fridays."),
    )
    term = hx.tui("--mode", "acceptEdits")
    term.submit("what port do we use, and what do the notes say")
    term.wait_for("Port 8080; the notes say to deploy on fridays.")
    term.press("ctrl+d")
    assert term.wait_exit() == 0

    notes.write_text("# Notes\n\n- deploy on fridays\n- never on holidays\n")
    stub.script(
        _edit(config, "port = 8080", "port = 9090"),
        _edit(notes, "- deploy on fridays", "- deploy on thursdays"),
        call("Read", file_path=str(notes)),
        _edit(notes, "- deploy on fridays", "- deploy on thursdays"),
        say("Port is 9090 and deploys moved to thursdays."),
    )
    term = hx.tui("resume", "--mode", "acceptEdits")
    term.wait_for("Port 8080; the notes say to deploy on fridays.")
    term.submit("move the port to 9090 and deploys to thursdays")
    term.wait_for("Port is 9090 and deploys moved to thursdays.")
    term.snapshot("edits after resume")

    turn = [r for r in stub.requests if r.tool_results() or "thursdays" in r.last_user_text()]
    first, after_config, after_notes, after_reread, after_edit = turn[-5:]
    assert "These files changed on disk since you last read them" in first.last_user_text()
    assert str(notes) in first.last_user_text()
    assert str(config) not in first.last_user_text()
    assert after_config.tool_results()[-1].startswith("Applied 1 edit"), after_config
    assert CHANGED in after_notes.tool_results()[-1]
    assert "never on holidays" in after_reread.tool_results()[-1]
    assert after_edit.tool_results()[-1].startswith("Applied 1 edit")
    assert config.read_text() == 'port = 9090\nhost = "localhost"\n'
    assert notes.read_text() == "# Notes\n\n- deploy on thursdays\n- never on holidays\n"


def test_clear_forgets_reads_and_resume_brings_them_back(hx: HX, stub: Stub) -> None:
    """After /clear the new session has read nothing, so an edit is refused and the model is
    told to use Read. /resume back to the first session restores its reads, and the same edit
    goes through."""
    config = hx.project / "config.toml"
    config.write_text('port = 8080\nhost = "localhost"\n')
    stub.title = "Config port"

    stub.script(call("Read", file_path=str(config)), say("Port is 8080."))
    term = hx.tui("--mode", "acceptEdits")
    term.submit("what port does config.toml use")
    term.wait_for("Port is 8080.")

    term.submit("/clear")
    term.wait_for("New session started.")
    stub.script(_edit(config, "port = 8080", "port = 1"), say("I need to read it first."))
    term.submit("set the port in config.toml to 1")
    term.wait_for("I need to read it first.")
    refused = stub.requests[-1].tool_results()[-1]
    assert NOT_READ in refused, refused
    assert "Read tool" in refused and "shell command does not count" in refused
    assert config.read_text().startswith("port = 8080")

    term.submit("/resume")
    term.wait_for("Resume session")
    term.press("enter")
    term.wait_for("Port is 8080.")
    stub.script(_edit(config, "port = 8080", "port = 1"), say("Port is 1."))
    term.submit("set it to 1")
    term.wait_for("Port is 1.")
    term.snapshot("edit after /resume")
    applied = stub.requests[-1].tool_results()[-1]
    assert applied.startswith("Applied 1 edit"), applied
    assert config.read_text().startswith("port = 1\n")


def test_rewind_keeps_reads_before_the_cut_and_drops_the_rest(hx: HX, stub: Stub) -> None:
    """A read in the first prompt and an edit in the second. Rewinding the second prompt puts
    the file back as it was read, so the model may edit it again. Rewinding the first prompt
    cuts the read too, and the next edit is refused."""
    target = hx.project / "a.txt"
    target.write_text("original\n")

    stub.script(call("Read", file_path=str(target)), say("It says original."))
    term = hx.tui("--mode", "acceptEdits")
    term.submit("what does a.txt say")
    term.wait_for("It says original.")
    stub.script(_edit(target, "original", "changed"), say("Changed it."))
    term.submit("change a.txt")
    term.wait_for("Changed it.")
    assert target.read_text() == "changed\n"

    term.submit("/rewind")
    term.wait_for("Rewind to")
    term.press("enter")
    term.wait_for("Rewound. 1 file(s) restored.")
    term.press("ctrl+u")
    stub.script(_edit(target, "original", "edited"), say("Edited it."))
    term.submit("edit a.txt instead")
    term.wait_for("Edited it.")
    applied = stub.requests[-1].tool_results()[-1]
    assert applied.startswith("Applied 1 edit"), applied
    assert target.read_text() == "edited\n"

    term.submit("/rewind")
    term.wait_for("Rewind to")
    term.press("down")
    term.press("enter")
    term.wait_for("Rewound. 1 file(s) restored.")
    term.snapshot("rewound to the first prompt")
    term.press("ctrl+u")
    assert target.read_text() == "original\n"
    stub.script(_edit(target, "original", "blind"), say("Refused."))
    term.submit("change a.txt to blind")
    term.wait_for("Refused.")
    refused = stub.requests[-1].tool_results()[-1]
    assert NOT_READ in refused, refused
    assert target.read_text() == "original\n"


def test_a_subagent_reads_for_itself(hx: HX, stub: Stub) -> None:
    """A subagent reads a file and reports on it. The parent never saw the file, so its edit
    is refused until it reads the file itself."""
    app = hx.project / "app.py"
    app.write_text("print('hi')\n")
    stub.script(
        call("Task", subagent_type="explore", prompt="What does app.py print?"),
        call("Read", file_path=str(app)),
        say("app.py prints 'hi'."),
        _edit(app, "'hi'", "'hello'"),
        call("Read", file_path=str(app)),
        _edit(app, "'hi'", "'hello'"),
        say("It prints hello now."),
    )
    hx.run("-p", "make app.py say hello", "--mode", "bypass", check=True)

    after_task, after_blind, after_read, after_edit = stub.requests[-4:]
    assert "prints 'hi'" in after_task.tool_results()[-1]
    assert NOT_READ in after_blind.tool_results()[-1]
    assert "print('hi')" in after_read.tool_results()[-1]
    assert after_edit.tool_results()[-1].startswith("Applied 1 edit")
    assert app.read_text() == "print('hello')\n"

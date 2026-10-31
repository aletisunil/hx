"""What a prompt-injected model cannot do without the user saying yes.

Each test drives a scripted model at the real `hx`, the way an attacker's text
would steer a real one: run a command whose dangerous part hides inside a
harmless one, read a key, fetch a server from a cloned repository. Headless,
an action that needs approval is refused - so a file that appears, or a secret
that reaches the model, is the escape.
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path

import pytest

from tests.e2e.conftest import HX
from tests.e2e.stub import Stub, ToolCall, call, calls, say

ECHO_SERVER = Path(__file__).parent / "fixtures" / "echo_server.py"
NEEDS_APPROVAL = "needs approval, but this session has no way to ask"


def settings(hx: HX, data: dict) -> None:
    (hx.hx_home / "settings.json").write_text(json.dumps(data))


# -- commands that hide another command ----------------------------------------

HIDDEN_COMMANDS = {
    "process substitution": ("/bin/bash", "cat <(touch {marker})"),
    "output process substitution": ("/bin/bash", "echo hi > >(touch {marker})"),
    "zsh =() substitution": ("/bin/zsh", "cat =(touch {marker})"),
    "zsh glob qualifier": ("/bin/zsh", "ls *(e:'touch {marker}':)"),
    "awk getline from a command": (
        "/bin/bash",
        "awk 'BEGIN {{ c = \"touch {marker}\"; c | getline }}'",
    ),
    "awk print into a command": (
        "/bin/bash",
        "awk 'BEGIN {{ c = \"touch {marker}\"; print 1 | c }}'",
    ),
    "awk include": ("/bin/bash", "awk '@include \"{marker}\"' /dev/null"),
    "find -fprint0": ("/bin/bash", "find . -maxdepth 0 -fprint0 {marker}"),
}


@pytest.mark.parametrize("case", list(HIDDEN_COMMANDS))
def test_a_command_hidden_inside_a_read_only_one_needs_approval(
    hx: HX, stub: Stub, case: str
) -> None:
    """`cat`, `ls`, `awk` and `find` read, so they run without asking - unless the command
    line carries another command inside it, or a flag that writes. Then it is refused
    headless, and the file the hidden command would make never appears."""
    shell, template = HIDDEN_COMMANDS[case]
    marker = hx.project / "escaped"
    (hx.project / "notes.txt").write_text("notes\n")
    stub.script(call("Bash", command=template.format(marker=marker.name)), say("Done."))
    hx.run("-p", "look around", env={"SHELL": shell}, check=True)

    assert not marker.exists(), f"{case}: ran without approval"
    assert NEEDS_APPROVAL in stub.requests[1].tool_results()[0]


def test_read_only_commands_still_run_without_asking(hx: HX, stub: Stub) -> None:
    """The stricter reading must not make ordinary inspection ask: awk's regex
    alternation, a subshell that changes directory, a pipeline from a redirect and a
    `find` that only prints all run headless, and their output reaches the model."""
    (hx.project / "data.txt").write_text("alpha 1\nbeta 2\ngamma 3\n")
    (hx.project / "sub").mkdir()
    (hx.project / "sub" / "inner.txt").write_text("inside\n")
    stub.script(
        call("Bash", command="awk '/alpha|gamma/ {print $1}' data.txt"),
        call("Bash", command="(cd sub && ls) && cat < data.txt | sort -r | head -1"),
        call("Bash", command="find . -name '*.txt' -print | sort"),
        say("Looked."),
    )
    hx.run("-p", "inspect the data", check=True)

    awk, subshell, find = (stub.requests[i].tool_results()[-1] for i in (1, 2, 3))
    assert awk.split() == ["alpha", "gamma"], awk
    assert "inner.txt" in subshell and "gamma 3" in subshell, subshell
    assert "./data.txt" in find and "./sub/inner.txt" in find, find


def test_a_deny_rule_holds_in_bypass_mode_however_the_command_is_spelled(
    hx: HX, stub: Stub
) -> None:
    """`deny: Bash(rm:*)` stops `rm` behind an environment assignment, a full path or a
    wrapper, even in bypass mode, where nothing else asks. A command that only mentions
    `rm` as an argument still runs."""
    settings(hx, {"permissions": {"deny": ["Bash(rm:*)"]}})
    victims = [hx.project / f"victim{i}" for i in range(4)]
    for victim in victims:
        victim.write_text("keep me\n")
    stub.script(
        calls(
            ToolCall("Bash", {"command": f"FOO=1 rm -f {victims[0].name}"}, id="assign"),
            ToolCall("Bash", {"command": f"/bin/rm -f {victims[1].name}"}, id="path"),
            ToolCall("Bash", {"command": f"nohup rm -f {victims[2].name}"}, id="nohup"),
            ToolCall(
                "Bash",
                {"command": f"xargs rm -f < /dev/null; command rm -f {victims[3].name}"},
                id="command",
            ),
            ToolCall("Bash", {"command": "echo rm -rf everything"}, id="echo"),
        ),
        say("Tried."),
    )
    hx.run("-p", "clean up", "--mode", "bypass", check=True)

    results = {
        m["tool_call_id"]: m["content"] for m in stub.requests[1].messages if m["role"] == "tool"
    }
    for victim in victims:
        assert victim.exists(), f"{victim.name} was removed"
    for call_id in ("assign", "path", "nohup", "command"):
        assert "matched a deny rule" in results[call_id], results[call_id]
    assert "rm -rf everything" in results["echo"]


# -- files that are not the project's -------------------------------------------


def test_credentials_need_approval_for_every_file_tool(hx: HX, stub: Stub) -> None:
    """The sandbox keeps the shell out of ~/.ssh and ~/.aws; Read, Grep and Write run inside
    HX, so they ask instead - by the real path, so a symlink in the project does not get
    round it. Headless, nothing of a key reaches the model. A search from $HOME still
    finds what it is looking for, but never inside a credential folder."""
    ssh = hx.home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")
    aws = hx.home / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[default]\naws_secret_access_key = PRIVATE KEY\n")
    (hx.home / "notes.txt").write_text("where is the PRIVATE KEY kept?\n")
    (hx.project / "key").symlink_to(ssh / "id_ed25519")
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(ssh / "id_ed25519")}, id="read"),
            ToolCall("Read", {"file_path": str(hx.project / "key")}, id="symlink"),
            ToolCall("Grep", {"pattern": "PRIVATE KEY", "path": str(aws)}, id="grep_aws"),
            ToolCall(
                "Grep",
                {"pattern": "PRIVATE KEY", "path": str(hx.home), "output_mode": "content"},
                id="grep_home",
            ),
        ),
        say("Searched."),
    )
    hx.run("-p", "find the key", check=True)

    results = {
        m["tool_call_id"]: m["content"] for m in stub.requests[1].messages if m["role"] == "tool"
    }
    for call_id in ("read", "symlink", "grep_aws"):
        assert NEEDS_APPROVAL in results[call_id], results[call_id]
    assert "notes.txt" in results["grep_home"], results["grep_home"]
    everything = json.dumps(stub.requests[1].messages)
    assert "secret" not in everything
    assert "aws_secret_access_key" not in everything


def test_a_ripgrep_search_skips_a_credential_folder_that_is_not_hidden(hx: HX, stub: Stub) -> None:
    """ripgrep skips hidden folders, which keeps most credentials out of a search - but
    ~/.config/gh is not hidden. A search of ~/.config finds the app's settings and
    nothing from the GitHub CLI's token file."""
    rg = shutil.which("rg")
    if rg is None:
        pytest.skip("ripgrep is not installed")
    (hx.bin / "rg").symlink_to(rg)
    config = hx.home / ".config"
    (config / "gh").mkdir(parents=True)
    (config / "gh" / "hosts.yml").write_text("github.com:\n  oauth_token: gho_e2e_secret\n")
    (config / "app").mkdir()
    (config / "app" / "settings.toml").write_text("oauth_token_file = '~/.app-token'\n")
    # Hidden: ripgrep skips it and the fallback walker does not, so its absence
    # shows that ripgrep ran the search.
    (config / ".notes").write_text("oauth_token goes here\n")
    stub.script(
        call("Grep", pattern="oauth_token", path=str(config), output_mode="content"),
        say("Found it."),
    )
    hx.run("-p", "where is the token configured?", check=True)

    output = stub.requests[1].tool_results()[0]
    assert "app/settings.toml" in output, output
    assert ".notes" not in output, "ripgrep did not run the search"
    assert "gho_e2e_secret" not in output and "hosts.yml" not in output, output


def test_an_explicit_allow_rule_opens_one_credential_file(hx: HX, stub: Stub) -> None:
    """Protection is a default, not a wall: a user who allows `Read(~/.ssh/config)` gets
    that file read without a prompt - and only that file."""
    ssh = hx.home / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Host build\n  HostName build.example.com\n")
    (ssh / "id_ed25519").write_text("secret\n")
    settings(hx, {"permissions": {"allow": ["Read(~/.ssh/config)"]}})
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(ssh / "config")}, id="config"),
            ToolCall("Read", {"file_path": str(ssh / "id_ed25519")}, id="key"),
        ),
        say("Read."),
    )
    hx.run("-p", "which hosts do I have?", check=True)

    results = {
        m["tool_call_id"]: m["content"] for m in stub.requests[1].messages if m["role"] == "tool"
    }
    assert "build.example.com" in results["config"]
    assert NEEDS_APPROVAL in results["key"]


def test_accept_edits_covers_the_project_and_nothing_else(hx: HX, stub: Stub) -> None:
    """acceptEdits writes the project's files without asking. Files outside it - a shell
    profile, an authorized_keys, the target of a symlink that leads out - and the two
    folders inside it that run code or grant permissions, .git and .hx, still ask."""
    (hx.project / ".git" / "hooks").mkdir(parents=True)
    (hx.project / "profile").symlink_to(hx.home / ".zshrc")
    inside = hx.project / "src" / "app.py"
    inside.parent.mkdir()
    targets = {
        "project": inside,
        "profile": hx.home / ".zshrc",
        "authorized_keys": hx.home / ".ssh" / "authorized_keys",
        "symlink": hx.project / "profile",
        "git_hook": hx.project / ".git" / "hooks" / "pre-commit",
        "hx_settings": hx.project / ".hx" / "settings.json",
    }
    stub.script(
        calls(
            *(
                ToolCall("Write", {"file_path": str(path), "content": "echo planted\n"}, id=name)
                for name, path in targets.items()
            )
        ),
        say("Written."),
    )
    hx.run("-p", "set things up", "--mode", "acceptEdits", check=True)

    results = {
        m["tool_call_id"]: m["content"] for m in stub.requests[1].messages if m["role"] == "tool"
    }
    assert inside.read_text() == "echo planted\n"
    for name in ("profile", "authorized_keys", "symlink", "git_hook", "hx_settings"):
        assert NEEDS_APPROVAL in results[name], f"{name}: {results[name]}"
    assert not (hx.home / ".zshrc").exists()
    assert not (hx.home / ".ssh" / "authorized_keys").exists()
    assert not targets["git_hook"].exists()
    assert not targets["hx_settings"].exists()


def test_the_tui_asks_before_a_key_or_a_hidden_command(hx: HX, stub: Stub) -> None:
    """Interactively, the same calls stop at the approval prompt with the file and the
    command shown, and a declined call leaves nothing behind."""
    ssh = hx.home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("secret\n")
    stub.script(
        call("Read", "Let me check your key.", file_path=str(ssh / "id_ed25519")),
        call("Bash", "And list the files.", command="cat <(touch escaped)"),
        say("Understood."),
    )
    term = hx.tui()
    term.submit("set up deploys")
    term.wait_for("Permission needed")
    term.settle()
    term.snapshot("asked before reading a key")
    assert "id_ed25519" in term.text()
    term.type("n")
    term.wait_for(lambda screen: "Permission needed" in screen and "touch escaped" in screen)
    term.settle()
    term.snapshot("asked before a hidden command")
    term.type("n")
    term.wait_for("Understood.")

    assert not (hx.project / "escaped").exists()
    assert "secret" not in json.dumps(stub.requests[1].messages)


# -- what arrives with a clone ---------------------------------------------------


def test_a_cloned_projects_mcp_server_waits_for_approval(hx: HX, stub: Stub) -> None:
    """A server in the project's .hx/mcp.json came with the clone, so HX does not start it
    - not for a session and not for `hx mcp list` - until `hx mcp approve`. The approval
    is for that command: when the file changes it, the server waits again."""
    marker = hx.root / "server-started"
    config = hx.project / ".hx" / "mcp.json"
    config.parent.mkdir()

    def write(extra: str) -> None:
        command = f"touch {marker}; exec {sys.executable} {ECHO_SERVER}{extra}"
        config.write_text(
            json.dumps({"mcpServers": {"echo": {"command": "/bin/sh", "args": ["-c", command]}}})
        )

    write("")
    stub.script(say("Hi."))
    first = hx.run("-p", "hello", check=True)
    assert not marker.exists(), "a cloned project's server ran at startup"
    assert "hx mcp approve echo" in first.stderr, first.stderr
    assert not any(name.startswith("mcp__echo") for name in stub.requests[0].tools)

    listed = hx.run("mcp", "list", check=True)
    assert not marker.exists(), "`hx mcp list` ran an unapproved server"
    assert "not approved" in listed.stdout and "hx mcp approve echo" in listed.stdout, listed.stdout

    approved = hx.run("mcp", "approve", "echo", check=True)
    assert "/bin/sh -c" in approved.stdout, approved.stdout

    stub.script(say("Hi again."))
    hx.run("-p", "hello", check=True)
    assert marker.exists()
    assert any(name.startswith("mcp__echo") for name in stub.requests[1].tools), stub.requests[
        1
    ].tools

    marker.unlink()
    write(" --crash")
    stub.script(say("Hi once more."))
    changed = hx.run("-p", "hello", check=True)
    assert not marker.exists(), "a changed server ran on the old approval"
    assert "changed since you approved it" in changed.stderr, changed.stderr


# -- what HX leaves on disk ------------------------------------------------------


def test_hx_home_is_private(hx: HX, stub: Stub) -> None:
    """Transcripts hold whatever the session read - source, logs, pasted secrets - so
    $HX_HOME is the user's alone, even when it already existed with open permissions."""
    hx.hx_home.chmod(0o755)
    stub.script(say("Noted."))
    hx.run("-p", "my token is abc123", check=True)

    assert stat.S_IMODE(hx.hx_home.stat().st_mode) == 0o700
    transcripts = list((hx.hx_home / "sessions").rglob("*.jsonl"))
    assert transcripts and "abc123" in transcripts[0].read_text()


# -- how calls run -----------------------------------------------------------------


def test_reads_before_a_write_run_together(hx: HX, stub: Stub) -> None:
    """Reads in one turn run concurrently even when a write follows them: a one-second
    PreToolUse hook on each Read starts for both at once, and the write still runs after
    both reads."""
    log = hx.root / "hook.log"
    hook = hx.root / "slow_hook.py"
    hook.write_text(
        "import json, sys, time\n"
        "event = json.load(sys.stdin)\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(f\"{time.time()} {event['tool_name']}\\n\")\n"
        "time.sleep(1)\n"
    )
    settings(
        hx,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Read|Write",
                        "hooks": [{"type": "command", "command": f"{sys.executable} {hook}"}],
                    }
                ]
            }
        },
    )
    for name in ("a.txt", "b.txt"):
        (hx.project / name).write_text(f"contents of {name}\n")
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(hx.project / "a.txt")}, id="read_a"),
            ToolCall("Read", {"file_path": str(hx.project / "b.txt")}, id="read_b"),
            ToolCall(
                "Write", {"file_path": str(hx.project / "c.txt"), "content": "c\n"}, id="write_c"
            ),
        ),
        say("Done."),
    )
    hx.run("-p", "copy things", "--mode", "acceptEdits", check=True)

    starts = [line.split() for line in log.read_text().splitlines()]
    assert [tool for _, tool in starts] == ["Read", "Read", "Write"], starts
    read_a, read_b, write = (float(at) for at, _ in starts)
    assert abs(read_a - read_b) < 0.5, f"the reads ran one after the other: {starts}"
    assert write - max(read_a, read_b) >= 0.9, f"the write did not wait for the reads: {starts}"
    assert (hx.project / "c.txt").read_text() == "c\n"


@pytest.mark.parametrize("shell", ["/bin/csh", "/bin/tcsh"])
def test_a_login_shell_that_is_not_posix_still_runs_commands(
    hx: HX, stub: Stub, shell: str
) -> None:
    """HX's shell protocol is POSIX sh. A user whose login shell is csh, tcsh or fish
    still gets commands run - by bash - rather than every one hanging until it times
    out."""
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    stub.script(
        call("Bash", command="echo hello from $0 && cd /tmp && pwd", timeout=15), say("Ran.")
    )
    hx.run("-p", "say hello", "--mode", "bypass", env={"SHELL": shell}, check=True, timeout=40)

    output = stub.requests[1].tool_results()[0]
    assert "hello from" in output, output
    assert "timed out" not in output and "exceeded" not in output, output

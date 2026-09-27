"""Print mode: one prompt, headless, stdout for the answer and stderr for the rest."""

from __future__ import annotations

import json
import struct
import uuid
import zlib
from pathlib import Path

from tests.e2e.conftest import HX, SANDBOX_REFUSALS, requires_sandbox
from tests.e2e.report import REPO
from tests.e2e.stub import GPT5, TEXT_ONLY, Reply, Stub, ToolCall, call, calls, fail, say


def png(path: Path, width: int = 4, height: int = 3) -> Path:
    """A real, tiny PNG, so the image is decoded and not just passed along."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def test_answer_streams_to_stdout(hx: HX, stub: Stub) -> None:
    """stdout carries only the model's words, so `hx -p` pipes cleanly; usage goes to stderr
    and counts every call the run made, the session-naming one included."""
    stub.script(say("The answer is 42.", prompt_tokens=1000, completion_tokens=8, cost=0.0031))
    result = hx.run("-p", "what is the answer?", check=True)

    assert result.stdout == "The answer is 42.\n"
    # 1000 + 300 in, 8 + 5 out, $0.0031 + $0.0042: the answer plus the title call.
    assert "[usage] in=1300 out=13 cache_read=0 cache_write=0 cost=$0.0073" in result.stderr

    (request,) = stub.requests
    assert request.last_user_text().endswith("what is the answer?")
    assert request.body["stream"] is True
    assert request.body["usage"] == {"include": True}
    assert request.headers["Authorization"].startswith("Bearer sk-or-v1-e2e")
    assert request.headers["X-Title"] == "HX"


def test_session_is_recorded_and_named(hx: HX, stub: Stub) -> None:
    """A headless run is still a session on disk, named by the model after the first exchange."""
    stub.title = "Explain the answer"
    stub.script(say("It is 42."))
    hx.run("-p", "what is the answer?", check=True)

    assert len(stub.title_requests) == 1
    (meta,) = hx.sessions()
    assert meta["title"] == "Explain the answer"


def test_tool_needing_approval_is_refused_headless(hx: HX, stub: Stub) -> None:
    """With nobody to ask, a write is refused and the model is told why; nothing is written."""
    target = hx.project / "notes.txt"
    stub.script(call("Write", file_path=str(target), content="hi\n"), say("I could not write it."))
    result = hx.run("-p", "write notes.txt", check=True)

    assert not target.exists()
    assert "[tool] Write" in result.stderr
    assert "no way to ask (non-interactive)" in result.stderr
    refusal = stub.requests[1].tool_results()[0]
    assert "needs approval, but this session has no way to ask" in refusal
    assert result.stdout == "I could not write it.\n"


def test_accept_edits_mode_writes_and_edits(hx: HX, stub: Stub) -> None:
    """--mode acceptEdits lets Write and Edit through; Read-before-Edit is honoured."""
    target = hx.project / "greeting.py"
    stub.script(
        call("Write", file_path=str(target), content='print("hello")\n'),
        call("Read", file_path=str(target)),
        call("Edit", file_path=str(target), old_string="hello", new_string="hello, world"),
        say("Updated the greeting."),
    )
    result = hx.run("-p", "greet the world", "--mode", "acceptEdits", check=True)

    assert target.read_text() == 'print("hello, world")\n'
    assert result.stderr.count("[tool] ok") == 3, result.stderr
    read_result = stub.requests[2].tool_results()[-1]
    assert 'print("hello")' in read_result
    assert result.stdout.strip() == "Updated the greeting."


def test_bash_runs_in_the_project(hx: HX, stub: Stub) -> None:
    """Bash output reaches the model; the shell starts in the project directory."""
    (hx.project / "marker.txt").write_text("present\n")
    stub.script(call("Bash", command="pwd && cat marker.txt"), say("Found it."))
    hx.run("-p", "look around", "--mode", "bypass", check=True)

    output = stub.requests[1].tool_results()[0]
    assert str(hx.project) in output
    assert "present" in output


@requires_sandbox
def test_sandbox_blocks_writes_outside_the_project(hx: HX, stub: Stub) -> None:
    """Even in bypass mode, the OS sandbox keeps a shell command from writing outside the
    project and its temp directory."""
    outside = REPO / f".e2e-sandbox-probe-{uuid.uuid4().hex}"
    stub.script(call("Bash", command=f"echo escaped > {outside}"), say("Blocked."))
    try:
        hx.run("-p", "try it", "--mode", "bypass", check=True)
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)
    output = stub.requests[1].tool_results()[0]
    assert any(refusal in output for refusal in SANDBOX_REFUSALS), output


@requires_sandbox
def test_sandbox_denies_credentials_inside_writable_paths(hx: HX, stub: Stub) -> None:
    """~/.ssh stays off limits to the shell, read and write, even in bypass mode."""
    ssh = hx.home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("PRIVATE KEY\n")
    stub.script(
        call("Bash", command=f"cat {ssh}/id_ed25519; echo planted >> {ssh}/authorized_keys"),
        say("Denied."),
    )
    hx.run("-p", "try it", "--mode", "bypass", check=True)

    output = stub.requests[1].tool_results()[0]
    assert "PRIVATE KEY" not in output
    assert not (ssh / "authorized_keys").exists()


@requires_sandbox
def test_sandbox_denies_credentials_that_do_not_exist_yet(hx: HX, stub: Stub) -> None:
    """A session opened in $HOME, with no ~/.ssh or ~/.netrc yet, cannot create them to plant
    a key or a login - and the rest of $HOME stays as writable as any project."""
    ssh, netrc = hx.home / ".ssh", hx.home / ".netrc"
    stub.script(
        call(
            "Bash",
            command=(
                "mkdir -p ~/.ssh && echo planted >> ~/.ssh/authorized_keys; "
                "echo 'machine evil.example login me password x' >> ~/.netrc; "
                "echo kept > ~/notes.txt && echo notes-written"
            ),
        ),
        say("Denied."),
    )
    hx.run("-p", "try it", "--mode", "bypass", cwd=hx.home, check=True)

    output = stub.requests[1].tool_results()[0]
    assert any(refusal in output for refusal in SANDBOX_REFUSALS), output
    assert not (ssh / "authorized_keys").exists()
    # Linux shadows a credential path by mounting over it, so it may now exist - empty.
    assert not ssh.exists() or not any(ssh.iterdir())
    assert not netrc.exists() or netrc.read_text() == ""
    assert "notes-written" in output
    assert (hx.home / "notes.txt").read_text() == "kept\n"


def test_parallel_tool_calls_all_answered(hx: HX, stub: Stub) -> None:
    """Several tool calls in one turn each get a result, matched by id, in one follow-up request."""
    for name in ("a.txt", "b.txt"):
        (hx.project / name).write_text(f"contents of {name}\n")
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(hx.project / "a.txt")}, id="read_a"),
            ToolCall("Read", {"file_path": str(hx.project / "b.txt")}, id="read_b"),
        ),
        say("Both read."),
    )
    hx.run("-p", "read both", check=True)

    follow_up = stub.requests[1].messages
    results = {m["tool_call_id"]: m["content"] for m in follow_up if m["role"] == "tool"}
    assert set(results) == {"read_a", "read_b"}
    assert "contents of a.txt" in results["read_a"]
    assert "contents of b.txt" in results["read_b"]


def test_plan_mode_offers_no_write_tools(hx: HX, stub: Stub) -> None:
    """--mode plan is read-only: the model is not even offered a way to change files."""
    stub.script(say("Here is the plan."))
    hx.run("-p", "plan it", "--mode", "plan", check=True)

    tools = stub.requests[0].tools
    assert "Read" in tools
    assert not {"Write", "Edit"} & set(tools)


def test_model_override(hx: HX, stub: Stub) -> None:
    """--model picks the model the request is sent to."""
    stub.script(say("hi"))
    hx.run("-p", "hello", "--model", GPT5, check=True)
    assert stub.requests[0].model == GPT5


def test_system_prompt_flags(hx: HX, stub: Stub) -> None:
    """--system-prompt @file replaces the prompt; --append-system-prompt adds to it."""
    prompt_file = hx.root / "prompt.md"
    prompt_file.write_text("You are a terse pirate.")
    stub.script(say("Arr."))
    hx.run(
        "-p",
        "hello",
        "--system-prompt",
        f"@{prompt_file}",
        "--append-system-prompt",
        "Never use emoji.",
        check=True,
    )
    system = stub.requests[0].system
    assert system.startswith("You are a terse pirate.")
    assert "Never use emoji." in system


def test_image_attached_to_prompt(hx: HX, stub: Stub) -> None:
    """--image sends the picture to a model that can see, as an image part of the user turn."""
    image = png(hx.project / "shot.png")
    stub.script(say("A red rectangle."))
    result = hx.run("-p", "what is this?", "--image", str(image), check=True)

    parts = stub.requests[0].last("user")["content"]
    urls = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
    assert len(urls) == 1
    assert urls[0].startswith("data:image/png;base64,")
    assert result.stdout.strip() == "A red rectangle."


def test_image_to_a_text_only_model(hx: HX, stub: Stub) -> None:
    """A model that cannot see is warned about, and told an image was left out - not sent one."""
    image = png(hx.project / "shot.png")
    stub.script(say("I cannot see images."))
    result = hx.run("-p", "what is this?", "--image", str(image), "--model", TEXT_ONLY, check=True)

    assert "does not accept images" in result.stderr
    body = json.dumps(stub.requests[0].messages)
    assert "image_url" not in body


def test_missing_image_is_a_usage_error(hx: HX) -> None:
    """A path that is not there fails fast, before any model call."""
    result = hx.run("-p", "look", "--image", "nope.png")
    assert result.returncode == 2
    assert "nope.png" in result.stderr


def test_transient_errors_are_retried(hx: HX, stub: Stub) -> None:
    """A 503 from upstream is retried; the user sees only the answer that eventually came."""
    stub.script(fail(503, "upstream overloaded"), say("Recovered."))
    result = hx.run("-p", "hello", check=True)
    assert result.stdout.strip() == "Recovered."
    assert len(stub.requests) == 2


def test_rejected_key_is_reported(hx: HX, stub: Stub) -> None:
    """A 401 is not retried: the run fails with OpenRouter's own message on stderr."""
    stub.script(fail(401, "No auth credentials found"))
    result = hx.run("-p", "hello")
    assert result.returncode == 1
    assert "OpenRouter 401: No auth credentials found" in result.stderr
    assert result.stdout.strip() == ""
    assert len(stub.requests) == 1


def test_reasoning_is_not_printed_as_the_answer(hx: HX, stub: Stub) -> None:
    """Thinking tokens stay out of stdout; only the answer is printed."""
    stub.script(Reply(text="Four.", reasoning="Two plus two is four."))
    result = hx.run("-p", "2+2?", check=True)
    assert result.stdout == "Four.\n"

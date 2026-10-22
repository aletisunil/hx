"""The claude-code route: a Claude subscription, through the ``claude`` CLI.

HX starts Claude Code with every built-in tool off and serves its own tools to
it as an MCP server over the CLI's control channel. These tests run the real
``hx`` against a scripted ``claude`` (``tests/e2e/fake_claude.py``) that speaks
the same stream-json protocol, so the argv, the environment, held tool calls
and rebuilt sessions are all exercised exactly as shipped.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import trustme

from tests.e2e import stub as openrouter
from tests.e2e.conftest import HX, FakeClaude
from tests.e2e.test_print import png


def say(text: str, **extra: Any) -> dict[str, Any]:
    return {"text": text, **extra}


def calls(*items: tuple[str, dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"calls": [{"name": name, "input": args} for name, args in items], **extra}


def prompts(claude: FakeClaude) -> list[Any]:
    return [entry["content"] for entry in claude.log("prompt")]


def sessions_folder(hx: HX, project: Path) -> Path:
    """Where Claude Code keeps a directory's sessions, in the test's private home."""
    return hx.home / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(project.resolve()))


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def test_print_turn_runs_tools_in_hx(hx: HX, claude: FakeClaude, project: Path) -> None:
    """A turn whose model reads two files at once, then globs, then answers: every call is
    held by the CLI while HX runs it, and one Claude Code process serves the whole turn."""
    (project / "a.txt").write_text("alpha=17\n")
    (project / "b.txt").write_text("beta=25\n")
    (project / "AGENTS.md").write_text("Always answer in one line.\n")
    claude.login()
    claude.use()
    claude.script(
        calls(
            ("Read", {"file_path": "a.txt"}),
            ("Read", {"file_path": "b.txt"}),
            thinking="Two files.",
        ),
        calls(("Glob", {"pattern": "*.txt"})),
        say("alpha+beta is 42."),
    )
    result = hx.run(
        "-p",
        "add alpha and beta",
        env={
            # Each would move Claude Code off the subscription; none may reach it.
            "ANTHROPIC_API_KEY": "sk-ant-api03-must-not-leak",
            "ANTHROPIC_BASE_URL": "https://gateway.example",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-subscription-token",
        },
        check=True,
    )

    assert result.stdout == "alpha+beta is 42.\n"
    assert "[usage]" in result.stderr and "cost=$0.0000" in result.stderr

    (session,) = claude.spawns(tools=True)
    argv = session["argv"]
    # Claude Code's own tools are off; HX's are its only ones, allowed up front.
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__hx"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert "--resume" not in argv
    # The project's AGENTS.md is appended to Claude Code's own prompt; HX's
    # base prompt is not sent, and the file is gone once the CLI read it.
    assert "Always answer in one line." in session["append"]
    assert "You are HX" not in session["append"]
    prompt_file = argv[argv.index("--append-system-prompt-file") + 1]
    assert not Path(prompt_file).exists()

    env = session["env"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env and "CLAUDECODE" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-subscription-token"
    assert env["ENABLE_TOOL_SEARCH"] == "false" and env["DISABLE_AUTO_COMPACT"] == "1"

    (tools,) = claude.log("tools")
    names = {tool["name"] for tool in tools["tools"]}
    assert {"Read", "Glob", "Grep", "Edit", "Write"} <= names

    (prompt,) = [p for p in claude.log("prompt") if p["pid"] == session["pid"]]
    assert text_of(prompt["content"]).startswith("add alpha and beta")
    assert prompt["client_composed"] is True

    results = {entry["id"]: entry["result"] for entry in claude.log("result")}
    texts = [text_of(r["content"]) for r in results.values()]
    assert any("alpha=17" in t for t in texts) and any("beta=25" in t for t in texts)
    assert any("a.txt" in t and "b.txt" in t for t in texts), "the Glob result"
    assert not any(r["isError"] for r in results.values())

    hx.note(f"{len(claude.spawns())} claude processes; {len(results)} tool calls answered")


def test_a_conversation_keeps_one_process_until_the_model_changes(
    hx: HX, claude: FakeClaude, project: Path
) -> None:
    """Prompt after prompt runs on the same Claude Code process. Switching model needs a new
    one, started on a session HX rebuilds from its own transcript: the earlier tool call, its
    result and the model's signed thinking are all replayed, and the file is gone after."""
    (project / "notes.txt").write_text("the treasure is under the oak\n")
    claude.login()
    claude.use()
    claude.script(
        calls(("Read", {"file_path": "notes.txt"}), thinking="Check the notes first."),
        say("The treasure is under the oak."),
        say("You asked where the treasure is."),
        say("Opus here: still under the oak."),
    )
    term = hx.tui()
    term.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("claude-sonnet-5 (sub)"))
    term.submit("where is the treasure?")
    term.wait_for("The treasure is under the oak.")
    term.submit("what did I ask?")
    term.wait_for("You asked where the treasure is.")
    term.settle()
    term.snapshot("two prompts, one Claude Code process")

    first, *_ = claude.spawns(tools=True)
    assert len(claude.spawns(tools=True)) == 1
    asked = [text_of(p["content"]) for p in claude.log("prompt") if p["pid"] == first["pid"]]
    assert [a.split("\n")[0] for a in asked] == ["where is the treasure?", "what did I ask?"]
    naming = [s for s in claude.spawns(tools=False) if s["system"]]
    assert naming and "You name things" in naming[0]["system"], "the session was named"

    # One model answers to it, so it is switched to rather than offered.
    term.submit("/model claude-opus")
    term.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("claude-opus-4-8 (sub)"))
    term.submit("and now?")
    term.wait_for("Opus here: still under the oak.")
    term.snapshot("after switching to Opus")

    sessions = claude.spawns(tools=True)
    assert len(sessions) == 2
    second = sessions[1]
    argv = second["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    folder = sessions_folder(hx, project)
    assert not folder.exists() or not list(folder.glob("*.jsonl")), "the rebuilt file was removed"

    records = second["resume"]
    roles = [r["message"]["role"] for r in records]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert records[0]["message"]["content"][0]["text"].startswith("where is the treasure?")
    thinking, call = records[1]["message"]["content"][:2]
    assert thinking == {
        "type": "thinking",
        "thinking": "Check the notes first.",
        "signature": thinking["signature"],
    } and thinking["signature"].startswith("sig-msg_fake_")
    assert call["type"] == "tool_use" and call["name"] == "mcp__hx__Read"
    (result,) = records[2]["message"]["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == call["id"]
    assert "the treasure is under the oak" in result["content"][0]["text"]
    assert records[5]["message"]["content"][0]["text"] == "You asked where the treasure is."
    (prompt,) = [p for p in claude.log("prompt") if p["pid"] == second["pid"]]
    assert text_of(prompt["content"]).startswith("and now?")

    term.press("ctrl+d")
    assert term.wait_exit() == 0
    exits = {e["pid"] for e in claude.log("exit")}
    assert {first["pid"], second["pid"]} <= exits, "no Claude Code outlives HX"


def test_a_held_call_waits_for_the_users_answer(hx: HX, claude: FakeClaude, project: Path) -> None:
    """A write needs approval. Claude Code holds the call for as long as the user takes, then
    gets HX's result - the file's new contents once allowed, a refusal once denied."""
    target = project / "notes.txt"
    claude.login()
    claude.use()
    claude.script(
        calls(("Write", {"file_path": str(target), "content": "first\n"})),
        say("Written."),
        calls(("Write", {"file_path": str(target), "content": "second\n"})),
        say("Understood, leaving it."),
    )
    term = hx.tui()
    term.submit("write notes")
    term.wait_for("Permission needed")
    term.snapshot("Claude Code waits while HX asks")
    time.sleep(2)
    term.type("y")
    term.wait_for("Written.")
    assert target.read_text() == "first\n"

    term.submit("overwrite them")
    term.wait_for("Permission needed")
    term.type("n")
    term.wait_for("Understood, leaving it.")
    term.settle()
    term.snapshot("after allowing one write and denying the next")

    assert target.read_text() == "first\n"
    allowed, denied = claude.log("result")
    assert not allowed["result"]["isError"]
    assert denied["result"]["isError"]
    assert "not permitted" in text_of(denied["result"]["content"])
    assert len(claude.spawns(tools=True)) == 1


def test_interrupting_a_reply_stops_claude_code(hx: HX, claude: FakeClaude, project: Path) -> None:
    """Esc while the model is writing stops the Claude Code process at once. The next prompt
    starts a new one on a rebuilt session that holds what had already been said."""
    claude.login()
    claude.use()
    story = " ".join(f"Sentence {n} of a long story." for n in range(1, 41))
    claude.script(say(story, chunks=40, chunk_delay=0.15), say("A short one, then."))
    term = hx.tui()
    term.submit("tell me a long story")
    term.wait_for("Sentence 3 of")
    term.press("esc")
    term.wait_for(lambda s: "Sentence 40" not in s and "interrupted" in s.lower())
    term.settle()
    term.snapshot("interrupted mid-reply")

    first = claude.spawns(tools=True)[0]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and first["pid"] not in {
        e["pid"] for e in claude.log("exit")
    }:
        time.sleep(0.1)
    assert first["pid"] in {e["pid"] for e in claude.log("exit")}, "the process was stopped"

    term.submit("shorter please")
    term.wait_for("A short one, then.")
    second = claude.spawns(tools=True)[1]
    records = second["resume"]
    assert records[0]["message"]["content"][0]["text"].startswith("tell me a long story")
    partial = records[1]["message"]["content"][0]["text"]
    assert partial.startswith("Sentence 1 of a long story.") and "Sentence 40" not in partial
    (prompt,) = [p for p in claude.log("prompt") if p["pid"] == second["pid"]]
    assert text_of(prompt["content"]).startswith("shorter please")


def test_a_steer_during_a_tool_reaches_the_model_as_its_own_turn(
    hx: HX, claude: FakeClaude, project: Path
) -> None:
    """A message sent while a tool runs is the user's next turn after the results. A held
    call cannot carry it, so Claude Code is restarted on a session ending with the results
    and the steer is what it is asked."""
    claude.login()
    claude.use()
    claude.script(
        calls(("Bash", {"command": "sleep 2; echo slept"})),
        say("Stopping there, as asked."),
    )
    term = hx.tui("--mode", "bypass")
    term.submit("run the slow thing")
    term.wait_for("sleep 2")
    term.submit("actually, stop after this")
    term.wait_for("queued")
    term.press("alt+enter")
    term.wait_for("Stopping there, as asked.")
    term.settle()
    term.snapshot("steered while the tool ran")

    first, second = claude.spawns(tools=True)
    assert first["pid"] in {e["pid"] for e in claude.log("exit")}
    records = second["resume"]
    assert records[-1]["message"]["role"] == "user"
    (result,) = records[-1]["message"]["content"]
    assert result["type"] == "tool_result" and "slept" in result["content"][0]["text"]
    (prompt,) = [p for p in claude.log("prompt") if p["pid"] == second["pid"]]
    assert text_of(prompt["content"]).startswith("actually, stop after this")


def test_a_usage_limit_is_explained(hx: HX, claude: FakeClaude) -> None:
    """A subscription out of usage says which limit, and when it resets."""
    claude.login(plan="pro")
    claude.use()
    resets = int(time.time()) + 3600
    claude.script(
        {
            "rate_limit": {"resetsAt": resets, "rateLimitType": "five_hour"},
            "error": "Claude AI usage limit reached",
        }
    )
    result = hx.run("-p", "hello")
    assert result.returncode != 0
    assert "five hour usage limit is reached. It resets at" in result.stderr
    # The time in the form the status bar's usage line gives it.
    assert time.strftime("%I:%M %p", time.localtime(resets)).lower() in result.stderr

    # The same limit in the words Claude Code uses once Extra Usage is spent
    # too: still the plan's limit, because the rejection says so.
    claude.script(
        {
            "rate_limit": {"resetsAt": resets, "rateLimitType": "seven_day"},
            "error": "You're out of extra usage · resets 6:30pm (America/New_York)",
        }
    )
    result = hx.run("-p", "hello")
    assert result.returncode != 0
    assert "seven day usage limit is reached. It resets at" in result.stderr
    assert "billed as Extra Usage" not in result.stderr


EXTRA_USAGE_400 = (
    "API Error: 400 You're out of extra usage. Add more at claude.ai/settings/usage and keep going."
)


def test_a_turn_billed_as_extra_usage_is_not_called_a_usage_limit(
    hx: HX, claude: FakeClaude, project: Path
) -> None:
    """Anthropic can refuse a turn as Extra Usage while the plan still has room - it decides
    from what the request carries, mostly the system prompt. HX says that, rather than that
    the plan's limit is reached; names its own part of the prompt; and the next turn runs."""
    (project / "AGENTS.md").write_text("Answer tersely.\n")
    claude.login()
    claude.use()
    claude.script(
        {
            # What Claude Code reports when it happens: the plan allowed, the
            # overage refused, and a 400 that no retry changes.
            "rate_limit": {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "resetsAt": int(time.time()) + 3600,
                "overageStatus": "rejected",
                "overageDisabledReason": "out_of_credits",
                "isUsingOverage": False,
            },
            "error": EXTRA_USAGE_400,
            "api_error_status": 400,
        },
        say("Fine now."),
    )
    term = hx.tui()
    term.submit("hello")
    term.wait_for("billed this turn as Extra Usage")
    screen = term.text()
    term.snapshot("a turn refused as Extra Usage")
    assert "usage limit is reached" not in screen
    flat = " ".join(screen.split())
    assert "plan's own limits are not reached" in flat
    assert "the project context" in flat
    assert "claude.ai/settings/usage" in flat
    # Exactly one attempt: a retry would only spend another request.
    assert len(claude.log("turn")) == 1

    term.submit("and now?")
    term.wait_for("Fine now.")
    term.snapshot("the next turn runs")


HX_PROMPT_COPY = """\
You are HX, an agentic coding assistant running in the user's terminal.

Work directly on the user's codebase using the tools available to you. Prefer
reading the relevant code over guessing at it. Make the change that was asked
for - do not narrow it, widen it, or substitute a different one.

Always write type hints.
"""


def test_hx_never_sends_its_own_prompt_to_claude_code(
    hx: HX, claude: FakeClaude, project: Path
) -> None:
    """HX's base prompt describes HX's harness; on this route the model gets Claude Code's
    instead, and another harness's prompt beside it can get a turn billed as Extra Usage. So a
    turn whose prompt still carries HX's text - a system prompt override copied from HX's and
    edited, an AGENTS.md quoting it - is refused before Claude Code starts, saying where the
    text is. A prompt of the user's own, or an addition to HX's, goes through."""
    claude.login()
    claude.use()

    # An override that began as a copy of HX's prompt, rewrapped and edited.
    override = hx.hx_home / "system-prompt.md"
    override.write_text(HX_PROMPT_COPY.replace("\nreading", " reading"))
    result = hx.run("-p", "hello")
    assert result.returncode != 0
    flat = " ".join(result.stderr.split())
    assert "HX's own system prompt is in the system prompt" in flat
    assert '("You are HX, an agentic coding…")' in flat
    assert "hx prompt" in flat
    assert claude.spawns(tools=True) == []

    # The same text quoted in an AGENTS.md.
    override.unlink()
    (project / "AGENTS.md").write_text(
        "# Notes\n\nOur old harness said:\n\n" + HX_PROMPT_COPY.split("\n\n")[1] + "\n"
    )
    result = hx.run("-p", "hello")
    assert result.returncode != 0
    flat = " ".join(result.stderr.split())
    assert "HX's own system prompt is in the project context" in flat
    assert "AGENTS.md" in flat
    assert claude.spawns(tools=True) == []

    # The user's own prompt, naming HX in passing, plus an addition: both go through.
    (project / "AGENTS.md").write_text("Answer tersely.\n")
    override.write_text("You help maintain HX's plugins. Be brief.\n")
    (hx.hx_home / "system-prompt-append.md").write_text("Prefer pathlib.\n")
    claude.script(say("ok"))
    result = hx.run("-p", "hello", check=True)
    assert result.stdout == "ok\n"
    (session,) = claude.spawns(tools=True)
    assert "You help maintain HX's plugins." in session["append"]
    assert "Prefer pathlib." in session["append"]
    assert "Answer tersely." in session["append"]
    assert "You are HX" not in session["append"]

    # An addition to HX's built-in prompt: the built-in part is dropped, the addition kept.
    override.unlink()
    claude.script(say("ok again"))
    result = hx.run("-p", "hello", check=True)
    assert result.stdout == "ok again\n"
    session = claude.spawns(tools=True)[-1]
    assert "Prefer pathlib." in session["append"]
    assert "You are HX" not in session["append"]


def test_a_subagent_inheriting_hx_prompt_is_refused_and_the_parent_hears_why(
    hx: HX, claude: FakeClaude, project: Path
) -> None:
    """A user-defined agent whose prompt pastes HX's in front of its role - the way subagent
    prompts often inherit their parent's - is refused before its process starts, and the
    parent model gets the reason as the Task result rather than an empty report."""
    agents = project / ".hx" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews a change.\n---\n"
        + HX_PROMPT_COPY
        + "\nYou are now a code reviewer. Report problems only.\n"
    )
    claude.login()
    claude.use()
    claude.script(
        calls(
            (
                "Task",
                {
                    "subagent_type": "reviewer",
                    "prompt": "Review the last commit.",
                    "description": "review",
                },
            )
        ),
        say("The reviewer could not run."),
    )
    result = hx.run("--mode", "bypass", "-p", "review my change", check=True)
    assert result.stdout == "The reviewer could not run.\n"

    # The parent's process only: the subagent's never started.
    assert len(claude.spawns(tools=True)) == 1
    task = next(e for e in claude.log("result") if e["name"] == "Task")
    assert task["result"]["isError"] is True
    report = " ".join(text_of(task["result"]["content"]).split())
    assert "HX's own system prompt is in the system prompt" in report
    assert "agents" in report


def test_a_crash_is_reported_and_the_next_turn_recovers(hx: HX, claude: FakeClaude) -> None:
    """Claude Code dying mid-turn surfaces what it printed; the next prompt starts afresh."""
    claude.login()
    claude.use()
    claude.script({"crash": "fatal: segment of something broke"}, say("Back again."))
    term = hx.tui()
    term.submit("hello")
    term.wait_for("fatal: segment of something broke")
    assert "Claude Code exited unexpectedly (code 1)" in term.text()
    term.submit("hello again")
    term.wait_for("Back again.")
    term.snapshot("recovered after a crash")
    assert len(claude.spawns(tools=True)) == 2


CERT_FAILURE = (
    "API Error: Unable to connect to API: SSL certificate verification failed. "
    "Check your proxy or corporate SSL certificates"
)
"""What Claude Code 2.1 reports when its connection meets a CA it does not trust."""


def corporate_ca(hx: HX) -> Path:
    """A network's CA certificate, as a PEM file HX can load."""
    path = hx.root / "corp-ca.pem"
    trustme.CA(organization_name="Corp").cert_pem.write_to_path(str(path))
    return path


def test_corporate_tls_settings_reach_claude_code(
    hx: HX, claude: FakeClaude, project: Path
) -> None:
    """Behind a network that intercepts TLS, what makes HX's own clients work must make the
    CLI's work too: HX's CA bundle becomes Node's extra CAs, HX_SSL_NO_VERIFY becomes
    Node's switch, and Claude Code's own certificate settings survive the scrub - while
    everything that moves billing off the subscription is still dropped."""
    (project / "a.txt").write_text("alpha=17\n")
    bundle = corporate_ca(hx)
    claude.login()
    claude.use()
    claude.script(calls(("Read", {"file_path": "a.txt"})), say("alpha is 17."))
    corporate = {
        "HX_CA_BUNDLE": str(bundle),
        "HTTPS_PROXY": "http://proxy.corp.example:3128",
        "CLAUDE_CODE_CERT_STORE": "system",
        "CLAUDE_CODE_CLIENT_CERT": "/etc/corp/device.pem",
        "CLAUDE_CODE_CLIENT_KEY": "/etc/corp/device.key",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE": "device-passphrase",
        "ANTHROPIC_BASE_URL": "https://gateway.example",
        "CLAUDE_CODE_USE_BEDROCK": "1",
    }
    result = hx.run("-p", "what is alpha", env=corporate, check=True)
    assert result.stdout == "alpha is 17.\n"
    assert f"Verifying TLS against {bundle}." in result.stderr

    (session,) = claude.spawns(tools=True)
    statuses = claude.log("status")
    assert statuses, "HX asked Claude Code whether it is signed in"
    for env in [session["env"], *(s["env"] for s in statuses)]:
        assert env["NODE_EXTRA_CA_CERTS"] == str(bundle)
        assert "NODE_TLS_REJECT_UNAUTHORIZED" not in env
        assert env["CLAUDE_CODE_CERT_STORE"] == "system"
        assert env["CLAUDE_CODE_CLIENT_CERT"] == "/etc/corp/device.pem"
        assert env["CLAUDE_CODE_CLIENT_KEY"] == "/etc/corp/device.key"
        assert env["CLAUDE_CODE_CLIENT_KEY_PASSPHRASE"] == "device-passphrase"
        assert "ANTHROPIC_BASE_URL" not in env and "CLAUDE_CODE_USE_BEDROCK" not in env

    # Node already pointed at a CA file is the user's own setup for Claude Code, and it
    # already worked there: HX's bundle does not replace it. Verification switched off
    # in HX is switched off in the CLI too, and said out loud.
    claude.script(say("Still here."))
    result = hx.run(
        "-p",
        "hello",
        env={**corporate, "NODE_EXTRA_CA_CERTS": "/etc/corp/node-ca.pem", "HX_SSL_NO_VERIFY": "1"},
        check=True,
    )
    assert "HX_SSL_NO_VERIFY is set: TLS certificates are not being verified." in result.stderr
    env = claude.spawns(tools=True)[-1]["env"]
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/corp/node-ca.pem"
    assert env["NODE_TLS_REJECT_UNAUTHORIZED"] == "0"

    # Nothing set, nothing added: a stock machine's CLI runs as it would on its own.
    claude.script(say("Plain."))
    hx.run("-p", "hello", check=True)
    env = claude.spawns(tools=True)[-1]["env"]
    assert not any(name.startswith("NODE_") for name in env)
    assert not any(name.startswith("CLAUDE_CODE_CLIENT") for name in env)

    # A bundle that holds no certificates - a DER file, a stale path's leftovers - is said
    # out loud and left out, as OpenSSL and Node leave it: HX starts and verifies against
    # the system store, and Claude Code is not handed a file it cannot read either.
    broken = hx.root / "broken-ca.pem"
    broken.write_text("-----BEGIN CERTIFICATE-----\nnot a certificate\n")
    claude.script(say("Still verifying."))
    result = hx.run("-p", "hello", env={"SSL_CERT_FILE": str(broken)}, check=True)
    assert result.stdout == "Still verifying.\n"
    assert f"SSL_CERT_FILE names {broken}, which holds no usable CA certificates" in result.stderr
    assert "Verifying TLS against the system trust store instead." in result.stderr
    assert "NODE_EXTRA_CA_CERTS" not in claude.spawns(tools=True)[-1]["env"]


def test_a_certificate_failure_says_how_to_fix_it(hx: HX, claude: FakeClaude) -> None:
    """Claude Code refusing the network's CA names HX's fix after its own words; with a
    bundle already set, it says which file to check instead. The conversation carries on
    once the network lets it."""
    claude.login()
    claude.use()
    claude.script({"error": CERT_FAILURE})
    result = hx.run("-p", "hello")
    assert result.returncode != 0
    assert (
        f"Claude Code: {CERT_FAILURE}. If your network intercepts TLS, add its CA to the "
        "system trust store or set HX_CA_BUNDLE to its PEM file."
    ) in result.stderr

    bundle = corporate_ca(hx)
    claude.script({"error": CERT_FAILURE}, say("Through the proxy now."))
    term = hx.tui(env={"HX_CA_BUNDLE": str(bundle)})
    term.submit("hello")
    # The path is wider than the screen and wraps mid-name, and where the words after it
    # break depends on how long the temp dir is: wait and compare without the breaks.
    term.wait_for(lambda screen: "holdstheCA" in "".join(screen.split()))
    solid = "".join(term.text().split())
    assert f"HXisverifyingTLSagainst{bundle};checkthatitholdstheCAyournetwork" in solid
    assert "setHX_CA_BUNDLE" not in solid
    term.snapshot("a certificate the CLI would not trust")
    term.submit("try again")
    term.wait_for("Through the proxy now.")


def test_routes_that_cannot_run_say_what_to_do(hx: HX, claude: FakeClaude) -> None:
    """Signed out, signed in to the Console instead of a subscription, or not installed:
    each says so and names the fix, before any turn is attempted."""
    claude.use()
    result = hx.run("-p", "hello")
    assert result.returncode != 0
    assert "Claude Code is not signed in. Run `hx auth login claude-code`" in result.stderr

    claude.login(method="console")
    result = hx.run("-p", "hello")
    assert "signed in to the Anthropic Console, which bills per token" in result.stderr

    missing = hx.root / "nowhere" / "claude"
    result = hx.run("-p", "hello", env={"HX_CLAUDE_CODE_PATH": str(missing)})
    assert f"HX_CLAUDE_CODE_PATH is set to {missing}, which is not" in result.stderr
    assert "not installed" not in result.stderr

    claude.uninstall()
    result = hx.run("-p", "hello")
    assert "Claude Code is not installed (no `claude` on PATH)" in result.stderr
    assert not claude.log("prompt")


def test_signing_in_and_out_from_hx(hx: HX, claude: FakeClaude) -> None:
    """`hx auth login claude-code` runs Claude Code's own sign-in and then lists the account's
    models; `hx auth` shows the plan; logout points at Claude Code, which owns the login."""
    status = hx.run("auth")
    assert re.search(r"claude-code\s+not signed in", status.stdout)

    login = hx.run("auth", "login", "claude-code", input="good-code\n")
    assert login.returncode == 0, login.stderr
    assert "Paste code here if prompted" in login.stdout
    assert (
        "Signed in to Claude (Pro/Max, via Claude Code) as e2e@example.com, max plan."
        in login.stdout
    )
    assert "claude-code/claude-sonnet-5" in login.stdout
    assert "claude-code/claude-opus-4-8" in login.stdout
    assert "claude-code/default" not in login.stdout

    status = hx.run("auth")
    assert re.search(r"claude-code\s+signed in \(max\)", status.stdout)

    logout = hx.run("auth", "logout", "claude-code")
    assert "Run `claude auth logout`" in logout.stdout
    assert json.loads((claude.state / "auth.json").read_text())["loggedIn"] is True
    assert (
        not (hx.hx_home / "auth.json").exists()
        or "claude-code" not in (hx.hx_home / "auth.json").read_text()
    )


def test_signing_in_from_the_tui(hx: HX, claude: FakeClaude) -> None:
    """/login claude-code shows Claude Code's sign-in URL and takes the pasted code."""
    term = hx.tui()
    term.submit("/login claude-code")
    term.wait_for("https://claude.example/oauth/authorize")
    term.snapshot("Claude Code sign-in")
    term.paste("good-code")
    term.press("enter")
    term.wait_for("Claude Code signed in (e2e@example.com, max plan)")
    term.snapshot("signed in")
    assert json.loads((claude.state / "auth.json").read_text())["loggedIn"] is True


WRONG_PURPOSE_HINT = (
    "Your network intercepts TLS with a certificate that is not valid for servers. Ask its "
    "administrator to fix that certificate or to exclude this service from inspection; until "
    "then, HX_SSL_NO_VERIFY=1 turns verification off."
)


def test_a_network_that_breaks_sign_in_says_why_and_what_to_do(hx: HX, claude: FakeClaude) -> None:
    """Behind TLS inspection whose certificate may not serve a TLS server, Claude Code's sign-in
    fails after the code is pasted. The notice gives Claude Code's reason and the fix - not the
    sign-in URL, which the dialog showed already - and not "add the CA", which is already
    trusted. Once the network is fixed, signing in again lists the subscription's models; a
    turn that meets the same network says the same."""
    bundle = corporate_ca(hx)
    (claude.state / "login_error").write_text("unsupported certificate purpose")
    term = hx.tui(env={"HX_CA_BUNDLE": str(bundle)})
    term.submit("/login claude-code")
    term.wait_for("https://claude.example/oauth/authorize")
    term.paste("good-code")
    term.press("enter")
    term.wait_for(lambda screen: "verificationoff." in "".join(screen.split()))
    solid = "".join(term.text().split())
    expected = "Sign-infailed:`claudeauthlogin`failed:unsupportedcertificatepurpose."
    assert "".join(f"{expected}{WRONG_PURPOSE_HINT}".split()) in solid
    assert "claude.example/oauth" not in solid
    assert "Pastecode" not in solid
    assert "holdstheCA" not in solid
    # Wrapped, the notice keeps its colour and nothing else: its foreground's 102 once
    # opened a bright green background under every line after the first.
    notice = [y for y, row in enumerate(term.text().split("\n")) if "intercepts TLS" in row]
    assert notice
    with term._lock:
        filled = {term.screen.buffer[notice[0]][x].bg for x in range(term.columns)}
    assert filled == {"default"}
    term.snapshot("sign-in refused by the network")

    (claude.state / "login_error").unlink()
    term.submit("/login claude-code")
    term.wait_for("https://claude.example/oauth/authorize")
    term.paste("good-code")
    term.press("enter")
    term.wait_for("Claude Code signed in (e2e@example.com, max plan)")
    term.submit("/model")
    term.paste("anthropic", newline="")
    term.wait_for("claude-code/claude-sonnet-5")
    term.snapshot("subscription models after signing in")
    term.press("esc")

    # Claude Code 2.1.289's words, measured behind such a proxy.
    claude.use()
    claude.script({"error": "API Error: Unable to connect to API (INVALID_PURPOSE)"})
    result = hx.run("-p", "hello", env={"HX_CA_BUNDLE": str(bundle)})
    assert result.returncode != 0
    assert (
        f"Claude Code: API Error: Unable to connect to API (INVALID_PURPOSE). {WRONG_PURPOSE_HINT}"
    ) in result.stderr


def test_a_subagent_gets_its_own_process_while_the_parent_waits(
    hx: HX, claude: FakeClaude, project: Path
) -> None:
    """A Task call runs a subagent on the same route. Its turns go to a Claude Code process of
    its own, while the parent's process keeps holding the Task call; only the subagent's report
    reaches the parent."""
    (project / "one.py").write_text("x = 1\n")
    (project / "two.py").write_text("y = 2\n")
    claude.login()
    claude.use()
    claude.script(
        calls(
            (
                "Task",
                {
                    "subagent_type": "general",
                    "prompt": "Count the .py files.",
                    "description": "count files",
                },
            )
        ),
        calls(("Glob", {"pattern": "*.py"})),
        say("There are 2 Python files."),
        say("The subagent counted 2 Python files."),
    )
    result = hx.run("--mode", "bypass", "-p", "how many python files?", check=True)
    assert result.stdout == "The subagent counted 2 Python files.\n"

    parent, child = claude.spawns(tools=True)
    child_tools = {
        t["name"] for t in next(e for e in claude.log("tools") if e["pid"] == child["pid"])["tools"]
    }
    assert "Task" not in child_tools, "a subagent cannot start another"
    (child_prompt,) = [p for p in claude.log("prompt") if p["pid"] == child["pid"]]
    assert text_of(child_prompt["content"]).startswith("Count the .py files.")
    task_result = next(e for e in claude.log("result") if e["name"] == "Task")
    assert task_result["pid"] == parent["pid"]
    assert "There are 2 Python files." in text_of(task_result["result"]["content"])
    # The parent was not restarted to receive it: one prompt, one process.
    assert len([p for p in claude.log("prompt") if p["pid"] == parent["pid"]]) == 1


def test_images_reach_claude_code_both_ways(hx: HX, claude: FakeClaude, project: Path) -> None:
    """An image attached to the prompt goes in the user turn; one a tool returns goes back
    as MCP image content on the held call."""
    shot = png(project / "shot.png", 6, 4)
    chart = png(project / "chart.png", 8, 8)
    claude.login()
    claude.use()
    claude.script(calls(("Read", {"file_path": str(chart)})), say("Both are red squares."))
    hx.run("--image", str(shot), "-p", "compare these", check=True)

    (session,) = claude.spawns(tools=True)
    (prompt,) = [p for p in claude.log("prompt") if p["pid"] == session["pid"]]
    kinds = [block["type"] for block in prompt["content"]]
    assert kinds[0] == "text" and "image" in kinds
    image = next(block for block in prompt["content"] if block["type"] == "image")
    assert image["source"]["type"] == "base64" and image["source"]["media_type"] == "image/png"

    (read,) = claude.log("result")
    returned = [c for c in read["result"]["content"] if c["type"] == "image"]
    assert returned and returned[0]["mimeType"] == "image/png" and returned[0]["data"]


def test_a_session_started_elsewhere_continues_on_claude_code(
    hx: HX, claude: FakeClaude, stub: openrouter.Stub, project: Path
) -> None:
    """Switching an OpenRouter session to Claude Code mid-conversation: the history - another
    route's tool call ids included - is rebuilt into a session Claude Code accepts."""
    (project / "todo.txt").write_text("buy milk\n")
    stub.script(
        openrouter.calls(
            openrouter.ToolCall("Read", {"file_path": "todo.txt"}, id="call:7/odd id")
        ),
        openrouter.say("Your list says: buy milk."),
    )
    claude.login()
    claude.script(say("Still just milk."))
    term = hx.tui()
    term.submit("what is on my list?")
    term.wait_for("Your list says: buy milk.")
    term.submit("/model claude-code/claude-sonnet")
    term.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("claude-sonnet-5 (sub)"))
    term.submit("anything else?")
    term.wait_for("Still just milk.")
    term.snapshot("continued on Claude Code")

    (session,) = claude.spawns(tools=True)
    records = session["resume"]
    call = next(b for r in records for b in r["message"]["content"] if b["type"] == "tool_use")
    result = next(b for r in records for b in r["message"]["content"] if b["type"] == "tool_result")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", call["id"]), call["id"]
    assert result["tool_use_id"] == call["id"]
    assert call["name"] == "mcp__hx__Read"
    assert "buy milk" in result["content"][0]["text"]
    assert not any(b["type"] == "thinking" for r in records for b in r["message"]["content"])


def picker_rows(screen: str) -> list[str]:
    """The /model picker's rows, cursor and tick stripped: the id first, then its columns."""
    return [line.strip(" →✓") for line in screen.splitlines() if re.search(r"\scache[✓~✗]", line)]


def test_the_model_picker_finds_subscription_models_by_their_maker(
    hx: HX, claude: FakeClaude, stub: openrouter.Stub
) -> None:
    """Filtering /model by "anthropic" lists the Claude models the subscription already pays
    for, ahead of the same maker's models billed per token through OpenRouter. A maker plus a
    model name puts both routes' Sonnets first, the subscription's on top, and a model that
    only mentions Sonnet in its description after them; the subscription's runs the next turn."""
    claude.login()
    claude.script(say("Sonnet 5, on your subscription."))
    term = hx.tui()
    term.submit("/model")
    term.wait_for("Select model")
    term.type("anthropic")
    screen = term.wait_for(lambda s: "openai/gpt-5" not in s and "claude-code/" in s)
    term.snapshot("/model filtered by anthropic")

    rows = picker_rows(screen)
    ids = [row.split()[0] for row in rows]
    assert ids == [
        "claude-code/claude-haiku-4-5",
        "claude-code/claude-opus-4-8",
        "claude-code/claude-sonnet-5",
        openrouter.SONNET,
    ], ids
    for row, window in zip(rows[:3], ("200k", "200k", "1M"), strict=True):
        assert re.search(rf"\s{window}\s+subscription\s", row), row
    assert re.search(r"\s\$3\.00/\$15\.00\s", rows[3]), rows[3]

    term.press("esc")
    term.wait_gone("Select model")
    term.submit("/model anthropic sonnet")
    screen = term.wait_for("Select model")
    picked = picker_rows(screen)
    assert [row.split()[0] for row in picked] == [
        "claude-code/claude-sonnet-5",
        openrouter.SONNET,
        "claude-code/claude-opus-4-8",
    ], picked
    # The picker opens on the model in force - OpenRouter's Sonnet - so one up.
    term.press("up")
    term.wait_for("→   claude-code/claude-sonnet-5")
    term.press("enter")
    term.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("claude-sonnet-5 (sub)"))
    term.submit("who are you?")
    term.wait_for("Sonnet 5, on your subscription.")
    term.snapshot("the subscription's Sonnet chosen by maker and name")
    (session,) = claude.spawns(tools=True)
    assert session["argv"][session["argv"].index("--model") + 1] == "claude-sonnet-5"
    assert not stub.requests, "nothing was billed per token"


def test_a_compaction_mid_turn_continues_on_a_rebuilt_session(
    hx: HX, claude: FakeClaude, project: Path
) -> None:
    """HX compacts its transcript between two tool calls of one turn. Claude Code's copy is
    then out of date, so it is replaced: the new process starts from the summary and the turns
    kept, ending on the tool results, and is asked to carry on from there."""
    for name in "abcd":
        (project / f"{name}.txt").write_text(f"{name}={ord(name)}\n")
    claude.login()
    claude.use()
    claude.script(
        calls(("Read", {"file_path": "a.txt"})),
        calls(("Read", {"file_path": "b.txt"})),
        say("A done."),
        calls(("Read", {"file_path": "c.txt"})),
        calls(("Read", {"file_path": "d.txt"})),
        say("B done."),
    )
    term = hx.tui(env={"HX_COMPACT_AT": "0.0005"})
    term.submit("read a and b")
    term.wait_for("A done.")
    term.submit("now read c and d")
    term.wait_for("B done.")
    term.settle()
    term.snapshot("compacted mid-turn and carried on")

    assert claude.log("summary"), "the summary was written by a one-off Claude Code call"
    rebuilt = [s for s in claude.spawns(tools=True) if s["resume"]]
    assert rebuilt, "the turn continued on a rebuilt session"
    records = rebuilt[0]["resume"]
    assert "This replaces the earlier conversation." in records[0]["message"]["content"][0]["text"]
    last = records[-1]["message"]
    assert last["role"] == "user" and all(b["type"] == "tool_result" for b in last["content"])
    (prompt,) = [p for p in claude.log("prompt") if p["pid"] == rebuilt[0]["pid"]]
    assert text_of(prompt["content"]).startswith("Continue from where you left off.")
    # The process the compaction made obsolete does not linger.
    live = {s["pid"] for s in claude.spawns(tools=True)} - {e["pid"] for e in claude.log("exit")}
    assert len(live) <= 1


def gauge(screen: str) -> str:
    """The status bar's context gauge: ``0/200k``, ``1.2k/1M``."""
    match = re.search(r"\s(\S+/\S+)\s+\(auto\)", screen.splitlines()[-1])
    assert match, screen.splitlines()[-1]
    return match.group(1)


def test_the_context_window_is_the_one_claude_code_reports(hx: HX, claude: FakeClaude) -> None:
    """Claude Code lists its models without their sizes, so one HX has no measurement for -
    Opus 4.8, which serves 1M on some plans and 200k on others - starts on a 200k guess. The
    first turn on Opus reports its real window, 1M: the gauge, the /model picker, a catalogue
    refresh that lists Opus without a size again, and the next session all say 1M from then
    on. Sonnet 5, never run, starts at the 1M it was measured serving on every plan."""
    claude.login()
    claude.limits("claude-opus-4-8", 1_000_000, 128_000)
    claude.use("claude-code/claude-opus-4-8")
    claude.script(say("Opus here."))

    term = hx.tui()
    term.wait_for(lambda s: gauge(s) == "0/200k")
    term.snapshot("Opus before its first turn, on the guess")
    term.submit("hello")
    term.wait_for("Opus here.")
    term.wait_for(lambda s: gauge(s).endswith("/1M"))
    term.snapshot("Opus after a turn, at the window Claude Code reported")

    term.submit("/model claude-code")
    term.wait_for(lambda s: "Select model" in s and "claude-code/claude-opus-4-8" in s, timeout=15)
    term.settle()
    rows = {row.split()[0]: row.split()[1] for row in picker_rows(term.text())}
    term.snapshot("/model with the window Opus reported")
    assert rows["claude-code/claude-opus-4-8"] == "1M"
    assert rows["claude-code/claude-sonnet-5"] == "1M", "never run, so the measured window"

    term.press("ctrl+r")
    term.wait_for("Refreshed", timeout=15)
    rows = {row.split()[0]: row.split()[1] for row in picker_rows(term.text())}
    term.snapshot("/model after a refresh")
    assert rows["claude-code/claude-opus-4-8"] == "1M", "a refresh does not undo what was learned"
    term.press("esc")
    term.wait_gone("Select model")
    term.press("ctrl+d")
    term.wait_exit()

    again = hx.tui()
    again.wait_for(lambda s: gauge(s) == "0/1M")
    again.snapshot("the next session starts at 1M")


def test_a_window_reported_in_print_mode_is_kept_too(hx: HX, claude: FakeClaude) -> None:
    """`hx -p` learns Opus's window the same way the TUI does, so a session opened after a
    scripted run starts at 1M rather than the guess."""
    claude.login()
    claude.limits("claude-opus-4-8", 1_000_000, 128_000)
    claude.use("claude-code/claude-opus-4-8")
    claude.script(say("Opus here."))
    hx.run("-p", "hello", check=True)

    term = hx.tui()
    term.wait_for(lambda s: gauge(s) == "0/1M")
    term.snapshot("a session after a print-mode run on Opus")


PRO_MODELS = [
    {
        "value": "default",
        "resolvedModel": "claude-opus-5-5",
        "displayName": "Default (recommended)",
        "description": "Opus 5.5 · Best for everyday, complex tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
    },
    {
        "value": "opus",
        "resolvedModel": "claude-opus-5-5",
        "displayName": "Opus",
        "description": "Opus 5.5 · Best for everyday, complex tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
    },
    {
        "value": "claude-fable-5-1[1m]",
        "resolvedModel": "claude-fable-5-1",
        "displayName": "Fable",
        "description": "Fable 5.1 · Most capable for your hardest and longest-running tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
    },
    {
        "value": "sonnet",
        "resolvedModel": "claude-sonnet-5-5",
        "displayName": "Sonnet",
        "description": "Sonnet 5.5 · Efficient for routine tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
    },
    {
        "value": "haiku",
        "resolvedModel": "claude-haiku-4-5-20251001",
        "displayName": "Haiku",
        "description": "Haiku 4.5 · Fastest for quick answers",
    },
]
"""What Claude Code 2.1.285 lists for a Pro account. Fable's long context is in ``value``
alone: ``resolvedModel`` names the model without it."""

CREDITS_REQUIRED = {
    # What Claude Code reports when a model, or its long context, needs usage
    # credits the account has off: rejected, with the billing period's reset
    # and no usage window - none is reached.
    "status": "rejected",
    "resetsAt": int(time.time()) + 27 * 86_400,
    "overageDisabledReason": "org_level_disabled",
    "isUsingOverage": False,
    "errorCode": "credits_required",
    "canUserPurchaseCredits": True,
    "hasChargeableSavedPaymentMethod": True,
}
FABLE_NEEDS_CREDITS = (
    "Fable 5.1 requires usage credits. Switch to another model, or manage usage credits at "
    "claude.ai/settings/usage?from=cc_cli_limit_message, to continue."
)
LONG_CONTEXT_NEEDS_CREDITS = (
    "API Error: Usage credits required for 1M context · turn on usage credits at "
    "claude.ai/settings/usage?from=cc_cli_limit_message (they take effect in a new session), "
    "or use --model to switch to standard context"
)


def test_a_model_claude_code_lists_at_its_long_context_runs_at_it(
    hx: HX, claude: FakeClaude
) -> None:
    """Claude Code lists Fable as ``claude-fable-5-1[1m]``, resolving to the bare name. HX
    runs what Claude Code's own /model would: the long-context id, shown at 1M before its
    first turn, and kept at 1M by the window the turn reports under that id."""
    claude.login(plan="pro")
    claude.install("2.1.285", PRO_MODELS)
    claude.limits("claude-fable-5-1[1m]", 1_000_000, 128_000)
    claude.use("claude-code/claude-fable-5-1[1m]")
    claude.script(say("Fable here."))

    term = hx.tui()
    term.wait_for(lambda s: gauge(s) == "0/1M")
    term.snapshot("Fable before its first turn, at its long context")
    term.submit("hello")
    term.wait_for("Fable here.")
    term.wait_for(lambda s: gauge(s).endswith("/1M"))
    (spawn,) = claude.spawns(tools=True)
    assert spawn["argv"][spawn["argv"].index("--model") + 1] == "claude-fable-5-1[1m]"

    term.submit("/model claude-code")
    term.wait_for(lambda s: "Select model" in s and "claude-code/claude-fable-5-1[1m]" in s)
    term.settle()
    rows = {row.split()[0]: row.split()[1] for row in picker_rows(term.text())}
    term.snapshot("/model on a Pro account's Claude Code")
    assert rows["claude-code/claude-fable-5-1[1m]"] == "1M"
    assert rows["claude-code/claude-opus-5-5"] == "1M", "measured, never run"
    assert rows["claude-code/claude-sonnet-5-5"] == "1M", "measured, never run"
    assert rows["claude-code/claude-haiku-4-5"] == "200k"
    assert "claude-code/claude-fable-5-1 " not in term.text(), "only the id Claude Code lists"


def test_a_window_served_below_the_measurement_wins(hx: HX, claude: FakeClaude) -> None:
    """The measured window is only where a model starts. Served smaller - a plan that keeps
    Sonnet 5.5 at 200k - the gauge drops to what the turn reported, and the next session
    starts there rather than back at the measurement."""
    claude.login(plan="pro")
    claude.install("2.1.285", PRO_MODELS)
    claude.limits("claude-sonnet-5-5", 200_000, 64_000)
    claude.use("claude-code/claude-sonnet-5-5")
    claude.script(say("Sonnet here."))

    term = hx.tui()
    term.wait_for(lambda s: gauge(s) == "0/1M")
    term.snapshot("Sonnet 5.5 before its first turn, at the measured 1M")
    term.submit("hello")
    term.wait_for("Sonnet here.")
    term.wait_for(lambda s: gauge(s).endswith("/200k"))
    term.snapshot("Sonnet 5.5 after a turn served at 200k")
    term.press("ctrl+d")
    term.wait_exit()

    again = hx.tui()
    again.wait_for(lambda s: gauge(s) == "0/200k")
    again.snapshot("the next session starts at the served 200k")


def test_a_model_that_needs_usage_credits_says_so(hx: HX, claude: FakeClaude) -> None:
    """Fable on Pro needs usage credits, and Claude Code refuses the turn with a rejected rate
    limit - but no usage window is reached and nothing resets. HX says what the model needs
    and how to get it, tries once, and once credits are on the same model runs: in a new
    Claude Code process, the only kind that sees them."""
    claude.login(plan="pro")
    claude.install("2.1.285", PRO_MODELS)
    claude.limits("claude-fable-5-1[1m]", 1_000_000, 128_000)
    claude.use("claude-code/claude-fable-5-1[1m]")
    claude.script(
        {"rate_limit": CREDITS_REQUIRED, "error": FABLE_NEEDS_CREDITS, "api_error_status": 429},
        say("Fable here."),
    )

    term = hx.tui()
    term.submit("hello")
    term.wait_for("usage credits")
    screen = flat(term.text())
    term.snapshot("a model that needs usage credits")
    assert "usage limit is reached" not in screen
    assert "resets at" not in screen.lower()
    assert "claude-fable-5-1 needs usage credits" in screen
    assert "claude.ai/settings/usage" in screen
    assert "pick another model" in screen
    assert "cc_cli_limit_message" not in screen, "Claude Code's link, for its own CLI"
    assert len(claude.log("turn")) == 1, "no retry: it would be refused the same way"

    term.submit("credits are on now")
    term.wait_for("Fable here.")
    term.snapshot("the same model runs once credits are on")
    first, second = claude.spawns(tools=True)
    assert first["pid"] != second["pid"]


def test_a_long_context_that_needs_usage_credits_says_so(hx: HX, claude: FakeClaude) -> None:
    """Asked for at 1M on a plan that charges credits for it, Claude Code refuses the turn.
    `hx -p` says it is the long context that needs credits, not a usage limit, and that the
    model at its standard context runs without them."""
    claude.login(plan="pro")
    claude.use("claude-code/claude-sonnet-4-6[1m]")
    claude.script(
        {
            "rate_limit": CREDITS_REQUIRED,
            "error": LONG_CONTEXT_NEEDS_CREDITS,
            "api_error_status": 429,
        }
    )
    result = hx.run("-p", "hello")
    hx.note(result.stderr)
    assert result.returncode != 0
    stderr = flat(result.stderr)
    assert "usage limit is reached" not in stderr
    assert "resets at" not in stderr.lower()
    assert "1M context of claude-sonnet-4-6 needs usage credits" in stderr
    assert "pick claude-code/claude-sonnet-4-6 for its standard context" in stderr
    assert "claude.ai/settings/usage" in stderr
    assert len(claude.log("turn")) == 1


def test_the_reasoning_effort_reaches_claude_code(hx: HX, claude: FakeClaude) -> None:
    """A saved effort is passed to Claude Code; without one, Claude Code picks per model."""
    claude.login()
    claude.use()
    claude.script(say("Default effort."), say("High effort."))
    hx.run("-p", "first", check=True)
    (hx.hx_home / "settings.json").write_text(json.dumps({"models": {"reasoning_effort": "high"}}))
    hx.run("-p", "second", check=True)

    default, high = claude.spawns(tools=True)
    assert "--effort" not in default["argv"]
    assert high["argv"][high["argv"].index("--effort") + 1] == "high"


def echoes_within(term: Any, text: str, seconds: float) -> float:
    """Type ``text`` and return how long it took to appear on screen."""
    started = time.monotonic()
    term.type(text)
    term.wait_for(text, timeout=seconds)
    return time.monotonic() - started


def test_a_slow_claude_code_never_freezes_the_screen(hx: HX, claude: FakeClaude) -> None:
    """`claude auth status` can take seconds - a cold start, a keychain prompt. HX asks it
    when /model and /login decide what to offer and when a sign-in completes; keys typed
    meanwhile appear at once, rather than after the CLI answers."""
    claude.login()
    first = hx.tui()  # writes today's model catalogue, so the next start skips the refresh
    first.close()
    claude.slow_status(3)

    term = hx.tui()
    asked = len(claude.log("status"))
    term.submit("/model claude-code/claude-sonnet")
    echoed = echoes_within(term, "abc", 1.0)
    hx.note(f"keys typed while /model asked Claude Code echoed after {echoed:.2f}s")
    term.snapshot("typing while /model waits on Claude Code")
    term.wait_for("Model set to claude-code/claude-sonnet-5", timeout=10)
    term.snapshot("switched to a Claude Code model")
    assert len(claude.log("status")) == asked + 1
    term.press("ctrl+u")

    term.submit("/login claude-code")
    term.wait_for("https://claude.example/oauth/authorize", timeout=10)
    term.paste("good-code")
    term.press("enter")
    term.wait_gone("https://claude.example/oauth/authorize", timeout=5)
    echoed = echoes_within(term, "def", 1.0)
    hx.note(f"keys typed while the sign-in was confirmed echoed after {echoed:.2f}s")
    term.wait_for("Claude Code signed in (e2e@example.com, max plan)", timeout=15)
    term.snapshot("signed in; what was typed meanwhile is still in the prompt")


CURRENT_MODELS = [
    {
        "value": "default",
        "resolvedModel": "claude-opus-5-5",
        "displayName": "Default (recommended)",
        "description": "Opus 5.5 · Best for everyday, complex tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "value": "opus",
        "resolvedModel": "claude-opus-5-5",
        "displayName": "Opus",
        "description": "Opus 5.5 · Best for everyday, complex tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "value": "sonnet",
        "resolvedModel": "claude-sonnet-5-5",
        "displayName": "Sonnet",
        "description": "Sonnet 5.5 · Efficient for routine tasks",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "value": "haiku",
        "resolvedModel": "claude-haiku-4-5-20251001",
        "displayName": "Haiku",
        "description": "Haiku 4.5 · Fastest for quick answers",
    },
]
"""What a Claude Code released after Opus 5.5 lists."""

RELEASES = {"stable": "2.1.285", "latest": "2.1.289"}


def flat(screen: str) -> str:
    """The screen as one line of words, so a sentence the picker wrapped still matches."""
    return " ".join(screen.split())


def claude_code_ids(screen: str) -> list[str]:
    return [row.split()[0] for row in picker_rows(screen) if row.startswith("claude-code/")]


def test_an_outdated_claude_code_says_how_to_get_newer_models(
    hx: HX, claude: FakeClaude, stub: openrouter.Stub
) -> None:
    """Claude Code decides which Claude models a subscription offers, and an old one only knows
    old models. /model says the installed Claude Code is behind its release and how to update
    it - with Homebrew, as it was installed here - and still says so after a restart, without
    asking the registry again. Upgraded while HX runs, the next /model lists the new models,
    with no hint, and the new Opus answers the next turn."""
    claude.login()
    claude.install("2.1.205", cask="claude-code")
    stub.claude_code_releases = dict(RELEASES)
    hint = (
        "Claude Code 2.1.205 is behind the current release, 2.1.285, so newer Claude models "
        "are missing. Update it: brew upgrade --cask claude-code"
    )

    term = hx.tui()
    term.submit("/model claude-code")
    screen = term.wait_for(lambda s: "Select model" in s and hint in flat(s), timeout=15)
    term.snapshot("/model under Claude Code 2.1.205, behind 2.1.285")
    assert claude_code_ids(screen) == [
        "claude-code/claude-haiku-4-5",
        "claude-code/claude-opus-4-8",
        "claude-code/claude-sonnet-5",
    ], screen
    term.press("esc")
    term.wait_gone("Select model")
    term.close()
    asked = stub.release_fetches
    assert asked >= 1

    term = hx.tui()
    term.submit("/model claude-code")
    screen = term.wait_for(lambda s: "Select model" in s and hint in flat(s), timeout=15)
    term.snapshot("/model after a restart, the catalogue still fresh")
    assert stub.release_fetches == asked, "a fresh catalogue is not a reason to ask npm again"
    term.press("esc")
    term.wait_gone("Select model")

    claude.install("2.1.285", CURRENT_MODELS, cask="claude-code")
    term.submit("/model claude-code")
    screen = term.wait_for(
        lambda s: "Select model" in s and "claude-code/claude-opus-5-5" in s, timeout=15
    )
    term.settle()
    screen = term.text()
    term.snapshot("/model once Claude Code is upgraded to 2.1.285")
    assert claude_code_ids(screen) == [
        "claude-code/claude-haiku-4-5",
        "claude-code/claude-opus-5-5",
        "claude-code/claude-sonnet-5-5",
    ], screen
    assert "is behind" not in flat(screen)
    term.press("esc")
    term.wait_gone("Select model")

    claude.script(say("Opus 5.5, on your subscription."))
    term.submit("/model claude-code/claude-opus-5-5")
    term.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("claude-opus-5-5 (sub)"))
    term.submit("who are you?")
    term.wait_for("Opus 5.5, on your subscription.")
    term.snapshot("the upgraded Claude Code's Opus answering")
    (session,) = claude.spawns(tools=True)
    assert session["argv"][session["argv"].index("--model") + 1] == "claude-opus-5-5"


def test_the_update_hint_is_only_given_when_a_release_is_known_to_be_newer(
    hx: HX, claude: FakeClaude, stub: openrouter.Stub
) -> None:
    """With the npm registry unreachable HX claims nothing and complains about nothing. Once it
    answers, ctrl+r in the picker says the installed Claude Code is behind - to be updated with
    `claude update`, as it was not installed through Homebrew - and a Claude Code on the
    latest channel, ahead of stable, is not told to update."""
    claude.login()
    claude.install("2.1.205")
    stub.claude_code_releases = None

    term = hx.tui()
    term.submit("/model claude-code")
    screen = term.wait_for(lambda s: "Select model" in s and "claude-code/" in s, timeout=15)
    term.settle()
    screen = term.text()
    term.snapshot("/model with the npm registry unreachable")
    assert "is behind" not in flat(screen) and "registry" not in screen.lower()
    assert stub.release_fetches >= 1

    stub.claude_code_releases = dict(RELEASES)
    term.press("ctrl+r")
    screen = term.wait_for("Refreshed", timeout=15)
    term.snapshot("ctrl+r in /model with Claude Code behind")
    assert (
        "Claude Code 2.1.205 is behind the current release, 2.1.285, so newer Claude models "
        "are missing. Update it: claude update"
    ) in flat(screen.split("Select model", 1)[1]), "said under the list it explains"
    term.press("esc")
    term.wait_gone("Select model")

    claude.install("2.1.289", CURRENT_MODELS)
    term.submit("/model claude-code")
    screen = term.wait_for(
        lambda s: "Select model" in s and "claude-code/claude-opus-5-5" in s, timeout=15
    )
    term.settle()
    screen = term.text()
    term.snapshot("/model under Claude Code 2.1.289, from the latest channel")
    after_picker = flat(screen.split("Select model", 1)[1])
    assert "is behind" not in after_picker, screen

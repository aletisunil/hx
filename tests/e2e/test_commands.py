"""Slash commands, the completion popups and the pickers."""

from __future__ import annotations

import json
import re
import threading
import time

from tests.e2e.conftest import HX, SANDBOX, SANDBOX_STATUS
from tests.e2e.stub import GPT5, SONNET, Stub, call, say
from tests.e2e.test_claude_code import flat, gauge


def test_slash_opens_command_completion(hx: HX) -> None:
    """Typing `/mo` lists the matching commands above the prompt; tab completes."""
    term = hx.tui()
    term.type("/mo")
    screen = term.wait_for("/model        Choose the model")
    term.snapshot("completion for /mo")
    assert "/mode         Set the permission mode" in screen
    assert "/models" not in screen, "refreshing lives in the /model picker, as ctrl+r"
    assert "List commands and keys" not in screen, "/help does not match"


def test_at_completes_a_path(hx: HX) -> None:
    """`@` completes files and folders in the project."""
    (hx.project / "src").mkdir()
    (hx.project / "src" / "main.py").write_text("x = 1\n")
    term = hx.tui()
    term.type("look at @sr")
    term.wait_for("→ src/")
    term.press("tab")
    term.wait_for("look at @src/")
    term.type("ma")
    term.wait_for("src/main.py")
    term.snapshot("completing a file")


def test_help_lists_commands_and_keys(hx: HX) -> None:
    """/help lists every command and every key, in aligned columns."""
    term = hx.tui(rows=60)
    term.submit("/help")
    screen = term.wait_for("drag an image")
    term.snapshot("/help")
    assert "/rewind" in screen and "/trace" in screen
    rows = [line for line in screen.splitlines() if re.match(r"^\s{5}\S", line)]
    keys = [r for r in rows if not r.strip().startswith("/")]
    # Every description starts in the same column, however long its key.
    columns = [re.search(r"\S+\s{2,}(\S)", row) for row in keys]
    starts = {column.start(1) for column in columns if column}
    assert len(starts) == 1, "\n".join(keys)


def test_cost_and_context(hx: HX, stub: Stub) -> None:
    """/cost breaks down spend; /context shows what fills the window."""
    stub.script(say("Hello there.", prompt_tokens=1200, completion_tokens=40, cost=0.0042))
    term = hx.tui(rows=40)
    term.submit("hi")
    term.wait_for("Hello there.")

    term.submit("/cost")
    screen = term.wait_for("Session cost:")
    assert "$0.0084  over 2 API requests across 1 prompt" in screen

    term.submit("/context")
    screen = term.wait_for("Context:")
    term.snapshot("/cost and /context")
    for section in ("system", "tools", "history"):
        assert re.search(rf"^\s+{section}\s+\S+", screen, re.MULTILINE), section
    assert "✗" not in screen


def test_model_picker_switches_the_model(hx: HX, stub: Stub) -> None:
    """/model lists the catalogue with prices; choosing one routes the next turn to it."""
    stub.script(say("Answered by GPT-5."))
    term = hx.tui()
    term.submit("/model")
    screen = term.wait_for("Select model")
    term.snapshot("model picker")
    assert re.search(r"anthropic/claude-sonnet-4.5\s+200k\s+\$3.00/\$15.00", screen)
    assert re.search(r"openai/gpt-5\s+400k\s+\$1.25/\$10.00", screen)

    term.type("gpt")
    term.wait_for(lambda s: "deepseek" not in s)
    term.press("enter")
    term.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("gpt-5"))
    term.submit("who are you?")
    term.wait_for("Answered by GPT-5.")
    assert stub.requests[0].model == GPT5


def test_ctrl_l_switches_the_model_and_remembers_it(hx: HX, stub: Stub) -> None:
    """ctrl+l opens the model picker on the model in force; choosing another routes the next
    turn to it and saves it for the next session, exactly as /model does."""
    stub.script(say("Answered by GPT-5."))
    term = hx.tui()
    term.press("ctrl+l")
    screen = term.wait_for("Select model")
    term.snapshot("ctrl+l model picker")
    assert re.search(r"→ ✓ anthropic/claude-sonnet-4\.5\s", screen), "opens on the current model"

    term.type("gpt")
    term.wait_for(lambda s: "deepseek" not in s)
    term.press("enter")
    term.wait_for("Model set to openai/gpt-5")
    term.submit("who are you?")
    term.wait_for("Answered by GPT-5.")
    assert stub.requests[0].model == GPT5
    saved = json.loads((hx.hx_home / "settings.json").read_text())
    assert saved["models"]["model"] == GPT5


def test_ctrl_r_refreshes_the_model_picker_in_place(hx: HX, stub: Stub) -> None:
    """ctrl+r in the model picker fetches the catalogue again with the picker still open: the
    filter and the selection survive, a model published since appears, a second ctrl+r while
    one is out does not ask twice, and a failed refresh says why under the list and keeps what
    was there. The new model is then a real choice: the next turn goes to it."""
    newer = "openai/gpt-5.5"
    stub.script(say("Answered by GPT-5.5."))
    term = hx.tui()
    term.submit("/model")
    screen = term.wait_for("Select model")
    assert "ctrl+r refresh" in screen
    assert newer not in screen

    def listed(screen: str) -> str:
        """The picker alone: the status bar names the model in force too."""
        return screen.split("Select model", 1)[-1].split("esc cancel", 1)[0]

    term.type("openai")
    term.wait_for(lambda s: "deepseek" not in listed(s) and "sonnet" not in listed(s))
    stub.models = [
        *stub.models,
        {
            "id": newer,
            "name": "OpenAI: GPT-5.5",
            "context_length": 1_000_000,
            "top_provider": {"context_length": 1_000_000, "max_completion_tokens": 128_000},
            "pricing": {"prompt": "0.000002", "completion": "0.000016"},
            "supported_parameters": ["tools", "reasoning"],
            "architecture": {"input_modalities": ["text", "image"]},
        },
    ]
    stub.models_hold = threading.Event()
    fetches = stub.model_fetches
    term.press("ctrl+r")
    screen = term.wait_for("Refreshing")
    term.snapshot("model picker refreshing")
    assert newer not in screen
    term.press("ctrl+r")
    stub.models_hold.set()
    screen = term.wait_for(lambda s: newer in s and "Refreshed" in s, timeout=15)
    term.settle()
    screen = term.text()
    term.snapshot("model picker refreshed in place")
    assert stub.model_fetches == fetches + 1, "a second ctrl+r while one is out asks again"
    assert "Select model" in screen, "the picker stays open"
    lines = screen.splitlines()
    title = next(i for i, line in enumerate(lines) if "Select model" in line)
    assert lines[title + 1].strip() == "openai", "the filter survives"
    assert re.search(r"→\s+openai/gpt-5\s", screen), "the selection stays on the same model"
    assert re.search(r"openai/gpt-5\.5\s+\S+\s+\$2\.00/\$16\.00", screen)

    stub.models_down = True
    term.press("ctrl+r")
    screen = term.wait_for("Refresh failed", timeout=15)
    term.snapshot("model picker refresh failed")
    assert newer in screen, "a failed refresh keeps the list it had"
    assert "Refreshed" not in screen, "the old outcome is replaced, not stacked"

    term.press(*["backspace"] * len("openai"))
    term.type("gpt-5.5")
    term.wait_for(lambda s: re.search(r"→\s+openai/gpt-5\.5\s", s) is not None)
    term.press("enter")
    term.wait_for(f"Model set to {newer}")
    term.submit("who are you?")
    term.wait_for("Answered by GPT-5.5.")
    assert stub.requests[0].model == newer


def test_a_refresh_moves_the_session_in_progress_to_the_new_window(hx: HX, stub: Stub) -> None:
    """OpenRouter raising a model's window mid-session reaches the session already running on
    it: after ctrl+r in /model, the gauge - and the compaction threshold measured against the
    same window - reads the new size with the tokens already used unchanged, without the
    model being picked again."""
    stub.script(say("Answered."))
    term = hx.tui()
    term.submit("hello")
    term.wait_for("Answered.")
    before = term.wait_for(lambda s: gauge(s).endswith("/200k") and not gauge(s).startswith("0/"))
    used = gauge(before).split("/")[0]

    stub.models = [
        {**entry, "context_length": 1_000_000} if entry["id"] == SONNET else entry
        for entry in stub.models
    ]
    term.submit("/model")
    term.wait_for("Select model")
    term.press("ctrl+r")
    term.wait_for("Refreshed", timeout=15)
    term.press("esc")
    term.wait_gone("Select model")
    screen = term.wait_for(lambda s: gauge(s) == f"{used}/1M")
    term.snapshot("the session in progress at the refreshed window")
    assert "Model set to" not in screen, "the model was not picked again"


def test_model_picker_opens_empty_and_ctrl_r_fills_it(hx: HX, stub: Stub) -> None:
    """With the catalogue unreachable since startup, /model still opens - saying why it is
    empty and that ctrl+r tries again - rather than refusing with a notice; once the network
    is back, ctrl+r fills the list in place, the explanation goes, and a pick from it routes
    the next turn."""
    stub.models_down = True
    stub.script(say("Answered by GPT-5."))
    term = hx.tui()
    term.submit("/model")
    screen = term.wait_for("Select model")
    term.settle()
    screen = term.text()
    term.snapshot("model picker with no catalogue")
    assert "nothing to list" in screen
    assert "no matches" not in screen, "no filter was typed, so nothing failed to match"
    assert "No models available: " in flat(screen) and "ctrl+r tries again" in flat(screen)

    stub.models_down = False
    term.press("ctrl+r")
    screen = term.wait_for(lambda s: "Refreshed" in s and "openai/gpt-5" in s, timeout=15)
    term.settle()
    screen = term.text()
    term.snapshot("model picker filled by ctrl+r")
    assert "No models available" not in screen
    assert "nothing to list" not in screen

    term.type("gpt-5")
    term.wait_for(lambda s: re.search(r"→\s+openai/gpt-5\s", s) is not None)
    term.press("enter")
    term.wait_for(f"Model set to {GPT5}")
    term.submit("who are you?")
    term.wait_for("Answered by GPT-5.")
    assert stub.requests[0].model == GPT5


def test_effort_carries_into_every_new_session(hx: HX) -> None:
    """A saved reasoning effort is in force - and on the status bar - from the first screen of
    a new session, through /clear and a restart; the picker opens on it, and a new pick
    replaces it for the sessions after."""
    codex = "openai-codex/gpt-5.6-terra"
    (hx.hx_home / "auth.json").write_text(
        json.dumps(
            {
                "openai-codex": {
                    "type": "oauth",
                    "access": "e2e-access",
                    "refresh": "e2e-refresh",
                    "expires": time.time() + 30 * 86_400,
                    "account_id": "acct-e2e",
                }
            }
        )
    )
    (hx.hx_home / "auth.json").chmod(0o600)
    # Fresh, so nothing reaches for the real Codex catalogue at startup.
    (hx.hx_home / "models.json").write_text(json.dumps({"fetched_at": time.time(), "models": []}))
    (hx.hx_home / "settings.json").write_text(json.dumps({"models": {"reasoning_effort": "high"}}))
    env = {"HX_MODEL": codex}

    term = hx.tui(env=env)
    term.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("gpt-5.6-terra (sub) · high"))
    term.snapshot("a new session starts at the saved effort")

    term.submit("/effort")
    screen = term.wait_for("Reasoning effort for gpt-5.6-terra")
    term.snapshot("/effort opens on the level in force")
    assert re.search(r"→ ✓ high\s*$", screen, re.MULTILINE)
    term.press("down")
    term.press("enter")
    term.wait_for("Reasoning effort set to xhigh.")
    assert term.lines()[-1].rstrip().endswith("gpt-5.6-terra (sub) · xhigh")
    saved = json.loads((hx.hx_home / "settings.json").read_text())
    assert saved["models"]["reasoning_effort"] == "xhigh"

    term.submit("/clear")
    term.wait_for(lambda s: "Reasoning effort set" not in s)
    assert term.lines()[-1].rstrip().endswith("gpt-5.6-terra (sub) · xhigh")
    term.press("ctrl+d")
    term.wait_exit()

    again = hx.tui(env=env)
    again.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("gpt-5.6-terra (sub) · xhigh"))
    again.snapshot("the next session starts at the new effort")


def test_ctrl_p_palette_runs_a_command(hx: HX) -> None:
    """ctrl+p opens the palette; filtering and enter runs the command."""
    term = hx.tui(rows=40)
    term.press("ctrl+p")
    term.wait_for("Filter commands…")
    term.type("perm")
    term.wait_for(lambda s: "/permissions" in s and "/agents" not in s)
    term.press("enter")
    screen = term.wait_for("Permission mode: default")
    term.snapshot("/permissions from the palette")
    assert f"Sandbox: {SANDBOX.value}" in screen


def test_mode_command(hx: HX) -> None:
    """/mode plan changes the mode and the status bar says so."""
    term = hx.tui()
    term.submit("/mode plan")
    term.wait_for("✓ Permission mode: plan")
    assert f"plan · {SANDBOX_STATUS}" in term.lines()[-2]


def test_prompt_command_names_the_agents_md_files(hx: HX) -> None:
    """/prompt shows the running system prompt, then every AGENTS.md in force, user's first."""
    (hx.hx_home / "AGENTS.md").write_text("Sign off every answer.\n")
    (hx.project / "AGENTS.md").write_text("Always answer in haiku.\n")
    term = hx.tui(columns=250, rows=60)
    term.submit("/prompt")
    screen = term.wait_for("Instructions: " + str(hx.project / "AGENTS.md"))
    term.snapshot("/prompt")
    user = screen.index("Instructions: " + str(hx.hx_home / "AGENTS.md"))
    assert screen.index("Source: built-in") < user < screen.index(str(hx.project / "AGENTS.md"))


def test_prompt_command_names_the_agents_md_in_force_not_on_disk(hx: HX) -> None:
    """/prompt lists the AGENTS.md files the session started with; one written, edited or
    emptied since is flagged as applying next run rather than claimed to be in force."""
    (hx.project / "AGENTS.md").write_text("Always answer in haiku.\n")
    term = hx.tui(columns=250, rows=60)
    user_md = hx.hx_home / "AGENTS.md"
    user_md.write_text("Sign off every answer.\n")
    (hx.project / "AGENTS.md").write_text("")
    term.submit("/prompt")
    screen = term.wait_for("applies next run")
    term.snapshot("/prompt after AGENTS.md changed on disk")
    assert "Instructions: " + str(hx.project / "AGENTS.md") in screen
    assert "Instructions: " + str(user_md) not in screen
    assert "An AGENTS.md has changed on disk since startup; it applies next run." in screen


def test_title_command(hx: HX, stub: Stub) -> None:
    """/title shows the model's name for the session, and /title <text> renames it."""
    stub.title = "Greeting exchange"
    stub.script(say("Hello."))
    term = hx.tui()
    term.submit("hi")
    term.wait_for("Hello.")
    term.submit("/title")
    term.wait_for("Session title: Greeting exchange")
    term.submit("/title Release planning")
    term.wait_for("✓ Session title: Release planning")
    term.press("ctrl+d")
    term.wait_exit()
    assert hx.sessions()[-1]["title"] == "Release planning"


def test_session_is_named_once_and_exit_does_not_wait(hx: HX, stub: Stub) -> None:
    """The session is named once, after its first exchange, and never renamed while it
    runs. /exit returns the shell at once, even with a rename of a left session still out."""
    parser = hx.project / "parser.py"
    parser.write_text(
        "".join(f"def rule_{n}(tokens):\n    return tokens[{n}:]\n\n" for n in range(80))
    )
    stub.title = "Parser rules overview"
    stub.script(
        call("Read", file_path=str(parser)),
        say("It is a table of slicing rules."),
        say("rule_3 drops the first three tokens."),
        say("Yes, every rule slices."),
        say("Added --verbose."),
        say("Tests pass."),
    )
    term = hx.tui()
    term.submit("what does parser.py do")
    term.wait_for("It is a table of slicing rules.")
    asked = stub.wait_for_title_requests(1)[0].last_user_text()
    assert "what does parser.py do" in asked, "the opening is shown"
    assert "It is a table of slicing rules." in asked, "so is how the exchange ended"
    assert "earlier conversation omitted" in asked, "the long middle is cut, and says so"
    assert hx.sessions()[-1]["title"] == "Parser rules overview"

    stub.title = "Never asked for"
    for prompt, reply in (
        ("what does rule_3 do", "rule_3 drops the first three tokens."),
        ("do they all slice", "Yes, every rule slices."),
        ("now add a --verbose flag to the CLI", "Added --verbose."),
        ("run the tests", "Tests pass."),
    ):
        term.submit(prompt)
        term.wait_for(reply)
    term.settle(0.5)
    assert len(stub.title_requests) == 1, "the live session is not renamed while it runs"
    assert hx.sessions()[-1]["title"] == "Parser rules overview"

    # Leaving it with /clear renames it for what it became, in the background.
    # A slow title model holds that call; /exit must not wait on it.
    stub.title_hold = threading.Event()
    term.submit("/clear")
    term.wait_for("New session started.")
    stub.wait_for_title_requests(2)
    term.type("/exit")
    started = time.monotonic()
    term.press("enter")
    assert term.wait_exit(timeout=5) == 0
    took = time.monotonic() - started
    hx.note(f"/exit with a rename in flight returned in {took:.2f}s")
    assert took < 1.0, f"/exit took {took:.2f}s"
    term.snapshot("shell prompt back")
    assert "Traceback" not in term.scrollback()
    (left,) = [meta for meta in hx.sessions() if meta["prompt_count"] == 5]
    assert left["title"] == "Parser rules overview", "the abandoned rename wrote nothing"


def test_exit_while_the_session_is_being_named_still_names_it(hx: HX, stub: Stub) -> None:
    """Leaving with the naming call still out names the session from the first prompt,
    rather than leaving /resume a row with no name at all."""
    stub.title_hold = threading.Event()
    stub.title = "Never arrives"
    stub.script(say("The retry wraps the request, not the session."))
    term = hx.tui()
    term.submit("why does the login test flake on CI")
    term.wait_for("The retry wraps the request, not the session.")
    stub.wait_for_title_requests(1)
    term.type("/exit")
    started = time.monotonic()
    term.press("enter")
    assert term.wait_exit(timeout=5) == 0
    took = time.monotonic() - started
    hx.note(f"/exit with the first naming call in flight returned in {took:.2f}s")
    assert took < 1.0, f"/exit took {took:.2f}s"
    assert "Traceback" not in term.scrollback()
    (meta,) = hx.sessions()
    assert meta["title"] == "why does the login test flake on CI"
    assert meta["title_pinned"] is False

    # Not a name the user chose: leaving it again, having moved on, renames it.
    stub.title_hold = None
    stub.title = "Login test flake on CI"
    stub.script(say("Pinned the session cookie."))
    term = hx.tui("resume")
    term.wait_for("The retry wraps the request, not the session.")
    term.submit("fix it")
    term.wait_for("Pinned the session cookie.")
    term.submit("/clear")
    term.wait_for("New session started.")
    stub.wait_for_title_requests(2)
    term.settle(0.5)
    (meta,) = [meta for meta in hx.sessions() if meta["prompt_count"] == 2]
    assert meta["title"] == "Login test flake on CI"
    term.press("ctrl+d")
    assert term.wait_exit() == 0


def test_resume_into_another_directory_renames_the_session_left(hx: HX, stub: Stub) -> None:
    """/resume of a session from another directory relaunches HX there. The session left
    behind is still renamed for what it became - by the new process, in the background."""
    elsewhere = hx.root / "elsewhere"
    elsewhere.mkdir()
    stub.title = "Owl facts"
    stub.script(say("Owls are nocturnal."))
    hx.run("-p", "tell me about owls", cwd=elsewhere, check=True)

    stub.title = "Parser question"
    stub.script(say("It parses rules."), say("Added --verbose."), say("Tests pass."))
    term = hx.tui()
    for prompt, reply in (
        ("what does parser.py do", "It parses rules."),
        ("now add a --verbose flag to the CLI", "Added --verbose."),
        ("run the tests", "Tests pass."),
    ):
        term.submit(prompt)
        term.wait_for(reply)
    stub.wait_for_title_requests(2)

    stub.title = "Verbose flag for the CLI"
    term.submit("/resume")
    screen = term.wait_for("Resume session")
    assert "Owl facts" in screen
    term.press("enter")
    term.wait_for("Owls are nocturnal.")
    term.snapshot("relaunched into the other directory")
    stub.wait_for_title_requests(3)
    term.settle(0.5)
    (left,) = [meta for meta in hx.sessions() if meta["prompt_count"] == 3]
    assert left["title"] == "Verbose flag for the CLI"
    (resumed,) = [meta for meta in hx.sessions() if meta["cwd"] == str(elsewhere)]
    assert resumed["title"] == "Owl facts", "the live session is not renamed"

    # The handoff stops at HX: nothing it starts inherits it, so a nested hx
    # cannot rename the session a second time.
    term.submit("!echo left=${HX_RENAME_LEFT_SESSION:-none}")
    term.wait_for("left=none")
    term.press("ctrl+d")
    assert term.wait_exit() == 0
    assert "Traceback" not in term.scrollback()


def test_a_title_the_user_chose_is_never_replaced(hx: HX, stub: Stub) -> None:
    """/title pins the name: neither the first naming, still out when it is set, nor the
    rename on /clear replaces it."""
    stub.title_hold = threading.Event()
    stub.title = "Generated name"
    stub.script(say("Hello."), say("A."), say("B."))
    term = hx.tui()
    term.submit("hi")
    term.wait_for("Hello.")
    stub.wait_for_title_requests(1)
    # The model is still naming the session when the user names it themselves.
    term.submit("/title Release planning")
    term.wait_for("✓ Session title: Release planning")
    stub.title_hold.set()
    stub.title_hold = None

    for prompt, reply in (("a", "A."), ("b", "B.")):
        term.submit(prompt)
        term.wait_for(reply)
    term.submit("/clear")
    term.wait_for("New session started.")
    term.settle(0.5)
    assert len(stub.title_requests) == 1, "a pinned name is not asked for again"
    term.press("ctrl+d")
    assert term.wait_exit() == 0
    (meta,) = [meta for meta in hx.sessions() if meta["prompt_count"] == 3]
    assert meta["title"] == "Release planning"
    assert meta["title_pinned"] is True


def test_copy_puts_the_last_reply_on_the_clipboard(hx: HX, stub: Stub) -> None:
    """/copy copies the last answer - not the command's own echo - and says how."""
    stub.script(say("Copy me, please."))
    term = hx.tui()
    term.submit("hi")
    term.wait_for("Copy me, please.")
    term.submit("/copy")
    term.wait_for("Copied the last reply to the clipboard")
    assert hx.clipboard.read_text() == "Copy me, please."


def test_theme_command(hx: HX) -> None:
    """/theme light switches the palette and names it; an unknown name is refused."""
    term = hx.tui()
    term.submit("/theme light")
    term.wait_for("✓ Theme: light")
    term.snapshot("light theme")
    term.submit("/theme neon")
    term.wait_for("Unknown theme 'neon'")


def test_compact_with_nothing_to_compact(hx: HX, stub: Stub) -> None:
    """/compact on a short session says there is nothing to do - and does not claim to start."""
    stub.script(say("Hello."))
    term = hx.tui()
    term.submit("hi")
    term.wait_for("Hello.")
    term.submit("/compact")
    screen = term.wait_for("Nothing to compact yet.")
    assert "Compacting" not in screen
    assert "2.2k/200k" in screen, "the context gauge is left alone"


def test_compact_summarises_older_turns(hx: HX, stub: Stub) -> None:
    """/compact on a long session asks the model for a summary and replaces older turns with it."""
    replies = [say(f"Answer {n}.") for n in range(1, 7)]
    stub.script(*replies, say("SUMMARY: we discussed six things."), say("Carrying on."))
    term = hx.tui()
    for n in range(1, 7):
        term.submit(f"question {n}")
        term.wait_for(f"Answer {n}.")
    term.submit("/compact focus on the questions")
    screen = term.wait_for("Compacted")
    term.snapshot("after /compact")
    assert "Compacting (/compact)" in term.scrollback()

    summary_request = stub.requests[6]
    assert "focus on the questions" in summary_request.last_user_text()

    term.submit("what next?")
    term.wait_for("Carrying on.")
    after = stub.requests[7]
    assert "SUMMARY: we discussed six things." in str(after.messages)
    assert "question 1" not in str(after.messages)
    gauge = re.search(r"([\d.]+k?)/200k", screen)
    assert gauge and gauge.group(1).endswith("k"), "the gauge still counts the system prompt"


def test_rewind_restores_files_and_the_prompt(hx: HX, stub: Stub) -> None:
    """/rewind goes back to an earlier prompt, undoing the edits made since, and puts that prompt
    back in the editor."""
    target = hx.project / "a.txt"
    target.write_text("original\n")
    stub.script(
        call("Read", file_path=str(target)),
        call("Edit", file_path=str(target), old_string="original", new_string="changed"),
        say("Changed it."),
    )
    term = hx.tui()
    term.submit("change a.txt")
    term.wait_for("Permission needed")
    term.type("y")
    term.wait_for("Changed it.")
    assert target.read_text() == "changed\n"

    term.submit("/rewind")
    term.wait_for("Rewind to")
    term.snapshot("rewind picker")
    term.press("enter")
    screen = term.wait_for("Rewound. 1 file(s) restored.")
    term.snapshot("after rewinding")
    assert target.read_text() == "original\n"
    assert "Changed it." not in screen
    assert "change a.txt" in term.lines()[-5], "the prompt is back in the editor"


def test_clear_and_resume(hx: HX, stub: Stub) -> None:
    """/clear starts a fresh session; /resume lists the old one by title and brings it back."""
    stub.title = "Talking about owls"
    stub.script(say("Owls are nocturnal."))
    term = hx.tui()
    term.submit("tell me about owls")
    term.wait_for("Owls are nocturnal.")

    term.submit("/clear")
    screen = term.wait_for("New session started.")
    assert "Owls are nocturnal." not in screen

    term.submit("/resume")
    screen = term.wait_for("Resume session")
    term.snapshot("resume picker")
    assert "Talking about owls" in screen
    term.press("enter")
    term.wait_for("Owls are nocturnal.")
    term.snapshot("resumed session")


def test_trace_writes_a_self_contained_page(hx: HX, stub: Stub) -> None:
    """/trace writes the session to one HTML file that needs no network to open."""
    stub.script(say("Traced answer."))
    term = hx.tui(columns=140)
    term.submit("trace me")
    term.wait_for("Traced answer.")
    term.submit("/trace")
    term.wait_for("Trace written to")
    (trace,) = hx.hx_home.glob("sessions/*/trace.html")
    page = trace.read_text()
    assert "Traced answer." in page
    assert "trace me" in page
    assert "<script src=" not in page and '<link rel="stylesheet" href="http' not in page


def test_unknown_command(hx: HX) -> None:
    """A mistyped command is reported and not sent to the model."""
    term = hx.tui()
    term.submit("/definitely-not-a-command")
    term.wait_for("definitely-not-a-command")
    term.settle()
    term.snapshot("unknown command")
    assert "Unknown command" in term.text() or "unknown command" in term.text()


SIGN_IN_ORDER = (
    "OpenAI (ChatGPT Plus/Pro)",
    "Claude (Pro/Max, via Claude Code)",
    "OpenRouter API key",
    "Devin (personal or Enterprise)",
)


def test_sign_in_offers_openai_claude_openrouter_devin(hx: HX) -> None:
    """/login and `hx auth login` list the accounts in one order: OpenAI, Claude, OpenRouter,
    Devin. Enter at the terminal's menu still picks OpenRouter, as it did when it came first."""
    term = hx.tui()
    term.submit("/login")
    screen = term.wait_for("Devin (personal or Enterprise)")
    rows = [row for row in screen.split("\n") if any(label in row for label in SIGN_IN_ORDER)]
    assert [label for row in rows for label in SIGN_IN_ORDER if label in row] == list(SIGN_IN_ORDER)
    assert "OpenAI" in rows[0] and "→" in rows[0], "the first account is the one selected"
    term.snapshot("sign-in accounts")
    term.press("esc")

    menu = hx.tui("auth", "login", ready="Choice")
    screen = menu.text()
    numbered = [f"{index}  {label}" for index, label in enumerate(SIGN_IN_ORDER, start=1)]
    assert all(line in screen for line in numbered), screen
    assert "Choice [3]:" in screen
    menu.snapshot("hx auth login menu")
    menu.press("enter")
    menu.wait_for("OpenRouter API key")

#!/usr/bin/env python3
"""LLM-free release tests. Run with `tests.py --self`.

Every check runs offline using the FakeAdapter and injected transports, so the
whole layered stack (core -> adapter -> runtime -> human -> session) is verified
without any model call. These are also the ratchet: evolutions may add checks,
never remove them.
"""
from __future__ import annotations

import json
import os
import pathlib
import py_compile
import sys
import tempfile


# Tests always exercise the release that CONTAINS this tests.py. Supervisor gates set
# ACTIVE_RELEASE consistently, but an improver's local verification command
# (`python ../runtime/releases/<candidate>/tests.py --self`) may inherit the active
# release in the environment; using __file__ avoids accidentally testing the live
# release instead of the candidate under test.
RELEASE = pathlib.Path(__file__).resolve().parent

# Make sibling modules importable regardless of cwd, ahead of any active-release path.
sys.path.insert(0, str(RELEASE))

import core
import adapters
import human as human_mod
import session as session_mod
import tools as tools_mod


def _release_text(name):
    return (RELEASE / name).read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
def check_files():
    """Every required release module is present and byte-compiles."""
    required = ["supervisor.py", "agent.py", "tests.py", "manifest.json",
                "core.py", "adapters.py", "tools.py", "human.py", "session.py",
                "self_model.py", "tui.py", "context.py"]
    for name in required:
        p = RELEASE / name
        assert p.exists(), f"missing {name}"
        if name.endswith(".py"):
            py_compile.compile(str(p), doraise=True)


def check_no_kernel_shadowing():
    """A release must not ship its own organism.py - the kernel is host-controlled."""
    assert not (RELEASE / "organism.py").exists(), "release must not shadow the kernel"


def check_core_is_provider_neutral():
    """core.py imports no provider/transport, keeping the model seam clean."""
    # The core must NOT import any provider/transport. This is the design seam.
    # (Docstrings may mention providers conceptually; imports may not.)
    import ast

    tree = ast.parse(_release_text("core.py"))
    forbidden = ("urllib", "requests", "http", "httpx", "openai", "socket")
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for n in names:
            head = n.split(".")[0].lower()
            assert head not in forbidden, f"core.py imports provider/transport: {n}"


def check_core_loop_executes_and_finishes():
    """The full layered turn loop runs offline and records tool observations in the log."""
    with tempfile.TemporaryDirectory() as d:
        ws = pathlib.Path(d)
        h = human_mod.AutoHumanInterface()
        approval = human_mod.ApprovalPolicy(h, mode="never")
        runtime = tools_mod.ShellToolRuntime(workspace=ws, approval=approval, human=h)
        store = session_mod.SessionStore(ws / "s.jsonl")
        store.seed([core.Event(role="system", content="t"),
                    core.Event(role="user", content="task")])
        adapter = adapters.FakeAdapter([
            core.ModelResult(say="look", tool_calls=[core.ToolCall("1", "shell", {"cmd": "echo hello"})]),
            core.ModelResult(say="bye", tool_calls=[core.ToolCall("2", "finish", {"summary": "ok"})]),
        ])
        outcome = core.run_agent_loop(adapter=adapter, runtime=runtime, session=store,
                                      tools=tools_mod.CANONICAL_TOOLS, system="t", mode="work")
        assert outcome == "finish", outcome
        joined = "\n".join(e.content for e in store.events())
        assert "hello" in joined, "shell observation not recorded in the event log"


def check_ask_user_routes_to_human():
    """The ask_user tool is answered by the HumanInterface, not invented by the model."""
    with tempfile.TemporaryDirectory() as d:
        ws = pathlib.Path(d)

        class Canned(human_mod.HumanInterface):
            def ask(self, q):
                return "blue"

            def confirm(self, p, detail=None):
                return True

        runtime = tools_mod.ShellToolRuntime(
            workspace=ws,
            approval=human_mod.ApprovalPolicy(Canned(), mode="never"),
            human=Canned(),
        )
        obs = runtime.execute(core.ToolCall("1", "ask_user", {"question": "color?"}), "work")
        assert "blue" in obs.output, obs.output


def check_adapter_json_protocol_parsing():
    """The portable JSON-text tool protocol parses plain and fenced replies, rejecting garbage."""
    # plain object
    r = adapters.result_from_protocol(
        adapters.parse_json_object('{"say":"hi","tool":"shell","arguments":{"cmd":"ls"}}'),
        call_id="x")
    assert r.tool_calls and r.tool_calls[0].name == "shell"
    assert r.tool_calls[0].arguments["cmd"] == "ls"
    # fenced + prose around it
    fenced = "Here you go:\n```json\n{\"say\":\"a\",\"final\":true}\n```\nthanks"
    r2 = adapters.result_from_protocol(adapters.parse_json_object(fenced), call_id="y")
    assert r2.final and not r2.tool_calls
    # garbage must raise
    try:
        adapters.parse_json_object("no json here")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def check_chat_adapter_with_fake_transport():
    """OpenAIChatAdapter (json_text) renders tools into the system prompt and parses the reply."""
    captured = {}

    def transport(endpoint, body, headers):
        captured["endpoint"] = endpoint
        captured["body"] = body
        return {"choices": [{"message": {
            "content": '{"say":"running","tool":"shell","arguments":{"cmd":"pwd"}}'}}]}

    a = adapters.OpenAIChatAdapter(endpoint="http://x/v1/chat/completions",
                                   model="m", api_key="k", tool_mode="json_text",
                                   transport=transport)
    turn = core.AgentTurn(system="sys",
                          events=[core.Event(role="user", content="hi")],
                          tools=tools_mod.CANONICAL_TOOLS, mode="work")
    res = a.run_turn(turn)
    assert res.tool_calls and res.tool_calls[0].name == "shell"
    # tools were rendered into the system message (portable protocol).
    sys_msg = captured["body"]["messages"][0]["content"]
    assert "TOOL PROTOCOL" in sys_msg and "shell" in sys_msg


def check_chat_adapter_native_tool_calls():
    """OpenAIChatAdapter (native) sends function schemas and parses message.tool_calls."""
    captured = {}

    def transport(endpoint, body, headers):
        captured["body"] = body
        return {"choices": [{"message": {"content": "running", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "shell", "arguments": '{"cmd":"pwd"}'}}]}}]}

    a = adapters.OpenAIChatAdapter(endpoint="http://x/v1/chat/completions",
                                   model="m", api_key="k", tool_mode="native",
                                   transport=transport)
    assert a.supports_native_tools is True
    turn = core.AgentTurn(system="sys",
                          events=[core.Event(role="user", content="hi")],
                          tools=tools_mod.CANONICAL_TOOLS, mode="work")
    res = a.run_turn(turn)
    assert res.tool_calls and res.tool_calls[0].name == "shell"
    assert res.tool_calls[0].arguments["cmd"] == "pwd"
    # tools sent as native function schemas (not in the system text)
    names = {t["function"]["name"] for t in captured["body"]["tools"]}
    assert "shell" in names and "write_file" in names


def check_native_message_pairing():
    """Native rendering keeps valid assistant/tool pairs and downgrades orphan tool results."""
    a = adapters.OpenAIChatAdapter(endpoint="x", model="m", api_key="k",
                                   tool_mode="native",
                                   transport=lambda *_: {"choices": [{"message": {"content": "ok"}}]})
    evs = [
        core.Event(role="system", content="s"),
        core.Event(role="user", content="do it"),
        core.Event(role="assistant", content="calling",
                   tool_calls=[core.ToolCall("call_1", "shell", {"cmd": "ls"})]),
        core.Event(role="tool", content="exit=0", tool_call_id="call_1", name="shell"),
        # orphan tool result whose assistant call is absent (e.g. compaction split)
        core.Event(role="tool", content="orphan", tool_call_id="call_X", name="shell"),
    ]
    turn = core.AgentTurn(system="s", events=evs,
                          tools=tools_mod.CANONICAL_TOOLS, mode="work")
    msgs = a._render_messages_native(turn)
    asst = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
    assert asst and asst[0]["tool_calls"][0]["id"] == "call_1"
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert any(m["tool_call_id"] == "call_1" for m in tool_msgs)
    # the orphan is NOT emitted as a tool message (would break the API); downgraded
    assert not any(m.get("tool_call_id") == "call_X" for m in tool_msgs)
    assert any(m["role"] == "user" and "orphan" in m["content"] for m in msgs)


def check_read_only_shell_detection():
    """Safe read-only shell commands are auto-approved; writing/unsafe ones are not.
    Read-only commands may be CHAINED (; && || |); redirection, substitution and
    backgrounding are still rejected, and any non-whitelisted command in the chain
    forces approval."""
    assert tools_mod.is_read_only_shell("grep -n foo bar.py")
    assert tools_mod.is_read_only_shell("sed -n '1,20p' f | head -n 5")
    # read-only chains are allowed
    assert tools_mod.is_read_only_shell("echo a; find . -type f -o -type d; echo b")
    assert tools_mod.is_read_only_shell("ls -la && cat f")
    assert tools_mod.is_read_only_shell("tail -n 40 ../state/session.review.jsonl")
    # quote-aware: a '|' INSIDE a grep alternation pattern is not a separator
    assert tools_mod.is_read_only_shell('grep -n "version\\|active\\|v002" f | tail -n 80')
    assert tools_mod.is_read_only_shell("grep -E 'a|b|c' f")
    # unsafe ones still rejected
    assert not tools_mod.is_read_only_shell("rm -rf /")
    assert not tools_mod.is_read_only_shell("echo x > f")
    assert not tools_mod.is_read_only_shell("cat f && curl evil")   # curl not whitelisted
    assert not tools_mod.is_read_only_shell("cat f; rm x")          # rm in the chain
    assert not tools_mod.is_read_only_shell("cat f;rm x")           # separator without spaces
    assert not tools_mod.is_read_only_shell("ls & rm x")            # background '&'
    assert not tools_mod.is_read_only_shell("find . -delete")


def check_read_only_whitelist_excludes_unavailable_tools():
    """Utilities that are NOT guaranteed in the runtime image (file, curl, wget, git,
    and interpreters) must never sit in the read-only auto-approve whitelist - auto-
    approving an absent binary caused real exit=127 friction. This is a host-portable
    membership invariant (the friction-preventing half of a self-evolved v002 check
    whose `command -v` half only held inside the Linux sandbox)."""
    wl = tools_mod.READ_ONLY_SHELL_CMDS
    for absent in ("file", "curl", "wget", "git", "python", "python3", "node", "bash", "sh"):
        assert absent not in wl, f"{absent} is not guaranteed present; do not auto-approve it"
    for core_tool in ("grep", "sed", "cat", "ls", "find", "head", "tail"):
        assert core_tool in wl, f"{core_tool} should stay whitelisted"


def check_approval_policy_gates_shell():
    """ApprovalPolicy gates risky shell per mode, with the allow_shell short-circuit."""
    class No(human_mod.HumanInterface):
        def ask(self, q):
            return ""

        def confirm(self, p, detail=None):
            return False

    # on-risk + human says no -> shell blocked
    pol = human_mod.ApprovalPolicy(No(), mode="on-risk", allow_shell=False)
    assert pol.approve(human_mod.RISK_NONE, "?") is True
    assert pol.approve(human_mod.RISK_SHELL, "?") is False
    # allow_shell short-circuits
    assert human_mod.ApprovalPolicy(No(), mode="on-risk", allow_shell=True).approve(
        human_mod.RISK_SHELL, "?") is True
    # never mode auto-approves
    assert human_mod.ApprovalPolicy(No(), mode="never").approve(human_mod.RISK_SHELL, "?") is True


def check_session_roundtrip_and_compaction():
    """The append-only session log round-trips on disk and compacts to system+head+tail."""
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "s.jsonl"
        store = session_mod.SessionStore(path)
        store.seed([core.Event(role="system", content="S"),
                    core.Event(role="user", content="task")])
        for i in range(20):
            store.append(core.Event(role="assistant", content=f"step {i}",
                                    tool_calls=[core.ToolCall(str(i), "shell", {"cmd": "x"})]))
            store.append(core.Event(role="tool", content="x" * 2000, name="shell"))
        # reload from disk == canonical truth
        again = session_mod.SessionStore(path)
        assert again.load()
        assert len(again.events()) == len(store.events())
        # compaction keeps system + first + tail, drops the middle
        view = store.compact_view(budget=1000, keep=6)
        assert view[0].role == "system"
        assert len(view) < len(store.events())
        assert any("condensed" in (e.content or "") for e in view)


def check_session_resume_is_mode_aware():
    """A session resumes only in the same mode and never after a clean clear."""
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "s.jsonl"
        s = session_mod.SessionStore(path)
        s.seed([core.Event(role="system", content="S"),
                core.Event(role="user", content="task")], mode="work")
        s.append(core.Event(role="assistant", content="step"))
        # same mode resumable, a different mode is not
        assert session_mod.SessionStore(path).resumable("work")
        assert not session_mod.SessionStore(path).resumable("improve")
        # load restores the full log
        again = session_mod.SessionStore(path)
        assert again.load() and len(again.events()) == 3
        # a clean finish (clear) makes it non-resumable
        again.clear()
        assert not session_mod.SessionStore(path).resumable("work")


def check_read_and_replace_tools():
    """read_file/replace_in_file edit candidate files with unique-match and path scoping."""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        ws = root / "workspace"
        ws.mkdir()
        releases = root / "runtime" / "releases"
        (releases / "v002-candidate").mkdir(parents=True)
        h = human_mod.AutoHumanInterface()
        rt = tools_mod.ShellToolRuntime(
            workspace=ws, human=h,
            approval=human_mod.ApprovalPolicy(h, mode="never"),
            releases=releases, state=root / "state")
        f = releases / "v002-candidate" / "m.py"
        f.write_text("a = 1\nb = 2\n")

        def call(name, **args):
            return rt.execute(core.ToolCall("x", name, args), "improve")

        cp = "../runtime/releases/v002-candidate/m.py"
        assert "a = 1" in call("read_file", path=cp).output
        assert "Replaced 1" in call("replace_in_file", path=cp, old="b = 2", new="b = 3").output
        assert f.read_text() == "a = 1\nb = 3\n"
        assert "No match" in call("replace_in_file", path=cp, old="zzz", new="y").output
        f.write_text("x\nx\n")
        assert "Ambiguous" in call("replace_in_file", path=cp, old="x", new="y").output
        # reading outside the project is denied
        assert "outside" in call("read_file", path="../../nope").output


def check_write_file_tool():
    """write_file is gated per mode: workspace in work, *-candidate in improve, never the kernel."""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        ws = root / "workspace"
        ws.mkdir()
        releases = root / "runtime" / "releases"
        (releases / "v001").mkdir(parents=True)
        (releases / "v002-candidate").mkdir(parents=True)
        h = human_mod.AutoHumanInterface()
        rt = tools_mod.ShellToolRuntime(
            workspace=ws, human=h,
            approval=human_mod.ApprovalPolicy(h, mode="never"),
            releases=releases, state=root / "state")

        def w(path, content, mode):
            return rt.execute(core.ToolCall("x", "write_file",
                                            {"path": path, "content": content}), mode)

        # work: workspace write (creates parent dirs)
        w("a/b.txt", "hi", "work")
        assert (ws / "a" / "b.txt").read_text() == "hi"
        # work: cannot write into a release
        assert "Denied" in w("../runtime/releases/v002-candidate/x.py", "x", "work").output
        # improve: can write into a *-candidate
        w("../runtime/releases/v002-candidate/x.py", "print(1)", "improve")
        assert (releases / "v002-candidate" / "x.py").read_text() == "print(1)"
        # improve: cannot overwrite the active (non-candidate) release
        assert "Denied" in w("../runtime/releases/v001/x.py", "x", "improve").output
        # review: read-only
        assert "Denied" in w("z.txt", "x", "review").output
        # outside workspace/releases (kernel etc.) is denied
        assert "Denied" in w("../organism.py", "x", "improve").output


def check_image_attachments_provider_neutral():
    """Images are stored as neutral data-URLs; only the adapter maps them to OpenAI parts."""
    with tempfile.TemporaryDirectory() as d:
        ws = pathlib.Path(d)
        (ws / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        # a local image path -> data-url image; the token becomes a placeholder
        text, imgs = human_mod.extract_image_attachments("look at shot.png", base_dir=ws)
        assert len(imgs) == 1 and imgs[0]["url"].startswith("data:image/png;base64,")
        assert "shot.png" not in text
        # Markdown image form
        _, imgs_md = human_mod.extract_image_attachments("see ![x](shot.png)", base_dir=ws)
        assert len(imgs_md) == 1
        # a pasted data URL passes through unchanged
        durl = "data:image/png;base64,QUJD"
        _, imgs_d = human_mod.extract_image_attachments(durl, base_dir=ws)
        assert imgs_d == [{"url": durl}]
        # /paste resolves the newest staged clip-*.png
        (ws / "clip-20200101-000000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        _, imgs_p = human_mod.extract_image_attachments("/paste what is this?", base_dir=ws)
        assert len(imgs_p) == 1
        # ONLY the adapter maps neutral images to OpenAI image_url parts
        ev = core.Event(role="user", content="hi", images=[{"url": durl}])
        a = adapters.OpenAIChatAdapter(endpoint="x", model="m", api_key="k",
                                       transport=lambda *_: {"choices": [{"message": {"content": "ok"}}]})
        parts = a._user_content(ev)
        assert isinstance(parts, list)
        assert parts[0]["type"] == "text" and parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"] == durl


def check_capability_floor_concepts():
    """The wiring layer keeps friction memory, a self-improvement path, and human-in-the-loop."""
    # Constitutional capabilities must stay visible in the wiring layer.
    agent = _release_text("agent.py").lower()
    assert "backlog" in agent or "friction" in agent, "must keep a friction memory"
    assert "pivot" in agent or "improve" in agent, "must keep a self-improvement path"
    assert "ask_user" in agent or "human" in agent, "must keep human-in-the-loop"


def check_modes_supported():
    """agent.py supports exactly the four modes work/improve/review/evolve."""
    agent = _release_text("agent.py")
    for mode in ("work", "improve", "review", "evolve"):
        assert mode in agent, f"agent must support {mode}"


def check_evolution_tools_gated():
    """request_promotion is offered only in the evolution modes, never in work/review."""
    # request_promotion must NOT be offered in work/review, only in evolution modes.
    base = {t.name for t in tools_mod.CANONICAL_TOOLS}
    evo = {t.name for t in tools_mod.EVOLUTION_TOOLS}
    assert "request_promotion" not in base
    assert "request_promotion" in evo
    assert base.issubset(evo)


def check_request_promotion_writes_request():
    """request_promotion is denied outside evolution modes and writes a validated request."""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        releases = root / "runtime" / "releases"
        (releases / "v002-candidate").mkdir(parents=True)
        state = root / "state"
        h = human_mod.AutoHumanInterface()
        runtime = tools_mod.ShellToolRuntime(
            workspace=root, human=h,
            approval=human_mod.ApprovalPolicy(h, mode="never"),
            releases=releases, state=state)

        # denied outside evolution modes
        denied = runtime.execute(
            core.ToolCall("1", "request_promotion", {"candidate": "v002-candidate"}), "work")
        assert "Denied" in denied.output, denied.output
        assert not (state / "promotion_request.json").exists()

        # allowed in improve -> writes a well-formed request
        ok = runtime.execute(
            core.ToolCall("2", "request_promotion",
                          {"candidate": "v002-candidate", "reason": "x"}), "improve")
        promo = state / "promotion_request.json"
        assert promo.exists(), ok.output
        data = json.loads(promo.read_text(encoding="utf-8"))
        assert data["candidate"] == "runtime/releases/v002-candidate"
        assert data["requested_by"] == "agent"

        # a non-existent candidate is refused
        missing = runtime.execute(
            core.ToolCall("3", "request_promotion", {"candidate": "v999-candidate"}), "improve")
        assert "does not exist" in missing.output, missing.output

        # a malformed candidate name is refused
        bad = runtime.execute(
            core.ToolCall("4", "request_promotion", {"candidate": "../etc"}), "improve")
        assert "Denied" in bad.output, bad.output


def check_self_model_reflects_tools_and_capabilities():
    """EVA's self-model is generated from its own code, so it lists the real tools
    and ratchet-pinned capabilities and cannot drift from reality."""
    import self_model as sm

    digest = sm.render_digest()
    # Every canonical tool (its actual skills) must appear in the self-model.
    for tool in tools_mod.CANONICAL_TOOLS:
        assert tool.name in digest, f"self-model omits tool {tool.name}"
    # Anatomy comes from manifest layers.
    assert "agent.py" in digest and "core.py" in digest, digest
    # Capabilities are derived from THIS tests.py: the set grows with each release,
    # and this very check must be among them (self-reference proves liveness).
    names = {n for n, _ in sm.capabilities()}
    assert "check_self_model_reflects_tools_and_capabilities" in names
    assert len(names) >= 1
    # The verbose view must also render without a model call.
    assert "Capability details:" in sm.render_full()


def check_self_model_is_llm_free():
    """The self-model is built from local code only - never a provider/model call."""
    import ast

    tree = ast.parse(_release_text("self_model.py"))
    forbidden = ("urllib", "requests", "http", "httpx", "openai", "socket")
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for n in names:
            assert n.split(".")[0].lower() not in forbidden, f"self_model imports {n}"


def check_inspect_self_tool():
    """EVA pulls its own anatomy/skills/capabilities ON DEMAND via the inspect_self
    tool, in every mode, instead of being preloaded with the full self-model."""
    assert any(t.name == "inspect_self" for t in tools_mod.CANONICAL_TOOLS)

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        h = human_mod.AutoHumanInterface()
        runtime = tools_mod.ShellToolRuntime(
            workspace=root, human=h,
            approval=human_mod.ApprovalPolicy(h, mode="never"))

        # default topic -> overview self-model (works even in read-only review mode)
        overview = runtime.execute(core.ToolCall("1", "inspect_self", {}), "review")
        assert "SELF-MODEL" in overview.output, overview.output

        # targeted slices
        anatomy = runtime.execute(
            core.ToolCall("2", "inspect_self", {"topic": "anatomy"}), "work")
        assert "agent.py" in anatomy.output and "core.py" in anatomy.output

        skills = runtime.execute(
            core.ToolCall("3", "inspect_self", {"topic": "skills"}), "work")
        assert "inspect_self" in skills.output

        # a filename topic returns that module's own docstring (deep-dive on demand)
        detail = runtime.execute(
            core.ToolCall("4", "inspect_self", {"topic": "self_model.py"}), "work")
        assert "self-model" in detail.output.lower()


def check_tui_status_view_renders_activity():
    """The TUI is presentation-only: its pure formatters turn loop events into clear,
    human-readable 'what EVA is doing now' lines (tool calls, shell exit codes,
    errors), and it imports no provider/transport."""
    import ast
    import io
    import tui

    # a tool call is summarised as a one-glance action
    line = tui.format_tool_call(core.ToolCall("1", "shell", {"cmd": "echo hi"}), color=False)
    assert "shell" in line and "echo hi" in line
    assert tui.summarize_args("write_file", {"path": "app.py", "content": "x"}) == "app.py"
    assert tui.summarize_args("inspect_self", {"topic": "anatomy"}) == "topic=anatomy"

    # shell observations surface the exit code; failures are distinguishable
    ok = tui.format_observation(core.ToolObservation("1", "shell", "exit=0\nstdout:\nhi"), color=False)
    bad = tui.format_observation(core.ToolObservation("2", "shell", "exit=1\nstderr:\nboom"), color=False)
    assert "exit=0" in ok and "exit=1" in bad

    err = tui.format_error("model", RuntimeError("down"), color=False)
    assert "model error" in err and "down" in err

    # finish is rendered once (as the tool-call line) and not echoed as an observation
    fin = tui.format_tool_call(core.ToolCall("9", "finish", {"summary": "all good"}), color=False)
    assert "finished" in fin and "all good" in fin
    # finish is EVA's final message -> shown IN FULL, never truncated
    long_summary = ("I can do many things: " + "details " * 80).strip()
    fin_long = tui.format_tool_call(core.ToolCall("9", "finish", {"summary": long_summary}), color=False)
    assert long_summary in fin_long and "…" not in fin_long
    buf_fin = io.StringIO()
    v2 = tui.StatusView(mode="work", stream=buf_fin, color=False)
    v2.observation(core.ToolObservation("9", "finish", "all good"))
    assert buf_fin.getvalue() == "", "finish observation must not be echoed"

    # EVA_TUI_FULL expands a long shell command instead of truncating it
    long_cmd = "echo " + "x" * 300
    compact = tui.format_tool_call(core.ToolCall("1", "shell", {"cmd": long_cmd}), color=False, full=False)
    expanded = tui.format_tool_call(core.ToolCall("1", "shell", {"cmd": long_cmd}), color=False, full=True)
    assert len(expanded) > len(compact) and "…" not in expanded

    # the StatusView writes to its stream (here a buffer, so tests need no TTY)
    buf = io.StringIO()
    view = tui.StatusView(mode="work", identity={"model": "fake"}, release="v001",
                          stream=buf, color=False)
    view.header()
    view.tool_call(core.ToolCall("1", "inspect_self", {"topic": "skills"}))
    text = buf.getvalue()
    assert "mode=work" in text and "inspect_self" in text

    # the branded welcome shows the run identity and a short how-to
    buf_w = io.StringIO()
    tui.StatusView(mode="work", identity={"model": "gpt-5.5"}, release="v001",
                   stream=buf_w, color=False).welcome()
    wtext = buf_w.getvalue()
    assert "mode=work" in wtext and "EVA_TUI_FULL" in wtext

    # presentation only: no provider/transport imports
    forbidden = ("urllib", "requests", "http", "httpx", "openai", "socket")
    tree = ast.parse(_release_text("tui.py"))
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for n in names:
            assert n.split(".")[0].lower() not in forbidden, f"tui imports {n}"


def check_approval_offers_full_detail_on_demand():
    """A risky action can be inspected before approving: the full command/content is
    passed to the human as `detail` and revealed on demand via the 'f' key, rather than
    dumped into the normal stream. Approving blind is never forced."""
    import builtins
    import io
    import sys as _sys

    # ApprovalPolicy forwards the detail to the human's confirm()
    seen = {}

    class Rec(human_mod.HumanInterface):
        def ask(self, q):
            return ""

        def confirm(self, prompt, detail=None):
            seen["detail"] = detail
            return True

    pol = human_mod.ApprovalPolicy(Rec(), mode="on-risk")
    assert pol.approve(human_mod.RISK_SHELL, "Approve shell?", detail="rm -rf /tmp/x") is True
    assert seen["detail"] == "rm -rf /tmp/x"

    # the real CLI confirm understands the 'f' fold key: it shows the full detail, then
    # re-prompts (here: 'f' to reveal, then 'y' to approve)
    answers = iter(["f", "y"])
    orig_input, orig_out = builtins.input, _sys.stdout
    builtins.input = lambda *a, **k: next(answers)
    _sys.stdout = io.StringIO()
    try:
        ok = human_mod.CliHumanInterface().confirm("Approve shell?", detail="the FULL command")
        shown = _sys.stdout.getvalue()
    finally:
        builtins.input, _sys.stdout = orig_input, orig_out
    assert ok is True and "the FULL command" in shown


def check_context_compaction_is_own_llm_free_layer():
    """Compaction is its own deterministic, LLM-free layer (context.py): it keeps
    system + trimmed first task + a condensed summary of dropped turns + the last
    `keep` events, never imports a provider, and the session store delegates to it."""
    import ast
    import context as context_mod

    tree = ast.parse(_release_text("context.py"))
    forbidden = ("urllib", "requests", "http", "httpx", "openai", "socket")
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for n in names:
            assert n.split(".")[0].lower() not in forbidden, n

    # small history -> sent verbatim
    small = [core.Event(role="system", content="S"), core.Event(role="user", content="task")]
    assert context_mod.compact(small, budget=1000, keep=6) == small

    # large history -> system + trimmed first + condensed summary + last `keep`
    big = [core.Event(role="system", content="S"), core.Event(role="user", content="task")]
    for i in range(20):
        big.append(core.Event(role="assistant", content=f"step {i}",
                              tool_calls=[core.ToolCall(str(i), "shell", {"cmd": "x"})]))
        big.append(core.Event(role="tool", content="x" * 2000, name="shell"))
    view = context_mod.compact(big, budget=1000, keep=6)
    assert view[0].role == "system" and len(view) < len(big)
    assert any("condensed" in (e.content or "") for e in view)

    # the session store delegates to this layer (no duplicate logic)
    with tempfile.TemporaryDirectory() as d:
        store = session_mod.SessionStore(pathlib.Path(d) / "s.jsonl")
        store.seed(small)
        assert store.compact_view(1000, 6) == small


def check_blob_store_keeps_images_out_of_the_log():
    """Image attachments are stored on disk as small blob references
    (state/blobs/<sha256>), not fat inline data-URLs, and rehydrate to provider-neutral
    data-URLs on load - so the event log stays compact while adapters still see
    {"url": "data:..."}."""
    import base64

    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "state" / "s.jsonl"
        raw = b"\x89PNG\r\n\x1a\n" + b"abc" * 200
        durl = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        store = session_mod.SessionStore(path)
        store.seed([core.Event(role="system", content="S"),
                    core.Event(role="user", content="see", images=[{"url": durl}])])

        # on disk: a blob exists and the JSONL holds a ref, not the fat data-URL
        blobs = list((path.parent / "blobs").glob("*.png"))
        assert len(blobs) == 1, blobs
        disk = path.read_text(encoding="utf-8")
        assert "data:image/png" not in disk and "blobs/" in disk

        # reload rehydrates to the exact provider-neutral data-URL
        again = session_mod.SessionStore(path)
        assert again.load()
        ev = [e for e in again.events() if e.role == "user"][0]
        assert ev.images and ev.images[0].get("url") == durl


def check_clipboard_bridge_container_safe():
    """The clipboard bridge respects the sandbox: inside the container EVA never tries
    to read the host clipboard (it can't) and /paste falls back to a staged file; the
    host-direct grab is a clearly separated, injectable path."""
    prev = os.environ.get("EVA_IN_CONTAINER")
    os.environ["EVA_IN_CONTAINER"] = "1"
    try:
        assert human_mod.running_in_container()
        with tempfile.TemporaryDirectory() as d:
            ws = pathlib.Path(d)
            # in the container the host clipboard is never read
            assert human_mod.grab_clipboard_image(ws) is None
            # /paste still resolves a staged screenshot to a provider-neutral image
            (ws / "clip-20200101-000000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            _, imgs = human_mod.extract_image_attachments("/paste look", base_dir=ws)
            assert len(imgs) == 1 and imgs[0]["url"].startswith("data:image/png;base64,")
    finally:
        if prev is None:
            os.environ.pop("EVA_IN_CONTAINER", None)
        else:
            os.environ["EVA_IN_CONTAINER"] = prev


def check_event_audit_metadata():
    """Assistant/tool events are stamped with audit metadata (timestamp, mode, step,
    model/adapter identity, tool id) and it survives a disk round-trip - so an evolving
    system stays accountable: who proposed what, with which model, when."""
    with tempfile.TemporaryDirectory() as d:
        ws = pathlib.Path(d)
        h = human_mod.AutoHumanInterface()
        runtime = tools_mod.ShellToolRuntime(
            workspace=ws, human=h,
            approval=human_mod.ApprovalPolicy(h, mode="never"))
        store = session_mod.SessionStore(ws / "s.jsonl")
        store.seed([core.Event(role="system", content="t"),
                    core.Event(role="user", content="task")])
        adapter = adapters.FakeAdapter([
            core.ModelResult(say="x", tool_calls=[core.ToolCall("1", "shell", {"cmd": "echo hi"})]),
            core.ModelResult(say="done", tool_calls=[core.ToolCall("2", "finish", {"summary": "ok"})]),
        ])
        core.run_agent_loop(adapter=adapter, runtime=runtime, session=store,
                            tools=tools_mod.CANONICAL_TOOLS, system="t", mode="work")

        asst = [e for e in store.events() if e.role == "assistant"]
        tools_ev = [e for e in store.events() if e.role == "tool"]
        assert asst and tools_ev
        m = asst[0].meta
        assert m.get("mode") == "work" and m.get("step") == 1
        assert isinstance(m.get("ts"), (int, float))
        assert m.get("adapter") == "fake" and m.get("model") == "fake"
        tm = tools_ev[0].meta
        assert tm.get("kind") == "tool" and tm.get("tool") == "shell"
        assert tm.get("tool_call_id") == "1" and tm.get("step") == 1
        # durable: reload from disk and the audit trail is still there
        again = session_mod.SessionStore(ws / "s.jsonl")
        assert again.load()
        reloaded = [e for e in again.events() if e.role == "assistant"][0]
        assert reloaded.meta.get("mode") == "work" and reloaded.meta.get("model") == "fake"


def check_mode_policy_table():
    """A single explicit table declares what each mode may do, and the runtime enforces
    exactly it: review is read-only, work cannot touch candidates or promote, and
    improve/evolve may edit candidates and request promotion; unknown modes lock down."""
    pol = tools_mod.MODE_POLICIES
    assert set(pol) == {"work", "review", "improve", "evolve"}
    assert pol["review"] == tools_mod.ModePolicy(False, False, False, False)
    assert pol["work"].write_workspace and pol["work"].run_writing_shell
    assert not pol["work"].write_candidate and not pol["work"].request_promotion
    for m in ("improve", "evolve"):
        assert pol[m].write_candidate and pol[m].request_promotion
    # an unexpected mode gets the most restrictive policy (fail safe)
    assert tools_mod.policy_for("nonsense") == tools_mod._LOCKED_POLICY
    # EVA can read its own boundaries on demand via the self-model
    import self_model as sm
    pol_text = sm.lookup("policy")
    assert "review" in pol_text and "request_promotion" in pol_text


def check_loop_stops_on_should_stop():
    """should_stop ends the loop promptly between actions (e.g. a pivot) instead of
    running until the model happens to finish - so a requested stop takes effect now."""
    with tempfile.TemporaryDirectory() as d:
        ws = pathlib.Path(d)
        h = human_mod.AutoHumanInterface()
        runtime = tools_mod.ShellToolRuntime(
            workspace=ws, human=h,
            approval=human_mod.ApprovalPolicy(h, mode="never"))
        store = session_mod.SessionStore(ws / "s.jsonl")
        store.seed([core.Event(role="system", content="t"),
                    core.Event(role="user", content="task")])
        adapter = adapters.FakeAdapter([
            core.ModelResult(say="a", tool_calls=[core.ToolCall("1", "shell", {"cmd": "echo hi"})]),
            core.ModelResult(say="b", tool_calls=[core.ToolCall("2", "shell", {"cmd": "echo more"})]),
        ])
        stop = {"flag": False}
        outcome = core.run_agent_loop(
            adapter=adapter, runtime=runtime, session=store,
            tools=tools_mod.CANONICAL_TOOLS, system="t", mode="work",
            on_observation=lambda obs: stop.__setitem__("flag", True),
            should_stop=lambda: stop["flag"])
        assert outcome == "stopped", outcome
        # only the FIRST tool ran; the loop did not start a second model turn
        assert sum(1 for e in store.events() if e.role == "tool") == 1


def check_shell_friction_ignores_normal_nonzero_exits():
    """A non-zero shell exit with NO stderr (grep no-match, `test`, `command -v`, a
    script's own `exit 1`) is normal control flow and records NO friction; a real error
    (non-empty stderr) records friction with an error-specific signature so unrelated
    failures don't collapse into one coarse `shell:exit=1` pivot bucket."""
    import agent

    recorded = []
    orig = agent.backlog_append
    agent.backlog_append = lambda entry: recorded.append(entry)
    try:
        assert agent.record_shell_friction("exit=1\nstdout:\ncurl_path=/x\nstderr:\n", "work") is None
        assert recorded == []
        sig = agent.record_shell_friction("exit=127\nstdout:\nstderr:\nfoo: command not found", "work")
        assert sig and sig.startswith("shell:exit=127:") and "command" in sig
        assert len(recorded) == 1
        sig2 = agent.record_shell_friction("exit=1\nstdout:\nstderr:\npermission denied", "work")
        assert sig2 != sig
    finally:
        agent.backlog_append = orig


def check_shell_friction_never_uses_coarse_exit_signature():
    """Recorded shell friction signatures always include an error-specific suffix,
    never the coarse `shell:exit=N` bucket, so unrelated failures cannot aggregate
    into a spurious pivot like repeated `shell:exit=1`."""
    import agent

    recorded = []
    orig = agent.backlog_append
    agent.backlog_append = lambda entry: recorded.append(entry)
    try:
        assert agent.record_shell_friction("exit=1\nstdout:\nstderr:\n!!!", "work") == \
            "shell:exit=1:unknown-stderr"
        sig = agent.record_shell_friction("exit=1\nstdout:\nstderr:\nNo such file", "work")
        assert sig == "shell:exit=1:no-such-file"
        assert all(r["signature"] != "shell:exit=1" for r in recorded)
        assert len({r["signature"] for r in recorded}) == 2
    finally:
        agent.backlog_append = orig


def check_session_self_awareness():
    """EVA always knows where its OWN conversation lives: every mode's system prompt
    points at the append-only session log and says it may read it to recall earlier
    details - so it looks things up instead of guessing or asking the user."""
    import agent

    for mode in ("work", "improve", "review", "evolve"):
        sys_text = agent.system_for(mode)
        assert f"session.{mode}.jsonl" in sys_text, mode
        low = sys_text.lower()
        assert "append-only" in low and "read" in low, mode


def check_prompt_warns_about_missing_optional_binaries():
    """The runtime environment prompt prevents repeated exit=127 friction by telling
    EVA that common utilities may be absent, to verify optional binaries before use,
    and to prefer known-available Python/Node fallbacks for HTTP and scripting.
    (Backport of a self-evolved v002 check.)"""
    import agent

    env = agent.ENV_CAPABILITIES.lower()
    assert "no curl/wget" in env
    assert "python" in env and "node" in env
    assert "not every common unix utility is installed" in env
    assert "command -v" in env


def check_exact_mode_set_is_single_source_consistent():
    """The exact EVA mode set (work, improve, review, evolve) is identical across EVERY
    layer that carries modes - agent.MODES, agent.SYSTEMS, tools.MODE_POLICIES and the
    supervisor's per-mode dry-run gates. An exact-set comparison (not a weak substring)
    so a candidate cannot silently drop a mode, add a stray one, or pass by accident.
    (Reconstructed backport of a self-evolved v002 check lost to a reseed.)"""
    import agent

    expected = {"work", "improve", "review", "evolve"}
    assert set(agent.MODES) == expected, agent.MODES
    assert set(agent.SYSTEMS.keys()) == expected, set(agent.SYSTEMS.keys())
    assert set(tools_mod.MODE_POLICIES.keys()) == expected, set(tools_mod.MODE_POLICIES.keys())
    # every mode is exercised by a supervisor dry-run gate
    sup = _release_text("supervisor.py")
    for mode in expected:
        assert f'"--dry-run", "{mode}"' in sup, f"supervisor omits dry-run for {mode}"


def check_supervisor_gates_all_modes():
    """Promotion qualification exercises every supported mode, including evolve.

    If the supervisor omits a mode from dry-runs or qualification rounds, a candidate
    can break that mode yet still be promoted. Keep this gate in sync with EVA's exact
    mode set: work, improve, review, evolve."""
    import ast

    tree = ast.parse(_release_text("supervisor.py"))

    def list_contains_mode_call(function_name: str, mode: str) -> bool:
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            elts = node.elts
            if len(elts) != 4:
                continue
            if not (isinstance(elts[1], ast.BinOp) and isinstance(elts[1].op, ast.Div)):
                continue
            right = elts[1].right
            if not (isinstance(right, ast.Constant) and right.value == "agent.py"):
                continue
            if not (isinstance(elts[2], ast.Constant) and elts[2].value == "--dry-run"):
                continue
            if not (isinstance(elts[3], ast.Constant) and elts[3].value == mode):
                continue
            parent = getattr(node, "_eva_parent", None)
            while parent is not None and not isinstance(parent, ast.FunctionDef):
                parent = getattr(parent, "_eva_parent", None)
            if parent is not None and parent.name == function_name:
                return True
        return False

    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._eva_parent = parent

    for mode in ("work", "improve", "review", "evolve"):
        assert list_contains_mode_call("qualification_round", mode), \
            f"qualification_round omits dry-run for {mode}"

    text = _release_text("supervisor.py")
    for mode in ("work", "improve", "review", "evolve"):
        assert f'--dry-run", "{mode}"' in text, f"candidate gate omits {mode} dry-run"


def check_ratchet_count_is_executed_not_static():
    """tests.py --count reports the checks that ACTUALLY execute (len of the live
    registry), so the promotion ratchet counts real checks - not dead or duplicated
    test code that a static regex would wrongly inflate."""
    import subprocess
    import sys as _sys

    r = subprocess.run([_sys.executable, str(RELEASE / "tests.py"), "--count"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    n = int(r.stdout.strip().splitlines()[-1])
    assert n == len(_all_checks()), f"{n} != {len(_all_checks())}"
    assert n >= 20


def _all_checks():
    return [v for k, v in sorted(globals().items())
            if k.startswith(("check_", "test_")) and callable(v)]


def run_self():
    failed = 0
    for fn in _all_checks():
        try:
            fn()
            print("ok  ", fn.__name__)
        except Exception as exc:
            failed += 1
            print("FAIL", fn.__name__, "->", type(exc).__name__, exc)
    if failed:
        print(f"\n{failed} check(s) failed.")
        sys.exit(1)
    print("\nall checks passed.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--count" in args:
        # Number of checks that ACTUALLY execute. Defined here (after every check) so
        # any dead code below this block is naturally excluded - the ratchet uses this
        # instead of a static regex that would also count unreachable test defs.
        print(len(_all_checks()))
    elif "--self" in args or not args:
        run_self()
    else:
        raise SystemExit("Usage: tests.py [--self|--count]")

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


RELEASE = pathlib.Path(os.environ.get("ACTIVE_RELEASE", pathlib.Path(__file__).resolve().parent)).resolve()

# Make sibling modules importable regardless of cwd.
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
    required = ["supervisor.py", "agent.py", "tests.py", "manifest.json",
                "core.py", "adapters.py", "tools.py", "human.py", "session.py"]
    for name in required:
        p = RELEASE / name
        assert p.exists(), f"missing {name}"
        if name.endswith(".py"):
            py_compile.compile(str(p), doraise=True)


def check_no_kernel_shadowing():
    assert not (RELEASE / "organism.py").exists(), "release must not shadow the kernel"


def check_core_is_provider_neutral():
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
    with tempfile.TemporaryDirectory() as d:
        ws = pathlib.Path(d)

        class Canned(human_mod.HumanInterface):
            def ask(self, q):
                return "blue"

            def confirm(self, p):
                return True

        runtime = tools_mod.ShellToolRuntime(
            workspace=ws,
            approval=human_mod.ApprovalPolicy(Canned(), mode="never"),
            human=Canned(),
        )
        obs = runtime.execute(core.ToolCall("1", "ask_user", {"question": "color?"}), "work")
        assert "blue" in obs.output, obs.output


def check_adapter_json_protocol_parsing():
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
    assert tools_mod.is_read_only_shell("grep -n foo bar.py")
    assert tools_mod.is_read_only_shell("sed -n '1,20p' f | head -n 5")
    assert not tools_mod.is_read_only_shell("rm -rf /")
    assert not tools_mod.is_read_only_shell("echo x > f")
    assert not tools_mod.is_read_only_shell("cat f && curl evil")
    assert not tools_mod.is_read_only_shell("find . -delete")


def check_approval_policy_gates_shell():
    class No(human_mod.HumanInterface):
        def ask(self, q):
            return ""

        def confirm(self, p):
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


def check_capability_floor_concepts():
    # Constitutional capabilities must stay visible in the wiring layer.
    agent = _release_text("agent.py").lower()
    assert "backlog" in agent or "friction" in agent, "must keep a friction memory"
    assert "pivot" in agent or "improve" in agent, "must keep a self-improvement path"
    assert "ask_user" in agent or "human" in agent, "must keep human-in-the-loop"


def check_modes_supported():
    agent = _release_text("agent.py")
    for mode in ("work", "improve", "review", "evolve"):
        assert mode in agent, f"agent must support {mode}"


def check_evolution_tools_gated():
    # request_promotion must NOT be offered in work/review, only in evolution modes.
    base = {t.name for t in tools_mod.CANONICAL_TOOLS}
    evo = {t.name for t in tools_mod.EVOLUTION_TOOLS}
    assert "request_promotion" not in base
    assert "request_promotion" in evo
    assert base.issubset(evo)


def check_request_promotion_writes_request():
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
    if "--self" in sys.argv[1:] or not sys.argv[1:]:
        run_self()
    else:
        raise SystemExit("Usage: tests.py --self")

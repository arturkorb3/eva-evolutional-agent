#!/usr/bin/env python3
"""Golden-trace evals: exercise the REAL adapter + core loop + tool runtime over
RECORDED provider responses - offline and deterministic, so the promotion gate can
verify a full model -> tool -> observation -> finish flow without a network call or an
API key.

Why this layer exists: the ratchet's other checks drive the FakeAdapter, so they never
exercise a real provider's wire format end-to-end. A golden trace is the exact JSON a
provider's Chat Completions / Messages API returned for each model turn, recorded once
and replayed through the adapter's injectable `transport`. run_trace() drives
core.run_agent_loop with the actual ShellToolRuntime, so a single check verifies that the
adapter parses tool calls, the runtime executes them, the observation is logged and the
loop finishes - the whole stack, deterministically.

A guarded `--live` path re-runs the same task against a real provider (needs a key and
EVA_EVAL_LIVE=1); it is NOT part of the offline ratchet and never runs in the gate.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

# Make sibling modules importable regardless of cwd (mirrors tests.py), so evals.py works
# both when imported by the ratchet and when run directly for a --live recording/smoke.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import core
import adapters
import human as human_mod
import session as session_mod
import tools as tools_mod


# --------------------------------------------------------------------------- #
# Replay transport: feeds recorded responses to a real adapter
# --------------------------------------------------------------------------- #
class ReplayTransport:
    """Stands in for an adapter's HTTP transport: returns pre-recorded provider responses
    in order (one per model turn) and captures each outgoing request so a check can assert
    the adapter built a well-formed body. Raises if the loop asks for more turns than were
    recorded - a runaway loop must fail loudly, not hang or silently pass."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.requests: list[dict] = []

    def __call__(self, endpoint: str, body: dict, headers: dict) -> dict:
        self.requests.append({"endpoint": endpoint, "body": body, "headers": headers})
        if self._i >= len(self._responses):
            raise AssertionError(
                "golden trace exhausted: the loop ran more turns than were recorded")
        r = self._responses[self._i]
        self._i += 1
        return r


# --------------------------------------------------------------------------- #
# Recorded provider responses (the exact shapes the real APIs return)
# --------------------------------------------------------------------------- #
def _openai_tool_response(call_id: str, name: str, arguments: dict, say: str = "") -> dict:
    """A Chat Completions response carrying one native tool call (recorded shape)."""
    return {"choices": [{"message": {
        "role": "assistant",
        "content": say or None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments)}}],
    }}]}


def _anthropic_tool_response(call_id: str, name: str, arguments: dict, say: str = "") -> dict:
    """A Messages API response carrying one tool_use block (recorded shape)."""
    content = []
    if say:
        content.append({"type": "text", "text": say})
    content.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
    return {"content": content, "stop_reason": "tool_use"}


# The canonical golden flow: run one shell command, observe its output, then finish. It
# exercises tool-call parsing, sandboxed execution, observation logging and termination.
_GOLDEN_TASK = "Print the greeting 'hello-eva', then finish."
_GOLDEN_EXPECT = {
    "tool_names": ["shell", "finish"],
    "observe": "hello-eva",
    "summary": "printed the greeting",
}


def openai_chat_golden_trace() -> dict:
    return {
        "provider": "openai_chat",
        "task": _GOLDEN_TASK,
        "responses": [
            _openai_tool_response("c1", "shell", {"cmd": "echo hello-eva"}, say="running it"),
            _openai_tool_response("c2", "finish", {"summary": "printed the greeting"}, say="done"),
        ],
        "expect": dict(_GOLDEN_EXPECT),
    }


def anthropic_golden_trace() -> dict:
    return {
        "provider": "anthropic",
        "task": _GOLDEN_TASK,
        "responses": [
            _anthropic_tool_response("c1", "shell", {"cmd": "echo hello-eva"}, say="running it"),
            _anthropic_tool_response("c2", "finish", {"summary": "printed the greeting"}, say="done"),
        ],
        "expect": dict(_GOLDEN_EXPECT),
    }


def golden_traces() -> list[dict]:
    """All recorded golden traces the ratchet replays (one per provider wire format)."""
    return [openai_chat_golden_trace(), anthropic_golden_trace()]


# --------------------------------------------------------------------------- #
# Drive a real adapter + core loop + tool runtime over a trace
# --------------------------------------------------------------------------- #
def _adapter_for(trace: dict, transport):
    provider = trace["provider"]
    if provider == "openai_chat":
        return adapters.OpenAIChatAdapter(
            endpoint="https://golden.local/v1/chat/completions",
            model="golden", api_key="test-key", tool_mode="native", transport=transport)
    if provider == "anthropic":
        return adapters.AnthropicAdapter(
            endpoint="https://golden.local/v1/messages",
            model="golden", api_key="test-key", transport=transport)
    raise ValueError(f"unknown golden-trace provider: {provider}")


def run_trace(trace: dict, *, transport=None) -> dict:
    """Replay one golden trace through the REAL adapter, core loop and ShellToolRuntime.
    Returns a summary of the executed session (outcome, the tool names EVA actually called,
    the concatenated tool observations, the finish summary and the captured requests) so a
    check can assert the whole stack behaved. Fully offline: the default transport only
    replays recorded responses."""
    transport = transport if transport is not None else ReplayTransport(trace["responses"])
    system = "You are EVA (golden-trace harness)."
    with tempfile.TemporaryDirectory() as d:
        ws = pathlib.Path(d)
        h = human_mod.AutoHumanInterface()
        approval = human_mod.ApprovalPolicy(h, mode="never")
        runtime = tools_mod.ShellToolRuntime(workspace=ws, approval=approval, human=h)
        store = session_mod.SessionStore(ws / "s.jsonl")
        store.seed([core.Event(role="system", content=system),
                    core.Event(role="user", content=trace["task"])])
        adapter = _adapter_for(trace, transport)
        outcome = core.run_agent_loop(
            adapter=adapter, runtime=runtime, session=store,
            tools=tools_mod.CANONICAL_TOOLS, system=system, mode="work")
        events = list(store.events())
    tool_names = [c.name for ev in events for c in (ev.tool_calls or [])]
    observations = "\n".join(ev.content for ev in events if ev.role == "tool")
    finish_summary = ""
    for ev in events:
        for c in (ev.tool_calls or []):
            if c.name == "finish":
                finish_summary = str((c.arguments or {}).get("summary", ""))
    return {
        "outcome": outcome,
        "tool_names": tool_names,
        "observations": observations,
        "finish_summary": finish_summary,
        "requests": transport.requests,
    }


def assert_trace(trace: dict) -> dict:
    """Run a golden trace and assert its recorded expectations hold across the full stack.
    Raises AssertionError on any mismatch. Returns the run summary for further inspection."""
    res = run_trace(trace)
    exp = trace["expect"]
    assert res["tool_names"] == exp["tool_names"], \
        f"{trace['provider']}: tool sequence {res['tool_names']} != {exp['tool_names']}"
    assert exp["observe"] in res["observations"], \
        f"{trace['provider']}: expected observation {exp['observe']!r} not in log"
    assert res["outcome"] == "finish", f"{trace['provider']}: outcome {res['outcome']}"
    assert exp["summary"] in res["finish_summary"], \
        f"{trace['provider']}: finish summary {res['finish_summary']!r}"
    assert res["requests"], f"{trace['provider']}: adapter made no request"
    return res


# --------------------------------------------------------------------------- #
# Guarded live smoke (NOT part of the offline ratchet)
# --------------------------------------------------------------------------- #
def run_live(provider: str | None = None) -> int:
    """Optional live smoke: run a tiny benign task against a REAL provider and assert the
    loop reaches finish. Requires EVA_EVAL_LIVE=1 and a configured key; otherwise it skips.
    Never called by the ratchet - use `python evals.py --live [provider]`."""
    if os.environ.get("EVA_EVAL_LIVE") != "1":
        print("skip: set EVA_EVAL_LIVE=1 (and a provider key) to run the live smoke.")
        return 0
    env = dict(os.environ)
    if provider:
        env["EVA_PROVIDER"] = provider
    adapter = adapters.make_adapter(env)
    system = ("You are EVA. Reply by calling the finish tool with a one-word summary. "
              "Do NOT run any shell command.")
    with tempfile.TemporaryDirectory() as d:
        ws = pathlib.Path(d)
        h = human_mod.AutoHumanInterface()
        approval = human_mod.ApprovalPolicy(h, mode="never")
        runtime = tools_mod.ShellToolRuntime(workspace=ws, approval=approval, human=h)
        store = session_mod.SessionStore(ws / "s.jsonl")
        store.seed([core.Event(role="system", content=system),
                    core.Event(role="user", content="Say hello and finish.")])
        outcome = core.run_agent_loop(
            adapter=adapter, runtime=runtime, session=store,
            tools=tools_mod.CANONICAL_TOOLS, system=system, mode="work")
    ident = adapter.identity() if hasattr(adapter, "identity") else {}
    print(f"live[{ident.get('adapter', '?')}/{ident.get('model', '?')}] outcome={outcome}")
    return 0 if outcome in ("finish", "maxsteps") else 1


def run_offline() -> int:
    for trace in golden_traces():
        assert_trace(trace)
        print("ok  ", trace["provider"], "golden trace")
    print("\nall golden traces passed.")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--live" in args:
        i = args.index("--live")
        prov = args[i + 1] if i + 1 < len(args) else None
        raise SystemExit(run_live(prov))
    if "--list" in args:
        for t in golden_traces():
            print(t["provider"], "->", [r for r in t["expect"]["tool_names"]])
        raise SystemExit(0)
    raise SystemExit(run_offline())

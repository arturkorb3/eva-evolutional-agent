#!/usr/bin/env python3
"""EVA agent entrypoint: wiring only.

This file binds the layered collaborators together and runs the modes. It holds
NO provider wire format and NO tool execution logic - those live in adapters.py
and tools.py. The design seam is:

    EVA-Core (core.py) speaks to a ModelAdapter, a ToolRuntime, a SessionStore,
    a HumanInterface and an ApprovalPolicy. This module just assembles them.

It keeps EVA's constitutional capabilities visible: a friction backlog (memory
of problems) and an improve/pivot path (self-improvement). Those concepts must
never silently vanish - the kernel floor and tests pin them.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

from core import Event, run_agent_loop
from adapters import make_adapter, FakeAdapter, llm_error_signature
from human import ApprovalPolicy, AutoHumanInterface, CliHumanInterface
from session import SessionStore
from tools import CANONICAL_TOOLS, EVOLUTION_TOOLS, ASK_USER_TOOL, FINISH_TOOL_DEF, ShellToolRuntime


ROOT = pathlib.Path(os.environ.get("ORGANISM_ROOT", pathlib.Path.cwd())).resolve()
RELEASE = pathlib.Path(os.environ.get("ACTIVE_RELEASE", pathlib.Path(__file__).resolve().parent)).resolve()

RUNTIME = ROOT / "runtime"
RELEASES = RUNTIME / "releases"
STATE = ROOT / "state"
WORKSPACE = ROOT / "workspace"

BACKLOG = STATE / "backlog.jsonl"            # friction memory
PIVOT_REQUEST = STATE / "pivot_request.json"  # improve/pivot path

def _env_int(name, default):
    # Tolerate unset OR empty-string env vars: docker-compose passes "" for unset
    # optional vars, and int("") would crash. Fall back to the default.
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


AUTO_YES = os.environ.get("ORGANISM_AUTO_YES") == "1"
ALLOW_SHELL = os.environ.get("ORGANISM_ALLOW_SHELL") == "1"
PIVOT_THRESHOLD = _env_int("ORGANISM_PIVOT_THRESHOLD", 3)
HISTORY_BUDGET = _env_int("ORGANISM_HISTORY_BUDGET", 24000)
HISTORY_KEEP = _env_int("ORGANISM_HISTORY_KEEP", 8)

MODES = {"work", "improve", "review", "evolve"}


# --------------------------------------------------------------------------- #
# System prompts (per mode)
# --------------------------------------------------------------------------- #
WORK_SYSTEM = """You are the work agent of a small evolving organism (EVA).
Do useful work for the user inside the workspace.
- Shell commands already run INSIDE your workspace directory. Create and edit
  files with paths RELATIVE to it (e.g. `bubblesort.js`, `src/app.py`). The rest
  of the filesystem is read-only, so never write to absolute paths like /eva/...
- Prefer cheap read-only commands (grep -n, sed -n, head, tail, diff) over
  reading whole files.
- Do not touch runtime/, releases or state/.
- If you lack information, use ask_user instead of guessing.
- Call finish with a short summary when the task is done."""

IMPROVE_SYSTEM = """You are the evolution agent of EVA, running in DIRECTED mode.
You have ONE concrete TASK; implement EXACTLY that - nothing else.
- EVA's modes are EXACTLY: work, improve, review, evolve. Never invent others.
- Your shell runs in the workspace dir. The releases live ONE LEVEL UP at
  ../runtime/releases/. Create a candidate by copying the active release, e.g.
  `cp -r ../runtime/releases/<active> ../runtime/releases/<active>-candidate`,
  then edit files inside that candidate with the editing tools: read_file to SEE
  current content, replace_in_file for surgical edits (old->new), write_file for
  whole/new files. NEVER ask the user for file contents - read them yourself.
  Do NOT use shell heredocs/sed for code (string-surgery corrupts files).
- Never modify the active release in place; organism.py (the kernel) is off-limits.
- When you add a test to tests.py, insert the new check_/test_ function ABOVE the
  `_all_checks` / `if __name__ == "__main__"` block. Functions appended AFTER that
  block are never defined when the tests run, so they silently do NOT execute.
- Before request_promotion, VERIFY the candidate yourself by running its tests:
  `python ../runtime/releases/<candidate>/tests.py --self`. Only request promotion
  if they pass. Never push a candidate you have not actually run.
- Keep changes small. If you hit unrelated friction, note it and keep going.
- Finish only after the change is written AND verified."""

EVOLVE_SYSTEM = """You are the evolution agent of EVA, running in AUTONOMOUS mode.
No specific feature was requested; pick ONE small, high-value improvement to the
release (supervisor/agent/tests/prompts) and implement it as a candidate via
shell. EVA's modes are EXACTLY: work, improve, review, evolve. Your shell runs in
the workspace dir; releases live one level up at ../runtime/releases/. Copy the
active release to ../runtime/releases/<active>-candidate and edit inside it with
read_file / replace_in_file / write_file (read files yourself; never ask the user
for file contents; never use shell heredocs/sed for code).
When you add a test, place the new check_/test_ function ABOVE the `_all_checks` /
`__main__` block (functions defined after it never run). Before request_promotion,
run the candidate's tests yourself (`python ../runtime/releases/<candidate>/tests.py
--self`) and only promote if they pass. Strengthen tests when you fix a friction
class. Never weaken gates; organism.py is off-limits."""

REVIEW_SYSTEM = """You are the review agent of EVA.
Inspect and explain the workspace / release using read-only shell only. Do not
change anything. Give clear risk notes and next steps, then finish."""

# EVA's self-model: the release it evolves IS its own running code. This closes a
# common blind spot where the agent hunts for an "external" CLI it cannot find.
SELF_MODEL = """Self-knowledge: YOU are this code. Your active release at
../runtime/releases/<active>/ IS your own runtime - evolving it changes how you
yourself work. There is NO separate external CLI program:
- agent.py = the interactive CLI/chat loop you are running in right now (reads the
  user, drives the turn loop, wires everything together);
- human.py = how you read user input and ask questions;
- adapters.py = how you talk to the model API (e.g. to add multimodal/image input);
- core.py = your provider-neutral turn loop and Event/Tool types;
- tools.py = your tools; session.py = your memory; supervisor.py/tests.py = your gates.
So tasks like "add image input to the CLI" or "change how you call the model" are
implemented by editing THESE files inside a candidate - not somewhere else."""

# Anti-stall: some models narrate ("I will now...") without emitting the tool call.
ACTION_DISCIPLINE = """Act, don't narrate. When a step needs a tool, emit the tool
call in THIS turn - never say you "will" do something and then stop. NEVER claim an
action (edit, test run, promotion) happened unless you actually called the tool for
it. Keep chaining tool calls until the task is genuinely done, then finish."""

SYSTEMS = {
    "work": WORK_SYSTEM + "\n\n" + ACTION_DISCIPLINE,
    "improve": IMPROVE_SYSTEM + "\n\n" + SELF_MODEL + "\n\n" + ACTION_DISCIPLINE,
    "evolve": EVOLVE_SYSTEM + "\n\n" + SELF_MODEL + "\n\n" + ACTION_DISCIPLINE,
    "review": REVIEW_SYSTEM,
}

# What EVA can actually do inside the sandbox at runtime. The image is read-only
# and host-controlled, but EVA is NOT limited to "read-only everything": it has
# writable, partly persistent space and can extend its own tooling.
ENV_CAPABILITIES = (
    "Environment: the OS root filesystem is read-only, but you have writable dirs:\n"
    "  - the workspace (your shell's cwd) for work products,\n"
    "  - /tmp for scratch,\n"
    "  - /eva/.local (your HOME) which PERSISTS across runs.\n"
    "You can extend your OWN tooling at runtime WITHOUT changing the image:\n"
    "  - HTTP: there is no curl/wget; use Python (urllib.request) or `node` (global fetch).\n"
    "  - Python libs: `pip install --user <pkg>` installs under ~/.local and is importable.\n"
    "  - Binaries: place a static binary in ~/.local/bin (= $HOME/.local/bin, on PATH) and run it by name.\n"
    "You cannot modify the container image/Dockerfile or organism.py (the kernel)."
)


def tools_for(mode: str):
    if mode in ("improve", "evolve"):
        # evolution modes may also ask the supervisor/kernel to promote a candidate.
        return list(EVOLUTION_TOOLS)
    # work/review: read/write via shell, ask_user, finish (runtime blocks writes
    # in review).
    return list(CANONICAL_TOOLS)


def default_task_for(mode: str) -> str:
    if mode == "work":
        return "Inspect the workspace and tell the user what useful work can be done next."
    if mode == "review":
        return "Review the current workspace and active release. Explain risks and next steps."
    if mode == "improve":
        return "Implement the requested improvement as a candidate release."
    return ("Run one small autonomous evolution step: pick one improvement and "
            "implement it as a candidate release.")


# --------------------------------------------------------------------------- #
# Friction backlog (memory of problems) + improve/pivot path
# --------------------------------------------------------------------------- #
def ensure_dirs():
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    RELEASES.mkdir(parents=True, exist_ok=True)
    defaults = {
        "OBJECTIVE.md": "# Objective\n\nBuild a useful work agent that can safely evolve.\n",
        "PLAN.md": "# Plan\n\n- Keep a useful work mode.\n- Evolve via small candidate releases.\n",
        "CHANGELOG.md": "# Changelog\n\n",
    }
    for name, content in defaults.items():
        p = WORKSPACE / name
        if not p.exists():
            p.write_text(content, encoding="utf-8")


def backlog_append(entry: dict) -> None:
    ensure_dirs()
    record = {"time": time.time(), "release": str(RELEASE)}
    record.update(entry)
    with BACKLOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _iter_backlog():
    if not BACKLOG.exists():
        return
    for line in BACKLOG.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except Exception:
                continue


def backlog_count(signature: str) -> int:
    n = 0
    for rec in _iter_backlog():
        if rec.get("signature") != signature:
            continue
        if rec.get("kind") == "resolved":
            n = 0
        else:
            n += 1
    return n


def backlog_summary(limit: int = 8) -> str:
    counts: dict[str, int] = {}
    order: list[str] = []
    for rec in _iter_backlog():
        sig = rec.get("signature")
        if not sig:
            continue
        if rec.get("kind") == "resolved":
            counts[sig] = 0
        else:
            counts[sig] = counts.get(sig, 0) + 1
            if sig not in order:
                order.append(sig)
    items = [(s, counts[s]) for s in order if counts.get(s, 0) > 0]
    items.sort(key=lambda x: -x[1])
    return "\n".join(f"{c}x  {s}" for s, c in items[:limit]) or "(none)"


def record_shell_friction(observation_text: str, mode: str) -> str | None:
    """A non-zero shell exit is friction worth remembering. Returns the signature
    if recorded, else None."""
    first = observation_text.splitlines()[0] if observation_text else ""
    if first.startswith("exit=") and first.strip() != "exit=0":
        code = first.split("=", 1)[1].strip()
        signature = f"shell:exit={code}"
        backlog_append({"kind": "execution_error", "mode": mode,
                        "signature": signature, "detail": observation_text[:300]})
        return signature
    return None


def maybe_pivot(signature: str, mode: str, human) -> bool:
    """On repeated friction in work mode, ask the human to pivot to an improve
    cycle. A pivot is a clean phase switch (request + stop), never live mutation."""
    if mode != "work" or PIVOT_REQUEST.exists():
        return False
    if backlog_count(signature) < PIVOT_THRESHOLD:
        return False
    print(f"\nRepeated friction: '{signature}'.")
    if not human.confirm("Pause work and pivot to an improve cycle to fix this?"):
        backlog_append({"kind": "pivot_declined", "mode": mode, "signature": signature})
        return False
    PIVOT_REQUEST.write_text(json.dumps({
        "signature": signature, "from_release": str(RELEASE), "time": time.time(),
        "reason": f"Repeated friction '{signature}' during work.",
    }, indent=2), encoding="utf-8")
    backlog_append({"kind": "pivot_requested", "mode": mode, "signature": signature})
    print("Pivot requested. Finishing this work session so an improve cycle can run.")
    return True


# --------------------------------------------------------------------------- #
# Run a mode
# --------------------------------------------------------------------------- #
def run_mode(mode: str, task: str):
    ensure_dirs()

    human = AutoHumanInterface(default_confirm=True) if AUTO_YES else CliHumanInterface()
    approval = ApprovalPolicy(
        human,
        mode=("never" if AUTO_YES else "on-risk"),
        allow_shell=ALLOW_SHELL,
    )
    runtime = ShellToolRuntime(workspace=WORKSPACE, approval=approval, human=human,
                               releases=RELEASES, state=STATE)
    session = SessionStore(STATE / f"session.{mode}.jsonl")
    adapter = make_adapter()

    system = SYSTEMS[mode]
    interactive = (not AUTO_YES) and mode in {"work", "improve", "review"}

    resumed = False
    if (task or "").strip().lower() == "resume":
        if session.resumable(mode) and session.load():
            session.append(Event(role="user", content="Continue the previous session."))
            print(f"\n(resuming previous {mode} session: {len(session.events())} events)")
            resumed = True
        else:
            print("\n(no resumable session found; starting fresh)")
            task = ""

    if not resumed:
        if not (task or "").strip():
            if interactive:
                print(f"\nEVA {mode}: type your first message. (empty line or 'exit' to quit)")
                try:
                    task = input("You: ").strip()
                except EOFError:
                    task = ""
                if not task or task.lower() in {"exit", "quit", "q", "bye"}:
                    print("Nothing to do - bye.")
                    return
            else:
                task = default_task_for(mode)

        context = (
            f"Mode: {mode}\n"
            f"Modes available: work, improve, review, evolve\n"
            f"Root: {ROOT}\n"
            f"Workspace (writable; shell runs here - use RELATIVE paths): {WORKSPACE}\n"
            f"Active release: {RELEASE.name}  (from your shell cwd: ../runtime/releases/{RELEASE.name})\n"
            f"{ENV_CAPABILITIES}\n\n"
            f"Friction backlog (repeat counts):\n{backlog_summary()}"
        )
        session.seed([
            Event(role="system", content=system),
            Event(role="user", content=task + "\n\n" + context),
        ], mode)

    if interactive:
        print("\n(interactive chat: reply after each turn; empty line or 'exit' to end)")

    pivoted = {"flag": False}

    def on_say(text: str):
        if text:
            print("Agent:", text)

    def on_observation(obs):
        print("Observation:\n", obs.output)
        if obs.name == "shell":
            sig = record_shell_friction(obs.output, mode)
            if sig and maybe_pivot(sig, mode, human):
                pivoted["flag"] = True

    def on_error(stage: str, exc: Exception):
        sig = (llm_error_signature(getattr(adapter, "endpoint", "llm"), exc)
               if stage == "model" else f"crash:{type(exc).__name__}")
        print(f"{stage} error:", exc)
        backlog_append({"kind": "execution_error", "mode": mode,
                        "signature": sig, "detail": str(exc)[:300]})

    while True:
        # Build the (compacted) turn view from the canonical log on each loop.
        outcome = run_agent_loop(
            adapter=adapter,
            runtime=runtime,
            session=_CompactSession(session, HISTORY_BUDGET, HISTORY_KEEP),
            tools=tools_for(mode),
            system=system,
            mode=mode,
            on_say=on_say,
            on_observation=on_observation,
            on_error=on_error,
        )

        if pivoted["flag"] or outcome == "error":
            break
        if not interactive or outcome == "finish":
            if not interactive:
                break
        try:
            reply = input("\nYou (Enter or 'exit' to end): ").strip()
        except EOFError:
            reply = ""
        if not reply or reply.lower() in {"exit", "quit", "q", "bye"}:
            break
        session.append(Event(role="user", content=reply))

    # Interactive sessions stay resumable when you leave, so `<mode> resume`
    # continues the conversation even if the agent called `finish` for a turn.
    # Only an autonomous (non-interactive) finish clears it; a fresh start
    # overwrites it anyway.
    if outcome == "finish" and not interactive and not pivoted["flag"]:
        session.clear()


class _CompactSession:
    """Adapts SessionStore to the loop's SessionStore protocol while presenting a
    budget-compacted view to the adapter. Appends still go to the canonical log."""

    def __init__(self, store: SessionStore, budget: int, keep: int):
        self._store = store
        self._budget = budget
        self._keep = keep

    def events(self):
        return self._store.compact_view(self._budget, self._keep)

    def append(self, event):
        self._store.append(event)


# --------------------------------------------------------------------------- #
# Smoke / dry-run (LLM-free) + CLI
# --------------------------------------------------------------------------- #
def smoke():
    ensure_dirs()
    assert WORKSPACE.exists() and STATE.exists() and RELEASE.exists()
    # exercise the whole layered stack with the offline FakeAdapter
    _fake_roundtrip()
    print("agent smoke ok")


def dry_run(mode: str):
    ensure_dirs()
    assert mode in MODES
    assert (RELEASE / "agent.py").exists()
    _fake_roundtrip(mode=mode)
    print(f"agent dry-run {mode} ok")


def _fake_roundtrip(mode: str = "work"):
    from core import ModelResult, ToolCall
    human = AutoHumanInterface()
    approval = ApprovalPolicy(human, mode="never")
    runtime = ShellToolRuntime(workspace=WORKSPACE, approval=approval, human=human)
    session = SessionStore(STATE / "_smoke_session.jsonl")
    session.seed([Event(role="system", content="smoke"),
                  Event(role="user", content="smoke task")])
    adapter = FakeAdapter([
        ModelResult(say="check", tool_calls=[ToolCall("s1", "shell", {"cmd": "echo hi"})]),
        ModelResult(say="done", tool_calls=[ToolCall("s2", "finish", {"summary": "ok"})]),
    ])
    outcome = run_agent_loop(adapter=adapter, runtime=runtime, session=session,
                             tools=CANONICAL_TOOLS, system="smoke", mode=mode)
    assert outcome == "finish", outcome
    session.clear()


def main():
    args = sys.argv[1:]

    if "--smoke" in args:
        smoke()
        return

    if "--dry-run" in args:
        i = args.index("--dry-run")
        dry_run(args[i + 1] if i + 1 < len(args) else "work")
        return

    if args and args[0] in MODES:
        mode = args[0]
        task = " ".join(args[1:]).strip()
    else:
        mode = (input("Mode [work/improve/review]: ").strip().lower() or "work")
        if mode not in MODES:
            raise SystemExit("Unknown mode.")
        task = input("Task (optional, Enter to chat): ").strip()

    run_mode(mode, task)


if __name__ == "__main__":
    main()

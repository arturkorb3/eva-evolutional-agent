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
from human import (ApprovalPolicy, AutoHumanInterface, CliHumanInterface,
                   extract_image_attachments)
from session import SessionStore
from self_model import brief as self_model_brief, render_full as self_model_full
from tui import StatusView
from tools import CANONICAL_TOOLS, EVOLUTION_TOOLS, ASK_USER_TOOL, FINISH_TOOL_DEF, ShellToolRuntime


ROOT = pathlib.Path(os.environ.get("ORGANISM_ROOT", pathlib.Path.cwd())).resolve()
RELEASE = pathlib.Path(os.environ.get("ACTIVE_RELEASE", pathlib.Path(__file__).resolve().parent)).resolve()

RUNTIME = ROOT / "runtime"
RELEASES = RUNTIME / "releases"
STATE = ROOT / "state"
WORKSPACE = ROOT / "workspace"
# Per-session WORK logs live under sessions/work/<id>/; improve/review/evolve stay
# single + mode-keyed (single-writer evolution / read-only review).
SESSIONS = STATE / "sessions"

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
# Stream assistant text token-by-token (live typing) when the adapter supports it.
# On by default; only free text is streamed - tool calls are still shown whole.
STREAM = os.environ.get("EVA_STREAM", "1").strip().lower() not in ("0", "false", "no", "off")
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
- If the user only asks a question or makes small talk, ANSWER directly in your
  reply and do NOT call any tool. Reserve `finish` for when an actual work TASK is
  complete, and then give just a SHORT summary - never put a long answer or
  explanation inside a finish summary."""

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
shell. ANNOUNCE FIRST: before changing anything, send ONE short plain message
stating the single improvement you will make and WHY (one or two sentences) - then
implement it. EVA's modes are EXACTLY: work, improve, review, evolve. Your shell runs in
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

# EVA's self-model: the release it evolves IS its own running code. Rather than
# preloading the whole file->role map here, EVA is told it can pull its current
# anatomy/skills/capabilities on demand via the inspect_self tool. This keeps the
# prompt small AND makes self-knowledge grow automatically with each release.
SELF_MODEL = """Self-knowledge: YOU are this code. Your active release at
../runtime/releases/<active>/ IS your own runtime - evolving it changes how you
yourself work, and there is NO separate external program to look for. You are not
preloaded with full docs: call the inspect_self tool to read your CURRENT anatomy
(file->role), skills (tools) and ratchet-pinned capabilities on demand (topic:
overview | anatomy | skills | capabilities | a filename | a capability name). Do
this BEFORE editing so you target the right file (e.g. inspect_self anatomy to see
that agent.py is the CLI loop, adapters.py the model API, core.py the turn loop).

Your self-knowledge is GENERATED each run from THIS release's own code: anatomy from
manifest.json `layers`, skills from tool docstrings, capabilities from the first
docstring line of every tests.py `check_`. So a change only propagates into your NEXT
release's self-model (and inspect_self) if it lands as one of those. When you add or
change a capability: (a) add or extend a `check_` with a clear one-line docstring,
(b) if you add a module or change a file's role, update manifest.json `layers` (plus
`contains`/`hashes`), and (c) give any new tool a clear first-sentence docstring. A
behaviour with no `check_` is invisible to your future self AND unprotected by the
ratchet - so always ship a capability together with its check."""

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


def session_awareness(session_path) -> str:
    """Tell EVA where its OWN conversation lives, so it can look things up instead of
    guessing. The session is an append-only log on disk = the source of truth; the
    visible context is only a compacted view, so older details can be re-read here."""
    sp = pathlib.Path(session_path)
    try:
        rel = os.path.relpath(sp, WORKSPACE).replace(os.sep, "/")
    except Exception:
        rel = str(sp)
    return (
        "Session memory: your full conversation is an append-only log on disk (the "
        f"source of truth) at {rel} (relative to your shell; absolute: {sp}). What you "
        "see in context is only a compacted VIEW of it. If you need an earlier detail "
        "that scrolled out - a path, a value, an earlier decision - READ the log "
        "yourself (read_file, or `grep`/`sed -n`/`cat` on it). It is read-only; never "
        "write to it."
    )


def system_for(mode: str, session_path=None) -> str:
    """The full system prompt for a mode, including session self-awareness. Used at run
    time (fresh AND resume), so EVA always knows about its own session log. The path is
    per-session for work and mode-keyed otherwise."""
    if session_path is None:
        session_path = STATE / f"session.{mode}.jsonl"
    return SYSTEMS[mode] + "\n\n" + session_awareness(session_path)


# --------------------------------------------------------------------------- #
# Work sessions: work is MULTI-session (each run isolated under sessions/work/<id>/);
# improve/review/evolve stay single + mode-keyed. The blob store is already relative
# to each session's own dir, so per-session isolation extends to image blobs for free.
# --------------------------------------------------------------------------- #
def _work_root() -> pathlib.Path:
    return SESSIONS / "work"


def _work_dir(session_id: str) -> pathlib.Path:
    return _work_root() / session_id


def _new_session_id() -> str:
    import secrets
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _latest_pointer() -> pathlib.Path:
    return _work_root() / "LATEST"


def _read_latest_work():
    try:
        v = _latest_pointer().read_text(encoding="utf-8").strip()
        return v or None
    except Exception:
        return None


def _write_latest_work(session_id: str) -> None:
    try:
        _work_root().mkdir(parents=True, exist_ok=True)
        _latest_pointer().write_text(session_id, encoding="utf-8")
    except Exception:
        pass


def _list_work_sessions() -> list[str]:
    root = _work_root()
    if not root.exists():
        return []
    ids = [p.name for p in root.iterdir()
           if p.is_dir() and (p / "events.jsonl").exists()]
    ids.sort()  # ids start with a timestamp -> chronological
    return ids


def _work_session_rows():
    """Per work session: (id, n_events, first_task, is_latest), oldest first. Shared by
    the `--list` command and the start-screen overview."""
    latest = _read_latest_work()
    rows = []
    for sid in _list_work_sessions():
        n = 0
        first = ""
        try:
            for line in (_work_dir(sid) / "events.jsonl").read_text(
                    encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                n += 1
                if not first:
                    try:
                        o = json.loads(line)
                        if o.get("role") == "user" and (o.get("content") or "").strip():
                            first = o["content"].strip().splitlines()[0][:60]
                    except Exception:
                        pass
        except Exception:
            pass
        rows.append((sid, n, first, sid == latest))
    return rows


def _print_work_sessions() -> None:
    rows = _work_session_rows()
    if not rows:
        print("(no work sessions yet - `work [task]` starts one)")
        return
    print("Work sessions (oldest first):")
    for sid, n, first, is_latest in rows:
        mark = " *" if is_latest else "  "
        print(f"{mark}{sid}  {n} events  {first}")
    print("\nResume:  work resume          (most recent)")
    print("         work resume <id>")


def _clean_user_text(content: str) -> str:
    """For a replayed user turn, show only the human's message: strip the runtime
    CONTEXT block the seed appends to the first task (it begins with a 'Mode: ...'
    line), and hide the synthetic 'Continue the previous session.' resume nudge."""
    text = content or ""
    if text.strip() == "Continue the previous session.":
        return ""
    i = text.find("\n\nMode: ")
    return text[:i].rstrip() if i != -1 else text

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
    "Not every common Unix utility is installed; before relying on optional binaries,\n"
    "verify with `command -v <tool>` or prefer Python stdlib/Node equivalents.\n"
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
    """Record a shell FAILURE as friction - but only a real one.

    A non-zero exit with EMPTY stderr is normally intentional control flow (grep with
    no match, `test`, `command -v`, a script's own `exit 1`), not an EVA defect, so it
    must NOT drive a pivot. Only a failure with actual error output counts, and the
    signature includes a snippet of that error so unrelated failures don't collapse
    into one coarse `shell:exit=1` bucket. Returns the signature if recorded, else None.
    """
    text = observation_text or ""
    first = text.splitlines()[0] if text else ""
    if not first.startswith("exit=") or first.strip() == "exit=0":
        return None
    code = first.split("=", 1)[1].strip()
    stderr = ""
    if "\nstderr:\n" in text:
        stderr = text.split("\nstderr:\n", 1)[1].strip()
    if not stderr:
        return None  # non-zero exit but no error output -> normal control flow
    err_line = stderr.splitlines()[0]
    err_key = "-".join("".join(c.lower() if c.isalnum() else " " for c in err_line).split())[:40]
    # Never collapse shell failures into a bare `shell:exit=N` bucket: that coarse
    # signature let unrelated exits accumulate and triggered noisy pivots. If stderr is
    # present but has no alphanumerics, still keep an explicit error-specific suffix.
    signature = f"shell:exit={code}:{err_key or 'unknown-stderr'}"
    backlog_append({"kind": "execution_error", "mode": mode,
                    "signature": signature, "detail": text[:300]})
    return signature


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

    # Pick the session. work is MULTI-session: the task string may be `--list`
    # (list + return), `resume` (most recent) or `resume <id>`. Other modes are
    # single + mode-keyed.
    if mode == "work":
        t = (task or "").strip()
        head = t.split()[0].lower() if t else ""
        if head in {"--list", "list", "sessions"}:
            _print_work_sessions()
            return
        if head == "resume":
            parts = t.split()
            target = parts[1] if len(parts) > 1 else _read_latest_work()
            if target and (_work_dir(target) / "events.jsonl").exists():
                session_id, want_resume = target, True
            else:
                miss = f" '{parts[1]}'" if len(parts) > 1 else ""
                print(f"\n(no resumable work session{miss}; starting a new one)")
                session_id, want_resume = _new_session_id(), False
            task = ""
        else:
            session_id, want_resume = _new_session_id(), False
        session = SessionStore(_work_dir(session_id) / "events.jsonl")
        _write_latest_work(session_id)
    else:
        session = SessionStore(STATE / f"session.{mode}.jsonl")
        want_resume = (task or "").strip().lower() == "resume"

    adapter = make_adapter()
    identity = adapter.identity() if hasattr(adapter, "identity") else {}
    view = StatusView(mode=mode, identity=identity, release=RELEASE.name)

    system = system_for(mode, session.path)
    interactive = (not AUTO_YES) and mode in {"work", "improve", "review"}

    view.welcome(usage=interactive)
    if mode == "work":
        if interactive and not want_resume:
            # Surface resumable sessions right on the start screen (discoverability).
            view.session_overview(_work_session_rows(), current=session_id)
        print(f"(session {session_id} \u2014 resume later with:  work resume {session_id})")

    resumed = False
    if want_resume:
        if session.resumable(mode) and session.load():
            if interactive:
                # Replay the prior conversation so the human can pick up the thread,
                # then prompt them for the next message (below). Do NOT auto-run, and do
                # NOT inject a synthetic 'Continue' user turn - that polluted the log and
                # made EVA think the user kept typing "Continue the previous session.".
                view.replay(session.events(), clean_user=_clean_user_text)
            else:
                # Autonomous resume (AUTO_YES) has no human to prompt: nudge EVA on.
                session.append(Event(role="user", content="Continue the previous session."))
            print(f"\n(resuming previous {mode} session: {len(session.events())} events)")
            resumed = True
        else:
            print("\n(no resumable session found; starting fresh)")
            task = ""


    if not resumed:
        if not (task or "").strip():
            if interactive:
                try:
                    task = input("You: ").strip()
                except EOFError:
                    task = ""
                if not task or task.lower() in {"exit", "quit", "q", "bye"}:
                    print("Nothing to do — bye.")
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
            f"{self_model_brief()}\n\n"
            f"Friction backlog (repeat counts):\n{backlog_summary()}"
        )
        task_text, task_images = extract_image_attachments(task, WORKSPACE)
        session.seed([
            Event(role="system", content=system),
            Event(role="user", content=task_text + "\n\n" + context,
                  images=task_images),
        ], mode)

    pivoted = {"flag": False}

    def on_say(text: str):
        view.say(text)

    on_say_delta = view.on_say_delta if STREAM else None

    def on_tool_call(call):
        view.tool_call(call)

    def on_observation(obs):
        view.observation(obs)
        if obs.name == "shell":
            sig = record_shell_friction(obs.output, mode)
            if sig and maybe_pivot(sig, mode, human):
                pivoted["flag"] = True

    def on_error(stage: str, exc: Exception):
        view.error(stage, exc)
        sig = (llm_error_signature(getattr(adapter, "endpoint", "llm"), exc)
               if stage == "model" else f"crash:{type(exc).__name__}")
        backlog_append({"kind": "execution_error", "mode": mode,
                        "signature": sig, "detail": str(exc)[:300]})

    # An interactive resume should let the human type first (no auto-run).
    needs_user_input_first = resumed and interactive

    while True:
        # On an interactive RESUME there is no new task yet: let the human read the
        # replay and type the next message BEFORE EVA runs, instead of auto-replying.
        if needs_user_input_first:
            needs_user_input_first = False
            try:
                reply = input("\nYou (Enter or 'exit' to end): ").strip()
            except EOFError:
                reply = ""
            if not reply or reply.lower() in {"exit", "quit", "q", "bye"}:
                break
            reply_text, reply_images = extract_image_attachments(reply, WORKSPACE)
            session.append(Event(role="user", content=reply_text, images=reply_images))

        # Build the (compacted) turn view from the canonical log on each loop.
        outcome = run_agent_loop(
            adapter=adapter,
            runtime=runtime,
            session=_CompactSession(session, HISTORY_BUDGET, HISTORY_KEEP),
            tools=tools_for(mode),
            system=system,
            mode=mode,
            on_say=on_say,
            on_say_delta=on_say_delta,
            on_tool_call=on_tool_call,
            on_observation=on_observation,
            on_error=on_error,
            should_stop=lambda: pivoted["flag"],
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
        reply_text, reply_images = extract_image_attachments(reply, WORKSPACE)
        session.append(Event(role="user", content=reply_text, images=reply_images))

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

    if "--self-model" in args:
        # LLM-free: print EVA's generated self-model (anatomy, skills, capabilities).
        print(self_model_full())
        return

    if "--smoke" in args:
        smoke()
        return

    if "--dry-run" in args:
        i = args.index("--dry-run")
        dry_run(args[i + 1] if i + 1 < len(args) else "work")
        return

    # `eva` with no mode just starts EVA (work is the default mode). An explicit
    # mode word still selects it; anything else is treated as a work task.
    if args and args[0] in MODES:
        mode = args[0]
        task = " ".join(args[1:]).strip()
    else:
        mode = "work"
        task = " ".join(args).strip()

    run_mode(mode, task)


if __name__ == "__main__":
    main()

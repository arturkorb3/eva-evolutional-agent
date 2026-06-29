#!/usr/bin/env python3
"""Release supervisor: orchestrates modes and gates candidate promotions.

It shells out to agent.py (the wiring entrypoint) and enforces release-level
gates: required files, a "tests only get stronger" ratchet, smoke, dry-runs and
qualification rounds. It is agent-internals-agnostic - it cares about the release
contract, not how the agent talks to a model.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time


ROOT = pathlib.Path(os.environ.get("ORGANISM_ROOT", pathlib.Path(__file__).resolve().parents[3])).resolve()
RELEASE = pathlib.Path(os.environ.get("ACTIVE_RELEASE", pathlib.Path(__file__).resolve().parent)).resolve()

RUNTIME = ROOT / "runtime"
RELEASES = RUNTIME / "releases"
STATE = ROOT / "state"
WORKSPACE = ROOT / "workspace"
PROMOTION = STATE / "promotion_request.json"
PIVOT_REQUEST = STATE / "pivot_request.json"
BACKLOG = STATE / "backlog.jsonl"
SUPERVISOR_LOG = STATE / "supervisor_history.jsonl"

REQUIRED = ["supervisor.py", "agent.py", "tests.py", "manifest.json"]


def log(kind, data):
    STATE.mkdir(exist_ok=True)
    with SUPERVISOR_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\n")


def backlog_append(entry):
    STATE.mkdir(exist_ok=True)
    record = {"time": time.time(), "release": str(RELEASE)}
    record.update(entry)
    with BACKLOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(cmd, extra_env=None, timeout=180):
    env = os.environ.copy()
    env["ORGANISM_ROOT"] = str(ROOT)
    env["ACTIVE_RELEASE"] = str(RELEASE)
    if extra_env:
        env.update(extra_env)
    print("\n$", " ".join(map(str, cmd)))
    r = subprocess.run([str(x) for x in cmd], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=timeout)
    if r.stdout:
        print(r.stdout)
    if r.stderr:
        print(r.stderr)
    return r.returncode == 0


def safe_release_rel(rel):
    rel = str(rel).strip()
    if not rel.startswith("runtime/releases/"):
        raise RuntimeError("Candidate must be below runtime/releases/")
    p = (ROOT / rel).resolve()
    base = RELEASES.resolve()
    if p != base and not str(p).startswith(str(base) + os.sep):
        raise RuntimeError("Candidate escapes releases directory")
    return rel, p


def ensure_workspace():
    WORKSPACE.mkdir(exist_ok=True)
    STATE.mkdir(exist_ok=True)


def release_files_ok(path):
    for name in REQUIRED:
        if not (path / name).exists():
            print("Missing:", name)
            return False
    return True


def count_checks(path):
    try:
        text = (path / "tests.py").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0
    return len(re.findall(r"(?m)^def (?:check|test)_", text))


def gates_not_weakened(candidate_path):
    current, candidate = count_checks(RELEASE), count_checks(candidate_path)
    if candidate < current:
        print(f"Candidate weakens tests: {candidate} < {current}.")
        return False
    return True


def run_agent(mode, task=None):
    ensure_workspace()
    cmd = [sys.executable, str(RELEASE / "agent.py"), mode]
    if task:
        cmd.append(task)
    env = os.environ.copy()
    env["ORGANISM_ROOT"] = str(ROOT)
    env["ACTIVE_RELEASE"] = str(RELEASE)
    print("\n$", " ".join(map(str, cmd)))
    # Inherit stdio: the agent is interactive (approvals, ask_user). Capturing
    # would hide prompts and block input.
    return subprocess.call([str(x) for x in cmd], cwd=ROOT, env=env) == 0


def smoke():
    ensure_workspace()
    assert release_files_ok(RELEASE)
    assert run([sys.executable, RELEASE / "agent.py", "--smoke"], timeout=120)
    assert run([sys.executable, RELEASE / "tests.py", "--self"], timeout=120)
    print("supervisor smoke ok")


def dry_run(mode):
    ensure_workspace()
    if mode not in {"work", "improve", "review", "evolve"}:
        raise SystemExit("Unknown dry-run mode.")
    assert run([sys.executable, RELEASE / "agent.py", "--dry-run", mode], timeout=120)
    print(f"supervisor dry-run {mode} ok")


def qualification_round(round_no):
    ensure_workspace()
    print(f"qualification round {round_no}")
    checks = [
        [sys.executable, RELEASE / "tests.py", "--self"],
        [sys.executable, RELEASE / "agent.py", "--dry-run", "work"],
        [sys.executable, RELEASE / "agent.py", "--dry-run", "improve"],
        [sys.executable, RELEASE / "agent.py", "--dry-run", "review"],
    ]
    for cmd in checks:
        if not run(cmd, timeout=120):
            raise SystemExit(1)
    print(f"qualification round {round_no} ok")


def candidate_gate(candidate_rel, rounds=2):
    rel, path = safe_release_rel(candidate_rel)
    print("\nSupervisor gate for:", rel)
    if not release_files_ok(path):
        return False
    if not gates_not_weakened(path):
        return False
    env = {"ORGANISM_ROOT": str(ROOT), "ACTIVE_RELEASE": str(path)}
    checks = [
        [sys.executable, path / "tests.py", "--self"],
        [sys.executable, path / "supervisor.py", "--smoke"],
        [sys.executable, path / "supervisor.py", "--dry-run", "work"],
        [sys.executable, path / "supervisor.py", "--dry-run", "improve"],
    ]
    for cmd in checks:
        if not run(cmd, extra_env=env, timeout=120):
            return False
    for i in range(1, rounds + 1):
        if not run([sys.executable, path / "supervisor.py", "--qualification-round", str(i)],
                   extra_env=env, timeout=180):
            return False
    return True


def handle_promotion_request():
    if not PROMOTION.exists():
        return False
    req = json.loads(PROMOTION.read_text(encoding="utf-8"))
    candidate = req.get("candidate")
    print("\nPromotion requested:", candidate)
    if not candidate_gate(candidate, rounds=2):
        print("Supervisor rejected candidate.")
        PROMOTION.unlink()
        log("promotion_rejected", {"candidate": candidate})
        return False
    req["supervisor_qualified"] = True
    req["supervisor_qualified_at"] = time.time()
    PROMOTION.write_text(json.dumps(req, indent=2), encoding="utf-8")
    print("Supervisor gate passed. Kernel makes the final decision.")
    log("promotion_qualified", {"candidate": candidate})
    return True


def objective_text():
    p = WORKSPACE / "OBJECTIVE.md"
    return p.read_text(errors="replace")[:4000] if p.exists() else "Improve EVA."


def evolve_one():
    task = ("Run one autonomous evolution round.\n\nObjective:\n" + objective_text()
            + "\n\nPick one small improvement and implement it as a candidate release. "
              "Request promotion only if the candidate should become the next live release.")
    if not run_agent("evolve", task):
        return False
    handle_promotion_request()
    return True


def pivot_task(signature, reason):
    return ("A repeated friction was detected during work and the user approved a "
            "self-improvement pivot.\n\n"
            f"Friction signature: {signature}\nContext: {reason}\n\n"
            "Create a candidate release that removes the root cause. Add or strengthen "
            "a check in tests.py so this class is caught in future. Never weaken gates. "
            "Request promotion only if the candidate is coherent.")


def handle_pivot_then_promotion():
    pivot_signature = None
    if PIVOT_REQUEST.exists():
        try:
            req = json.loads(PIVOT_REQUEST.read_text(encoding="utf-8"))
        except Exception:
            req = {}
        PIVOT_REQUEST.unlink()
        pivot_signature = req.get("signature", "")
        print("\n=== Pivot: improve cycle for", pivot_signature, "===")
        log("pivot_started", {"signature": pivot_signature})
        run_agent("improve", pivot_task(pivot_signature, req.get("reason", "")))
        log("pivot_finished", {"signature": pivot_signature})
    qualified = handle_promotion_request()
    if pivot_signature and qualified:
        backlog_append({"kind": "resolved", "mode": "improve",
                        "signature": pivot_signature, "detail": "candidate qualified"})


def main():
    args = sys.argv[1:]

    if "--smoke" in args:
        smoke()
        return
    if "--dry-run" in args:
        i = args.index("--dry-run")
        dry_run(args[i + 1] if i + 1 < len(args) else "work")
        return
    if "--qualification-round" in args:
        i = args.index("--qualification-round")
        qualification_round(args[i + 1] if i + 1 < len(args) else "1")
        return

    if not args:
        run_agent("work")
        handle_pivot_then_promotion()
        return

    cmd = args[0]
    rest = " ".join(args[1:]).strip()

    if cmd == "work":
        run_agent("work", rest or None)
        handle_pivot_then_promotion()
        return
    if cmd == "improve":
        run_agent("improve", rest or None)
        handle_promotion_request()
        return
    if cmd == "review":
        run_agent("review", rest or None)
        return
    if cmd == "evolve-one":
        evolve_one()
        return

    raise SystemExit("Usage: supervisor.py [work|improve|review|evolve-one] | "
                     "--smoke | --dry-run MODE | --qualification-round N")


if __name__ == "__main__":
    main()

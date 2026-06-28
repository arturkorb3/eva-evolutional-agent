#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
RELEASES = RUNTIME / "releases"
CURRENT = RUNTIME / "CURRENT"
LAST_GOOD = RUNTIME / "LAST_GOOD"
STATE = ROOT / "state"
WORKSPACE = ROOT / "workspace"
PROMOTION = STATE / "promotion_request.json"
KERNEL_LOG = STATE / "kernel_history.jsonl"

# This file is the tiny non-evolving kernel. It seeds v001, starts the active
# release, performs final promotion checks, and can roll back.
# The evolving organism lives in runtime/releases/<version>/.

V001_SUPERVISOR = '#!/usr/bin/env python3\nimport json\nimport os\nimport pathlib\nimport re\nimport subprocess\nimport sys\nimport time\n\n\nROOT = pathlib.Path(os.environ.get("ORGANISM_ROOT", pathlib.Path(__file__).resolve().parents[3])).resolve()\nRELEASE = pathlib.Path(os.environ.get("ACTIVE_RELEASE", pathlib.Path(__file__).resolve().parent)).resolve()\n\nRUNTIME = ROOT / "runtime"\nRELEASES = RUNTIME / "releases"\nSTATE = ROOT / "state"\nWORKSPACE = ROOT / "workspace"\nPROMOTION = STATE / "promotion_request.json"\nSUPERVISOR_LOG = STATE / "supervisor_history.jsonl"\nBACKLOG = STATE / "backlog.jsonl"\nPIVOT_REQUEST = STATE / "pivot_request.json"\n\n\ndef log(kind, data):\n    STATE.mkdir(exist_ok=True)\n    with SUPERVISOR_LOG.open("a", encoding="utf-8") as f:\n        f.write(json.dumps({"time": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\\n")\n\n\ndef backlog_append(entry):\n    STATE.mkdir(exist_ok=True)\n    record = {"time": time.time(), "release": str(RELEASE)}\n    record.update(entry)\n    with BACKLOG.open("a", encoding="utf-8") as f:\n        f.write(json.dumps(record, ensure_ascii=False) + "\\n")\n\n\ndef run(cmd, cwd=ROOT, extra_env=None, timeout=120):\n    env = os.environ.copy()\n    env["ORGANISM_ROOT"] = str(ROOT)\n    env["ACTIVE_RELEASE"] = str(RELEASE)\n\n    if extra_env:\n        env.update(extra_env)\n\n    print("\\n$", " ".join(map(str, cmd)))\n    r = subprocess.run(\n        [str(x) for x in cmd],\n        cwd=cwd,\n        env=env,\n        capture_output=True,\n        text=True,\n        timeout=timeout,\n    )\n\n    if r.stdout:\n        print(r.stdout)\n    if r.stderr:\n        print(r.stderr)\n\n    return r.returncode == 0\n\n\ndef safe_release_rel(rel):\n    rel = str(rel).strip()\n    if not rel.startswith("runtime/releases/"):\n        raise RuntimeError("Promotion candidate must be below runtime/releases/")\n    p = (ROOT / rel).resolve()\n    base = RELEASES.resolve()\n    if p != base and not str(p).startswith(str(base) + os.sep):\n        raise RuntimeError("Candidate escapes releases directory")\n    return rel, p\n\n\ndef ensure_workspace():\n    WORKSPACE.mkdir(exist_ok=True)\n    STATE.mkdir(exist_ok=True)\n\n\ndef release_files_ok(path):\n    required = ["supervisor.py", "agent.py", "tests.py", "manifest.json"]\n    for name in required:\n        if not (path / name).exists():\n            print("Missing:", name)\n            return False\n    return True\n\n\ndef count_checks(path):\n    # Number of check_/test_ functions in a release\'s tests.py. Used as a crude\n    # but effective "gates only get stricter" ratchet.\n    try:\n        text = (path / "tests.py").read_text(encoding="utf-8", errors="replace")\n    except Exception:\n        return 0\n    return len(re.findall(r"(?m)^def (?:check|test)_", text))\n\n\ndef gates_not_weakened(candidate_path):\n    current = count_checks(RELEASE)\n    candidate = count_checks(candidate_path)\n    if candidate < current:\n        print(f"Candidate weakens tests: {candidate} checks < {current} in current release.")\n        return False\n    return True\n\n\ndef run_agent(mode, task=None):\n    ensure_workspace()\n    agent = RELEASE / "agent.py"\n\n    cmd = [sys.executable, str(agent), mode]\n    if task:\n        cmd.append(task)\n\n    # The agent is interactive (approval prompts, ask_user, key/feedback input) and\n    # may run for a while. Inherit stdio so its output streams live and the user can\n    # answer prompts. Do NOT capture here - capturing hides prompts and blocks input.\n    env = os.environ.copy()\n    env["ORGANISM_ROOT"] = str(ROOT)\n    env["ACTIVE_RELEASE"] = str(RELEASE)\n\n    print("\\n$", " ".join(map(str, cmd)))\n    return subprocess.call([str(x) for x in cmd], cwd=ROOT, env=env) == 0\n\n\ndef run_tests_self():\n    return run([sys.executable, RELEASE / "tests.py", "--self"], timeout=120)\n\n\ndef smoke():\n    ensure_workspace()\n    assert release_files_ok(RELEASE)\n    assert run([sys.executable, RELEASE / "agent.py", "--smoke"], timeout=120)\n    assert run([sys.executable, RELEASE / "tests.py", "--self"], timeout=120)\n    print("supervisor smoke ok")\n\n\ndef dry_run(mode):\n    ensure_workspace()\n    if mode not in {"work", "improve", "review", "evolve"}:\n        raise SystemExit("Unknown dry-run mode.")\n    assert run([sys.executable, RELEASE / "agent.py", "--dry-run", mode], timeout=120)\n    print(f"supervisor dry-run {mode} ok")\n\n\ndef qualification_round(round_no):\n    ensure_workspace()\n    print(f"qualification round {round_no}")\n\n    checks = [\n        [sys.executable, RELEASE / "tests.py", "--self"],\n        [sys.executable, RELEASE / "agent.py", "--dry-run", "work"],\n        [sys.executable, RELEASE / "agent.py", "--dry-run", "improve"],\n        [sys.executable, RELEASE / "agent.py", "--dry-run", "review"],\n    ]\n\n    for cmd in checks:\n        if not run(cmd, timeout=120):\n            raise SystemExit(1)\n\n    print(f"qualification round {round_no} ok")\n\n\ndef candidate_gate(candidate_rel, rounds=2):\n    rel, path = safe_release_rel(candidate_rel)\n\n    print("\\nSupervisor gate for:", rel)\n\n    if not release_files_ok(path):\n        return False\n\n    if not gates_not_weakened(path):\n        return False\n\n    env = {\n        "ORGANISM_ROOT": str(ROOT),\n        "ACTIVE_RELEASE": str(path),\n    }\n\n    checks = [\n        [sys.executable, path / "tests.py", "--self"],\n        [sys.executable, path / "supervisor.py", "--smoke"],\n        [sys.executable, path / "supervisor.py", "--dry-run", "work"],\n        [sys.executable, path / "supervisor.py", "--dry-run", "improve"],\n    ]\n\n    for cmd in checks:\n        if not run(cmd, extra_env=env, timeout=120):\n            return False\n\n    for i in range(1, rounds + 1):\n        if not run(\n            [sys.executable, path / "supervisor.py", "--qualification-round", str(i)],\n            extra_env=env,\n            timeout=180,\n        ):\n            return False\n\n    return True\n\n\ndef handle_promotion_request():\n    if not PROMOTION.exists():\n        return False\n\n    req = json.loads(PROMOTION.read_text(encoding="utf-8"))\n    candidate = req.get("candidate")\n    reason = req.get("reason", "")\n\n    print("\\nPromotion requested:")\n    print("Candidate:", candidate)\n    print("Reason:", reason)\n\n    ok = candidate_gate(candidate, rounds=2)\n\n    if not ok:\n        print("Supervisor rejected candidate.")\n        PROMOTION.unlink()\n        log("promotion_rejected", {"candidate": candidate, "reason": "supervisor gate failed"})\n        return False\n\n    req["supervisor_qualified"] = True\n    req["supervisor_qualified_at"] = time.time()\n    PROMOTION.write_text(json.dumps(req, indent=2), encoding="utf-8")\n\n    print("Supervisor gate passed. Kernel will make final decision.")\n    log("promotion_qualified", {"candidate": candidate, "reason": reason})\n    return True\n\n\ndef objective_text():\n    p = WORKSPACE / "OBJECTIVE.md"\n    if p.exists():\n        return p.read_text(errors="replace")[:4000]\n    return "Improve the organism while preserving a useful work mode."\n\n\ndef evolve_one():\n    task = (\n        "Run one autonomous evolution round.\\n\\n"\n        "Objective:\\n"\n        + objective_text()\n        + "\\n\\n"\n        "Pick exactly one small improvement to the release. "\n        "Improve supervisor, agent, tests, prompts, or gates. "\n        "Create a candidate release if useful. "\n        "Request promotion only if the candidate should become the next live release."\n    )\n\n    ok = run_agent("improve", task)\n    if not ok:\n        return False\n\n    handle_promotion_request()\n    return True\n\n\ndef pivot_task(signature, reason):\n    return (\n        "A repeated friction was detected during work and the user approved a "\n        "self-improvement pivot.\\n\\n"\n        f"Friction signature: {signature}\\n"\n        f"Context: {reason}\\n\\n"\n        "Do ONE focused thing: create a candidate release that removes the root "\n        "cause of this friction class. You MUST also add or strengthen a check in "\n        "tests.py so this friction class is caught in future (the ratchet). Never "\n        "weaken or remove existing tests or gates. Request promotion only if the "\n        "candidate is coherent and passes smoke + qualification."\n    )\n\n\ndef handle_pivot_then_promotion():\n    # If the work agent asked to pivot (variant a: the user already approved it),\n    # run one improve cycle aimed at the friction. Mark the friction resolved ONLY\n    # if that cycle produced a candidate which passed the supervisor gate -\n    # otherwise the friction stays open and recurs until a real fix lands.\n    pivot_signature = None\n    if PIVOT_REQUEST.exists():\n        try:\n            req = json.loads(PIVOT_REQUEST.read_text(encoding="utf-8"))\n        except Exception:\n            req = {}\n        PIVOT_REQUEST.unlink()\n        pivot_signature = req.get("signature", "")\n        reason = req.get("reason", "")\n\n        print("\\n=== Pivot: improve cycle to fix repeated friction ===")\n        print("Signature:", pivot_signature)\n        log("pivot_started", {"signature": pivot_signature, "reason": reason})\n\n        run_agent("improve", pivot_task(pivot_signature, reason))\n        log("pivot_finished", {"signature": pivot_signature})\n\n    qualified = handle_promotion_request()\n\n    if pivot_signature and qualified:\n        backlog_append({"kind": "resolved", "mode": "improve",\n                        "signature": pivot_signature,\n                        "detail": "candidate qualified for promotion"})\n\n\ndef main():\n    args = sys.argv[1:]\n\n    if "--smoke" in args:\n        smoke()\n        return\n\n    if "--dry-run" in args:\n        i = args.index("--dry-run")\n        mode = args[i + 1] if i + 1 < len(args) else "work"\n        dry_run(mode)\n        return\n\n    if "--qualification-round" in args:\n        i = args.index("--qualification-round")\n        round_no = args[i + 1] if i + 1 < len(args) else "1"\n        qualification_round(round_no)\n        return\n\n    if not args:\n        run_agent("work")\n        handle_pivot_then_promotion()\n        return\n\n    cmd = args[0]\n    rest = " ".join(args[1:]).strip()\n\n    if cmd == "work":\n        run_agent("work", rest or None)\n        handle_pivot_then_promotion()\n        return\n\n    if cmd == "improve":\n        run_agent("improve", rest or None)\n        handle_promotion_request()\n        return\n\n    if cmd == "review":\n        run_agent("review", rest or None)\n        handle_promotion_request()\n        return\n\n    if cmd == "evolve-one":\n        evolve_one()\n        return\n\n    if cmd == "gate":\n        if not rest:\n            raise SystemExit("Usage: supervisor.py gate runtime/releases/vXXX-candidate")\n        ok = candidate_gate(rest, rounds=2)\n        raise SystemExit(0 if ok else 1)\n\n    raise SystemExit("Unknown supervisor command: " + cmd)\n\n\nif __name__ == "__main__":\n    main()\n'
V001_AGENT = '#!/usr/bin/env python3\nimport getpass\nimport json\nimport os\nimport pathlib\nimport re\nimport shutil\nimport subprocess\nimport sys\nimport time\nimport urllib.error\nimport urllib.parse\nimport urllib.request\n\n\nROOT = pathlib.Path(os.environ.get("ORGANISM_ROOT", pathlib.Path.cwd())).resolve()\nRELEASE = pathlib.Path(os.environ.get("ACTIVE_RELEASE", pathlib.Path(__file__).resolve().parent)).resolve()\n\nRUNTIME = ROOT / "runtime"\nRELEASES = RUNTIME / "releases"\nSTATE = ROOT / "state"\nWORKSPACE = ROOT / "workspace"\nPROMOTION = STATE / "promotion_request.json"\nLOG = STATE / "agent_session.jsonl"\nBACKLOG = STATE / "backlog.jsonl"\nPIVOT_REQUEST = STATE / "pivot_request.json"\n\nAUTO_YES = os.environ.get("ORGANISM_AUTO_YES") == "1"\nALLOW_SHELL = os.environ.get("ORGANISM_ALLOW_SHELL") == "1"\nPIVOT_THRESHOLD = int(os.environ.get("ORGANISM_PIVOT_THRESHOLD", "3"))\n\n# Kinds of backlog entries that count as "friction" toward the pivot threshold.\nCOUNTING_KINDS = {\n    "execution_error",\n    "tool_error",\n    "protocol_error",\n    "capability_gap",\n    "agent_note",\n    "human_feedback",\n}\n\n# Signatures the user declined to pivot on, for this process only (avoid nagging).\n_declined_pivots = set()\n\n\nPROTOCOL = """\nReturn exactly one JSON object per step:\n\n{\n  "say": "short explanation",\n  "action": {"type": "list_dir", "path": "."}\n}\n\nActions:\n\nWorkspace:\n- list_dir: {"type":"list_dir","path":"."}\n- read_file: {"type":"read_file","path":"README.md","max_chars":12000}\n- write_file: {"type":"write_file","path":"README.md","content":"..."}\n\nRelease inspection:\n- list_release: {"type":"list_release"}\n- read_release_file: {"type":"read_release_file","path":"agent.py","max_chars":30000}\n\nCandidate evolution:\n- make_candidate: {"type":"make_candidate","candidate":"v002-candidate"}\n- list_candidate: {"type":"list_candidate","candidate":"v002-candidate"}\n- read_candidate_file: {"type":"read_candidate_file","candidate":"v002-candidate","path":"agent.py","max_chars":30000}\n- write_candidate_file: {"type":"write_candidate_file","candidate":"v002-candidate","path":"agent.py","content":"..."}\n\nExecution:\n- shell: {"type":"shell","cmd":"python -m pytest","timeout":60}\n\nPromotion:\n- request_promotion: {"type":"request_promotion","candidate":"v002-candidate","reason":"..."}\n\nInteraction:\n- ask_user: {"type":"ask_user","question":"..."}\n  Ask the human a single question when you genuinely lack information needed to\n  proceed. Prefer asking over guessing. The answer comes back as the observation.\n\nSelf-observation:\n- note_problem: {"type":"note_problem","signature":"short-stable-id","detail":"what is missing or broke"}\n  Use this when a tool is missing, an action repeatedly fails, or you notice a\n  capability gap ("something is missing here"). The system counts repeats and may\n  pause to fix itself.\n\nFinish:\n- finish: {"type":"finish","summary":"..."}\n"""\n\n\nWORK_SYSTEM = """\nYou are the work agent of a small evolving organism.\n\nMission:\nDo useful work for the user inside workspace/.\n\nRules:\n- Work mode is the default purpose of the system.\n- Do not modify runtime/, releases, supervisor.py, agent.py, tests.py, or state/.\n- Do not create candidates.\n- Do not request promotion.\n- Keep changes small and explain them.\n- Use shell only when useful.\n- If you lack information you need from the user, use ask_user instead of guessing.\n- If a tool is missing or an action keeps failing, call note_problem. Do not\n  silently work around capability gaps - record them so the organism can evolve.\n""" + PROTOCOL\n\n\nIMPROVE_SYSTEM = """\nYou are the evolution agent of a small self-hosting organism.\n\nMission:\nImprove the whole versioned release, not just yourself. A release contains:\n- supervisor.py\n- agent.py\n- tests.py\n- manifest.json\n\nArchitecture:\n- organism.py is the tiny external kernel and should not be modified.\n- The active release must never overwrite itself.\n- Create a new candidate under runtime/releases/<version>-candidate/.\n- Modify files only inside that candidate.\n- A candidate must pass smoke and qualification rounds before promotion.\n- Preserve work mode as the default useful mode.\n\nEvolution loop:\n1. Inspect workspace/OBJECTIVE.md, workspace/PLAN.md, workspace/CHANGELOG.md, and the current release.\n2. Choose one small improvement.\n3. Create or update a candidate release.\n4. Update candidate files.\n5. Ensure candidate supports:\n   - python supervisor.py --smoke\n   - python supervisor.py --dry-run work\n   - python supervisor.py --dry-run improve\n   - python supervisor.py --qualification-round 1\n6. Request promotion only if the candidate is plausibly safer or more useful.\n\nFriction-driven priority:\n- A "Friction backlog" is provided in your context with repeat counts. Prefer\n  fixing the highest-count friction first - that is where real usage hurts.\n- Ratchet rule: whenever you fix a friction class, you MUST also add or\n  strengthen a check in tests.py so the same class is caught in future.\n- Never remove or weaken existing tests, gates, or safety checks. Gates may only\n  become stricter, never looser.\n\nKeep changes small. Prefer improving tests and gates before adding power.\n""" + PROTOCOL\n\n\nREVIEW_SYSTEM = """\nYou are the review agent of a small evolving organism.\n\nMission:\nInspect and explain the current workspace, release, or candidate without changing anything.\n\nRules:\n- Do not write files.\n- Do not run shell unless explicitly necessary.\n- Do not request promotion.\n- Give clear risk notes and next steps.\n""" + PROTOCOL\n\n\ndef ensure_dirs():\n    WORKSPACE.mkdir(exist_ok=True)\n    STATE.mkdir(exist_ok=True)\n    RELEASES.mkdir(parents=True, exist_ok=True)\n\n    defaults = {\n        "OBJECTIVE.md": "# Objective\\n\\nBuild a useful work agent that can safely evolve its supervisor, agent, tests, and prompts.\\n",\n        "PLAN.md": "# Plan\\n\\n- Preserve a useful work mode.\\n- Evolve by small candidate releases.\\n- Strengthen tests and qualification gates.\\n",\n        "CHANGELOG.md": "# Changelog\\n\\n",\n    }\n\n    for name, content in defaults.items():\n        p = WORKSPACE / name\n        if not p.exists():\n            p.write_text(content, encoding="utf-8")\n\n\ndef log(kind, data):\n    ensure_dirs()\n    with LOG.open("a", encoding="utf-8") as f:\n        f.write(json.dumps({"time": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\\n")\n\n\ndef backlog_append(entry):\n    ensure_dirs()\n    try:\n        release = str(RELEASE.relative_to(ROOT))\n    except Exception:\n        release = str(RELEASE)\n    record = {"time": time.time(), "release": release}\n    record.update(entry)\n    with BACKLOG.open("a", encoding="utf-8") as f:\n        f.write(json.dumps(record, ensure_ascii=False) + "\\n")\n\n\ndef _iter_backlog():\n    if not BACKLOG.exists():\n        return\n    for line in BACKLOG.read_text(encoding="utf-8", errors="replace").splitlines():\n        line = line.strip()\n        if not line:\n            continue\n        try:\n            yield json.loads(line)\n        except Exception:\n            continue\n\n\ndef backlog_count(signature):\n    # Counts friction entries for a signature since the last time it was resolved,\n    # so a fixed problem stops triggering pivots.\n    n = 0\n    for rec in _iter_backlog():\n        if rec.get("signature") != signature:\n            continue\n        if rec.get("kind") == "resolved":\n            n = 0\n            continue\n        if rec.get("kind") in COUNTING_KINDS:\n            n += 1\n    return n\n\n\ndef backlog_summary(limit=8):\n    counts = {}\n    order = []\n    for rec in _iter_backlog():\n        sig = rec.get("signature")\n        kind = rec.get("kind")\n        if not sig:\n            continue\n        if kind == "resolved":\n            counts[sig] = 0\n            continue\n        if kind in COUNTING_KINDS:\n            counts[sig] = counts.get(sig, 0) + 1\n            if sig not in order:\n                order.append(sig)\n    items = [(s, counts[s]) for s in order if counts.get(s, 0) > 0]\n    items.sort(key=lambda x: -x[1])\n    if not items:\n        return "(none)"\n    return "\\n".join(f"{c}x  {s}" for s, c in items[:limit])\n\n\ndef classify_friction(action, result):\n    # Maps an (action, observation) pair to (kind, signature, detail) when it\n    # represents real friction, else None. By-design denials and user rejections\n    # are NOT friction - they are not capability gaps.\n    typ = (action or {}).get("type", "") or "unknown"\n    text = result if isinstance(result, str) else str(result)\n    low = text.lower()\n\n    if text.strip() == "Unknown action.":\n        return ("capability_gap", f"missing_action:{typ}", "model requested an unknown action type")\n\n    # Content/benign actions return arbitrary data, not a status - never scan them\n    # for failure markers, or file/answer content would trigger false positives.\n    if typ in ("ask_user", "read_file", "read_release_file", "read_candidate_file",\n               "list_dir", "list_release", "list_candidate", "note_problem", "finish"):\n        return None\n\n    if typ == "shell":\n        first = text.splitlines()[0] if text else ""\n        if first.startswith("exit=") and first.strip() != "exit=0":\n            parts = str(action.get("cmd", "")).strip().split()\n            head = parts[0] if parts else "shell"\n            code = first.split("=", 1)[1].strip()\n            return ("execution_error", f"shell:{head}:exit={code}", text[:500])\n        return None\n\n    if low.startswith("denied:") or "rejected" in low or "declined" in low:\n        return None\n\n    for marker in ("traceback", "error", " missing", "escapes", "no such", "not found"):\n        if marker in low:\n            return ("tool_error", f"action_failed:{typ}:{marker.strip()}", text[:300])\n\n    return None\n\n\ndef maybe_pivot(signature, mode):\n    # Variant (a): on repeated friction, ASK the user before pivoting to improve.\n    # Only work mode hands off; improve already evolves. A pivot is a clean phase\n    # switch, never live self-modification - we record the request and stop.\n    if mode != "work":\n        return False\n    if signature in _declined_pivots:\n        return False\n    if PIVOT_REQUEST.exists():\n        return False\n\n    count = backlog_count(signature)\n    if count < PIVOT_THRESHOLD:\n        return False\n\n    print(f"\\nRepeated friction: \'{signature}\' seen {count}x.")\n    if not approve("Pause work and pivot to an improve cycle to fix this?"):\n        _declined_pivots.add(signature)\n        backlog_append({"kind": "pivot_declined", "mode": mode, "signature": signature, "detail": ""})\n        return False\n\n    PIVOT_REQUEST.write_text(json.dumps({\n        "signature": signature,\n        "reason": f"Repeated friction \'{signature}\' ({count}x) during work.",\n        "from_release": str(RELEASE),\n        "time": time.time(),\n    }, indent=2), encoding="utf-8")\n    backlog_append({"kind": "pivot_requested", "mode": mode, "signature": signature, "detail": ""})\n    print("Pivot requested. Finishing this work session so the improve cycle can run.")\n    return True\n\n\ndef record_friction(action, result, mode):\n    # Append any friction to the backlog and, if a threshold is reached, ask to\n    # pivot. Returns True if a pivot was requested (caller should stop the loop).\n    f = classify_friction(action, result)\n    if not f:\n        return False\n    kind, signature, detail = f\n    backlog_append({"kind": kind, "mode": mode, "signature": signature, "detail": detail})\n    return maybe_pivot(signature, mode)\n\n\ndef approve(prompt):\n    if AUTO_YES:\n        print(prompt + " [auto-yes]")\n        return True\n    return input(prompt + " [y/N] ").strip().lower() == "y"\n\n\ndef safe_under(base, path):\n    base = pathlib.Path(base).resolve()\n    p = (base / path).resolve()\n    if p != base and not str(p).startswith(str(base) + os.sep):\n        raise RuntimeError("Path escapes allowed directory")\n    return p\n\n\ndef workspace_path(path):\n    return safe_under(WORKSPACE, path)\n\n\ndef release_path(path):\n    return safe_under(RELEASE, path)\n\n\ndef normalize_candidate(candidate):\n    name = str(candidate).strip()\n    if name.startswith("runtime/releases/"):\n        name = pathlib.Path(name).name\n    if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]*-candidate", name):\n        raise RuntimeError("Candidate must look like v002-candidate")\n    return name\n\n\ndef candidate_dir(candidate):\n    name = normalize_candidate(candidate)\n    return safe_under(RELEASES, name)\n\n\ndef candidate_path(candidate, path):\n    return safe_under(candidate_dir(candidate), path)\n\n\ndef render_tree(base, limit=200):\n    base = pathlib.Path(base)\n    if not base.exists():\n        return "(not found)"\n    if base.is_file():\n        return base.name\n\n    out = []\n    for x in sorted(base.rglob("*"))[:limit]:\n        if "__pycache__" in x.parts:\n            continue\n        out.append(str(x.relative_to(base)) + ("/" if x.is_dir() else ""))\n    return "\\n".join(out) or "(empty)"\n\n\ndef llm_flavor(endpoint):\n    # Provider-neutral: OpenAI Responses API when the endpoint ends in /responses,\n    # otherwise the widely-compatible Chat Completions shape (OpenAI, Azure,\n    # Ollama, LM Studio, vLLM, OpenRouter, ...). Freely evolvable - add more here.\n    return "responses" if endpoint.rstrip("/").endswith("/responses") else "chat"\n\n\ndef build_llm_payload(flavor, model, messages):\n    if flavor == "responses":\n        instructions = ""\n        items = []\n        for m in messages:\n            if m.get("role") == "system" and not instructions:\n                instructions = m.get("content", "")\n            else:\n                items.append({"role": m.get("role", "user"), "content": m.get("content", "")})\n        payload = {"model": model, "input": items}\n        if instructions:\n            payload["instructions"] = instructions\n        # No temperature: the newest OpenAI models (o-series, gpt-5) reject it.\n        return payload\n    return {"model": model, "messages": messages, "temperature": 0.2}\n\n\ndef extract_llm_text(flavor, data):\n    if flavor == "chat":\n        return data["choices"][0]["message"]["content"]\n    # Responses API: prefer the convenience field, else walk output content parts.\n    if isinstance(data.get("output_text"), str) and data["output_text"]:\n        return data["output_text"]\n    parts = []\n    for item in data.get("output", []):\n        for c in item.get("content", []) or []:\n            if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):\n                parts.append(c["text"])\n    if parts:\n        return "\\n".join(parts)\n    raise RuntimeError("Could not parse Responses output: " + json.dumps(data)[:300])\n\n\ndef call_llm(endpoint, key, model, messages):\n    flavor = llm_flavor(endpoint)\n    body = json.dumps(build_llm_payload(flavor, model, messages)).encode("utf-8")\n\n    req = urllib.request.Request(\n        endpoint,\n        data=body,\n        headers={\n            "Authorization": f"Bearer {key}",\n            "Content-Type": "application/json",\n        },\n        method="POST",\n    )\n\n    with urllib.request.urlopen(req, timeout=120) as r:\n        data = json.loads(r.read().decode("utf-8"))\n\n    return extract_llm_text(flavor, data)\n\n\ndef llm_error_signature(endpoint, exc):\n    try:\n        host = urllib.parse.urlsplit(endpoint).netloc or "llm"\n    except Exception:\n        host = "llm"\n    code = f":{exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""\n    return f"llm_error:{host}:{type(exc).__name__}{code}"\n\n\ndef describe_llm_error(exc):\n    if isinstance(exc, urllib.error.HTTPError):\n        try:\n            detail = exc.read().decode("utf-8", "replace")[:400]\n        except Exception:\n            detail = ""\n        return f"HTTP {exc.code}: {detail}"\n    return f"{type(exc).__name__}: {exc}"\n\n\ndef parse_json(s):\n    s = s.strip()\n    try:\n        return json.loads(s)\n    except Exception:\n        start = s.find("{")\n        end = s.rfind("}") + 1\n        if start < 0 or end <= start:\n            raise\n        return json.loads(s[start:end])\n\n\ndef mode_allows_evolution(mode):\n    return mode in {"improve", "evolve"}\n\n\ndef run_action(action, mode):\n    typ = action.get("type")\n\n    if typ == "list_dir":\n        return render_tree(workspace_path(action.get("path", ".")))\n\n    if typ == "read_file":\n        p = workspace_path(action["path"])\n        return p.read_text(errors="replace")[:int(action.get("max_chars", 12000))]\n\n    if typ == "write_file":\n        if mode == "review":\n            return "Denied: review mode is read-only."\n        p = workspace_path(action["path"])\n        print("\\nWRITE workspace:", p.relative_to(WORKSPACE))\n        if not approve("Approve workspace write?"):\n            return "Workspace write rejected."\n        p.parent.mkdir(parents=True, exist_ok=True)\n        p.write_text(action.get("content", ""), encoding="utf-8")\n        return "Wrote workspace/" + str(p.relative_to(WORKSPACE))\n\n    if typ == "list_release":\n        return render_tree(RELEASE)\n\n    if typ == "read_release_file":\n        p = release_path(action["path"])\n        return p.read_text(errors="replace")[:int(action.get("max_chars", 30000))]\n\n    if typ == "make_candidate":\n        if not mode_allows_evolution(mode):\n            return "Denied: candidate creation only in improve/evolve mode."\n        name = normalize_candidate(action["candidate"])\n        dest = candidate_dir(name)\n        print("\\nMAKE CANDIDATE:", name)\n        if dest.exists():\n            return "Candidate already exists."\n        if not approve("Approve candidate creation?"):\n            return "Candidate creation rejected."\n        shutil.copytree(RELEASE, dest)\n        return "Created runtime/releases/" + name\n\n    if typ == "list_candidate":\n        if not mode_allows_evolution(mode):\n            return "Denied: candidates only in improve/evolve mode."\n        return render_tree(candidate_dir(action["candidate"]))\n\n    if typ == "read_candidate_file":\n        if not mode_allows_evolution(mode):\n            return "Denied: candidates only in improve/evolve mode."\n        p = candidate_path(action["candidate"], action["path"])\n        return p.read_text(errors="replace")[:int(action.get("max_chars", 30000))]\n\n    if typ == "write_candidate_file":\n        if not mode_allows_evolution(mode):\n            return "Denied: candidate writes only in improve/evolve mode."\n        p = candidate_path(action["candidate"], action["path"])\n        print("\\nWRITE candidate:", p.relative_to(ROOT))\n        if not approve("Approve candidate write?"):\n            return "Candidate write rejected."\n        p.parent.mkdir(parents=True, exist_ok=True)\n        p.write_text(action.get("content", ""), encoding="utf-8")\n        if p.name.endswith(".py"):\n            p.chmod(0o755)\n        return "Wrote " + str(p.relative_to(ROOT))\n\n    if typ == "shell":\n        if mode == "review":\n            return "Denied: review mode does not run shell."\n        print("\\nSHELL in workspace:", action["cmd"])\n        if not ALLOW_SHELL and not approve("Approve shell?"):\n            return "Shell rejected."\n        r = subprocess.run(\n            action["cmd"],\n            cwd=WORKSPACE,\n            shell=True,\n            capture_output=True,\n            text=True,\n            timeout=int(action.get("timeout", 60)),\n        )\n        return f"exit={r.returncode}\\nstdout:\\n{r.stdout[-6000:]}\\nstderr:\\n{r.stderr[-6000:]}"\n\n    if typ == "request_promotion":\n        if not mode_allows_evolution(mode):\n            return "Denied: promotion only in improve/evolve mode."\n        name = normalize_candidate(action["candidate"])\n        PROMOTION.write_text(json.dumps({\n            "candidate": "runtime/releases/" + name,\n            "reason": action.get("reason", ""),\n            "requested_by": "agent",\n            "time": time.time(),\n        }, indent=2), encoding="utf-8")\n        return "Promotion requested. Finish now."\n\n    if typ == "ask_user":\n        question = str(action.get("question", "")).strip() or "(no question)"\n        if AUTO_YES:\n            return "No interactive user (auto mode). Proceed with your best assumption."\n        print("\\nAGENT ASKS:", question)\n        try:\n            answer = input("Your answer: ").strip()\n        except EOFError:\n            answer = ""\n        return "User answered: " + (answer or "(no answer)")\n\n    if typ == "note_problem":\n        sig = str(action.get("signature") or action.get("about") or "note").strip()[:80]\n        backlog_append({\n            "kind": "agent_note",\n            "mode": mode,\n            "signature": f"note:{sig}",\n            "detail": str(action.get("detail", ""))[:500],\n        })\n        return "Noted problem: " + sig\n\n    if typ == "finish":\n        return action.get("summary", "Finished.")\n\n    return "Unknown action."\n\n\ndef default_task_for(mode):\n    if mode == "work":\n        return "Inspect the workspace and tell the user what useful work can be done next."\n    if mode == "review":\n        return "Review the current workspace and active release. Explain risks and next steps."\n    return (\n        "Run one small autonomous evolution step. Inspect objective, plan, changelog, "\n        "and current release. Improve supervisor, agent, tests, or prompts by creating "\n        "a candidate release. Request promotion only if the candidate is coherent."\n    )\n\n\ndef system_for(mode):\n    if mode == "work":\n        return WORK_SYSTEM\n    if mode == "review":\n        return REVIEW_SYSTEM\n    return IMPROVE_SYSTEM\n\n\ndef run_loop(mode, task):\n    ensure_dirs()\n\n    endpoint = (\n        os.environ.get("LLM_ENDPOINT")\n        or input("Endpoint [https://api.openai.com/v1/responses]: ").strip()\n        or "https://api.openai.com/v1/responses"\n    )\n    model = os.environ.get("LLM_MODEL") or input("Model: ").strip()\n    if not model:\n        raise SystemExit("No model given.")\n\n    key = (\n        os.environ.get("LLM_API_KEY")\n        or os.environ.get("OPENAI_API_KEY")\n        or getpass.getpass("API key: ")\n    )\n\n    context = (\n        f"Mode: {mode}\\n"\n        f"Root: {ROOT}\\n"\n        f"Active release: {RELEASE.relative_to(ROOT)}\\n\\n"\n        f"Workspace tree:\\n{render_tree(WORKSPACE)}\\n\\n"\n        f"Release tree:\\n{render_tree(RELEASE)}\\n\\n"\n        f"Friction backlog (repeat counts):\\n{backlog_summary()}"\n    )\n\n    messages = [\n        {"role": "system", "content": system_for(mode)},\n        {"role": "user", "content": task + "\\n\\n" + context},\n    ]\n\n    for step in range(50):\n        print(f"\\n--- agent step {step + 1} ---")\n        try:\n            raw = call_llm(endpoint, key, model, messages)\n        except Exception as e:\n            signature = llm_error_signature(endpoint, e)\n            detail = describe_llm_error(e)\n            print("LLM call failed:", detail)\n            backlog_append({"kind": "execution_error", "mode": mode,\n                            "signature": signature, "detail": detail[:300]})\n            if maybe_pivot(signature, mode):\n                break\n            time.sleep(2)\n            try:\n                raw = call_llm(endpoint, key, model, messages)\n            except Exception as e2:\n                print("LLM retry failed; ending session.")\n                backlog_append({"kind": "execution_error", "mode": mode,\n                                "signature": signature,\n                                "detail": ("retry: " + describe_llm_error(e2))[:300]})\n                break\n        log("raw", raw)\n\n        try:\n            obj = parse_json(raw)\n        except Exception as e:\n            msg = f"Invalid JSON: {e}. Return exactly one JSON object."\n            print(msg)\n            backlog_append({"kind": "protocol_error", "mode": mode,\n                            "signature": "protocol:invalid_json", "detail": str(e)[:300]})\n            if maybe_pivot("protocol:invalid_json", mode):\n                break\n            messages.append({"role": "user", "content": msg})\n            continue\n\n        print("Agent:", obj.get("say", ""))\n        action = obj.get("action", {})\n\n        crash_signature = None\n        try:\n            result = run_action(action, mode)\n        except Exception as e:\n            typ = (action or {}).get("type", "unknown") or "unknown"\n            crash_signature = f"crash:{typ}:{type(e).__name__}"\n            result = f"Action \'{typ}\' crashed: {type(e).__name__}: {e}"\n        print("Observation:\\n", result)\n\n        log("action", obj)\n        log("observation", result)\n\n        if action.get("type") == "finish":\n            break\n\n        if crash_signature is not None:\n            backlog_append({"kind": "execution_error", "mode": mode,\n                            "signature": crash_signature, "detail": result[:300]})\n            pivoted = maybe_pivot(crash_signature, mode)\n        else:\n            pivoted = record_friction(action, result, mode)\n\n        messages.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)})\n        messages.append({"role": "user", "content": "OBSERVATION:\\n" + result})\n\n        if pivoted:\n            break\n\n        if len(messages) > 18:\n            messages = [messages[0]] + messages[-17:]\n\n    if mode == "work" and not AUTO_YES:\n        try:\n            ans = input("\\nDid this work session help? [y/n/Enter to skip]: ").strip().lower()\n        except EOFError:\n            ans = ""\n        if ans in ("y", "n"):\n            backlog_append({\n                "kind": "human_feedback",\n                "mode": mode,\n                "signature": "human:" + ("positive" if ans == "y" else "negative"),\n                "detail": "",\n            })\n\n\ndef smoke():\n    ensure_dirs()\n    assert WORKSPACE.exists()\n    assert STATE.exists()\n    assert RELEASE.exists()\n    print("agent smoke ok")\n\n\ndef dry_run(mode):\n    ensure_dirs()\n    assert mode in {"work", "improve", "review", "evolve"}\n    assert (RELEASE / "agent.py").exists()\n    print(f"agent dry-run {mode} ok")\n\n\ndef main():\n    args = sys.argv[1:]\n\n    if "--smoke" in args:\n        smoke()\n        return\n\n    if "--dry-run" in args:\n        i = args.index("--dry-run")\n        mode = args[i + 1] if i + 1 < len(args) else "work"\n        dry_run(mode)\n        return\n\n    if args and args[0] in {"work", "improve", "review", "evolve"}:\n        mode = args[0]\n        task = " ".join(args[1:]).strip() or default_task_for(mode)\n    else:\n        mode = input("Mode [work/improve/review]: ").strip().lower() or "work"\n        if mode not in {"work", "improve", "review"}:\n            raise SystemExit("Unknown mode.")\n        task = input("Task: ").strip() or default_task_for(mode)\n\n    run_loop(mode, task)\n\n\nif __name__ == "__main__":\n    main()\n'
V001_TESTS = '#!/usr/bin/env python3\nimport json\nimport os\nimport pathlib\nimport py_compile\nimport sys\n\n\nROOT = pathlib.Path(os.environ.get("ORGANISM_ROOT", pathlib.Path(__file__).resolve().parents[3])).resolve()\nRELEASE = pathlib.Path(os.environ.get("ACTIVE_RELEASE", pathlib.Path(__file__).resolve().parent)).resolve()\n\n\ndef check_manifest():\n    p = RELEASE / "manifest.json"\n    data = json.loads(p.read_text(encoding="utf-8"))\n    assert "name" in data\n    assert "version" in data\n    assert "contains" in data\n    return data\n\n\ndef check_files():\n    required = ["supervisor.py", "agent.py", "tests.py", "manifest.json"]\n    for name in required:\n        p = RELEASE / name\n        assert p.exists(), f"missing {name}"\n        if name.endswith(".py"):\n            py_compile.compile(str(p), doraise=True)\n\n\ndef check_no_obvious_kernel_shadowing():\n    # The release may evolve supervisor/agent/tests, but should not contain\n    # another top-level organism.py that pretends to replace the kernel.\n    assert not (RELEASE / "organism.py").exists(), "release must not shadow kernel"\n\n\ndef check_friction_backlog_capability():\n    # Floor: the friction-driven improve loop must not be silently removed by a\n    # future evolution. Both the backlog and the improve-pivot must persist.\n    agent = (RELEASE / "agent.py").read_text(encoding="utf-8", errors="replace").lower()\n    assert "backlog" in agent, "agent must keep a friction backlog"\n    assert "pivot" in agent, "agent must keep the improve-pivot capability"\n    assert "ask_user" in agent, "agent must keep minimal user-dialog capability"\n    supervisor = (RELEASE / "supervisor.py").read_text(encoding="utf-8", errors="replace").lower()\n    assert "pivot_request" in supervisor, "supervisor must handle pivot requests"\n    assert "gates_not_weakened" in supervisor, "supervisor must keep the ratchet gate"\n\n\ndef self_test():\n    check_files()\n    check_manifest()\n    check_no_obvious_kernel_shadowing()\n    check_friction_backlog_capability()\n    print("tests self ok")\n\n\ndef main():\n    if "--self" in sys.argv or "--smoke" in sys.argv:\n        self_test()\n        return\n    self_test()\n\n\nif __name__ == "__main__":\n    main()\n'
V001_MANIFEST = {'name': 'minimal-evolving-organism', 'version': 'v001', 'contains': ['supervisor.py', 'agent.py', 'tests.py', 'manifest.json'], 'principle': 'Only organism.py is the tiny non-evolving kernel. Releases evolve as complete supervisor-agent-test bundles.'}


def log(kind, data):
    STATE.mkdir(exist_ok=True)
    with KERNEL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\n")


def ensure_seed():
    RELEASES.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(exist_ok=True)
    WORKSPACE.mkdir(exist_ok=True)

    v1 = RELEASES / "v001"
    if not v1.exists():
        v1.mkdir(parents=True)
        (v1 / "supervisor.py").write_text(V001_SUPERVISOR, encoding="utf-8")
        (v1 / "agent.py").write_text(V001_AGENT, encoding="utf-8")
        (v1 / "tests.py").write_text(V001_TESTS, encoding="utf-8")
        (v1 / "manifest.json").write_text(json.dumps(V001_MANIFEST, indent=2), encoding="utf-8")

        for name in ["supervisor.py", "agent.py", "tests.py"]:
            (v1 / name).chmod(0o755)

    if not CURRENT.exists():
        CURRENT.write_text("runtime/releases/v001", encoding="utf-8")

    objective = WORKSPACE / "OBJECTIVE.md"
    if not objective.exists():
        objective.write_text(
            "# Objective\n\n"
            "Build a useful work agent that can safely evolve its supervisor, agent, tests, and prompts.\n",
            encoding="utf-8",
        )


def safe_release_rel(rel):
    rel = str(rel).strip()

    if not rel.startswith("runtime/releases/"):
        raise RuntimeError("Release path must start with runtime/releases/")

    p = (ROOT / rel).resolve()
    base = RELEASES.resolve()

    if p != base and not str(p).startswith(str(base) + os.sep):
        raise RuntimeError("Release path escapes runtime/releases")

    return rel, p


def current_release():
    ensure_seed()
    rel = CURRENT.read_text(encoding="utf-8").strip()
    return safe_release_rel(rel)


def run(cmd, release_path=None, timeout=180):
    env = os.environ.copy()
    env["ORGANISM_ROOT"] = str(ROOT)

    if release_path is not None:
        env["ACTIVE_RELEASE"] = str(release_path)

    print("\n$", " ".join(map(str, cmd)))

    r = subprocess.run(
        [str(x) for x in cmd],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if r.stdout:
        print(r.stdout)
    if r.stderr:
        print(r.stderr)

    return r.returncode == 0


def kernel_capability_floor(path):
    # Constitutional identity: the few capabilities that define EVA as a
    # self-improving organism and may never SILENTLY vanish. Enforced by the
    # immutable kernel so an evolved release cannot quietly erode them.
    #
    # Deliberately LOOSE and concept-based (synonym sets), so the organism stays
    # free to rename, refactor and redesign. Only TOTAL removal of a concept is
    # blocked - not any particular implementation or name.
    try:
        agent = (path / "agent.py").read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        print("Kernel floor: cannot read candidate agent.py")
        return False

    required_concepts = {
        "a memory of friction/problems": ("backlog", "journal", "friction"),
        "a path to self-improvement": ("pivot", "improve"),
    }

    for label, alternatives in required_concepts.items():
        if not any(a in agent for a in alternatives):
            print("Kernel floor: candidate lost capability:", label)
            return False

    return True


def kernel_gate(candidate_rel):
    rel, path = safe_release_rel(candidate_rel)

    print("\nKernel gate for:", rel)

    required = ["supervisor.py", "agent.py", "tests.py", "manifest.json"]
    for name in required:
        if not (path / name).exists():
            print("Missing:", name)
            return False

    if not kernel_capability_floor(path):
        return False

    # Kernel-level floor. The active/evolved supervisor may have stronger gates,
    # but these checks cannot be skipped.
    checks = [
        [sys.executable, path / "tests.py", "--self"],
        [sys.executable, path / "supervisor.py", "--smoke"],
        [sys.executable, path / "supervisor.py", "--qualification-round", "kernel-1"],
        [sys.executable, path / "supervisor.py", "--qualification-round", "kernel-2"],
    ]

    for cmd in checks:
        if not run(cmd, release_path=path, timeout=240):
            return False

    return True


def approve(prompt, auto_yes=False):
    if auto_yes:
        print(prompt + " [auto-yes]")
        return True
    return input(prompt + " [y/N] ").strip().lower() == "y"


def maybe_promote(auto_yes=False):
    if not PROMOTION.exists():
        return False

    req = json.loads(PROMOTION.read_text(encoding="utf-8"))
    candidate = req.get("candidate")
    reason = req.get("reason", "")

    print("\nKernel saw promotion request:")
    print("Candidate:", candidate)
    print("Reason:", reason)

    if not req.get("supervisor_qualified"):
        print("Warning: request has no supervisor_qualified flag. Kernel will still run its own gate.")

    if not kernel_gate(candidate):
        print("Kernel rejected candidate.")
        PROMOTION.unlink()
        log("promotion_rejected", {"candidate": candidate, "reason": "kernel gate failed"})
        return False

    if not approve("Promote candidate after kernel gate?", auto_yes=auto_yes):
        print("Promotion declined.")
        PROMOTION.unlink()
        log("promotion_declined", {"candidate": candidate})
        return False

    old_rel = CURRENT.read_text(encoding="utf-8").strip()
    LAST_GOOD.write_text(old_rel, encoding="utf-8")
    CURRENT.write_text(candidate, encoding="utf-8")
    PROMOTION.unlink()

    log("promoted", {"from": old_rel, "to": candidate, "reason": reason})
    print("Promoted:", candidate)
    return True


def run_current_supervisor(args, auto_yes=False, allow_shell=False):
    rel, path = current_release()
    supervisor = path / "supervisor.py"

    env = os.environ.copy()
    env["ORGANISM_ROOT"] = str(ROOT)
    env["ACTIVE_RELEASE"] = str(path)

    if auto_yes:
        env["ORGANISM_AUTO_YES"] = "1"
    if allow_shell:
        env["ORGANISM_ALLOW_SHELL"] = "1"

    print("\nActive release:", rel)

    r = subprocess.call(
        [sys.executable, str(supervisor)] + list(args),
        cwd=ROOT,
        env=env,
    )

    return r


def parse_flag_value(args, flag, default):
    if flag not in args:
        return default
    i = args.index(flag)
    if i + 1 >= len(args):
        raise SystemExit(f"{flag} needs a value")
    return args[i + 1]


def status():
    ensure_seed()
    current = CURRENT.read_text(encoding="utf-8").strip()
    last = LAST_GOOD.read_text(encoding="utf-8").strip() if LAST_GOOD.exists() else "(none)"

    print("Current:", current)
    print("Last good:", last)
    print("\nReleases:")

    for p in sorted(RELEASES.iterdir()):
        if p.is_dir():
            marker = " *" if ("runtime/releases/" + p.name) == current else ""
            print("-", p.name + marker)


def rollback():
    ensure_seed()

    if not LAST_GOOD.exists():
        print("No LAST_GOOD release recorded.")
        return

    old = CURRENT.read_text(encoding="utf-8").strip()
    target = LAST_GOOD.read_text(encoding="utf-8").strip()
    safe_release_rel(target)

    CURRENT.write_text(target, encoding="utf-8")
    log("rollback", {"from": old, "to": target})
    print("Rolled back to:", target)


def main():
    ensure_seed()

    args = sys.argv[1:]

    auto_yes = "--yes" in args
    allow_shell = "--allow-shell" in args
    args = [a for a in args if a not in {"--yes", "--allow-shell"}]

    if not args:
        run_current_supervisor([], auto_yes=auto_yes, allow_shell=allow_shell)
        maybe_promote(auto_yes=auto_yes)
        return

    cmd = args[0]

    if cmd == "status":
        status()
        return

    if cmd == "rollback":
        rollback()
        return

    if cmd == "evolve":
        rounds = int(parse_flag_value(args, "--rounds", "1"))

        # Keep only arguments relevant to the supervisor out of the kernel parse.
        for i in range(1, rounds + 1):
            print(f"\n=== evolution round {i}/{rounds} ===")
            run_current_supervisor(["evolve-one"], auto_yes=auto_yes, allow_shell=allow_shell)
            maybe_promote(auto_yes=auto_yes)

        return

    # Forward normal modes to the active supervisor.
    if cmd in {"work", "improve", "review"}:
        run_current_supervisor(args, auto_yes=auto_yes, allow_shell=allow_shell)
        maybe_promote(auto_yes=auto_yes)
        return

    print("Usage:")
    print("  python organism.py work [task]")
    print("  python organism.py improve [task]")
    print("  python organism.py review [task]")
    print("  python organism.py evolve --rounds N [--yes] [--allow-shell]")
    print("  python organism.py status")
    print("  python organism.py rollback")
    raise SystemExit(2)


if __name__ == "__main__":
    main()

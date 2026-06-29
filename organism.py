#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import re
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
#
# The initial genome is NOT embedded as string constants. It lives as normal,
# reviewable files under seed/v001/ and is copied into the runtime on first
# start, after an integrity check against the hashes pinned in its manifest.
SEED = ROOT / "seed" / "v001"


def log(kind, data):
    STATE.mkdir(exist_ok=True)
    with KERNEL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\n")


def _normalized_sha256(path):
    # Normalize CRLF -> LF so the integrity check is stable across platforms and
    # git autocrlf checkouts (the genome is plain text).
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify_seed(src):
    # Reproducible, tamper-evident bootstrap: the seed manifest pins a sha256 of
    # every genome file. Refuse to materialize a corrupt or altered embryo.
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    hashes = manifest.get("hashes") or {}
    if not hashes:
        raise RuntimeError("Seed manifest has no hashes; cannot verify integrity.")

    for name, expected in hashes.items():
        f = src / name
        if not f.exists():
            raise RuntimeError(f"Seed missing file: {name}")
        if _normalized_sha256(f) != expected:
            raise RuntimeError(f"Seed integrity check failed for {name}")

    return manifest


def ensure_seed():
    RELEASES.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(exist_ok=True)
    WORKSPACE.mkdir(exist_ok=True)

    v1 = RELEASES / "v001"
    if not v1.exists():
        if not SEED.exists():
            raise RuntimeError(f"Seed genome not found at {SEED}")
        verify_seed(SEED)
        shutil.copytree(SEED, v1)
        log("seeded", {"from": str(SEED), "to": "runtime/releases/v001"})
        for p in v1.glob("*.py"):
            p.chmod(0o755)

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


def next_version_name():
    # The kernel assigns clean, sequential version names (v001, v002, ...). A
    # promoted candidate is renamed to the next free number so the live release
    # never carries a "-candidate" suffix.
    nums = []
    for p in RELEASES.iterdir():
        if p.is_dir():
            m = re.fullmatch(r"v(\d+)", p.name)
            if m:
                nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    name = f"v{n:03d}"
    while (RELEASES / name).exists():
        n += 1
        name = f"v{n:03d}"
    return name


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

    # Rename a promoted "*-candidate" to the next clean version so the live release
    # never keeps a candidate name. The kernel owns version numbering.
    final_rel = candidate
    _, cand_path = safe_release_rel(candidate)
    if cand_path.name.endswith("-candidate"):
        new_name = next_version_name()
        cand_path.rename(RELEASES / new_name)
        final_rel = "runtime/releases/" + new_name

    LAST_GOOD.write_text(old_rel, encoding="utf-8")
    CURRENT.write_text(final_rel, encoding="utf-8")
    PROMOTION.unlink()

    log("promoted", {"from": old_rel, "to": final_rel, "candidate": candidate, "reason": reason})
    print("Promoted:", final_rel)
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

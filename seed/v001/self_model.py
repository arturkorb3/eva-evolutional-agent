#!/usr/bin/env python3
"""EVA's self-model: a GENERATED, always-current description of who EVA is.

A self-evolving agent must know its own anatomy and skills - and that knowledge
must never drift from the code. So the self-model is NOT a hand-written blob: it is
DERIVED on each run from the live release. Whenever a future release adds a tool, a
mode capability, or a ratchet-pinned guarantee, EVA's self-knowledge grows with it
automatically - no manual sync, no stale prose.

Sources of truth (all already canonical in the release):
  - tools.py      -> the tools EVA can call (its skills),
  - tests.py      -> the ratchet checks (capabilities guaranteed to keep working),
  - manifest.json -> the layer/role map (its anatomy) + version.

Like tests.py, this module is provider-neutral and makes NO model calls. The whole
self-model can be produced offline (`agent.py --self-model`).
"""
from __future__ import annotations

import ast
import json
import os
import pathlib

RELEASE = pathlib.Path(
    os.environ.get("ACTIVE_RELEASE", pathlib.Path(__file__).resolve().parent)
).resolve()


def _read(name: str, release: pathlib.Path | None = None) -> str:
    base = release or RELEASE
    try:
        return (base / name).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _first_sentence(text: str, limit: int = 100) -> str:
    """Collapse whitespace and keep a short, single-line gist for the prompt."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    dot = text.find(". ")
    if 0 <= dot < limit:
        return text[: dot + 1]
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


def version(release: pathlib.Path | None = None) -> str:
    """The LIVE release name, e.g. 'v002'. Uses the actual release directory name
    (which the kernel updates on promotion), not the manifest's static genome version,
    so EVA's self-model reports which release it is really running."""
    base = release or RELEASE
    if base.name:
        return base.name
    try:
        man = json.loads(_read("manifest.json", release) or "{}")
    except Exception:
        return "?"
    return str(man.get("version", "?"))


def anatomy(release: pathlib.Path | None = None) -> dict[str, str]:
    """{filename: role} from manifest.layers - EVA's body map."""
    try:
        man = json.loads(_read("manifest.json", release) or "{}")
    except Exception:
        return {}
    layers = man.get("layers", {})
    return {str(k): str(v) for k, v in layers.items()} if isinstance(layers, dict) else {}


def skills(release: pathlib.Path | None = None) -> list[tuple[str, str, str]]:
    """(scope, tool_name, short_desc) for every tool EVA can actually call.

    Read straight from tools.py's canonical Tool lists so the inventory reflects the
    REAL toolset rather than a hand-kept duplicate. Tools available everywhere are
    labelled "all modes"; tools gated to the evolution modes are labelled
    "improve/evolve".
    """
    try:
        import tools as t  # sibling module (RELEASE is on sys.path at call time)
    except Exception:
        return []

    canonical = list(getattr(t, "CANONICAL_TOOLS", []))
    evolution = list(getattr(t, "EVOLUTION_TOOLS", []))
    canon_names = {tool.name for tool in canonical}

    out: list[tuple[str, str, str]] = []
    for tool in canonical:
        out.append(("all modes", tool.name, _first_sentence(tool.description)))
    for tool in evolution:
        if tool.name not in canon_names:
            out.append(("improve/evolve", tool.name, _first_sentence(tool.description)))
    return out


def capabilities(release: pathlib.Path | None = None) -> list[tuple[str, str]]:
    """(name, one-line summary) for every ratchet-pinned check in tests.py.

    Each check_/test_ function is a behaviour EVA guarantees keeps working, so the
    set of checks IS the catalogue of capabilities the organism promises to itself.
    """
    text = _read("tests.py", release)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith(("check_", "test_")):
            summary = _first_sentence(ast.get_docstring(node) or "", 90)
            out.append((node.name, summary))
    return out


def mode_policy(release: pathlib.Path | None = None) -> dict[str, dict[str, bool]]:
    """The per-mode permission table (what each mode may write/shell/promote), read
    from the live tools.MODE_POLICIES so EVA knows its OWN security boundaries."""
    try:
        import tools as t
    except Exception:
        return {}
    out: dict[str, dict[str, bool]] = {}
    for mode, p in getattr(t, "MODE_POLICIES", {}).items():
        out[mode] = {
            "write_workspace": p.write_workspace,
            "write_candidate": p.write_candidate,
            "run_writing_shell": p.run_writing_shell,
            "request_promotion": p.request_promotion,
        }
    return out


def render_digest(release: pathlib.Path | None = None) -> str:
    """Token-frugal self-model for the system/context prompt.

    Lists anatomy, skills and the count + names of guaranteed capabilities. Compact
    on purpose: model INPUT tokens are the cost driver, so capability descriptions
    are omitted here (see render_full for the verbose view)."""
    ana = anatomy(release)
    sk = skills(release)
    caps = capabilities(release)

    lines = [
        f"SELF-MODEL (release {version(release)}; generated live from your own code):",
        "You ARE this code: editing your active release changes how you yourself "
        "work. There is no separate external program to look for.",
    ]
    if ana:
        lines.append("Anatomy (file = role):")
        lines += [f"  - {name}: {role}" for name, role in ana.items()]
    if sk:
        lines.append("Skills (tools you can call):")
        lines += [f"  - {name} [{scope}]: {desc}" for scope, name, desc in sk]
    if caps:
        lines.append(
            f"Guaranteed capabilities ({len(caps)} ratchet-pinned, cannot silently regress):"
        )
        lines.append("  " + ", ".join(name for name, _ in caps))
    return "\n".join(lines)


def render_full(release: pathlib.Path | None = None) -> str:
    """Verbose self-model (for `agent.py --self-model`): digest + capability detail."""
    parts = [render_digest(release), "", "Capability details:"]
    for name, summary in capabilities(release):
        parts.append(f"  - {name}: {summary or '(no description)'}")
    return "\n".join(parts)


def brief(release: pathlib.Path | None = None) -> str:
    """One-shot, token-frugal awareness for the prompt: tells EVA the self-model
    EXISTS and how to fetch it on demand - WITHOUT preloading the full thing."""
    return (
        f"You ARE this code (release {version(release)}), a self-evolving agent - "
        f"there is no separate external program. You are NOT preloaded with full "
        f"docs: read your own anatomy, {len(skills(release))} skills and "
        f"{len(capabilities(release))} ratchet-pinned capabilities ON DEMAND via the "
        f"inspect_self tool (topics: overview | anatomy | skills | capabilities | "
        f"policy | sandbox | a filename | a capability name)."
    )


def _module_docstring(name: str, release: pathlib.Path | None = None) -> str:
    text = _read(name, release)
    if not text:
        return ""
    try:
        return (ast.get_docstring(ast.parse(text)) or "").strip()
    except SyntaxError:
        return ""


def detail(topic: str, release: pathlib.Path | None = None) -> str:
    """Deep-dive for one topic: a filename returns that module's role + docstring; a
    capability name returns its check summary; otherwise a usage hint."""
    topic = (topic or "").strip()
    ana = anatomy(release)
    if topic in ana:
        doc = _module_docstring(topic, release)
        return f"{topic} - {ana[topic]}\n\n{doc or '(no module docstring)'}"
    for name, summary in capabilities(release):
        if name == topic:
            return f"{name}: {summary or '(no description)'}"
    for scope, name, desc in skills(release):
        if name == topic:
            return f"{name} [{scope}]: {desc}"
    return (f"Unknown topic '{topic}'. Try: overview, anatomy, skills, capabilities, "
            f"or the name of a file/capability/skill.")


def sandbox(release: pathlib.Path | None = None) -> str:
    """The current CONTAINMENT level (safe | free) - a launch-time SANDBOX, NOT an agent
    mode. SAFE (default): read-only rootfs, non-root, no apt. FREE: writable rootfs + root
    + apt (the user starts EVA with -Free / --free). Read from EVA_SANDBOX so EVA knows
    whether it may install system packages - and that 'free' is a sandbox, not a 5th mode."""
    mode = (os.environ.get("EVA_SANDBOX", "safe") or "safe").strip().lower()
    if mode == "free":
        return ("Sandbox: FREE (writable rootfs + root + apt). The user launched you in the "
                "powerful sandbox, so you MAY `apt-get install` system packages/libraries. "
                "This is a CONTAINMENT level chosen at launch, NOT an agent mode.")
    return ("Sandbox: SAFE (read-only rootfs, non-root, no apt) - the default hardened "
            "container. System packages/libraries CANNOT be installed here; that needs the "
            "'free' sandbox (the user launches with -Free / --free) or an image change. It "
            "is a CONTAINMENT level, NOT an agent mode (the modes are work/review/improve/"
            "evolve).")


def lookup(topic: str | None = None, release: pathlib.Path | None = None) -> str:
    """Single entry point for on-demand self-inspection (used by the inspect_self
    tool). Routes a topic word to the right slice of the self-model."""
    t = (topic or "overview").strip().lower()
    if t in ("", "overview", "self", "all", "whoami"):
        return render_digest(release)
    if t == "anatomy":
        ana = anatomy(release)
        body = "\n".join(f"  - {k}: {v}" for k, v in ana.items()) or "  (unknown)"
        return "Anatomy (file = role):\n" + body
    if t in ("skills", "tools"):
        sk = skills(release)
        body = "\n".join(f"  - {n} [{s}]: {d}" for s, n, d in sk) or "  (none)"
        return "Skills (tools you can call):\n" + body
    if t in ("capabilities", "caps", "guarantees", "tests"):
        caps = capabilities(release)
        body = "\n".join(f"  - {n}: {d or '(no description)'}" for n, d in caps)
        return f"Guaranteed capabilities ({len(caps)} ratchet-pinned):\n" + body
    if t in ("policy", "policies", "permissions"):
        pol = mode_policy(release)
        if not pol:
            return "Mode policy unavailable."
        lines = ["Mode policy (what each mode may do; reads are universal):"]
        for mode, perms in pol.items():
            allowed = [k for k, v in perms.items() if v] or ["read-only"]
            lines.append(f"  - {mode}: " + ", ".join(allowed))
        lines.append("")
        lines.append(sandbox(release))
        return "\n".join(lines)
    if t in ("sandbox", "containment", "free", "safe", "free mode", "safe mode"):
        return sandbox(release)
    return detail(topic or "", release)


if __name__ == "__main__":
    print(render_full())

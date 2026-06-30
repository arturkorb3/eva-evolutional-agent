#!/usr/bin/env python3
"""Tool runtime: EVA's canonical tools and their sandboxed execution.

The model may only ASK to run a tool. EVA owns the decision, the gating, the
logging and the execution. This is where "the agent" actually touches the world.

The starting toolset is intentionally tiny and shell-centric (the cheap,
elementary tool): `shell`, plus `ask_user` (human-in-the-loop) and `finish`.
More tools are added by defining a canonical Tool here - adapters render them
automatically.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import time
from dataclasses import dataclass

from core import Tool, ToolCall, ToolObservation
from human import (ApprovalPolicy, HumanInterface, RISK_NONE, RISK_PROMOTE,
                   RISK_SHELL, RISK_WRITE)
import self_model


# --------------------------------------------------------------------------- #
# Canonical tool schema (defined ONCE, provider-independent)
# --------------------------------------------------------------------------- #
SHELL_TOOL = Tool(
    name="shell",
    description="Run a shell command in the workspace. Prefer read-only commands "
                "(grep -n, sed -n, head, tail, cat, ls, find, diff) to inspect "
                "code cheaply; use writes/heredocs to edit files.",
    input_schema={
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        "required": ["cmd"],
    },
)

ASK_USER_TOOL = Tool(
    name="ask_user",
    description="Ask the human ONE question when you genuinely lack information "
                "needed to proceed. Prefer asking over guessing.",
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
)

FINISH_TOOL_DEF = Tool(
    name="finish",
    description="End the current turn with a short summary of what was done.",
    input_schema={
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": [],
    },
)

INSPECT_SELF_TOOL = Tool(
    name="inspect_self",
    description="Read your OWN self-model on demand - you are a self-evolving agent "
                "and are not preloaded with full docs. Returns your anatomy "
                "(file->role), skills (tools), ratchet-pinned capabilities, or the "
                "per-mode security policy. `topic` selects the slice: 'overview' "
                "(default), 'anatomy', 'skills', 'capabilities', 'policy', or the name "
                "of a file/capability/skill for detail (a filename also returns that "
                "module's docstring). Use it before editing yourself so you target the "
                "right file.",
    input_schema={
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": [],
    },
)

WRITE_FILE_TOOL = Tool(
    name="write_file",
    description="Write a file with its FULL content - the reliable way to edit code. "
                "Prefer this over shell heredocs/sed for any multi-line change. In "
                "work mode it writes into the workspace; in improve/evolve it can "
                "also write into a *-candidate release (e.g. "
                "../runtime/releases/v002-candidate/adapters.py). It can NOT touch the "
                "active release, state/, or the kernel.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
)

READ_FILE_TOOL = Tool(
    name="read_file",
    description="Read a file's full text (your own code under ../runtime/releases/, "
                "workspace files, or the kernel). Use this to SEE current content "
                "before editing - never ask the user for file contents.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        "required": ["path"],
    },
)

REPLACE_IN_FILE_TOOL = Tool(
    name="replace_in_file",
    description="Surgically replace an exact text block in a file (old -> new). The "
                "old text must occur EXACTLY ONCE - include enough surrounding lines "
                "to be unique. Best for editing existing code; same write permissions "
                "as write_file.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    },
)

REQUEST_PROMOTION_TOOL = Tool(
    name="request_promotion",
    description="Ask EVA's supervisor + kernel to gate and (if it passes) promote a "
                "candidate release you have finished building. Only in improve/evolve "
                "mode, and only AFTER the candidate is complete. Pass the candidate "
                "name, e.g. 'v002-candidate'.",
    input_schema={
        "type": "object",
        "properties": {
            "candidate": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["candidate"],
    },
)

CANONICAL_TOOLS: list[Tool] = [SHELL_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL,
                              REPLACE_IN_FILE_TOOL, INSPECT_SELF_TOOL, ASK_USER_TOOL,
                              FINISH_TOOL_DEF]
# Extra tool available only when the agent is allowed to evolve the release.
EVOLUTION_TOOLS: list[Tool] = CANONICAL_TOOLS + [REQUEST_PROMOTION_TOOL]


_CANDIDATE_RE = re.compile(r"v[0-9][A-Za-z0-9._-]*-candidate")


def normalize_candidate(candidate: str) -> str:
    """Accept 'v002-candidate' or 'runtime/releases/v002-candidate' and return the
    bare, validated candidate name. Raises ValueError on anything else."""
    name = str(candidate or "").strip()
    if "/" in name:
        name = name.rstrip("/").split("/")[-1]
    if not _CANDIDATE_RE.fullmatch(name):
        raise ValueError("candidate must look like v002-candidate")
    return name


# --------------------------------------------------------------------------- #
# Read-only shell detection (auto-approve safe inspection commands)
# --------------------------------------------------------------------------- #
# Only commands that ACTUALLY exist in the runtime image (shell builtins + coreutils
# + grep/sed/find/diff) belong here. `file` was removed: it is a separate package not
# in the slim image, so auto-approving it produced exit=127 "file: not found" friction.
READ_ONLY_SHELL_CMDS = {
    "grep", "egrep", "fgrep", "sed", "find", "head", "tail", "cat", "wc",
    "sort", "uniq", "cut", "tr", "diff", "ls", "nl", "stat",
    "basename", "dirname", "realpath", "echo", "pwd", "true", "test",
}

_SHELL_WRITE_FLAGS = ("-i", "-delete", "-exec", "-execdir", "-ok", "-fprint", "-fprintf")


def is_read_only_shell(cmd: str) -> bool:
    """Conservative whitelist: only auto-approve commands that just read/search and
    cannot write. Read-only commands MAY be chained with ; && || and pipes (each
    segment's command must be whitelisted). Redirection (> <), command substitution
    ($( `) and backgrounding (&) are rejected, as are known write flags.

    The scan is QUOTE-AWARE: a separator or redirect character INSIDE a quoted argument
    (e.g. grep's `"a\\|b"` alternation, or a literal `>` in a search pattern) is part of
    that argument, not shell syntax, so read-only inspection commands are not wrongly
    rejected. Unbalanced quotes -> reject (fail safe)."""
    text = str(cmd or "")
    segments: list[str] = []
    cur: list[str] = []
    quote: "str | None" = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote is not None:
            cur.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            cur.append(c)
            i += 1
            continue
        if c in ("<", ">", "`"):
            return False  # redirection / command substitution can hide writes
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            return False
        if c == "&":
            if i + 1 < n and text[i + 1] == "&":  # && = separator
                segments.append("".join(cur)); cur = []; i += 2; continue
            return False  # a lone & = backgrounding
        if c == "|":
            step = 2 if (i + 1 < n and text[i + 1] == "|") else 1
            segments.append("".join(cur)); cur = []; i += step; continue
        if c == ";":
            segments.append("".join(cur)); cur = []; i += 1; continue
        cur.append(c)
        i += 1
    if quote is not None:
        return False  # unbalanced quote
    segments.append("".join(cur))

    for seg in segments:
        if not seg.strip():
            continue
        try:
            parts = shlex.split(seg)
        except ValueError:
            return False
        if not parts:
            continue
        head = os.path.basename(parts[0])
        if head not in READ_ONLY_SHELL_CMDS:
            return False
        if any(f in parts[1:] for f in _SHELL_WRITE_FLAGS):
            return False
    return True


# --------------------------------------------------------------------------- #
# Mode policy: the single, explicit, tested table of what each mode may do.
# --------------------------------------------------------------------------- #
# Security boundaries used to be implicit conditionals scattered across the tool
# handlers. They are now declared ONCE here so they are visible, auditable and
# pinned by a test. The runtime consults policy_for(mode); it never re-decides
# per-mode permissions inline. Tools that only READ (read_file, inspect_self,
# read-only shell) and the human/finish tools are universal and not gated here.
@dataclass(frozen=True)
class ModePolicy:
    write_workspace: bool      # may write_file/replace_in_file inside the workspace
    write_candidate: bool      # may write into a *-candidate release
    run_writing_shell: bool    # may run non-read-only shell (writes/side effects)
    request_promotion: bool    # may ask the supervisor/kernel to promote a candidate


MODE_POLICIES: dict[str, ModePolicy] = {
    #                       ws     cand   shell  promote
    "work":    ModePolicy(True,  False, True,  False),
    "review":  ModePolicy(False, False, False, False),
    "improve": ModePolicy(True,  True,  True,  True),
    "evolve":  ModePolicy(True,  True,  True,  True),
}

# Unknown/unexpected mode = the most restrictive policy (fail safe).
_LOCKED_POLICY = ModePolicy(False, False, False, False)


def policy_for(mode: str) -> ModePolicy:
    return MODE_POLICIES.get(mode, _LOCKED_POLICY)


# --------------------------------------------------------------------------- #
# The runtime
# --------------------------------------------------------------------------- #
class ShellToolRuntime:
    """Executes canonical tool calls. `shell` runs in the workspace; `ask_user`
    routes to the HumanInterface; `finish` returns its summary."""

    def __init__(self, *, workspace: pathlib.Path, approval: ApprovalPolicy,
                 human: HumanInterface, releases: "pathlib.Path | None" = None,
                 state: "pathlib.Path | None" = None, max_output: int = 6000):
        self.workspace = pathlib.Path(workspace)
        self.approval = approval
        self.human = human
        self.releases = pathlib.Path(releases) if releases else None
        self.state = pathlib.Path(state) if state else None
        self.max_output = max_output

    def execute(self, call: ToolCall, mode: str) -> ToolObservation:
        if call.name == "finish":
            summary = str(call.arguments.get("summary", "") or "Finished.")
            return ToolObservation(call.id, call.name, summary)

        if call.name == "ask_user":
            question = str(call.arguments.get("question", "") or "(no question)")
            answer = self.human.ask(question)
            return ToolObservation(call.id, call.name,
                                   "User answered: " + (answer or "(no answer)"))

        if call.name == "shell":
            return self._run_shell(call, mode)

        if call.name == "read_file":
            return self._read_file(call)

        if call.name == "write_file":
            return self._write_file(call, mode)

        if call.name == "replace_in_file":
            return self._replace_in_file(call, mode)

        if call.name == "inspect_self":
            return self._inspect_self(call)

        if call.name == "request_promotion":
            return self._request_promotion(call, mode)

        return ToolObservation(call.id, call.name, f"Unknown tool: {call.name}")

    def _run_shell(self, call: ToolCall, mode: str) -> ToolObservation:
        cmd = str(call.arguments.get("cmd", "") or "")
        if not cmd:
            return ToolObservation(call.id, call.name, "Denied: empty shell command.")

        read_only = is_read_only_shell(cmd)
        if not read_only and not policy_for(mode).run_writing_shell:
            return ToolObservation(call.id, call.name,
                                   f"Denied: {mode} mode allows only read-only shell.")

        risk = RISK_NONE if read_only else RISK_SHELL
        # The command is shown compactly on the activity (▸) line; the FULL command is
        # passed as detail so the human can reveal it with 'f' before approving.
        if not self.approval.approve(risk, "Approve shell?", detail=cmd):
            return ToolObservation(call.id, call.name, "Shell rejected.")

        try:
            timeout = int(call.arguments.get("timeout", 60))
        except (TypeError, ValueError):
            timeout = 60

        r = subprocess.run(
            cmd, cwd=str(self.workspace), shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        out = (f"exit={r.returncode}\n"
               f"stdout:\n{r.stdout[-self.max_output:]}\n"
               f"stderr:\n{r.stderr[-self.max_output:]}")
        return ToolObservation(call.id, call.name, out)

    def _resolve_write_target(self, path: str, mode: str):
        """Resolve a write target and enforce where each mode may write (per the
        central MODE_POLICIES table). Returns (resolved_path, None) when allowed,
        else (None, denial_message)."""
        pol = policy_for(mode)
        raw = str(path or "").strip()
        if not raw:
            return None, "Denied: empty path."
        target = (self.workspace / raw).resolve()
        ws = self.workspace.resolve()
        if target == ws or str(target).startswith(str(ws) + os.sep):
            if not pol.write_workspace:
                return None, f"Denied: {mode} mode is read-only (no workspace writes)."
            return target, None
        if self.releases is not None:
            rel = self.releases.resolve()
            if str(target).startswith(str(rel) + os.sep):
                if not pol.write_candidate:
                    return None, "Denied: release writes only in improve/evolve mode."
                release_dir = target.relative_to(rel).parts[0]
                if not release_dir.endswith("-candidate"):
                    return None, ("Denied: may only write inside a *-candidate release, "
                                  "never the active or other releases.")
                return target, None
        return None, "Denied: path is outside the workspace and any candidate release."

    def _write_file(self, call: ToolCall, mode: str) -> ToolObservation:
        target, err = self._resolve_write_target(call.arguments.get("path", ""), mode)
        if err:
            return ToolObservation(call.id, call.name, err)
        content = call.arguments.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        detail = f"write {target}  ({len(content)} chars)"
        if content.strip():
            preview = content if len(content) <= 4000 else content[:4000] + "\n...(truncated)"
            detail += "\n\n" + preview
        if not self.approval.approve(RISK_WRITE, "Approve file write?", detail=detail):
            return ToolObservation(call.id, call.name, "File write rejected.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if target.suffix == ".py":
            try:
                target.chmod(0o755)
            except Exception:
                pass
        return ToolObservation(call.id, call.name,
                               f"Wrote {len(content)} chars to {target}")

    def _read_file(self, call: ToolCall) -> ToolObservation:
        raw = str(call.arguments.get("path", "")).strip()
        if not raw:
            return ToolObservation(call.id, call.name, "Denied: empty path.")
        target = (self.workspace / raw).resolve()
        root = self.workspace.parent.resolve()
        if not (target == root or str(target).startswith(str(root) + os.sep)):
            return ToolObservation(call.id, call.name,
                                   "Denied: path is outside the project.")
        if not target.is_file():
            return ToolObservation(call.id, call.name, f"Not a file: {target}")
        try:
            max_chars = int(call.arguments.get("max_chars", 20000))
        except (TypeError, ValueError):
            max_chars = 20000
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) <= max_chars:
            return ToolObservation(call.id, call.name, text)
        return ToolObservation(call.id, call.name,
                               text[:max_chars] +
                               f"\n...[truncated {len(text) - max_chars} chars; raise max_chars]")

    def _replace_in_file(self, call: ToolCall, mode: str) -> ToolObservation:
        target, err = self._resolve_write_target(call.arguments.get("path", ""), mode)
        if err:
            return ToolObservation(call.id, call.name, err)
        if not target.is_file():
            return ToolObservation(call.id, call.name,
                                   f"Denied: file does not exist: {target}")
        old = call.arguments.get("old", "")
        new = call.arguments.get("new", "")
        if not isinstance(old, str) or not old:
            return ToolObservation(call.id, call.name,
                                   "Denied: 'old' must be a non-empty string.")
        if not isinstance(new, str):
            new = str(new)
        text = target.read_text(encoding="utf-8", errors="replace")
        n = text.count(old)
        if n == 0:
            return ToolObservation(call.id, call.name, "No match: 'old' text not found.")
        if n > 1:
            return ToolObservation(call.id, call.name,
                                   f"Ambiguous: 'old' occurs {n} times; add more "
                                   "surrounding context to make it unique.")
        detail = f"edit {target}\n  - {old.strip()[:400]}\n  + {new.strip()[:400]}"
        if not self.approval.approve(RISK_WRITE, "Approve file edit?", detail=detail):
            return ToolObservation(call.id, call.name, "File edit rejected.")
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        if target.suffix == ".py":
            try:
                target.chmod(0o755)
            except Exception:
                pass
        return ToolObservation(call.id, call.name, f"Replaced 1 occurrence in {target}")

    def _inspect_self(self, call: ToolCall) -> ToolObservation:
        # Read-only self-knowledge, available in every mode. EVA queries its own
        # anatomy/skills/capabilities on demand instead of being preloaded with them.
        topic = str(call.arguments.get("topic", "") or "overview")
        try:
            text = self_model.lookup(topic)
        except Exception as exc:  # never let introspection crash a turn
            text = f"inspect_self failed: {type(exc).__name__}: {exc}"
        return ToolObservation(call.id, call.name, text)

    def _request_promotion(self, call: ToolCall, mode: str) -> ToolObservation:
        if not policy_for(mode).request_promotion:
            return ToolObservation(call.id, call.name,
                                   "Denied: promotion only in improve/evolve mode.")
        if self.releases is None or self.state is None:
            return ToolObservation(call.id, call.name,
                                   "Denied: promotion is not available in this context.")
        try:
            name = normalize_candidate(call.arguments.get("candidate", ""))
        except ValueError as e:
            return ToolObservation(call.id, call.name, f"Denied: {e}")

        dest = self.releases / name
        if not dest.exists():
            return ToolObservation(call.id, call.name,
                                   f"Denied: candidate {name} does not exist under "
                                   "runtime/releases/.")

        reason = str(call.arguments.get("reason", "") or "")
        if not self.approval.approve(RISK_PROMOTE, "Approve promotion request?",
                                     detail=f"promote {name}"):
            return ToolObservation(call.id, call.name, "Promotion request rejected.")

        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "promotion_request.json").write_text(json.dumps({
            "candidate": "runtime/releases/" + name,
            "reason": reason,
            "requested_by": "agent",
            "time": time.time(),
        }, indent=2), encoding="utf-8")
        return ToolObservation(call.id, call.name,
                               f"Promotion requested for {name}. Finish now; the "
                               "supervisor and kernel will gate it.")

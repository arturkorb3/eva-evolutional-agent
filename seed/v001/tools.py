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
import subprocess
import time

from core import Tool, ToolCall, ToolObservation
from human import ApprovalPolicy, HumanInterface, RISK_NONE, RISK_PROMOTE, RISK_SHELL


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

CANONICAL_TOOLS: list[Tool] = [SHELL_TOOL, ASK_USER_TOOL, FINISH_TOOL_DEF]
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
READ_ONLY_SHELL_CMDS = {
    "grep", "egrep", "fgrep", "sed", "find", "head", "tail", "cat", "wc",
    "sort", "uniq", "cut", "tr", "diff", "ls", "nl", "stat", "file",
    "basename", "dirname", "realpath", "echo", "pwd", "true", "test",
}

_SHELL_WRITE_FLAGS = ("-i", "-delete", "-exec", "-execdir", "-ok", "-fprint", "-fprintf")


def is_read_only_shell(cmd: str) -> bool:
    """Conservative whitelist: only auto-approve commands that just read/search
    and cannot write. Any redirection, chaining, substitution or known write
    flag forces the approval path. When unsure, return False (fail safe)."""
    text = str(cmd or "")
    if any(tok in text for tok in (">", "<", "$(", "`", "&", ";")):
        return False
    for piece in text.split("|"):
        parts = piece.strip().split()
        if not parts:
            return False
        head = os.path.basename(parts[0])
        if head not in READ_ONLY_SHELL_CMDS:
            return False
        if any(f in parts[1:] for f in _SHELL_WRITE_FLAGS):
            return False
    return True


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

        if call.name == "request_promotion":
            return self._request_promotion(call, mode)

        return ToolObservation(call.id, call.name, f"Unknown tool: {call.name}")

    def _run_shell(self, call: ToolCall, mode: str) -> ToolObservation:
        cmd = str(call.arguments.get("cmd", "") or "")
        if not cmd:
            return ToolObservation(call.id, call.name, "Denied: empty shell command.")

        read_only = is_read_only_shell(cmd)
        if mode == "review" and not read_only:
            return ToolObservation(call.id, call.name,
                                   "Denied: review mode allows only read-only shell.")

        risk = RISK_NONE if read_only else RISK_SHELL
        label = "  [read-only, auto-approved]" if read_only else ""
        print("\nSHELL in workspace:", cmd + label)

        if not self.approval.approve(risk, "Approve shell?"):
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

    def _request_promotion(self, call: ToolCall, mode: str) -> ToolObservation:
        if mode not in ("improve", "evolve"):
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
        print("\nPROMOTION request for:", name)
        if not self.approval.approve(RISK_PROMOTE, "Approve promotion request?"):
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

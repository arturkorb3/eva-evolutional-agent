#!/usr/bin/env python3
"""A small, dependency-free status view: human-readable, on-the-fly tracking of
"what EVA is doing right now".

This layer is PRESENTATION ONLY. It consumes the core loop's hooks (on_say,
on_tool_call, on_observation, on_error) and renders a clean, colour-coded stream
to the terminal. It holds NO agent logic, NO provider calls and NO tool execution:
removing or swapping it changes nothing about what EVA *does*, only how a human
SEES it. Pure stdlib (ANSI escapes), so it works inside the slim Docker image with
no extra packages and degrades to plain text when stdout is not a TTY.

The format helpers are pure functions (string in -> string out) so they can be
unit-tested without a terminal.
"""
from __future__ import annotations

import json
import os
import sys

_CODES = {
    "reset": "\x1b[0m", "dim": "\x1b[2m", "bold": "\x1b[1m",
    "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "magenta": "\x1b[35m", "cyan": "\x1b[36m", "gray": "\x1b[90m",
}


def supports_color(stream) -> bool:
    """Colour only on a real TTY, and never when NO_COLOR or EVA_TUI=0 is set. Empty
    values count as UNSET (compose forwards "" for unset vars, which must not silently
    disable colour)."""
    if os.environ.get("EVA_TUI") == "0" or os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _paint(text: str, *names: str, color: bool = True) -> str:
    if not color or not names:
        return text
    prefix = "".join(_CODES.get(n, "") for n in names)
    return f"{prefix}{text}{_CODES['reset']}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def summarize_args(name: str, args: dict, limit: int = 72) -> str:
    """A short, human-readable gist of a tool call's arguments - what EVA is about
    to do, in one glance."""
    args = args or {}
    if name == "shell":
        return "$ " + _clip(args.get("cmd", ""), limit)
    if name in ("write_file", "read_file", "replace_in_file"):
        return _clip(args.get("path", "") or "(no path)", limit)
    if name == "inspect_self":
        return "topic=" + _clip(args.get("topic", "") or "overview", limit)
    if name == "ask_user":
        return _clip(args.get("question", ""), limit)
    if name == "request_promotion":
        return _clip(args.get("candidate", ""), limit)
    if name == "finish":
        return _clip(args.get("summary", "") or "done", limit)
    if not args:
        return ""
    try:
        return _clip(json.dumps(args, ensure_ascii=False), limit)
    except Exception:
        return _clip(str(args), limit)


def format_header(mode: str, identity: dict, release: str, *, color: bool = True) -> str:
    ident = identity or {}
    model = ident.get("model", "?")
    bits = [f"mode={mode}", f"model={model}"]
    if release:
        bits.append(f"release={release}")
    line = "EVA · " + "  ".join(bits)
    rule = "─" * max(8, len(line))
    return _paint(rule, "gray", color=color) + "\n" + \
        _paint(line, "bold", "cyan", color=color) + "\n" + \
        _paint(rule, "gray", color=color)


_BRAND_ART = (
    "  ███████ ██    ██  █████ ",
    "  ██      ██    ██ ██   ██",
    "  █████   ██    ██ ███████",
    "  ██       ██  ██  ██   ██",
    "  ███████   ████   ██   ██",
)

_USAGE_TIPS = (
    "Just tell EVA what to do — it works, then replies after each turn.",
    "Press Enter on an empty line (or type 'exit') to end the session.",
    "/paste attaches your most recent screenshot.",
    "Set EVA_TUI_FULL=1 to see full commands and complete output.",
)


def format_brand(*, color: bool = True) -> str:
    lines = [_paint(l, "bold", "cyan", color=color) for l in _BRAND_ART]
    lines.append(_paint("  Evolutional Agent · self-improving · sandboxed", "gray", color=color))
    return "\n".join(lines)


def format_welcome(mode: str, identity: dict, release: str, *,
                   color: bool = True, usage: bool = True) -> str:
    ident = identity or {}
    meta = f"mode={mode}  model={ident.get('model', '?')}"
    if release:
        meta += f"  release={release}"
    parts = ["", format_brand(color=color), "", _paint("  " + meta, "bold", color=color)]
    if usage:
        parts.append("")
        for tip in _USAGE_TIPS:
            parts.append(_paint("  • ", "cyan", color=color) + tip)
    parts.append("")
    return "\n".join(parts)


def format_say(text: str, *, color: bool = True) -> str:
    return _paint("● EVA ", "bold", "magenta", color=color) + (text or "").strip()


def format_tool_call(call, *, color: bool = True, full: bool = False) -> str:
    name = getattr(call, "name", "?")
    args = getattr(call, "arguments", {}) or {}
    if name == "finish":
        # finish carries EVA's final message to the user - show it IN FULL (preserving
        # line breaks), never truncated like a mid-task activity gist.
        summ = str(args.get("summary", "") or "done").strip()
        head = _paint("\u2713 finished", "bold", "green", color=color)
        return head + (": " + summ if summ else "")
    # ask_user questions are short and important - don't clip them as hard. Shell
    # commands are what a human approves, so show a bit more of them too.
    limit = 100000 if full else (200 if name == "ask_user"
                                 else 110 if name == "shell" else 72)
    arrow = _paint("\u25b8", "bold", "blue", color=color)
    label = _paint(name, "bold", color=color)
    gist = summarize_args(name, args, limit)
    gist = (" " + _paint(gist, "gray", color=color)) if gist else ""
    return f"{arrow} {label}{gist}"


def format_observation(obs, *, color: bool = True, lines: int = 3,
                       width: int = 100, full: bool = False) -> str:
    if full:
        lines, width = 10 ** 6, 10 ** 6
    name = getattr(obs, "name", "") or "tool"
    out = getattr(obs, "output", "") or ""
    first = out.splitlines()[0] if out else ""

    # Shell results begin with "exit=<n>" - surface that prominently.
    if first.startswith("exit="):
        code = first.split("=", 1)[1].strip()
        ok = code == "0"
        badge = _paint(f"exit={code}", "green" if ok else "red", color=color)
        body_lines = [l for l in out.splitlines()[1:] if l.strip()
                      and not l.strip().startswith(("stdout:", "stderr:"))]
        shown = body_lines[:lines]
        body = "\n".join("    " + _clip(l, width) for l in shown)
        more = len(body_lines) - len(shown)
        tail = _paint(f"    …(+{more} more lines)", "gray", color=color) if more > 0 else ""
        head = _paint("  ↳ ", "gray", color=color) + badge
        return "\n".join(p for p in (head, body, tail) if p)

    # Denials / rejections stand out.
    tag = "yellow" if first.startswith(("Denied", "Shell rejected", "File")) else "gray"
    gist = _clip(out, width)
    return _paint("  ↳ ", "gray", color=color) + _paint(f"[{name}] ", tag, color=color) + gist


def format_error(stage: str, exc, *, color: bool = True) -> str:
    return _paint(f"✗ {stage} error: ", "bold", "red", color=color) + _clip(str(exc), 160)


class StatusView:
    """Renders the loop's live events. Construct one per run; pass its methods as the
    on_say / on_tool_call / on_observation / on_error callbacks of run_agent_loop."""

    def __init__(self, *, mode: str, identity: "dict | None" = None,
                 release: str = "", stream=None, color: "bool | None" = None,
                 full: "bool | None" = None):
        self.mode = mode
        self.identity = identity or {}
        self.release = release
        self.stream = stream or sys.stdout
        self.color = supports_color(self.stream) if color is None else color
        # Compact by default; EVA_TUI_FULL=1 expands commands and tool output.
        self.full = (os.environ.get("EVA_TUI_FULL") == "1") if full is None else full
        # True while assistant text is being streamed live for the current turn, so
        # say() (the final reconcile) closes the open line instead of reprinting it.
        self._streaming = False

    def _w(self, line: str) -> None:
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except Exception:
            pass

    def _raw(self, text: str) -> None:
        """Write without a trailing newline and flush, so streamed text appears as it
        arrives (live typing) rather than buffered per line."""
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def header(self) -> None:
        self._w(format_header(self.mode, self.identity, self.release, color=self.color))

    def welcome(self, usage: bool = True) -> None:
        """One-time branded start screen: logo, run identity and a short how-to."""
        self._w(format_welcome(self.mode, self.identity, self.release,
                               color=self.color, usage=usage))

    def say(self, text: str) -> None:
        if self._streaming:
            # The text was already rendered live via on_say_delta; just close the line.
            self._raw("\n")
            self._streaming = False
            return
        if text and text.strip():
            self._w(format_say(text, color=self.color))

    def on_say_delta(self, chunk: str) -> None:
        """Render an assistant text fragment as it streams in. The first chunk of a
        turn prints the '● EVA ' prefix once; say() later closes the line. Tool calls
        are NOT streamed here - only free text, which is the only thing worth showing
        token-by-token."""
        if not chunk:
            return
        if not self._streaming:
            self._streaming = True
            self._raw(_paint("● EVA ", "bold", "magenta", color=self.color))
        self._raw(chunk)

    def tool_call(self, call) -> None:
        self._w(format_tool_call(call, color=self.color, full=self.full))

    def observation(self, obs) -> None:
        # finish is already shown as the tool-call line; don't echo it again.
        if (getattr(obs, "name", "") or "") == "finish":
            return
        self._w(format_observation(obs, color=self.color, full=self.full))

    def error(self, stage: str, exc) -> None:
        if self._streaming:
            self._raw("\n")
            self._streaming = False
        self._w(format_error(stage, exc, color=self.color))

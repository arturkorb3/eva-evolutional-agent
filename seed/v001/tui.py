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
import re
import shutil
import sys
import textwrap
import threading
import time

try:  # comfort only: stdlib on Linux/macOS (present in the slim Docker image),
    import readline as _readline  # absent on bare Windows. NEVER a hard dependency.
except Exception:  # pragma: no cover - platform dependent
    _readline = None

# readline computes the visible prompt width to drive line-editing/wrapping; any
# non-printing (colour) sequence in the prompt must be wrapped in \001..\002 so it is
# excluded from that width, otherwise the cursor drifts on long / recalled lines.
_RL_IGNORE_START, _RL_IGNORE_END = "\001", "\002"

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
    if name == "fetch_url":
        return _clip(args.get("url", ""), limit)
    if name == "web_search":
        return _clip(args.get("query", ""), limit)
    if name == "apply_patch":
        n = len(args.get("edits") or [])
        return _clip(args.get("path", "") or "(no path)", limit) + f"  ({n} edits)"
    if name == "make_candidate":
        return _clip(args.get("name", "") or "(clone active release)", limit)
    if name == "run_tests":
        return _clip(args.get("candidate", ""), limit)
    if name == "note_evolution_need":
        return _clip(args.get("need", ""), limit)
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
    "Type /help for in-chat commands; /paste attaches your latest screenshot.",
    "Press Enter on an empty line (or type 'exit') to end the session.",
    "Set EVA_TUI_FULL=1 to see full commands and complete output.",
)


_LOGO_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGO_SIZES = (160, 120, 84)   # finest first; a matching eva_logo_<W>.ans may ship
_LOGO_CACHE: "dict[int, str | None]" = {}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _read_logo(width: int) -> "str | None":
    if width not in _LOGO_CACHE:
        try:
            data = open(os.path.join(_LOGO_DIR, f"eva_logo_{width}.ans"),
                        "r", encoding="utf-8").read()
        except Exception:
            data = ""
        _LOGO_CACHE[width] = data if data.strip() else None
    return _LOGO_CACHE[width]


def _logo_display_width(text: str) -> int:
    return max((len(_ANSI_RE.sub("", ln)) for ln in text.splitlines()), default=0)


def _load_logo(max_cols: int) -> "str | None":
    """Pick the FINEST pre-rendered contour logo that fits within max_cols columns (an
    unlisted genome asset shipped next to this module). Returns None if even the
    smallest is too wide, so the caller falls back to the block-letter wordmark. Never
    raises."""
    for width in _LOGO_SIZES:
        text = _read_logo(width)
        if text and _logo_display_width(text) <= max_cols:
            return text
    return None


def format_brand(*, color: bool = True, logo: "str | None" = None) -> str:
    if logo:
        return logo.rstrip("\n") + "\n" + _paint(
            "  EVA · Evolvable Virtual Agent — self-improving · sandboxed", "gray", color=color)
    lines = [_paint(l, "bold", "cyan", color=color) for l in _BRAND_ART]
    lines.append(_paint("  Evolvable Virtual Agent · self-improving · sandboxed", "gray", color=color))
    return "\n".join(lines)


def format_welcome(mode: str, identity: dict, release: str, *,
                   color: bool = True, usage: bool = True,
                   logo: "str | None" = None) -> str:
    ident = identity or {}
    meta = f"mode={mode}  model={ident.get('model', '?')}"
    if release:
        meta += f"  release={release}"
    parts = ["", format_brand(color=color, logo=logo), "", _paint("  " + meta, "bold", color=color)]
    if usage:
        parts.append("")
        for tip in _USAGE_TIPS:
            parts.append(_paint("  • ", "cyan", color=color) + tip)
    parts.append("")
    return "\n".join(parts)


_SLASH_COMMANDS = (
    ("/help", "show this list of in-chat commands"),
    ("/model", "show or switch the model within the current provider, e.g. /model gpt-5.5"),
    ("/resume", "list work sessions, or switch to one: /resume <id>  (work mode)"),
    ("/paste", "attach your most recent screenshot (take one with Win+Shift+S first)"),
    ("exit", "end the session (or just press Enter on an empty line)"),
)


def format_slash_help(*, color: bool = True) -> str:
    """The in-chat command list shown when the user types /help (presentation only)."""
    lines = ["", _paint("  In-chat commands", "bold", color=color)]
    for cmd, desc in _SLASH_COMMANDS:
        lines.append("  " + _paint(cmd.ljust(10), "bold", "cyan", color=color) + " " + desc)
    lines.append("")
    lines.append(_paint(
        "  Modes (work/improve/review/evolve) are chosen at launch, e.g. `eva improve`.",
        "gray", color=color))
    lines.append(_paint(
        "  Run `eva help` in your shell for all commands. Otherwise just talk to EVA.",
        "gray", color=color))
    lines.append("")
    return "\n".join(lines)


def _is_table_row(s: str) -> bool:
    s = s.strip()
    return len(s) >= 2 and s.startswith("|") and s.endswith("|")


def _is_table_sep(s: str) -> bool:
    s = s.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return False
    inner = s.strip("|")
    return "-" in inner and set(inner) <= set("-: |")


def _term_width(default: int = 100) -> int:
    try:
        w = shutil.get_terminal_size((default, 24)).columns
        return w if isinstance(w, int) and w > 20 else default
    except Exception:
        return default


def _wrap_cell(text: str, width: int) -> list:
    # Wrap one cell to its column width so a very long entry no longer blows the table
    # past the terminal and breaks its borders. Long unbreakable tokens (e.g. URLs) are
    # hard-split. Returns at least one (possibly empty) line.
    lines: list = []
    for para in str(text or "").split("\n"):
        lines.extend(textwrap.wrap(para, width=max(1, width),
                                   break_long_words=True, break_on_hyphens=False) or [""])
    return lines or [""]


def _render_table(block: list, max_width: "int | None" = None) -> str:
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in block]
    header, body = rows[0], rows[2:]      # rows[1] is the --- separator; drop it
    ncols = max(len(r) for r in rows)

    def pad(r):
        return r + [""] * (ncols - len(r))

    header, body = pad(header), [pad(r) for r in body]
    widths = [0] * ncols
    for r in [header] + body:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    # Keep the whole table within the terminal (or a given max) width: shrink the widest
    # columns and WRAP their cells instead of letting a huge entry break the borders.
    if max_width is None:
        max_width = _term_width()
    budget = max(max_width - (3 * ncols + 1), ncols * 6)
    while sum(widths) > budget and max(widths) > 6:
        widths[widths.index(max(widths))] -= 1

    def rule(left, mid, right):
        return left + mid.join("\u2500" * (w + 2) for w in widths) + right

    def render_row(cells):
        wrapped = [_wrap_cell(cells[i], widths[i]) for i in range(ncols)]
        height = max(len(w) for w in wrapped)
        out = []
        for k in range(height):
            segs = [(wrapped[i][k] if k < len(wrapped[i]) else "").ljust(widths[i])
                    for i in range(ncols)]
            out.append("\u2502" + "\u2502".join(f" {s} " for s in segs) + "\u2502")
        return out

    out = [rule("\u250c", "\u252c", "\u2510")]
    out += render_row(header)
    out.append(rule("\u251c", "\u253c", "\u2524"))
    for r in body:
        out += render_row(r)
    out.append(rule("\u2514", "\u2534", "\u2518"))
    return "\n".join(out)


def render_markdown_tables(text: str) -> str:
    """Reformat GitHub-style markdown tables (`| a | b |` with a `---` separator row) into
    aligned box tables for the terminal; non-table lines pass through unchanged. Pure
    function, so it renders the same for live output, replay and finish summaries."""
    lines = (text or "").split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        if _is_table_row(lines[i]) and i + 1 < n and _is_table_sep(lines[i + 1]):
            block = [lines[i], lines[i + 1]]
            i += 2
            while i < n and _is_table_row(lines[i]):
                block.append(lines[i])
                i += 1
            try:
                out.append(_render_table(block))
            except Exception:
                out.extend(block)      # never let rendering swallow content
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def format_say(text: str, *, color: bool = True) -> str:
    return _paint("● EVA ", "bold", "magenta", color=color) + \
        render_markdown_tables((text or "").strip())


def format_tool_call(call, *, color: bool = True, full: bool = False) -> str:
    name = getattr(call, "name", "?")
    args = getattr(call, "arguments", {}) or {}
    arrow = _paint("\u25b8", "bold", "blue", color=color)
    if name == "finish":
        # finish carries EVA's final message to the user - show it IN FULL (preserving
        # line breaks + rendering any markdown table), never truncated like a gist.
        summ = render_markdown_tables(str(args.get("summary", "") or "done").strip())
        head = _paint("\u2713 finished", "bold", "green", color=color)
        return head + (": " + summ if summ else "")
    # Shell commands are what a human approves, so show a bit more of them.
    limit = 100000 if full else (110 if name == "shell" else 72)
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


def format_prompt(label: str = "you", *, color: bool = True,
                  readline_active: bool = False) -> str:
    """The input prompt string, e.g. '❯ you '. Colour is optional; when readline is
    active the colour codes are wrapped in the ignore-markers so the terminal's cursor
    math stays correct. Pure function (string in -> string out), so it is unit-testable
    without a terminal."""
    arrow = "\u276f"  # ❯
    if not color:
        return f"{arrow} {label} "

    def mark(code: str) -> str:
        return (f"{_RL_IGNORE_START}{code}{_RL_IGNORE_END}"
                if readline_active else code)

    on = "".join(_CODES.get(n, "") for n in ("bold", "cyan"))
    off = _CODES["reset"]
    return f"{mark(on)}{arrow} {label}{mark(off)} "


class Prompt:
    """Comfortable, dependency-free line input. When stdlib `readline` is available it
    adds inline editing (←/→, Ctrl-A/E, Ctrl-U), recall of earlier messages (↑/↓) and a
    history that persists across sessions. Multi-line entry: end a line with a backslash
    '\\' to continue on the next line. It degrades gracefully to a plain input() prompt
    when readline or colour is unavailable, so it stays pure stdlib and works headless.

    Presentation only: it reads a line of text and returns it - no agent logic, no tool
    calls. A `reader` can be injected (defaulting to builtins.input) so the multi-line
    and hint behaviour is testable without a real terminal."""

    def __init__(self, *, color: bool = True, stream=None, history_file=None,
                 reader=None):
        self.color = color
        self.stream = stream or sys.stdout
        self.reader = reader or input
        # Only drive real readline when using the real input(); an injected reader
        # (tests) must not touch history files or emit ignore-markers.
        self._rl = _readline if reader is None else None
        self._hist_path = str(history_file) if history_file else None
        self._hinted = False
        if self._rl and self._hist_path:
            try:
                self._rl.read_history_file(self._hist_path)
            except Exception:
                pass
            try:
                self._rl.set_history_length(1000)
            except Exception:
                pass

    def _save_history(self) -> None:
        if self._rl and self._hist_path:
            try:
                self._rl.write_history_file(self._hist_path)
            except Exception:
                pass

    def hint(self, text: str) -> None:
        try:
            self.stream.write(_paint("  " + text, "gray", color=self.color) + "\n")
            self.stream.flush()
        except Exception:
            pass

    def ask(self, label: str = "you", hint: "str | None" = None) -> str:
        """Read one (possibly multi-line) message and return it stripped. EOF/Ctrl-D
        returns '' (never raises). A trailing backslash continues on the next line. The
        usage hint is shown at most once per Prompt (first call), so later turns stay
        clean."""
        try:
            self.stream.write("\n")  # a little breathing room before each prompt
            self.stream.flush()
        except Exception:
            pass
        if hint is None and not self._hinted:
            hint = ("Enter sends · end a line with \\ for a new line · "
                    "/paste adds your last screenshot · type exit to leave")
        if hint:
            self.hint(hint)
        self._hinted = True
        first = format_prompt(label, color=self.color,
                              readline_active=bool(self._rl))
        cont = format_prompt("\u2026", color=self.color,
                             readline_active=bool(self._rl))
        parts, prompt = [], first
        while True:
            try:
                line = self.reader(prompt)
            except EOFError:
                break
            if line.endswith("\\"):
                parts.append(line[:-1])
                prompt = cont
                continue
            parts.append(line)
            break
        self._save_history()
        return "\n".join(parts).strip()


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
        # Line-buffered streaming state, so a markdown table can be rendered inline as a
        # clean box table the moment it completes (no cursor tricks, no duplication).
        self._sbuf = ""
        self._hold = None
        self._tbl = None
        self._said_prefix = False
        # Live elapsed-time spinner for long tool calls (real TTY only).
        self._spin_stop = None
        self._spin_thread = None
        # Lazily created comfortable input prompt (readline editing + history).
        self._prompt = None

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

    def _tty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except Exception:
            return False

    def _start_spinner(self, label: str) -> None:
        # A background thread that, after a short quiet delay (so fast tools stay silent),
        # shows a spinner + elapsed seconds on its own line. Real TTY only, so piped logs
        # and tests stay clean and deterministic.
        if not self._tty() or self._spin_stop is not None:
            return
        stop = threading.Event()
        stream, color = self.stream, self.color

        def run():
            if stop.wait(1.0):
                return
            frames = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
            start, i = time.monotonic(), 0
            while not stop.is_set():
                el = time.monotonic() - start
                msg = _paint(f"  {frames[i % len(frames)]} {label} {el:0.0f}s", "gray", color=color)
                try:
                    stream.write("\r" + msg + "   ")
                    stream.flush()
                except Exception:
                    return
                i += 1
                if stop.wait(0.2):
                    break
            try:
                stream.write("\r" + " " * 52 + "\r")
                stream.flush()
            except Exception:
                pass

        self._spin_stop = stop
        self._spin_thread = threading.Thread(target=run, daemon=True)
        self._spin_thread.start()

    def _stop_spinner(self) -> None:
        if self._spin_stop is not None:
            self._spin_stop.set()
        if self._spin_thread is not None:
            self._spin_thread.join(timeout=1.5)
        self._spin_stop = None
        self._spin_thread = None

    def tool_running(self, name: str = "") -> None:
        """Called by the runtime AFTER approval, just before a potentially slow op runs, so
        a long shell / fetch shows a live elapsed timer instead of a frozen screen."""
        self._start_spinner(name or "running")

    def header(self) -> None:
        self._w(format_header(self.mode, self.identity, self.release, color=self.color))

    def welcome(self, usage: bool = True) -> None:
        """One-time branded start screen: logo, run identity and a short how-to."""
        logo = None
        pref = os.environ.get("EVA_TUI_LOGO", "")
        if pref != "0" and (pref == "1" or (self.color and self._tty())):
            cols = shutil.get_terminal_size((80, 24)).columns
            logo = _load_logo(10_000 if pref == "1" else cols - 1)
        self._w(format_welcome(self.mode, self.identity, self.release,
                               color=self.color, usage=usage, logo=logo))

    def slash_help(self) -> None:
        """Render the in-chat command list (response to a local /help command)."""
        self._w(format_slash_help(color=self.color))

    def notice(self, msg: str) -> None:
        """A small dim status line for in-chat command feedback (/model, /provider, /resume)."""
        self._w(_paint("  " + msg, "gray", color=self.color))

    def ask(self, label: str = "you", hint: "str | None" = None,
            history_file=None) -> str:
        """Prompt the human for a line of input using the comfortable, dependency-free
        Prompt (inline editing + cross-session history via stdlib readline, multi-line
        via a trailing backslash). Reuses one Prompt per view so history + the
        shown-once hint persist across turns. Returns the message stripped; '' on EOF or
        an empty line."""
        if self._prompt is None:
            self._prompt = Prompt(color=self.color, stream=self.stream,
                                  history_file=history_file)
        return self._prompt.ask(label=label, hint=hint)

    def session_overview(self, rows, current=None) -> None:
        """Surface resumable work sessions on the start screen so the user can pick up a
        prior session without running --list first. rows = [(id, n_events, first_task,
        is_latest), ...]; the brand-new `current` session is excluded."""
        others = [r for r in rows if r[0] != current]
        if not others:
            return
        self._w(_paint("  Resume a previous session:", "bold", color=self.color))
        for sid, n, first, is_latest in others[-6:]:
            mark = _paint(" *", "green", color=self.color) if is_latest else "  "
            meta = _paint(f"  {n} events  ", "gray", color=self.color)
            self._w("  " + _paint(sid, "cyan", color=self.color) + mark + meta
                    + _clip(first, 50))
        self._w(_paint("  \u2192 work resume <id>   (or `work resume` for the most recent)",
                       "gray", color=self.color) + "\n")

    def say(self, text: str) -> None:
        # Reconcile the end of a streamed turn: flush any buffered partial line, a held
        # table candidate and an open table, then reset.
        if self._streaming:
            if self._sbuf:
                self._feed_line(self._sbuf)
                self._sbuf = ""
            if self._hold is not None:
                self._emit_line(self._hold)
                self._hold = None
            if self._tbl is not None:
                self._flush_table()
            if not self._said_prefix and text and text.strip():
                self._w(format_say(text, color=self.color))
            self._streaming = False
            self._said_prefix = False
            return
        if text and text.strip():
            self._w(format_say(text, color=self.color))

    def on_say_delta(self, chunk: str) -> None:
        """Render an assistant text fragment as it streams in, LINE-BUFFERED so a markdown
        table can be shown as a clean box table (not raw pipes) the moment it completes -
        no cursor tricks, no duplication. Prose lines stream as they finish. Tool calls are
        NOT streamed here; only free text."""
        if not chunk:
            return
        if not self._streaming:
            self._streaming = True
            self._sbuf = ""
            self._hold = None
            self._tbl = None
            self._said_prefix = False
        self._sbuf += chunk
        while "\n" in self._sbuf:
            line, self._sbuf = self._sbuf.split("\n", 1)
            self._feed_line(line)

    def _feed_line(self, line: str) -> None:
        # A tiny state machine over completed lines: collect a markdown-table block (header
        # + `---` separator + rows) and render it as a box table; stream every other line as
        # prose. One line of lookahead is needed (a `|...|` line is only a table once the
        # NEXT line is the separator), hence the held candidate.
        if self._tbl is not None:
            if _is_table_row(line):
                self._tbl.append(line)
                return
            self._flush_table()
        if self._hold is not None:
            if _is_table_sep(line):
                self._tbl = [self._hold, line]
                self._hold = None
                return
            self._emit_line(self._hold)
            self._hold = None
        if _is_table_row(line):
            self._hold = line
        else:
            self._emit_line(line)

    def _emit_line(self, line: str) -> None:
        if not self._said_prefix:
            self._raw(_paint("● EVA ", "bold", "magenta", color=self.color))
            self._said_prefix = True
        self._w(line)

    def _flush_table(self) -> None:
        if self._tbl is None:
            return
        if not self._said_prefix:
            self._raw(_paint("● EVA ", "bold", "magenta", color=self.color))
            self._said_prefix = True
            self._raw("\n")
        try:
            self._w(_render_table(self._tbl))
        except Exception:
            for row in self._tbl:
                self._w(row)
        self._tbl = None

    def tool_call(self, call) -> None:
        self._w(format_tool_call(call, color=self.color, full=self.full))

    def observation(self, obs) -> None:
        self._stop_spinner()
        # finish is already shown as the tool-call line; don't echo it again.
        if (getattr(obs, "name", "") or "") == "finish":
            return
        self._w(format_observation(obs, color=self.color, full=self.full))

    def error(self, stage: str, exc) -> None:
        self._stop_spinner()
        if self._streaming:
            self._raw("\n")
            self._streaming = False
        self._w(format_error(stage, exc, color=self.color))

    def replay(self, events, clean_user=None) -> None:
        """Render a previously-saved conversation so a human RESUMING a session can pick
        up the thread: prior user messages, EVA's replies and what it did (tool calls),
        in order. Presentation only - it reads the loaded event log, runs nothing."""
        self._w("\n" + _paint("\u2500\u2500 resuming \u00b7 previous conversation \u2500\u2500",
                              "gray", color=self.color))
        for ev in events:
            role = getattr(ev, "role", "")
            if role == "system":
                continue
            if role == "user":
                text = getattr(ev, "content", "") or ""
                if clean_user:
                    text = clean_user(text)
                text = text.strip()
                if not text:
                    continue
                if len(text) > 2000:
                    text = text[:2000].rstrip() + "\u2026"
                self._w(_paint("\u276f you", "bold", "cyan", color=self.color) + " " + text)
            elif role == "assistant":
                say = (getattr(ev, "content", "") or "").strip()
                if say:
                    self._w(format_say(say, color=self.color))
                for c in (getattr(ev, "tool_calls", None) or []):
                    self._w(format_tool_call(c, color=self.color, full=self.full))
        self._w(_paint("\u2500\u2500 end of previous conversation \u2500\u2500",
                       "gray", color=self.color) + "\n")

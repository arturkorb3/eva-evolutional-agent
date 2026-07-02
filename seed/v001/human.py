#!/usr/bin/env python3
"""Human interaction + approval, as a swappable layer.

EVA-Core never calls input() directly. It asks a HumanInterface. That keeps the
agent independent of HOW a human answers (CLI today; web, TUI, API or a test
double tomorrow) and lets autonomous/CI runs proceed without blocking.

Two concerns are separated:
  - HumanInterface : confirm a yes-no (approvals).
  - ApprovalPolicy : decide WHEN a human must confirm a risky action.
"""
from __future__ import annotations

import base64
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _image_file_to_data_url(path):
    p = pathlib.Path(path)
    if not p.is_file() or p.suffix.lower() not in _IMAGE_EXTS:
        return None
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"url": f"data:{mime};base64,{data}"}


def _token_to_image(token, base_dir):
    s = str(token or "").strip().strip("\"'")
    if not s:
        return None
    if s.startswith("data:image/") and ";base64," in s:
        return {"url": s}
    p = pathlib.Path(s).expanduser()
    if not p.is_absolute():
        p = pathlib.Path(base_dir) / p
    return _image_file_to_data_url(p)


def latest_staged_image(base_dir):
    """Newest staged screenshot (clip-*.png) in base_dir, or None."""
    clips = sorted(pathlib.Path(base_dir).glob("clip-*.png"))
    return clips[-1] if clips else None


def running_in_container() -> bool:
    """True when EVA itself runs inside its Docker sandbox. A container can NOT read
    the host clipboard, so we must fall back to staged files there. Set explicitly via
    EVA_IN_CONTAINER, or detected via Docker's /.dockerenv marker."""
    if os.environ.get("EVA_IN_CONTAINER") == "1":
        return True
    if os.environ.get("EVA_IN_CONTAINER") == "0":
        return False
    return os.path.exists("/.dockerenv")


def _grab_windows(out: pathlib.Path) -> bool:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "if([System.Windows.Forms.Clipboard]::ContainsImage()){"
        "$i=[System.Windows.Forms.Clipboard]::GetImage();"
        f"$i.Save('{out}',[System.Drawing.Imaging.ImageFormat]::Png);exit 0"
        "}else{exit 1}"
    )
    exe = shutil.which("powershell.exe") or shutil.which("pwsh") or "powershell.exe"
    return subprocess.run([exe, "-NoProfile", "-STA", "-Command", ps],
                          capture_output=True, timeout=15).returncode == 0


def _grab_macos(out: pathlib.Path) -> bool:
    if shutil.which("pngpaste"):
        return subprocess.run(["pngpaste", str(out)],
                              capture_output=True, timeout=15).returncode == 0
    script = ('try\n  set d to (the clipboard as «class PNGf»)\n'
              '  set f to open for access POSIX file "%s" with write permission\n'
              '  write d to f\n  close access f\non error\n  return "no"\nend try' % out)
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, timeout=15).returncode == 0


def _grab_linux(out: pathlib.Path) -> bool:
    for tool, args in (("wl-paste", ["-t", "image/png"]),
                       ("xclip", ["-selection", "clipboard", "-t", "image/png", "-o"])):
        if shutil.which(tool):
            with open(out, "wb") as f:
                if subprocess.run([tool, *args], stdout=f,
                                  stderr=subprocess.DEVNULL, timeout=15).returncode == 0:
                    return True
    return False


def grab_clipboard_image(dest_dir):
    """Best-effort HOST clipboard grab: if an image sits on the host clipboard, save it
    as clip-<ts>.png in dest_dir and return the path. Returns None inside the container
    (it cannot see the host clipboard) or when no image / no tooling is available.

    This is the direct-attach path the review asks for; in the sandbox EVA still relies
    on the host wrapper staging a file (see latest_staged_image)."""
    if running_in_container():
        return None
    dest_dir = pathlib.Path(dest_dir)
    out = dest_dir / ("clip-" + time.strftime("%Y%m%d-%H%M%S") + ".png")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            ok = _grab_windows(out)
        elif sys.platform == "darwin":
            ok = _grab_macos(out)
        else:
            ok = _grab_linux(out)
    except Exception:
        ok = False
    if ok and out.is_file() and out.stat().st_size > 0:
        return out
    try:
        if out.exists():
            out.unlink()
    except Exception:
        pass
    return None


def extract_image_attachments(text, base_dir="."):
    """Provider-neutral: pull image attachments out of a CLI message.

    Returns (clean_text, images) where each image is a {"url": "data:..."} dict
    (a data URL - a generic container, NOT provider-specific; adapters translate
    it to their own wire format). Supported forms in the user's text:
      - `/paste`               -> the newest staged screenshot (clip-*.png),
      - data:image/...;base64  -> a pasted data URL,
      - a local image path,
      - Markdown ![alt](path).
    """
    text = str(text or "")
    images = []

    if "/paste" in text:
        # On the host, grab the clipboard image directly (the direct-attach path).
        # In the sandbox, fall back to whatever the host wrapper staged for us.
        latest = None
        if not running_in_container():
            latest = grab_clipboard_image(base_dir)
        if latest is None:
            latest = latest_staged_image(base_dir)
        text = re.sub(r"(?<!\S)/paste(?!\S)", (latest.name if latest else ""), text)

    def _md(match):
        img = _token_to_image(match.group(1), base_dir)
        if img:
            images.append(img)
            return "[image]"
        return match.group(0)

    text = _MARKDOWN_IMAGE_RE.sub(_md, text)

    kept = []
    for tok in text.split():
        img = _token_to_image(tok, base_dir)
        if img:
            images.append(img)
            kept.append("[image]")
        else:
            kept.append(tok)
    return " ".join(kept).strip(), images


class HumanInterface:
    """Abstract human channel."""

    interactive = True

    def confirm(self, prompt: str, detail: "str | None" = None) -> bool:
        raise NotImplementedError


class CliHumanInterface(HumanInterface):
    """Local CLI: prompts on stdin/stdout."""

    interactive = True

    def confirm(self, prompt: str, detail: "str | None" = None) -> bool:
        # When a `detail` (e.g. the full shell command) is available, offer an 'f'
        # (full) key: it reveals the detail on demand and re-prompts, so the normal
        # stream stays compact but nothing is ever approved blind.
        opts = "[y/N/f]" if detail else "[y/N]"
        while True:
            try:
                sys.stdout.flush()
                ans = input(f"{prompt} {opts} ").strip().lower()
            except EOFError:
                return False
            if detail and ans in ("f", "full"):
                print("  " + str(detail).replace("\n", "\n  "))
                continue
            return ans == "y"


class AutoHumanInterface(HumanInterface):
    """Non-interactive (CI / --yes / autonomous evolve).

    Confirmations follow a fixed default so the agent proceeds without blocking.
    """

    interactive = False

    def __init__(self, *, default_confirm: bool = True):
        self.default_confirm = default_confirm

    def confirm(self, prompt: str, detail: "str | None" = None) -> bool:
        print(prompt + (" [auto-yes]" if self.default_confirm else " [auto-no]"))
        return self.default_confirm


# Risk levels an action can carry.
RISK_NONE = "none"      # read-only, always allowed
RISK_WRITE = "write"    # mutates workspace / candidate files
RISK_SHELL = "shell"    # arbitrary shell that is not read-only
RISK_PROMOTE = "promote"  # promotion / release pointer changes


class ApprovalPolicy:
    """Decides whether a human must confirm an action of a given risk.

        mode = "never"    autonomous / CI: never ask (RISK_NONE auto-allowed).
        mode = "on-risk"  ask only for write/shell/promote (the default).
        mode = "always"   confirm every non-trivial action.
    """

    def __init__(self, human: HumanInterface, *, mode: str = "on-risk",
                 allow_shell: bool = False):
        self.human = human
        self.mode = mode
        self.allow_shell = allow_shell

    def approve(self, risk: str, prompt: str, detail: "str | None" = None) -> bool:
        if risk == RISK_NONE:
            return True
        if risk == RISK_SHELL and self.allow_shell:
            return True
        if self.mode == "never":
            return True
        if self.mode == "on-risk" and risk == RISK_NONE:
            return True
        # `detail` (the full command/content) is passed through, not dumped: the human
        # can reveal it on demand with the 'f' key before deciding.
        return self.human.confirm(prompt, detail)

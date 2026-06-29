#!/usr/bin/env python3
"""Human interaction + approval, as a swappable layer.

EVA-Core never calls input() directly. It asks a HumanInterface. That keeps the
agent independent of HOW a human answers (CLI today; web, TUI, API or a test
double tomorrow) and lets autonomous/CI runs proceed without blocking.

Two concerns are separated:
  - HumanInterface : ask an open question / confirm a yes-no.
  - ApprovalPolicy : decide WHEN a human must confirm a risky action.
"""
from __future__ import annotations

import base64
import mimetypes
import pathlib
import re


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

    def ask(self, question: str) -> str:
        raise NotImplementedError

    def confirm(self, prompt: str) -> bool:
        raise NotImplementedError


class CliHumanInterface(HumanInterface):
    """Local CLI: prompts on stdin/stdout."""

    interactive = True

    def ask(self, question: str) -> str:
        print("\nAGENT ASKS:", question)
        try:
            return input("Your answer: ").strip()
        except EOFError:
            return ""

    def confirm(self, prompt: str) -> bool:
        try:
            return input(prompt + " [y/N] ").strip().lower() == "y"
        except EOFError:
            return False


class AutoHumanInterface(HumanInterface):
    """Non-interactive (CI / --yes / autonomous evolve).

    Open questions get a neutral "no human available" answer so the agent
    proceeds with its best assumption; confirmations follow a fixed default.
    """

    interactive = False

    def __init__(self, *, default_confirm: bool = True):
        self.default_confirm = default_confirm

    def ask(self, question: str) -> str:
        print("\nAGENT ASKS (auto):", question)
        return "No interactive user available. Proceed with your best assumption."

    def confirm(self, prompt: str) -> bool:
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

    def approve(self, risk: str, prompt: str) -> bool:
        if risk == RISK_NONE:
            return True
        if risk == RISK_SHELL and self.allow_shell:
            return True
        if self.mode == "never":
            return True
        if self.mode == "on-risk" and risk == RISK_NONE:
            return True
        return self.human.confirm(prompt)

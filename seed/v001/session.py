#!/usr/bin/env python3
"""Session store: the canonical, append-only event log = the source of truth.

EVA can reconstruct everything that happened from this log. Provider-side
conversation state (e.g. a Responses previous_response_id) may later be added as
a *cache* on top, but it is never the truth.

Compaction lives here too (its own layer): the full log is the record; a budget
view trims older turns into a deterministic, LLM-free summary for sending.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib

import context
from core import Event


class SessionStore:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.meta = self.path.parent / (self.path.name + ".meta")
        self._events: list[Event] = []

    # -- canonical log ----------------------------------------------------- #
    def events(self) -> list[Event]:
        return self._events

    def append(self, event: Event) -> None:
        self._events.append(event)
        self._persist(event)

    # -- blob store: keep fat image data-URLs OUT of the JSONL log --------- #
    # Images are provider-neutral {"url": "data:..."} in memory (adapters render them
    # directly). On disk we externalize each to state/blobs/<sha256>.<ext> and store a
    # tiny {"ref": ..., "mime": ...} instead, so the append-only log stays small and
    # deduplicates identical images. load() rehydrates refs back to data-URLs.
    def _blob_dir(self) -> pathlib.Path:
        return self.path.parent / "blobs"

    def _externalize_images(self, d: dict) -> dict:
        imgs = d.get("images") or []
        if not imgs:
            return d
        out = []
        for img in imgs:
            url = img.get("url") if isinstance(img, dict) else None
            if url and url.startswith("data:") and ";base64," in url:
                header, b64 = url.split(";base64,", 1)
                mime = header[5:] or "image/png"
                ext = (mime.split("/")[-1].split("+")[0] or "bin")
                try:
                    raw = base64.b64decode(b64)
                except Exception:
                    out.append(img)
                    continue
                sha = hashlib.sha256(raw).hexdigest()
                bd = self._blob_dir()
                bd.mkdir(parents=True, exist_ok=True)
                p = bd / f"{sha}.{ext}"
                if not p.exists():
                    p.write_bytes(raw)
                out.append({"ref": f"blobs/{sha}.{ext}", "mime": mime})
            else:
                out.append(img)
        d = dict(d)
        d["images"] = out
        return d

    def _rehydrate_images(self, d: dict) -> dict:
        imgs = d.get("images") or []
        if not imgs:
            return d
        out = []
        for img in imgs:
            if isinstance(img, dict) and img.get("ref") and not img.get("url"):
                try:
                    raw = (self.path.parent / img["ref"]).read_bytes()
                    mime = img.get("mime") or "image/png"
                    b64 = base64.b64encode(raw).decode("ascii")
                    out.append({"url": f"data:{mime};base64,{b64}"})
                except Exception:
                    out.append(img)  # keep the ref if the blob is missing
            else:
                out.append(img)
        d = dict(d)
        d["images"] = out
        return d

    def seed(self, events: list[Event], mode: "str | None" = None) -> None:
        """Initialise a fresh session (system + first task) and persist it."""
        self._events = list(events)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            for ev in self._events:
                f.write(json.dumps(self._externalize_images(ev.to_dict()),
                                   ensure_ascii=False) + "\n")
        self._write_meta(mode)

    def _write_meta(self, mode) -> None:
        try:
            self.meta.write_text(json.dumps({"mode": mode, "done": False}),
                                 encoding="utf-8")
        except Exception:
            pass

    def resumable(self, mode) -> bool:
        # Resumable only if a session for the SAME mode exists and was not cleared
        # by a clean finish (clear() removes both the log and this meta).
        if not self.path.exists() or not self.meta.exists():
            return False
        try:
            meta = json.loads(self.meta.read_text(encoding="utf-8"))
        except Exception:
            return False
        return meta.get("mode") == mode and not meta.get("done")

    def _persist(self, event: Event) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(self._externalize_images(event.to_dict()),
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    # -- resume / clear ---------------------------------------------------- #
    def load(self) -> bool:
        if not self.path.exists():
            return False
        events: list[Event] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(Event.from_dict(self._rehydrate_images(json.loads(line))))
            except Exception:
                continue
        if not events:
            return False
        self._events = events
        return True

    def clear(self) -> None:
        self._events = []
        for p in (self.path, self.meta):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    # -- compaction (own layer: context.py) ------------------------------- #
    def chars(self) -> int:
        return context.chars(self._events)

    def compact_view(self, budget: int, keep: int) -> list[Event]:
        """Budgeted view of the canonical log for sending to the model. The policy
        lives in context.py; the store just feeds it the full log."""
        return context.compact(self._events, budget, keep)

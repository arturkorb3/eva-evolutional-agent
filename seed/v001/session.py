#!/usr/bin/env python3
"""Session store: the canonical, append-only event log = the source of truth.

EVA can reconstruct everything that happened from this log. Provider-side
conversation state (e.g. a Responses previous_response_id) may later be added as
a *cache* on top, but it is never the truth.

Compaction lives here too (its own layer): the full log is the record; a budget
view trims older turns into a deterministic, LLM-free summary for sending.
"""
from __future__ import annotations

import json
import pathlib

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

    def seed(self, events: list[Event], mode: "str | None" = None) -> None:
        """Initialise a fresh session (system + first task) and persist it."""
        self._events = list(events)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            for ev in self._events:
                f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
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
                f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
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
                events.append(Event.from_dict(json.loads(line)))
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

    # -- compaction (own layer) ------------------------------------------- #
    def chars(self) -> int:
        return sum(len(e.content or "") for e in self._events)

    def compact_view(self, budget: int, keep: int) -> list[Event]:
        """Send the full log while it fits `budget`; past that send
        system + first task + a condensed summary of dropped turns + the last
        `keep` events verbatim. Deterministic and LLM-free (zero extra cost)."""
        evs = self._events
        if self.chars() <= budget or len(evs) <= keep + 2:
            return list(evs)

        system = evs[0]
        first = evs[1]
        first_trimmed = Event(role=first.role, content=(first.content or "")[:800])
        tail = evs[-keep:]
        dropped = evs[2:-keep]

        view = [system, first_trimmed]
        summary = _summarize(dropped)
        if summary:
            view.append(Event(role="user", content=summary))
        view.extend(tail)
        return view


def _summarize(dropped: list[Event]) -> str | None:
    lines = []
    step = 0
    for ev in dropped:
        if ev.role == "assistant":
            step += 1
            say = (ev.content or "")[:80]
            tool = ev.tool_calls[0].name if ev.tool_calls else "-"
            lines.append(f"{step}. {say} [{tool}]")
    if not lines:
        return None
    return "Progress so far (older steps, condensed):\n" + "\n".join(lines)

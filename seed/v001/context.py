#!/usr/bin/env python3
"""Context compaction: its own policy layer.

The canonical event log (session.py) is the TRUTH. What gets SENT to the model is a
compacted VIEW of it - this module owns that transformation, kept separate so the
policy can grow (full / last-n / deterministic summary / tool-result compression /
image-reference policy / mode-specific budgets) without touching the session store or
the turn loop.

Deterministic and LLM-free: compaction never calls a model, so it adds zero token cost
and stays reproducible for tests, resume and audit.
"""
from __future__ import annotations

from core import Event


def chars(events) -> int:
    """Total character weight of an event list (the budget metric)."""
    return sum(len(e.content or "") for e in events)


def summarize_dropped(dropped) -> "str | None":
    """A deterministic, LLM-free progress summary of the turns we drop from the sent
    view. One line per assistant step: its short say + the tool it used."""
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


def compact(events, budget: int, keep: int) -> "list[Event]":
    """Send the full log while it fits `budget`; past that send system + first task +
    a condensed summary of the dropped middle + the last `keep` events verbatim."""
    evs = list(events)
    if chars(evs) <= budget or len(evs) <= keep + 2:
        return evs

    system = evs[0]
    first = evs[1]
    first_trimmed = Event(role=first.role, content=(first.content or "")[:800])
    tail = evs[-keep:]
    dropped = evs[2:-keep]

    view = [system, first_trimmed]
    summary = summarize_dropped(dropped)
    if summary:
        view.append(Event(role="user", content=summary))
    view.extend(tail)
    return view

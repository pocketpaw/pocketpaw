# ee/pocketpaw_ee/sites/app_plan.py — reads a project site's PLAN.md (the
# ``sites-app-plan`` skill's milestone checklist) to answer one question: which
# milestone is next, or is the plan finished?
#
# Two callers, both in ``cloud/chat/runs/run_core``: the turn-cap raise (a project
# turn with an unfinished plan gets ``sites_app_build_max_turns``) and the honest
# stop note (a run cut off by its turn limit says which milestone is next).
#
# Pure text parsing, no I/O. The agent writes PLAN.md freehand, so the parse is
# lenient: any markdown checkbox counts, and the label is the nearest "M<n>" name
# (the item's own text, its parent item, or the heading above it), else the
# unticked item's own text.
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: The plan file at the repo root (same name as ``project_tools.PLAN_FILE``).
PLAN_FILE = "PLAN.md"

_CHECKBOX_RE = re.compile(
    r"^(?P<indent>\s*)(?:[-*+]|\d+[.)])\s+\[(?P<mark>[ xX])\]\s*(?P<text>.*)$"
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.*?)\s*#*\s*$")
_MILESTONE_RE = re.compile(r"\bM\d+\b")
_MAX_LABEL = 80


def _clean(text: str) -> str:
    text = re.sub(r"[*_`]+", "", text).strip(" :-–—")
    text = " ".join(text.split())
    return text[: _MAX_LABEL - 3] + "..." if len(text) > _MAX_LABEL else text


def next_milestone(plan: str) -> str | None:
    """The label of the first milestone with an unticked checkbox, or None when the
    plan has no unticked checkbox (finished, or no checklist at all)."""
    heading = ""
    parents: list[tuple[int, str]] = []  # (indent, text) of the open checkbox items
    for line in plan.splitlines():
        head = _HEADING_RE.match(line)
        if head:
            heading = head.group("text")
            parents = []
            continue
        box = _CHECKBOX_RE.match(line)
        if not box:
            continue
        indent = len(box.group("indent").expandtabs(4))
        text = box.group("text")
        while parents and parents[-1][0] >= indent:
            parents.pop()
        if box.group("mark") == " ":
            for candidate in (text, *(t for _, t in reversed(parents)), heading):
                if _MILESTONE_RE.search(candidate):
                    return _clean(candidate)
            return _clean(text) or "the next unticked item"
        parents.append((indent, text))
    return None


def next_milestone_in(source: Mapping[str, Any] | None) -> str | None:
    """:func:`next_milestone` of a project source map's PLAN.md (None without one)."""
    plan = (source or {}).get(PLAN_FILE)
    return next_milestone(plan) if isinstance(plan, str) else None


__all__ = ["PLAN_FILE", "next_milestone", "next_milestone_in"]

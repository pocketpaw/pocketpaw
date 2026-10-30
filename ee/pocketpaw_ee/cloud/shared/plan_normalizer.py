# plan_normalizer.py — turns a backend's plan-tool call into the canonical
# ``agent.plan_updated`` payload (HTN-5).
# Created: 2026-08-15 (HTN-5) — ships ONE normalizer, ``write_plan`` from
#   pydantic-ai-harness, verified against the installed
#   ``pydantic_ai_harness/planning/_toolset.py`` rather than inferred. The
#   registry exists so ``write_todos`` (deep_agents) and ``TodoWrite``
#   (claude_agent_sdk) arrive in HTN-6 as a ``register_plan_normalizer`` call
#   instead of a refactor. ``TodoWrite``'s payload is a CLI-side builtin
#   compiled into ``claude_agent_sdk/_bundled/claude.exe`` and has NOT been
#   captured live, so no normalizer is guessed for it here — a guessed shape
#   would give the default backend's users a panel that is silently blank.
"""Normalize plan-tool arguments into the canonical plan wire shape.

Three agent backends express the same concept with incompatible schemas, so the
bridge must not learn any of them. It asks this module two questions — "is this
a plan tool?" and "what changed?" — and emits whatever comes back.

**Whole-list replacement.** ``write_plan`` overwrites its ``PlanState`` outright
on every call: the model resends the entire ordered list, with no indices and no
deltas. The wire contract inherits that, so a receiver REPLACES its state and
never merges. There is deliberately no diffing anywhere in this module, and
adding some later would break the contract rather than extend it.

**Item ids are positional.** ``write_plan`` carries no ids. Under whole-list
replacement the index *is* the identity, so ``id`` is synthesised as ``"1"``,
``"2"``, … — stable within one event, not across events. Do not key animations
or persistence off it.

**Status is the superset.** ``pending | in_progress | completed | cancelled``.
Backends with three states simply never emit ``cancelled``; anything outside the
four falls back to ``pending`` so a receiver can switch exhaustively.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The superset every source normalizes into. ``cancelled`` comes from
#: ``write_plan``'s ``TaskStatus``; the three-state sources never emit it.
PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
DEFAULT_STATUS = "pending"

#: Bounds on what reaches a WebSocket broadcast. The plan is model-authored
#: free text, so neither the item count nor the per-item length is trustworthy.
MAX_ITEMS = 100
MAX_CONTENT_CHARS = 500


@dataclass(frozen=True)
class PlanItem:
    """One canonical plan step: what the wire carries, nothing more."""

    id: str
    content: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "content": self.content, "status": self.status}


#: A normalizer takes one tool call's argument dict and returns the ordered
#: plan. It returns ``[]`` — never raises — when the arguments are unusable.
PlanNormalizer = Callable[[dict[str, Any]], list[PlanItem]]

_REGISTRY: dict[str, PlanNormalizer] = {}


def register_plan_normalizer(tool_name: str, normalizer: PlanNormalizer) -> None:
    """Register *normalizer* as the reader for *tool_name*'s arguments."""
    _REGISTRY[tool_name] = normalizer


def is_plan_tool(tool_name: str) -> bool:
    """True when *tool_name* has a registered normalizer."""
    return bool(tool_name) and tool_name in _REGISTRY


def plan_tools() -> frozenset[str]:
    """Every currently-registered plan tool name."""
    return frozenset(_REGISTRY)


def normalize_plan(tool_name: str, tool_input: Any) -> list[PlanItem]:
    """Normalize one plan-tool call, or return ``[]``.

    Never raises: a malformed plan must not take down the response stream, and
    an empty result routes the caller to its ordinary tool signal instead.
    """
    normalizer = _REGISTRY.get(tool_name)
    if normalizer is None:
        return []
    try:
        return normalizer(tool_input if isinstance(tool_input, dict) else {})
    except Exception:
        logger.debug("Plan normalization failed for %s", tool_name, exc_info=True)
        return []


def _items_from_content_status_list(raw: Any) -> list[PlanItem]:
    """Shape an ordered ``[{content, status}, ...]`` list into canonical items.

    Shared rather than inlined because ``write_todos`` (deep_agents) uses the
    same shape — HTN-6 reuses this verbatim and only registers a different key.
    """
    if isinstance(raw, str):
        # Defensive: a backend that hands tool arguments through un-decoded
        # gives us the JSON text rather than the list.
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []

    items: list[PlanItem] = []
    for entry in raw[:MAX_ITEMS]:
        if isinstance(entry, dict):
            content = entry.get("content")
            status = entry.get("status", DEFAULT_STATUS)
        elif isinstance(entry, str):
            content, status = entry, DEFAULT_STATUS
        else:
            continue

        # Collapse whitespace: a plan step renders as one line in a panel, and
        # a newline in model-authored text would otherwise break the row.
        content = " ".join(str(content or "").split())[:MAX_CONTENT_CHARS]
        if not content:
            # Nothing renderable. Dropping it keeps ids contiguous and keeps
            # ``total`` honest about what the panel can actually show.
            continue

        # ``status`` may arrive as a ``TaskStatus`` enum member if a backend
        # validated the arguments before announcing the call.
        status = getattr(status, "value", status)
        status = str(status or DEFAULT_STATUS).strip().lower()
        if status not in PLAN_STATUSES:
            logger.debug("Unknown plan status %r; treating as %s", status, DEFAULT_STATUS)
            status = DEFAULT_STATUS

        items.append(PlanItem(id=str(len(items) + 1), content=content, status=status))
    return items


def _normalize_write_plan(tool_input: dict[str, Any]) -> list[PlanItem]:
    """``write_plan(items: list[PlanItem])`` — pydantic-ai-harness planning.

    Read off ``pydantic_ai_harness/planning/_toolset.py``: one argument,
    ``items``, an ordered list of ``{content, status}`` where ``status`` is the
    four-value ``TaskStatus``. The tool assigns ``self._state.items = list(items)``
    — the overwrite that the whole-list-replacement contract inherits.
    """
    return _items_from_content_status_list(tool_input.get("items"))


register_plan_normalizer("write_plan", _normalize_write_plan)


def plan_progress(items: list[PlanItem]) -> dict[str, int]:
    """``{completed, total}`` for *items*.

    ``cancelled`` counts toward ``total`` but not ``completed``, matching the
    harness's own ``render_plan`` summary so the panel and the string the model
    reads back cannot disagree.
    """
    return {
        "completed": sum(1 for item in items if item.status == "completed"),
        "total": len(items),
    }


def plan_fingerprint(items: list[PlanItem]) -> str:
    """A stable hash of the plan, used to emit only on actual change."""
    payload = json.dumps([item.as_dict() for item in items], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanObservation:
    """What the caller should do with one observed tool call.

    ``recognized`` false means "this was not a usable plan call" — the caller
    should fall back to its ordinary tool signal. ``recognized`` true with a
    ``payload`` of ``None`` means "a plan call that changed nothing" — the
    caller should emit nothing at all.
    """

    recognized: bool
    payload: dict[str, Any] | None


UNRECOGNIZED = PlanObservation(recognized=False, payload=None)


class PlanTracker:
    """Per-run plan state: the monotonic ``seq`` and the change fingerprint.

    One instance per agent run, held on the stack of the run and discarded with
    it, so there is no registry to leak and no cross-run bleed.

    It exists for two reasons:

    * **``write_plan`` fires twice per step** — the tool's own docstring tells
      the model to call it "when you start and when you finish a step" — and
      resends the whole list each time. Without the fingerprint the channel
      floods and the panel flickers through identical renders.
    * **``seq`` lets a receiver drop a stale plan.** Whether the realtime layer
      guarantees per-run ordering is unconfirmed; a receiver that ignores any
      ``seq`` below the one it holds is correct either way.
    """

    __slots__ = ("_run_id", "_seq", "_fingerprint")

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._seq = 0
        self._fingerprint = ""

    @property
    def seq(self) -> int:
        """The ``seq`` of the last emitted update (0 before the first)."""
        return self._seq

    def observe(self, tool_name: str, tool_input: Any) -> PlanObservation:
        """Fold one tool call into the run's plan state."""
        if not is_plan_tool(tool_name):
            return UNRECOGNIZED

        items = normalize_plan(tool_name, tool_input)
        if not items:
            # A registered plan tool whose arguments we could not read. Today
            # that is the common case on pydantic-ai: the backend announces the
            # call from ``PartStartEvent`` with ``input={}`` before the
            # arguments finish streaming (HTN-9 fixes that). Reporting it as
            # unrecognized keeps the caller on its ordinary tool signal, so the
            # surface degrades to today's behaviour instead of going silent.
            return UNRECOGNIZED

        fingerprint = plan_fingerprint(items)
        if fingerprint == self._fingerprint:
            return PlanObservation(recognized=True, payload=None)

        self._fingerprint = fingerprint
        self._seq += 1
        return PlanObservation(
            recognized=True,
            payload={
                "run_id": self._run_id,
                "seq": self._seq,
                "items": [item.as_dict() for item in items],
                "progress": plan_progress(items),
            },
        )

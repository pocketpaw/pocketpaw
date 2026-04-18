# ee/instinct/correction.py — Correction Loop data types.
# Created: 2026-04-12 (Move 1 PR-A) — Captures the diff between what the agent
# proposed and what the human approved so it can be used as a learning signal.
# Pairs with soul-protocol to form the correction loop: proposal → edit → soul
# remembers → next proposal improves.

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ee.fabric.models import _gen_id
from ee.instinct.models import Action

_CORRECTABLE_SCALAR_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "recommendation",
    "category",
    "priority",
)


class CorrectionPatch(BaseModel):
    """One field-level change between the proposed and approved action."""

    path: str
    before: Any
    after: Any


class Correction(BaseModel):
    """Captured when a human edits an Action before approving it.

    Stored alongside the approval and later consumed by soul-protocol's
    `observe()` + `recall()` to bias future proposals toward the actor's
    preferred shape.
    """

    id: str = Field(default_factory=lambda: _gen_id("cor"))
    action_id: str
    pocket_id: str
    actor: str
    patches: list[CorrectionPatch]
    context_summary: str
    action_title: str
    created_at: datetime = Field(default_factory=datetime.now)


def compute_patches(before: Action, after: Action) -> list[CorrectionPatch]:
    """Diff two Action snapshots and return the list of field changes.

    Only fields a human would meaningfully edit are compared:
    title/description/recommendation/category/priority as flat scalars, and
    the top-level keys of `parameters` (path = "parameters.<key>").

    `context` is intentionally skipped — it holds reasoning metadata, not
    action content, and will be captured separately by the decision-trace
    collector (Move 2).
    """
    patches: list[CorrectionPatch] = []

    for field in _CORRECTABLE_SCALAR_FIELDS:
        b = getattr(before, field)
        a = getattr(after, field)
        if _normalize(b) != _normalize(a):
            patches.append(CorrectionPatch(path=field, before=_normalize(b), after=_normalize(a)))

    before_params = before.parameters or {}
    after_params = after.parameters or {}
    for key in sorted(set(before_params) | set(after_params)):
        b_val = before_params.get(key)
        a_val = after_params.get(key)
        if b_val != a_val:
            patches.append(
                CorrectionPatch(path=f"parameters.{key}", before=b_val, after=a_val),
            )

    return patches


def summarize_correction(action: Action, patches: list[CorrectionPatch]) -> str:
    """Short natural-language summary used as a recall key by soul-protocol.

    Kept deliberately terse and deterministic — no LLM call on the hot path.
    Format: "<title> — edited <N> field(s): <path1>, <path2>, ..."
    """
    if not patches:
        return f"{action.title} — approved without edits"
    fields = ", ".join(p.path for p in patches[:5])
    more = f" (+{len(patches) - 5} more)" if len(patches) > 5 else ""
    return f"{action.title} — edited {len(patches)} field(s): {fields}{more}"


def _normalize(value: Any) -> Any:
    """Convert enums to their string values so patches serialize cleanly."""
    if hasattr(value, "value"):
        return value.value
    return value

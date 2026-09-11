# ee/pocketpaw_ee/cloud/models/workspace_turn_usage.py — the per-workspace
# daily agent-run counter.
#
# Created 2026-09-11 (feat/abuse-budgets) — every model call the product makes
# on the PLATFORM's account happens inside an agent run: the reply itself, and
# the tools a run may call (image generation, speech, OCR, translate,
# research). Bounding runs therefore bounds all of them, which is why this is
# one counter and not six.
#
# The credit quota in ``run_core`` already exists and is the right long-term
# ceiling, but it is gated on ``billing_enforced`` (False by default), so a
# deployment without billing configured has no bound on LLM spend for a
# signed-up account. Guests have one (``guest_budget``, 40 turns/day); until
# now a free signup had less protection than a guest.
#
# One row per workspace per UTC day, keyed on ``<workspace>:<YYYY-MM-DD>``,
# claimed with one atomic upsert. See ``workspace_upload_usage`` for the
# reasoning on the field names and on why this lives in ``cloud.models``.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WorkspaceTurnUsage(TimestampedDocument):
    """How many agent runs one workspace has started today."""

    #: ``<workspace>:<YYYY-MM-DD>``. UNIQUE — the upsert keys on it.
    key: Indexed(str, unique=True)  # type: ignore[valid-type]
    workspace: str
    day: str
    used: int = 0

    class Settings:
        name = "workspace_turn_usage"

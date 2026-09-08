# ee/pocketpaw_ee/cloud/models/studio_generation.py — one Studio generation.
#
# Created 2026-09-08 (feat/studio-history-store, SM-0). Replaces the
# ``~/.pocketpaw/studio/generations.jsonl`` history, which was ONE append-only
# file for the whole deployment with tenancy as a ``_workspace`` field filtered
# in Python on read. That shape failed four ways:
#
#   * every read decoded every tenant's entire history, unindexed and unrotated;
#   * ``backend`` and ``worker`` both mount the ``backend-data`` volume, so two
#     processes appended to one file (the reader already swallowed corrupt lines);
#   * the ``or _workspace is None`` clause in ``list_generations`` showed untagged
#     rows to EVERY workspace while its docstring claimed the opposite — here
#     tenancy is a query filter, so there is no untagged branch to fall through;
#   * append was the only write, so all eight ``_append_history`` call sites wrote
#     ``status="succeeded"`` and the four-value status enum only ever held one
#     value. Nothing existed while a job was queued or running and a failure left
#     no trace, which is why async video had nowhere to record "rendering".
#
# The sibling JSONL stores in ee/cloud are NOT precedent for keeping this one:
# ``outcomes``, ``trust_ledger`` and ``audit`` are append-only ledgers keyed per
# workspace, and ``trust_ledger`` is on the import-linter "Pockets" allowlist that
# forbids Beanie writes outright. A gallery listing is none of those — it is
# mutable content wanting query, filter, pagination and delete.
#
# ``created_at_ms`` deliberately does NOT reuse the base class's ``createdAt``:
# the wire schema (``studio.schemas.Generation.createdAt``) is a unix-ms int the
# frontend sorts on, while ``TimestampedDocument.createdAt`` is a datetime. Two
# different types under one name is how a silent coercion bug starts.

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class StudioGeneration(TimestampedDocument):
    """One image / video / audio generation, owned by exactly one workspace."""

    # Tenancy boundary — every read filters on this, no exceptions.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The wire id (``schemas.Generation.id``), unique within a workspace.
    generation_id: str

    prompt: str
    # queued | running | succeeded | failed — the full lifecycle, not just the
    # terminal success the JSONL could express.
    status: str
    kind: str  # image | video | audio
    model: str
    params: dict[str, Any] = Field(default_factory=dict)
    assets: list[dict[str, Any]] = Field(default_factory=list)
    created_at_ms: int
    error: str | None = None
    sourceGenerationId: str | None = None

    # Provenance. "studio" is a human on the /studio page; "sites" is the site
    # authoring agent generating into a page. Without this the linked gallery
    # mixes a user's own experiments with everything an agent made while
    # building a site, and reads as clutter rather than as a feature.
    source: str = "studio"
    pocket_id: str | None = None

    class Settings:
        name = "studio_generations"
        indexes = [
            # The gallery: a workspace's history, newest first.
            [("workspace", 1), ("created_at_ms", -1)],
            # Point lookup + the upsert key, so a retry cannot duplicate a tile.
            [("workspace", 1), ("generation_id", 1)],
            # "everything the site agent made", and "everything for this site".
            [("workspace", 1), ("source", 1), ("created_at_ms", -1)],
            [("workspace", 1), ("pocket_id", 1), ("created_at_ms", -1)],
        ]

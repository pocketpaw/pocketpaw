# ee/pocketpaw_ee/versions/models.py
# Created: 2026-06-18 (feat/branch-primitive-versions, BP-1) — the
# ArtifactVersion Beanie document: the durable, append-only version log for
# the universal Branch primitive.
#
# Design notes (BP-1):
#   * GENERIC by ``scope_type``. A version row belongs to one artifact,
#     identified by (``scope_type``, ``scope_id``). The only wired
#     ``scope_type`` today is ``"pocket"``; ``"site"`` / ``"dashboard"`` /
#     anything else slot in later with NO model change — that is the whole
#     point of keeping this artifact-generic rather than sites-specific.
#   * FULL SNAPSHOTS. ``content`` is the complete artifact snapshot (a
#     pocket's rippleSpec dict, OR a svelte source map ``{path: contents}``),
#     not a diff. Content is small, so full snapshots keep the model dead
#     simple and make diff/revert (BP-4) a pure function of two rows.
#   * STATE MACHINE. ``status`` ∈ {draft, published, merged, superseded,
#     discarded, reverted}. A write creates a ``draft``; ``publish()`` flips a
#     row to ``published``; ``merged`` is the merge gate's accept path (BP-3).
#     ``superseded`` and ``discarded`` are the two ways a draft stops being the
#     working draft, and they are deliberately DIFFERENT words — see below.
#     ``reverted`` is LEGACY: nothing writes it any more.
#   * POINTERS ARE DERIVED, NOT STORED. There is deliberately no separate
#     ``ArtifactRef`` doc holding draft_version_id / published_version_id.
#     The "current draft" and "published" pointers are derived from this
#     collection: latest row by ``version_no`` within (scope, branch)
#     filtered by status. Reasons:
#       - A second pointer doc is a second write that can drift from the
#         versions collection (two-write consistency problem). One source of
#         truth removes that entire failure class.
#       - The compound index (scope_type, scope_id, branch, version_no)
#         makes "latest row of status X" a cheap indexed query.
#       - Extraction to OSS later is cleaner with a single collection.
#     The service (service.py) owns the derivation; see ``get_draft`` /
#     ``get_published`` there.
#
# TODO(BP-3): the merge gate sets status="merged" on an accepted candidate.
#
# Updated: 2026-08-21 (feat/version-history-truthful-lifecycle) — the state
# machine gained ``superseded`` and ``discarded``, and ``reverted`` retired to
# legacy. ``reverted`` had become the answer to three different questions:
# write_draft wrote it when a later edit replaced the draft head, and discard /
# discard_all_drafts wrote it when the owner threw a draft away. revert() never
# wrote it at all — a revert moves FORWARD, writing a new draft — so the one
# reading the word would take at face value was the one meaning it never had.
# The timeline renders status verbatim, so a site edited six times showed five
# rows claiming a rollback nobody performed.
#
# The split is by intent, because the timeline is read by the site's owner:
#   * ``superseded`` — a later edit replaced this draft. Nothing was decided;
#     this is just what typing twice looks like. Reads as ordinary history.
#   * ``discarded``  — the owner (or the merge gate's reject path) threw this
#     draft away. That WAS a decision, and the history should keep it.
# ``reverted`` stays in the Literal so rows written before this change still
# validate. They are not rewritten — ``service.resolve_legacy_statuses`` splits
# them on the read path instead, from lineage already in the rows: a row some
# later row descends from was superseded, anything else was discarded.
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import pymongo
from beanie import Document
from pydantic import Field

__all__ = ["ArtifactVersion", "ArtifactVersionStatus"]

# The version state machine. ``draft`` (write_draft / branch) and ``published``
# (publish) are the pointers; ``merged`` is BP-3's accept path. ``superseded``
# and ``discarded`` are the two exits from being the working draft, split by
# whether a human decided it. ``reverted`` is LEGACY — kept so rows written
# before 2026-08-21 still validate, never written by this module now. See the
# module header for why the one word became three meanings.
ArtifactVersionStatus = Literal[
    "draft",
    "published",
    "merged",
    "superseded",
    "discarded",
    "reverted",
]


class ArtifactVersion(Document):
    """One immutable version of a versionable artifact (Branch primitive).

    A row is created once and not mutated except for its ``status`` (the
    state-machine transition). The (scope_type, scope_id) pair identifies
    the artifact; ``branch`` separates candidate lineages from ``main``;
    ``version_no`` is monotonic within (scope, branch).
    """

    # --- identity / scope (generic) ---
    # ``scope_type`` is the genericity seam — "pocket" today, others later
    # with no schema change. ``scope_id`` is the artifact's id within that
    # scope (e.g. the Pocket _id as a str).
    scope_type: str
    scope_id: str
    workspace_id: str

    # --- branch + ordering ---
    # ``branch`` defaults to "main" — the published lineage. ``branch()``
    # opens a named candidate lineage off main. ``version_no`` is monotonic
    # within (scope_type, scope_id, branch).
    branch: str = "main"
    version_no: int

    # --- payload ---
    # Full snapshot of the artifact at this version. A pocket rippleSpec
    # dict, OR a svelte source map {path: contents}. NOT a diff.
    content: dict[str, Any] = Field(default_factory=dict)

    # --- lineage + state ---
    # The version this one descends from (the row it was branched/derived
    # from). ``None`` for the very first version of an artifact/branch.
    parent_version_id: str | None = None
    status: ArtifactVersionStatus = "draft"

    # --- metadata ---
    label: str | None = None
    author: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "artifact_versions"
        indexes = [
            # The spine index: every "latest row in (scope, branch)" query
            # and the monotonic version_no assignment ride on this. Ordered
            # so a prefix scan on (scope_type, scope_id, branch) is cheap and
            # the trailing descending version_no gives the latest row first.
            pymongo.IndexModel(
                [
                    ("scope_type", pymongo.ASCENDING),
                    ("scope_id", pymongo.ASCENDING),
                    ("branch", pymongo.ASCENDING),
                    ("version_no", pymongo.DESCENDING),
                ],
                name="scope_branch_version",
            ),
        ]

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
#   * STATE MACHINE. ``status`` ∈ {draft, published, merged, reverted}. A
#     write creates a ``draft``; ``publish()`` flips a row to ``published``;
#     ``merged`` / ``reverted`` are reserved for BP-3 (merge gate) / BP-4
#     (revert) and are NOT produced in BP-1.
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
# TODO(BP-4): revert reads two snapshots from here + a Journal projection
#             builds the history view; reverted rows get status="reverted".
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import pymongo
from beanie import Document
from pydantic import Field

__all__ = ["ArtifactVersion", "ArtifactVersionStatus"]

# The version state machine. BP-1 only produces ``draft`` (write_draft /
# branch) and ``published`` (publish). ``merged`` and ``reverted`` are
# reserved seams for BP-3 / BP-4 — declared now so the model is stable.
ArtifactVersionStatus = Literal["draft", "published", "merged", "reverted"]


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

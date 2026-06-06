# ee/pocketpaw_ee/cloud/models/pocket_version.py — a single version (snapshot) of
# a pocket's content in the draft/published version-history log (pocketpaw#1345
# Phase 1, plan §5). Each row is a FULL snapshot of the pocket content. The
# snapshot is engine-aware: ``content`` holds the rippleSpec dict, ``source``
# holds the svelte SvelteKit source map ({path: contents}), and ``engine`` records
# which track the snapshot belongs to — both are captured together so a svelte
# site (rippleSpec=None, all content in ``source``) versions and rolls back
# correctly. version_no is a per-(workspace, pocket) monotonic 1-based counter;
# status is draft|published|archived; parent_version_no links the chain so
# history/rollback can walk it; origin records what triggered the snapshot.
# workspace-scoped for tenant isolation.
#
# Why a snapshot, not a diff: the content is small (one spec / a few source
# files), so full snapshots are cheap and language-agnostic — plan §4 option C.
# Why a separate collection, not an embedded array: a refine-heavy site would
# blow Mongo's 16MB doc limit if every snapshot were embedded on the Pocket;
# Site/Lead are the sibling-collection precedent.
#
# Created 2026-06-06 (feat/1345-draft-published).
# Updated 2026-06-06 (architect schema review): 3-state status enum
# (draft|published|archived) from day one; split the snapshot into ``content``
# (rippleSpec) + ``source`` (svelte map) + ``engine`` so the svelte track isn't
# silently versioned empty; renamed parent → ``parent_version_no``; added
# ``origin``; compound index ordered version_no DESC (newest-first reads).
# Updated 2026-06-06 (code review BLOCK-1 + engine guard): made the
# (workspace, pocket_id, version_no) index UNIQUE so a check-then-act race in
# record_draft can no longer write two rows with the same version_no — the second
# insert now fails loudly with a duplicate-key error (the service retries it
# instead of silently corrupting the log). Constrained ``engine`` to
# ^(ripple|svelte)$ so a bad engine value can't slip a snapshot into a track that
# returns a None preview.
from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class PocketVersion(TimestampedDocument):
    """One snapshot of a pocket's content in the version-history log."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    # Per-(workspace, pocket) monotonic version number, 1-based. Don't rely on
    # createdAt ordering — this is the authoritative order.
    version_no: int
    # The rippleSpec snapshot (ripple track). None for a pure svelte site.
    content: dict[str, Any] | None = None
    # The svelte source-map snapshot ({relative_path: file_contents}). None for a
    # ripple site. Captured ALONGSIDE content so the svelte track — which lives
    # entirely in ``source`` — is never versioned empty.
    source: dict[str, str] | None = None
    # Which generation track this snapshot belongs to ("ripple" | "svelte"), so a
    # publish/rollback restores the right track. Constrained to the two known
    # tracks: a stray value would version into a track whose preview reader
    # returns None (silent data loss on read-back).
    engine: str = Field(default="ripple", pattern="^(ripple|svelte)$")
    # Optional human label for the version (e.g. "before redesign"). Phase 2 UI.
    label: str | None = None
    # Who created the version: a user id, or "agent" for an agent-authored edit.
    author: str | None = None
    # draft = a working version; published = the promoted live candidate;
    # archived = superseded / pruned (Phase 2 rollback + retention).
    status: str = Field(default="draft", pattern="^(draft|published|archived)$")
    # The version_no this one was derived from (None for the first version).
    # Impossible to reconstruct after the fact — captured at write time.
    parent_version_no: int | None = None
    # What triggered the snapshot: create | refine | merge_spec | rest_update |
    # rollback | publish. Free-form so new call sites can add their own tag.
    origin: str | None = None

    class Settings:
        name = "pocket_versions"
        indexes = [
            # Every Phase-2 query is "this pocket's versions, newest first".
            # UNIQUE: two concurrent record_draft calls for the same (workspace,
            # pocket) would otherwise both read the same latest_version_no, +1, and
            # insert duplicate version_no rows — corrupting the monotonic log.
            # Uniqueness turns that race into a loud DuplicateKeyError the service
            # retries (see versions/service.record_draft).
            IndexModel(
                [("workspace", 1), ("pocket_id", 1), ("version_no", -1)],
                unique=True,
            ),
            IndexModel([("workspace", 1), ("pocket_id", 1), ("status", 1)]),
        ]

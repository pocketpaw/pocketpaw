# ee/pocketpaw_ee/cloud/models/other_hand_page.py — the server-side Otherhand
# notebook page. One document per PAGE.
#
# Created 2026-09-15 (feat/otherhand-page-store). Until now a page's ink lived
# only in the browser: ``paw-enterprise/src/lib/core/other-hand/persistence.ts``
# writes a versioned ``{"v":1,...}`` blob to localStorage, so closing the tab on
# another device loses the page. This document is the server half — the thing
# that makes "open it anywhere and it is there" true.
#
# WHY ONE DOC PER PAGE (not per session, not per book): a dense handwritten page
# is a few hundred KB of stroke JSON, comfortably under Mongo's 16MB document
# cap, while a 50-page annotated PDF as a single document would not be. A page
# is also the unit the frontend already saves, snapshots and clears, so the
# document boundary matches the write boundary exactly — no read-modify-write of
# a sibling's ink.
#
# WHY STROKES LIVE IN THE DOCUMENT (not object storage): at this size blob
# storage buys nothing and costs presigning, CORS, and a canvas-tainting trap on
# restore. The strokes are JSON the renderer takes verbatim.
#
# THE NATURAL KEY IS ``(workspace, page_id)``. ``page_id`` is MINTED BY THE
# CLIENT (``persistence.ts:draftPageId``) and is the same id the snapshot
# endpoint already overwrites per page — lining the two up means one page has
# one id everywhere, and an existing localStorage page maps 1:1 on migration.
# ``session_id`` is a nullable GROUPING field, deliberately not the key: a
# session owns more than one page ("New page"), and the draft sheet at a bare
# ``/other-hand`` has no session at all.
#
# ``strokes`` and ``book`` are OPAQUE JSON. The frontend's ``Stroke`` shape grows
# (``text`` / ``img`` / ``icon`` / ``kind``, and a ``color`` field landed the same
# day as this document); a server that re-modelled each field would need a
# migration every time the pen did. Round-trip verbatim is the contract.
#
# ``rev`` is the compare-and-set counter. Last-write-wins is the merge policy,
# but a plain LWW silently eats a second tab's work: tab A saves, stale tab B's
# 2s debounce fires and overwrites it. Every write names the ``rev`` it is based
# on; a mismatch is refused (409) rather than merged. No CRDT, no OT — the
# promise is sequential access, not co-editing.

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class OtherhandPage(TimestampedDocument):
    """One Otherhand notebook page's ink, scoped to a workspace."""

    # Tenancy boundary — every read filters on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    #: Client-minted page id. Unique WITHIN a workspace (see the compound index),
    #: not globally: two workspaces may legitimately mint the same UUID and
    #: neither should see the other's page.
    page_id: str
    #: Who saved it last. Recorded for provenance; the tenancy check is the
    #: workspace, matching the sibling snapshot endpoint where any member may
    #: write their own workspace's page.
    user_id: str = ""
    #: The ``/other-hand/[[sessionId]]`` session this page belongs to, or None
    #: for the unaddressed draft sheet.
    session_id: str | None = None
    #: The whole ink model, user and agent alike — exactly what ``renderPage``
    #: takes. Opaque: stored and returned verbatim.
    strokes: list[dict[str, Any]] = Field(default_factory=list)
    #: ``PersistedBookRef`` — which upload was open beside this paper and at
    #: which page. Opaque, optional, and NOT validated against the files
    #: collection here (see the PR's follow-up note).
    book: dict[str, Any] | None = None
    #: Compare-and-set counter. Starts at 1 on insert and increments on every
    #: content-changing write. 0 means "no server copy" on the wire.
    rev: int = 1

    class Settings:
        name = "other_hand_pages"
        indexes = [
            # The natural key. UNIQUE so a racing double-insert of the same page
            # cannot produce two rows the reads would then silently pick between.
            IndexModel([("workspace", 1), ("page_id", 1)], unique=True),
            # The workspace's page list, most-recent first.
            IndexModel([("workspace", 1), ("updatedAt", -1)]),
        ]

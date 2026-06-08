# WorkspaceSensePreference Beanie document — per-(workspace[,pocket],sense) provider pick.
# Created: 2026-06-08 — Sense tier chunk 2 (EE SenseResolver). EE-side home
# for the sense->connector preference used to disambiguate when more than one
# enabled connector can fill a sense (e.g. github vs gitlab for paw.code.v1).
# Soul-carried preferences are explicitly deferred to Phase 2 — this is the
# v1 store and does NOT touch soul-protocol. One row per
# (workspace, pocket_id, sense_id); workspace_id is required + indexed and
# every read filters on it (ee/cloud rule §7).

from __future__ import annotations

from beanie import Indexed
from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WorkspaceSensePreference(TimestampedDocument):
    """Preferred connector for one sense, scoped to a workspace (and optional pocket).

    ``pocket_id`` is ``None`` for the workspace-level preference. The resolver
    looks up the pocket-level row first when a ``pocket_id`` is supplied, then
    falls back to the workspace-level row. Uniqueness per
    (workspace, pocket_id, sense_id) is enforced at the service layer via
    upsert-on-set, not a Mongo unique index, so set is idempotent.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str | None = None
    sense_id: str
    connector_name: str

    class Settings(TimestampedDocument.Settings):
        name = "workspace_sense_preferences"

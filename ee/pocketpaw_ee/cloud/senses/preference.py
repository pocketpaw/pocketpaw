# Sense->provider preference store — EE-side, v1 home for the pick.
# Created: 2026-06-08 — Sense tier chunk 2. get/set a preferred connector per
# (workspace_id[, pocket_id], sense_id). Backed by the WorkspaceSensePreference
# Beanie doc. NOT in the .soul file — soul-carried prefs are deferred to
# Phase 2 (do NOT touch soul-protocol). Every read filters by workspace_id
# (ee/cloud rule §7); set upserts so it is idempotent (no Mongo unique index).

from __future__ import annotations

from pocketpaw_ee.cloud.models.sense_preference import WorkspaceSensePreference


async def get_preference(
    workspace_id: str,
    sense_id: str,
    *,
    pocket_id: str | None = None,
) -> str | None:
    """Return the preferred connector name for a sense, or ``None`` if unset.

    When ``pocket_id`` is supplied the pocket-scoped preference wins; if there
    is none, fall back to the workspace-level (``pocket_id is None``) row. This
    lets a pocket override the workspace default without losing it.
    """
    if pocket_id is not None:
        doc = await WorkspaceSensePreference.find_one(
            WorkspaceSensePreference.workspace == workspace_id,
            WorkspaceSensePreference.pocket_id == pocket_id,
            WorkspaceSensePreference.sense_id == sense_id,
        )
        if doc is not None:
            return doc.connector_name

    doc = await WorkspaceSensePreference.find_one(
        WorkspaceSensePreference.workspace == workspace_id,
        WorkspaceSensePreference.pocket_id == None,  # noqa: E711 — Beanie expects ==
        WorkspaceSensePreference.sense_id == sense_id,
    )
    return doc.connector_name if doc is not None else None


async def set_preference(
    workspace_id: str,
    sense_id: str,
    connector_name: str,
    *,
    pocket_id: str | None = None,
) -> None:
    """Upsert the preferred connector for a (workspace[, pocket], sense).

    Idempotent: re-setting the same triple updates the existing row rather
    than inserting a duplicate. Uniqueness is enforced here, not by a Mongo
    index, so the store stays forgiving on races.
    """
    doc = await WorkspaceSensePreference.find_one(
        WorkspaceSensePreference.workspace == workspace_id,
        WorkspaceSensePreference.pocket_id == pocket_id,
        WorkspaceSensePreference.sense_id == sense_id,
    )
    if doc is None:
        doc = WorkspaceSensePreference(
            workspace=workspace_id,
            pocket_id=pocket_id,
            sense_id=sense_id,
            connector_name=connector_name,
        )
        await doc.insert()
    else:
        doc.connector_name = connector_name
        await doc.save()


__all__ = ["get_preference", "set_preference"]

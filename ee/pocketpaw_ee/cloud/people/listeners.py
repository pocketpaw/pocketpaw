# listeners.py — in-process bus subscribers for People (Person freshness).
# Created: 2026-08-05 (feat/coupling-person-freshness, T-2) — the Fabric
#   ``Person`` used to be a one-time snapshot taken at invite-accept, so a
#   role change or profile edit left agents orienting on stale identity
#   (a stale-role Person has leaked wrong cards before). These subscribers
#   re-run the idempotent Person upsert whenever the source data moves:
#   ``workspace.member_role`` → refresh that member's role in that
#   workspace; ``profile.updated`` → refresh name/avatar in EVERY
#   workspace the user belongs to. Mirrors the tasks/listeners.py house
#   pattern; wired into the bus from ``ee.cloud.__init__:mount_cloud``
#   after ``init_realtime`` has installed the singleton bus.
"""People bus subscribers — keep the Fabric ``Person`` fresh.

The workspace service emits :class:`WorkspaceMemberRole` on every role
change and the auth service emits :class:`ProfileUpdated` on every
profile edit. This module subscribes both and re-materializes the
affected Person(s) through the people service's shared upsert (keyed on
the deterministic ``person-{workspace_id}-{user_id}`` id, so a refresh
updates — never duplicates). Failures are logged and swallowed: the
membership/profile write is the source of truth, the Person is a derived
projection, and the lazy ``get_person`` backfill converges any miss.
"""

from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.realtime.bus import get_bus
from pocketpaw_ee.cloud._core.realtime.events import (
    Event,
    ProfileUpdated,
    WorkspaceMemberRole,
)

logger = logging.getLogger(__name__)


async def refresh_person_on_role_change(event: Event) -> None:
    """Re-materialize a member's Person when their workspace role changes.

    Payload: ``{workspace_id, user_id, role}`` (emitted by
    ``workspace.service.update_member_role``). Malformed payloads are
    skipped defensively — the bus should never deliver one, but a broken
    handler must not take down the rest of the subscriber chain.
    """

    data = getattr(event, "data", None) or {}
    workspace_id = data.get("workspace_id")
    user_id = data.get("user_id")
    role = data.get("role")
    if not workspace_id or not user_id or not role:
        return

    try:
        from pocketpaw_ee.cloud.people import service as people_service

        await people_service.refresh_person_role(workspace_id, user_id, role)
    except Exception:
        logger.warning(
            "workspace.member_role → Person refresh failed for user=%s workspace=%s",
            user_id,
            workspace_id,
            exc_info=True,
        )


async def refresh_person_on_profile_update(event: Event) -> None:
    """Re-materialize a user's Person everywhere when their profile changes.

    Payload: ``{user_id, changed: [...]}`` (emitted by
    ``auth.service.update_profile``). Only identity-bearing changes
    (``full_name`` / ``avatar``) trigger a refresh — a status flip doesn't
    touch the Person. The fresh values are re-read from the user record
    inside the people service (source of truth), not trusted from the
    event payload.
    """

    data = getattr(event, "data", None) or {}
    user_id = data.get("user_id")
    if not user_id:
        return
    changed = data.get("changed") or []
    if not any(f in changed for f in ("full_name", "avatar")):
        return

    try:
        from pocketpaw_ee.cloud.people import service as people_service

        await people_service.refresh_person_profile(user_id)
    except Exception:
        logger.warning(
            "profile.updated → Person refresh failed for user=%s",
            user_id,
            exc_info=True,
        )


def register_people_listeners() -> None:
    """Wire the People subscribers into the bus.

    Called once from ``mount_cloud`` after ``init_realtime`` has set the
    singleton. Idempotent at the framework level only — calling twice
    would double-register; the bootstrap path calls it exactly once.
    """

    bus = get_bus()
    bus.subscribe(WorkspaceMemberRole.EVENT_TYPE, refresh_person_on_role_change)
    bus.subscribe(ProfileUpdated.EVENT_TYPE, refresh_person_on_profile_update)


__all__ = [
    "refresh_person_on_profile_update",
    "refresh_person_on_role_change",
    "register_people_listeners",
]

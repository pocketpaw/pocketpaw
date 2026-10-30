# domain.py — Cloud "people" entity value objects (the identity spine).
#
# Created: 2026-06-08 (feat/vip-fabric-person, pp#1366) — frozen dataclass
# describing a workspace member materialized as a standalone Fabric
# ``Person`` object on invite accept. Tenancy (``workspace_id``) is
# required positionally with no default, matching the ee/cloud rule that
# the workspace tag is impossible to forget — constructing a ``Person``
# without one is a TypeError, not a silent leak.
#
# STANDALONE by design: this models *identity* (who the member is), a
# different axis from the per-pocket agent-policy surface profile
# (``PocketSurfaceProfile`` / entity-pocket-profile work). No import of,
# or dependency on, that surface — Person never reaches across the seam.
#
# The ``Person`` mirrors the property bag written into the Fabric object
# (``FabricObject.properties``) plus the provenance fields the journal
# event carries. It exists so callers (tests, the later VIP-onboarding
# read) get a typed view instead of poking a raw ``dict[str, Any]``.

from __future__ import annotations

from dataclasses import dataclass

# The Fabric object type this entity materializes as. A stable string id
# (not a generated one) so the journal projection groups every Person row
# under one ``type_id`` and ``query(type_name="Person")`` is exact.
PERSON_TYPE_ID = "person"
PERSON_TYPE_NAME = "Person"

# Provenance marker stored on every Person written from an invite's admin
# context. Surfaced both as ``FabricObject.source_connector`` and inside
# the property bag (``source``) so a reader that only has the projected
# object — not the journal event — can still tell where the row came from.
SOURCE_ADMIN_CONTEXT = "admin_context"


@dataclass(frozen=True)
class Person:
    """A workspace member's identity, materialized as a Fabric ``Person``.

    Tenancy is enforced at construction — ``workspace_id`` is required
    positionally with no default. Same rule as the rest of ``ee/cloud``.

    Identity fields come from the member's own profile; onboarding fields
    (``focus`` / ``profile_pic``) and ``role`` / ``group`` come from the
    inviting admin's context. ``invited_by`` + ``source`` record the
    provenance so the later VIP-onboarding flow knows the row was seeded
    by an admin, not self-authored.

    Fields:
      * ``id`` — the Fabric object id. Deterministic
        (``person-{workspace_id}-{user_id}``) so re-materializing the same
        member updates the existing object instead of minting a duplicate.
      * ``workspace_id`` — owning workspace. Tagged at construction.
      * ``user_id`` — the member's cloud user id. The stable external key
        the materializer keys idempotency off.
      * ``name`` / ``email`` / ``avatar`` — from the member's own profile.
      * ``role`` — workspace role from the invite (``member`` / ``admin``).
      * ``group`` — team / group id from the invite. ``None`` when the
        invite carried no group.
      * ``focus`` — one-line "what they'll own", from the invite's admin
        context. Empty string when the admin supplied none.
      * ``profile_pic`` — suggested avatar reference, from the invite's
        admin context. Empty string when the admin supplied none.
      * ``invited_by`` — user id of the admin who sent the invite
        (provenance).
      * ``source`` — provenance marker; always ``admin_context`` for
        invite-materialized people.
    """

    id: str
    workspace_id: str
    user_id: str
    name: str
    email: str
    avatar: str
    role: str
    group: str | None
    focus: str
    profile_pic: str
    invited_by: str
    source: str = SOURCE_ADMIN_CONTEXT

    def to_properties(self) -> dict[str, str | None]:
        """Project the identity + provenance fields into the property bag
        stored on ``FabricObject.properties``.

        ``id`` / ``workspace_id`` are intentionally NOT duplicated into the
        bag: the id is the object's own id, and the workspace is carried by
        the journal event's scope (the canonical tenancy source). Folding
        them into properties too would create two places to keep in sync.
        """

        return {
            "user_id": self.user_id,
            "name": self.name,
            "email": self.email,
            "avatar": self.avatar,
            "role": self.role,
            "group": self.group,
            "focus": self.focus,
            "profile_pic": self.profile_pic,
            "invited_by": self.invited_by,
            "source": self.source,
        }


__all__ = [
    "PERSON_TYPE_ID",
    "PERSON_TYPE_NAME",
    "SOURCE_ADMIN_CONTEXT",
    "Person",
]

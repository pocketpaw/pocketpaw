# pocketpaw_ee/cloud/people/ — the workspace-member identity spine.
#
# Created: 2026-06-08 (feat/vip-fabric-person, pp#1366). Materializes a
# standalone Fabric ``Person`` object for a member on invite accept, built
# from the member's profile + the invite's admin context, provenance-tracked.
# Read by a later VIP-onboarding flow. STANDALONE — models identity only,
# independent of the per-pocket agent-policy surface profile.
#
# Changes: 2026-06-08 (feat/vip-agent-block, pp#1367) — exported the read
# side, ``get_person``, so the agent-orientation flow can fetch a member's
# Person to render an "about this member" block in the system prompt.
#
# Changes: 2026-08-05 (feat/coupling-person-freshness, T-2) — the Person is
# no longer a one-time invite snapshot. Added ``listeners.py`` (bus
# subscribers: workspace.member_role + profile.updated re-run the idempotent
# upsert) and exported the freshness surface:
# ``materialize_person_from_membership`` (owner-at-create + every non-invite
# path), ``refresh_person_role``, ``refresh_person_profile``. ``get_person``
# now lazily backfills a missing Person for a live member on first read.
#
# No router yet: the entry points are the service functions the workspace
# accept_invite / create paths (write), the bus listeners (refresh), and the
# chat agent-orientation path (read) call. A read API can land in a later slice.

from pocketpaw_ee.cloud.people.domain import Person
from pocketpaw_ee.cloud.people.service import (
    get_person,
    materialize_person_from_invite,
    materialize_person_from_membership,
    refresh_person_profile,
    refresh_person_role,
)

__all__ = [
    "Person",
    "get_person",
    "materialize_person_from_invite",
    "materialize_person_from_membership",
    "refresh_person_profile",
    "refresh_person_role",
]

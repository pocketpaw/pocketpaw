# pocketpaw_ee/cloud/people/ — the workspace-member identity spine.
#
# Created: 2026-06-08 (feat/vip-fabric-person, pp#1366). Materializes a
# standalone Fabric ``Person`` object for a member on invite accept, built
# from the member's profile + the invite's admin context, provenance-tracked.
# Read by a later VIP-onboarding flow. STANDALONE — models identity only,
# independent of the per-pocket agent-policy surface profile.
#
# No router yet: the only entry point is the service function the workspace
# accept_invite path calls. A read API can land in a later slice.

from pocketpaw_ee.cloud.people.domain import Person
from pocketpaw_ee.cloud.people.service import materialize_person_from_invite

__all__ = ["Person", "materialize_person_from_invite"]

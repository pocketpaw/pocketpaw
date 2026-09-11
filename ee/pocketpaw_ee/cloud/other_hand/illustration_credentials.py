# ee/pocketpaw_ee/cloud/other_hand/illustration_credentials.py — whose fal key
# pays for this illustration, and whether it may happen at all.
#
# Created 2026-09-11 (feat/byok-image-key).
#
# There are two ways to reach the illustrator — the agent's ``illustrate`` tool
# and the toolbar button's ``POST /other-hand/illustrate`` — and before this
# module each decided independently. They agreed, but only because the same
# three checks had been written twice; the first time one of them grew a fourth,
# they would not have. The rules live here once.
#
# THE RULES, in order:
#
#   1. The workspace's OWN fal key wins. It pays for its own pictures, so the
#      platform's daily ceiling does not apply and a guest is not refused —
#      the guest refusal exists because a guest can mint a fresh workspace for
#      a fresh ceiling, which is an argument about the PLATFORM's money and
#      says nothing about someone spending their own.
#   2. No image key, and the caller is an account: the platform's key, under
#      the daily cap, exactly as before this module existed.
#   3. No image key, and the caller is a guest: refused, exactly as before.
#
# The budget is claimed ONLY on branch 2. A workspace on its own key that also
# consumed the platform ceiling would be paying twice — once in money and once
# in a quota it has no reason to be inside.

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: What the agent is told when it cannot illustrate. Worded so it explains in
#: words and moves on rather than retrying a refusal.
GUEST_REFUSAL = (
    "Illustrations need an account, or your own image key in Settings. Say so "
    "plainly and offer to keep going in words and your own drawing; do not try "
    "again this turn."
)
NO_KEY_REFUSAL = (
    "No illustrator is configured on this deployment. Explain in words and with "
    "your own drawing instead; do not try again."
)


@dataclass(frozen=True)
class IllustrationGrant:
    """Permission to generate one illustration, and whose key pays for it."""

    api_key: str
    #: True when the key is the workspace's own. The caller stamps an auth
    #: failure against the row only in that case — a refused PLATFORM key is an
    #: operator problem, not something to show this workspace as its own error.
    byok: bool


@dataclass(frozen=True)
class IllustrationRefusal:
    """No illustration this time, and the words to say so."""

    reason: str
    #: True when an account would have been allowed. The REST route turns this
    #: into the guest gate the page already renders; the agent tool just speaks.
    guest_gate: bool = False


async def resolve(
    workspace_id: str | None,
    *,
    is_guest: bool,
) -> IllustrationGrant | IllustrationRefusal:
    """Apply the three rules above. Never raises."""
    from pocketpaw_ee.cloud.byok import service as byok_service
    from pocketpaw_ee.cloud.studio import fal_edit

    own = await byok_service.resolve_image_key(workspace_id)
    if own:
        return IllustrationGrant(api_key=own, byok=True)

    if is_guest:
        return IllustrationRefusal(reason=GUEST_REFUSAL, guest_gate=True)

    platform = fal_edit.fal_api_key()
    if not platform:
        return IllustrationRefusal(reason=NO_KEY_REFUSAL)
    return IllustrationGrant(api_key=platform, byok=False)

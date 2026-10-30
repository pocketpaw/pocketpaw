# ee/pocketpaw_ee/cloud/chat/kiosk_byok.py — who has to bring their own key on
# the Otherhand kiosk, asked in ONE place.
#
# Created 2026-09-13 (feat/kiosk-plan-gate). The rule existed before this
# module and was written down TWICE: ``run_core._requires_own_key`` (the
# enforcement, reading the server-resolved surface) and
# ``agent_router._assert_own_key_if_kiosk_requires_it`` (a pre-stream 402 for
# the browser, reading the client's surface hint). Two copies of a predicate is
# fine while the predicate is two lines; it stops being fine the moment it
# grows a term, because the router answers FIRST. A plan check added to the
# executor alone would have had the router refuse a paying member before the
# executor ever got to let them through — a gate that is open downstream and
# shut upstream, which reads as "I paid and it still asks for a key".
#
# So the two seams keep their different JOBS (one is UX, one is enforcement,
# and the router's surface hint is still not trustworthy) but share this one
# answer.
#
# The rule, 2026-09-13: a signed-up FREE account on the kiosk brings its own
# key; any PAID tier runs on the platform subscription. Before today the flag
# was all-or-nothing for accounts, which made the kiosk's only signup path a
# dead end for anyone who did not already hold a provider key.
#
# Guests are NOT handled here. They are refused earlier, unconditionally, with
# their own code (``guest_key_required``) — a guest is told to create an
# account, an account is told to add a key, and answering a guest with the
# account code is a dead end that reads as a broken product.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.surface import SurfaceKind

logger = logging.getLogger(__name__)

#: The one tier that pays with its own key. Everything else in the catalog
#: (go, pro, pro_max, enterprise) is paying us, so we cover the model.
#:
#: Expressed as "is it free" rather than a list of paid tiers on purpose: a new
#: tier added to the catalog is PAID until someone decides otherwise, which is
#: the direction that cannot silently hand our subscription to a free tier.
_FREE_PLAN = "free"


async def requires_own_key(ctx: Any) -> bool:
    """Must THIS turn pay with the user's own key, having found none?

    True only when every one of these holds, cheap checks first so an ordinary
    turn on any other surface pays one boolean and no database read:

    * ``other_hand_require_byok`` is on (default OFF, so every existing deploy
      and the whole of Paw OS are untouched until an operator sets it);
    * the run is on the Otherhand surface;
    * the workspace is on the FREE plan.

    The plan comes from ``entitlements.resolve_entitlements``, the same
    resolver the credit quota uses, which reads ``Workspace.plan`` and falls
    back to ``free`` for a missing, deleted or unknown tier. That fallback is
    the safe direction here and is inherited deliberately rather than
    re-implemented: this gate decides who spends OUR money, so an unresolvable
    plan must land on "bring your own key", never on "have ours".

    A resolver FAILURE is treated the same way, and that is the one place this
    function is deliberately unlike its neighbours. The daily budgets and the
    storage cap fail OPEN, because refusing the whole product over one
    unreadable counter is worse than one workspace briefly exceeding a limit.
    The reverse is true here: failing open hands an unbounded platform
    credential to anyone who can make a lookup fail, and the closed answer is
    not an outage — it is the key prompt the user can act on in ten seconds.
    """
    from pocketpaw.config import get_settings

    if not get_settings().other_hand_require_byok:
        return False

    sc = getattr(ctx, "surface_context", None)
    if sc is None or sc.kind is not SurfaceKind.OTHER_HAND:
        return False

    workspace_id = getattr(ctx, "workspace_id", "") or ""
    if not workspace_id:
        # No workspace to resolve a plan for. Closed, per the docstring.
        return True

    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    try:
        ent = await entitlements_service.resolve_entitlements(workspace_id)
    except Exception:
        logger.warning(
            "kiosk byok gate: could not resolve entitlements for %s — asking for a key",
            workspace_id,
            exc_info=True,
        )
        return True

    return (ent.plan or _FREE_PLAN) == _FREE_PLAN

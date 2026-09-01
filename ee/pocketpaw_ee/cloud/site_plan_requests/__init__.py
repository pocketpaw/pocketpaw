# ee/cloud/site_plan_requests — an employee asks for a paid site plan; an admin
# approves it, and the approval is what buys.
#
# Created: 2026-09-01 (feat/sites-plan-purchase-request).
#
# The gap this fills. ``sites.buy_plan`` (ADMIN) stopped a member charging the
# company card for a site plan, which was the right refusal and a dead end: the
# employee who builds the site gets a 403 and no way forward except finding an
# admin and describing what they wanted. This turns that refusal into a request.
#
# The blob is the 14th gated proposal kind, alongside ``_admin_action`` and
# ``_artifact_change``. Its shape mirrors ``_admin_action`` closely because the
# problem is the same one — gate a privileged write behind a human — with ONE
# inversion that is the entire point of this module:
#
#   ``_admin_action`` re-checks the PROPOSER's role at execute time. Here the
#   proposer is, by construction, NOT authorized: a member asking for a plan is
#   exactly who this exists for. So the execute-time RBAC check runs against the
#   APPROVER (``Action.approved_by``, the authenticated identity the router
#   records — never a free-text field). Re-checking the proposer would refuse
#   every request this feature creates; omitting the check entirely would let any
#   pending request buy the moment anyone approved it.
#
# That inversion is safe because ``instinct.approve`` is itself ADMIN — the same
# minimum as ``sites.buy_plan`` — so an approver has already cleared the bar the
# purchase needs. The executor re-checks anyway rather than inferring it: the two
# rules live in different files and can drift, and "the approve endpoint already
# checked something similar" is not a control.
#
# See ``propose.py`` for the blob shape and ``executor.py`` for what approving
# actually does.

from pocketpaw_ee.cloud.site_plan_requests.propose import (
    SITE_PLAN_REQUEST_KIND,
    SITE_PLAN_REQUEST_PARAM_KEY,
    SITE_PLAN_REQUEST_SCHEMA,
    compute_request_hash,
    propose_site_plan_request,
)

__all__ = [
    "SITE_PLAN_REQUEST_KIND",
    "SITE_PLAN_REQUEST_PARAM_KEY",
    "SITE_PLAN_REQUEST_SCHEMA",
    "compute_request_hash",
    "propose_site_plan_request",
]

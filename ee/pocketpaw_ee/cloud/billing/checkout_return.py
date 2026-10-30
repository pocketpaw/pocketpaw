# ee/pocketpaw_ee/cloud/billing/checkout_return.py — the PUBLIC post-checkout
# return hop: ``GET|POST /billing/checkout/return/{outcome}?to=<app origin>``.
#
# Dodo's checkout return_url / cancel_url point here (``billing.service.
# _checkout_return_urls``), not at the app. Dodo processes Indian cards through
# Razorpay, which sends the buyer back with a cross-site form POST; the app's
# /settings/billing page cannot take that (a SvelteKit server rejects it on its
# CSRF origin check, a static SPA / Tauri build has no server at all). This hop
# accepts GET or POST and answers 303 See Other to
# ``<to>/settings/billing?checkout=<outcome>`` so the browser follows with a GET.
#
# Invariants:
#   * NO auth and NO license dependency: it is a cross-site browser navigation
#     with no session. It is on the public allowlist in
#     tests/cloud/auth/test_route_auth_audit.py.
#   * NO state change. Fulfilment stays webhook-driven (``billing.webhooks``);
#     the request carries nothing this hop trusts.
#   * The POST body is never read or logged: it can carry payment identifiers.
#   * ``to`` must pass ``billing.service.checkout_return_target`` (the CORS
#     allowlist), so the hop is never an open redirect. With no trusted target
#     it renders a static page that reflects no input.
#   * The outcome is a PATH segment because Dodo appends its own ``status``
#     query param; nothing the gateway appends is forwarded to the app.

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pocketpaw_ee.cloud.billing import service as billing_service

# Mounted on its own in mount_cloud(), apart from the licensed billing router, so
# no auth / license dependency can attach to it.
router = APIRouter(prefix="/billing/checkout", tags=["Billing"])

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto;
 padding: 0 1rem">
<h1>{title}</h1>
<p>{body}</p>
</body></html>"""

_SUCCESS = (
    "Payment received",
    "You can close this tab and return to the app. Your plan updates there in a moment.",
)
_CANCEL = (
    "Payment cancelled",
    "Nothing was charged. You can close this tab and return to the app.",
)


def _return(outcome: str, to: str | None) -> Response:
    target = billing_service.checkout_return_target(outcome, to)
    if target is not None:
        return RedirectResponse(target, status_code=303)
    title, body = _SUCCESS if outcome == "success" else _CANCEL
    return HTMLResponse(
        _PAGE.format(title=title, body=body),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/return/{outcome}", include_in_schema=True)
async def checkout_return_get(outcome: str, to: str | None = None) -> Response:
    """Send the buyer back to the app billing page after a Dodo checkout (303)."""
    return _return(outcome, to)


@router.post("/return/{outcome}", include_in_schema=True)
async def checkout_return_post(outcome: str, to: str | None = None) -> Response:
    """Same as GET, for gateways (Razorpay) that return the buyer with a form POST.

    The body is deliberately never read: it can carry payment identifiers, and
    nothing in it is trusted (fulfilment is the signed webhook's job).
    """
    return _return(outcome, to)

"""Social sign-in routes — the public OAuth dance for Google and GitHub.

Created 2026-07-29 (AM-2/AM-3/AM-4/AM-5).

Updated 2026-08-01 (AM-6): the connected-accounts surface — list, link, unlink,
and the desktop link completion. Those four are the exception to the paragraph
below and MUST carry ``current_active_user``. They act on the caller's own credentials, so the
identity has to come from the session and from nowhere else: a link endpoint
that took its target user from a path or body parameter would let anyone
attach their own Google account to any user id they can name, which is an
account-takeover primitive, not a settings page.

Every OTHER route here is unauthenticated by necessity: the caller has no
session yet, which is the entire point. That makes the single-use ``state``
from ``auth/_oauth_state.py`` the load-bearing defence — see that module for
why it is server-side rather than a signed token.

Failures redirect rather than return JSON. These endpoints are reached by a
full-page browser navigation back from the provider, so a JSON error body would
render as raw text in the address bar. A refusal goes back to the app with the
dialog reopened and the reason attached (``/?auth=signin&auth_error=<code>``),
because ``auth.unverified_link`` is a normal outcome for a careful user who
declined an email scope, not a malfunction.

Every redirect out of here is ABSOLUTE against the frontend origin. A relative
one resolves against the API origin, which only coincides with the app when both
are served from a single domain.

Two client shapes finish differently, decided by the ``flow`` pinned into the
state payload at authorize time — never read from the callback's query string,
which is attacker-influenced:

  * **web** — mint the cookie session and redirect into the app.
  * **desktop** — redirect to the SPA's /oauth-callback carrying a one-time
    exchange CODE, which the app trades at POST /auth/social/exchange for a
    bearer. The redirect never carries the token itself; a token in a URL leaks
    through history, Referer, window titles, and proxy logs.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse, Response

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud._core.rate_limit import rate_limit_social_exchange
from pocketpaw_ee.cloud.auth._login_helpers import mint_and_record
from pocketpaw_ee.cloud.auth.core import (
    bearer_backend,
    cookie_backend,
    current_active_user,
    current_optional_user,
)
from pocketpaw_ee.cloud.auth.social import service as social_service
from pocketpaw_ee.cloud.auth.social.dto import (
    LinkedIdentity,
    SocialExchangeRequest,
    SocialIdentitiesResponse,
    SocialLinkCompleteRequest,
    SocialLinkCompleteResponse,
    SocialLinkStartResponse,
    SocialProvidersResponse,
)
from pocketpaw_ee.cloud.auth.social.providers import configured_providers

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Social auth"])


def _safe_next(next_path: Any) -> str:
    """Reduce a caller-supplied ``next`` to a same-origin relative path.

    The frontend validates this at parse time, but both endpoints that read it
    are reachable directly, so it is re-checked rather than trusted. A single
    leading slash and no backslash rules out absolute and protocol-relative
    targets — the two shapes that turn a redirect into an open redirect.
    """
    if (
        isinstance(next_path, str)
        and next_path.startswith("/")
        and not next_path.startswith("//")
        and "\\" not in next_path
    ):
        return next_path
    return "/"


def _error_redirect(reason: str) -> RedirectResponse:
    """Send a refusal back into the app with the dialog open and the reason.

    Two things were wrong with the previous `/auth/error?reason=` target, both
    found on the first live Google run:

      * it was a RELATIVE path, so it resolved against the BACKEND origin. In
        any split-origin setup (locally the SPA is :1420 and the API is :8888)
        that lands the user on the API, not the app.
      * the SPA has no /auth/error route at all — only forgot, reset, verify.
        So the refusal was a dead end pointing at a 404.

    A refusal is a UI state, not an error page: the user who declined an email
    scope needs the dialog back with an explanation, so that is where they go.
    """
    base = social_service.frontend_base_url()
    return RedirectResponse(url=f"{base}/?auth=signin&auth_error={quote(reason)}", status_code=302)


@router.get("/auth/social/providers", response_model=SocialProvidersResponse)
async def list_providers() -> SocialProvidersResponse:
    """Which provider buttons the auth dialog should render."""
    return SocialProvidersResponse(providers=configured_providers())


@router.get("/auth/social/{provider}/login")
async def social_login(
    provider: str,
    flow: str = "web",
    next: str | None = None,  # noqa: A002 — matches the query-param name
) -> RedirectResponse:
    """Begin consent. Redirects to the provider."""
    try:
        url = await social_service.begin_login(provider, flow=flow, next_path=next)
    except CloudError as exc:
        return _error_redirect(exc.code)
    except Exception:  # noqa: BLE001 — discovery / network failure
        logger.exception("social.begin_login failed for provider=%s", provider)
        return _error_redirect("social.begin_failed")
    return RedirectResponse(url=url, status_code=302)


def _link_redirect(next_path: Any, **params: str) -> RedirectResponse:
    """Send a link OUTCOME back to whatever page started it.

    Settings navigates the whole page away to the provider, so the result has
    to arrive as a redirect carrying a marker the page reads on mount — the
    same shape ``?auth=…`` already uses for the sign-in dialog. ``next`` is the
    page that started the link, run through the same validator as the sign-in
    path, so a hostile value degrades to "/" instead of leaving the origin.
    """
    base = social_service.frontend_base_url()
    path = _safe_next(next_path)
    separator = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{base}{path}{separator}{urlencode(params)}", status_code=302)


def _desktop_link_redirect(**params: str) -> RedirectResponse:
    """Send a desktop LINK outcome to the route the Tauri webview watches.

    Always `<frontend>/oauth-callback`, and deliberately ignores ``next``. The
    webview's whole job is to close and hand back a result; there is no page
    for it to navigate to, and the Settings panel that opened it is still
    mounted in the main window. Not interpolating ``next`` here also means the
    hostile-value problem cannot reach this redirect at all — the safest
    handling of an attacker-influenced path is not to build one.
    """
    base = social_service.frontend_base_url()
    return RedirectResponse(url=f"{base}/oauth-callback?{urlencode(params)}", status_code=302)


@router.get("/auth/social/callback")
async def social_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    user: Any = Depends(current_optional_user),
):
    """Finish consent: redeem the code, apply the policy, then sign in or link.

    Which of those two it is comes from the single-use state payload, never
    from this request. ``user`` is optional because a sign-in callback has no
    session by definition — but a LINK callback must have one, and the service
    refuses when it does not match the account the state names. This dependency
    is what makes that session visible here; it never rejects anyone itself.
    """
    if error:
        # The user pressed "Cancel" on the provider's consent screen, or the
        # provider itself refused. Not our failure — send them back to the
        # dialog rather than an error page that implies something broke.
        return _error_redirect(error)
    if not code or not state:
        return _error_redirect("social.missing_code_or_state")

    try:
        result = await social_service.complete_callback(code, state, session_user=user)
    except social_service.LinkRefused as exc:
        # A link refusal must NOT reopen the sign-in dialog — this user is
        # already signed in, and prompting them to sign in explains nothing.
        # The exception carries ``next`` and ``flow`` because the state it came
        # from has already been consumed, so neither is recoverable here.
        if exc.flow == "desktop":
            return _desktop_link_redirect(link_error=exc.code)
        return _link_redirect(exc.next_path, social_error=exc.code)
    except CloudError as exc:
        # Includes the policy refusals (auth.unverified_link,
        # auth.sso_enforced), which the dialog renders as guidance.
        return _error_redirect(exc.code)
    except Exception:  # noqa: BLE001
        logger.exception("social.complete_callback failed")
        return _error_redirect("social.callback_failed")

    if result["mode"] == "link_pending":
        # Desktop link. Nothing has been attached — this webview cannot prove
        # who it is, so the identity is parked and the app finishes the job
        # from POST /auth/social/link/complete under its bearer. The webview
        # recognises `link=` (the login branch uses `xc=`) and closes.
        return _desktop_link_redirect(link=result["link_code"], provider=result["provider"])

    if result["mode"] == "link":
        # Web link. Already authenticated — no session to mint, nothing to set.
        # Just report the outcome back to the page that started it.
        return _link_redirect(result.get("next"), social_linked=result["provider"])

    # Desktop: hand back a one-time REFERENCE, never a token. The Tauri client
    # has no cookie jar we can write to from here, and putting the bearer in
    # this redirect would leak it through history, Referer, window titles and
    # any proxy log on the path. The existing /oauth-callback route picks the
    # code up, emits its Tauri event, and the app trades it below.
    #
    # `flow` comes from the single-use state payload, pinned at authorize time
    # — never from this request's query string.
    if result["flow"] == "desktop":
        xc = await social_service.issue_exchange_code(str(result["user"].id))
        base = social_service.frontend_base_url()
        return RedirectResponse(url=f"{base}/oauth-callback?xc={quote(xc)}", status_code=302)

    response = await mint_and_record(cookie_backend, result["user"], request)

    safe_next = _safe_next(result.get("next"))

    # Absolute, against the FRONTEND origin. `safe_next` is a relative path, and
    # a relative redirect from here resolves against the API origin — which is
    # the same host only when both are served from one domain. In production
    # they are; in local dev they are not, and the user lands on the API root.
    redirect = RedirectResponse(
        url=f"{social_service.frontend_base_url()}{safe_next}", status_code=302
    )
    for key, value in response.headers.items():
        if key.lower() == "set-cookie":
            redirect.headers.append("set-cookie", value)
    return redirect


@router.post("/auth/social/exchange", dependencies=[Depends(rate_limit_social_exchange)])
async def social_exchange(body: SocialExchangeRequest, request: Request):
    """Trade a desktop one-time code for a bearer token.

    Unauthenticated by necessity — this is how a desktop client obtains its
    FIRST token. The controls are the code itself: 32 bytes of entropy,
    single-use via GET-then-DEL, a 60-second TTL, and a per-IP rate limit.

    Returns the same body the password bearer login returns, so the client
    stores it through exactly one code path.
    """
    body = SocialExchangeRequest.model_validate(body)
    user = await social_service.redeem_exchange_code(body.xc)
    return await mint_and_record(bearer_backend, user, request)


# ---------------------------------------------------------------------------
# Connected accounts (AM-6) — the signed-in half of this router
# ---------------------------------------------------------------------------
#
# All three take ``current_active_user``. The user id is read from that
# dependency and never from the path or body: the provider name is the only
# thing a caller gets to choose, so there is no request shape that acts on
# somebody else's credentials.


@router.get("/auth/social/identities", response_model=SocialIdentitiesResponse)
async def list_social_identities(
    user: Any = Depends(current_active_user),
) -> SocialIdentitiesResponse:
    """Which provider identities are attached to the caller's own account."""
    rows = await social_service.list_identities(user)
    return SocialIdentitiesResponse(identities=[LinkedIdentity.model_validate(row) for row in rows])


@router.post("/auth/social/{provider}/link", response_model=SocialLinkStartResponse)
async def start_social_link(
    provider: str,
    flow: str = "web",
    next: str | None = None,  # noqa: A002 — matches the query-param name
    user: Any = Depends(current_active_user),
) -> SocialLinkStartResponse:
    """Begin consent for an identity that will ATTACH to the caller.

    Returns the URL instead of redirecting: Settings calls this with fetch,
    where a 302 is followed opaquely by the browser and the page never moves.

    ``flow`` selects how the callback will prove who the caller is — the
    session cookie for ``web``, a bearer-authorised follow-up call for
    ``desktop``. A desktop client that omits it gets the web branch, whose
    cookie check it can never satisfy: consent succeeds and nothing attaches.
    An unknown value is refused rather than defaulted, for the same reason.

    Errors surface as JSON (``CloudError`` → the shared handler) rather than as
    the redirects the sign-in routes use, because unlike those this is an XHR
    with a caller waiting on a response, not a browser navigation.
    """
    url = await social_service.begin_link(user, provider, flow=flow, next_path=next)
    return SocialLinkStartResponse(authorize_url=url)


@router.post("/auth/social/link/complete", response_model=SocialLinkCompleteResponse)
async def complete_social_link(
    body: SocialLinkCompleteRequest,
    user: Any = Depends(current_active_user),
) -> SocialLinkCompleteResponse:
    """Finish a DESKTOP link by redeeming the code the callback parked.

    This endpoint exists because the desktop client cannot authenticate the
    callback. It signs in with a bearer held in localStorage, and the Tauri
    webview that completes consent carries no cookie for this origin — so the
    callback has no way to tell whose account to attach to, and attaching on
    the strength of the state alone is precisely the theft the web branch's
    cookie check exists to prevent.

    The proof therefore moves here, onto a request the app CAN authenticate.
    The parked record names the account the link was started for, and the
    service compares it against ``current_active_user``. A stolen code is
    worth nothing without that account's bearer, which makes the desktop path
    stronger than the cookie check rather than a concession to it.

    Returns the refreshed identity list so the panel renders the outcome
    without a second round trip — the window that started this has already
    closed, and a follow-up refetch that fails would leave the panel stale
    with no way to explain itself.
    """
    body = SocialLinkCompleteRequest.model_validate(body)
    result = await social_service.complete_pending_link(user, body.code)
    rows = await social_service.list_identities(result["user"])
    return SocialLinkCompleteResponse(
        provider=result["provider"],
        identities=[LinkedIdentity.model_validate(row) for row in rows],
    )


@router.delete("/auth/social/identities/{provider}", status_code=204)
async def unlink_social_identity(
    provider: str,
    user: Any = Depends(current_active_user),
) -> Response:
    """Detach one of the caller's identities.

    409 ``auth.last_credential`` when it is the only way into the account —
    the service refuses rather than letting a user lock themselves out of an
    account support cannot restore them to.
    """
    await social_service.unlink_identity(user, provider)
    return Response(status_code=204)

"""Social sign-in orchestration — begin the flow, complete it, apply the policy.

Created 2026-07-29 (AM-2/AM-3/AM-4).

Updated 2026-08-01 (AM-6): the same OAuth dance now serves a SECOND purpose —
attaching an identity to an account that is already signed in. Both purposes
land on one callback, so the distinction has to be carried somewhere the
callback can trust. It is pinned into the single-use state payload as
``link_user_id`` at authorize time, never inferred from the request.

Two things about the link branch that are load-bearing:

  * **The callback re-checks the session against the pinned id.** State is a
    bearer secret: whoever presents it wins. For sign-in that costs an
    attacker at most a session on their own account. For linking it would be
    worse — completing someone else's link state with your OWN provider
    account attaches your identity to their account, and you can then sign in
    as them. Requiring the callback's session to BE the account named in the
    state removes that, because the state alone stops being enough.
  * **Desktop links finish somewhere else entirely.** The desktop client
    authenticates with a bearer from localStorage, and the Tauri webview that
    completes consent carries no cookie for this origin — so the session
    re-check above can never pass there, and the callback has no way to
    authenticate anyone at all. Rather than weaken the check for desktop, the
    desktop branch attaches NOTHING at the callback: it parks the consented
    identity behind a one-time code and redirects to ``/oauth-callback``, and
    the app redeems that code at ``POST /auth/social/link/complete`` under its
    bearer, where ``current_active_user`` can be compared against the account
    the link was started for. Same property, moved to a request that can
    actually carry proof — and strictly stronger, because possession of the
    parked code is not sufficient without that account's token.

Shape notes against the cloud code rules, since this module deviates twice and
both are deliberate:

  * **No ``workspace_id`` parameter.** Rule 5's signature assumes a tenant is
    already known. Sign-in runs *before* that: the caller has no session, and
    the resulting user may belong to no workspace at all (they land in the
    /welcome funnel). Tenancy is enforced on every request after this one.
  * **Global reads.** Resolving an identity to an account is inherently
    cross-tenant — we are answering "who is this person", not "what may this
    tenant see". Each such query carries an explicit ``# global-read:`` note.

Everything the callback needs is pinned into the single-use state payload at
authorize time and read back from there. Nothing on the callback's query string
is trusted beyond ``code`` and ``state`` themselves, because a callback URL is
attacker-influenced by definition.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    ValidationError,
)
from pocketpaw_ee.cloud.auth import _oauth_state
from pocketpaw_ee.cloud.auth.social import domain
from pocketpaw_ee.cloud.auth.social.providers import get_provider
from pocketpaw_ee.cloud.auth.social.providers.base import SocialIdentity
from pocketpaw_ee.cloud.models.user import OAuthAccount as _OAuthAccount
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc

logger = logging.getLogger(__name__)

_STATE_NAMESPACE = "social"

#: Flows the callback knows how to finish. Pinned into state at authorize time,
#: never read from the callback query string.
_FLOWS = ("web", "desktop")


class LinkRefused(Forbidden):
    """A refusal from the LINK branch specifically.

    A distinct type rather than a plain ``Forbidden`` so the router can route
    the outcome without guessing. The two branches need different destinations
    — a link refusal must not reopen the sign-in dialog at somebody who is
    already signed in — and "is there a session?" is not a reliable proxy for
    "was this a link?", since a signed-in user can still reach the sign-in
    flow.

    It carries ``next_path`` and ``flow`` because the state has already been
    consumed by the time this is raised, so the router can recover neither.
    Without ``next_path`` the user is dropped on the app root and the Settings
    panel that started the flow never gets to explain what happened; without
    ``flow`` a desktop refusal is sent to a web URL inside a Tauri webview,
    which is a window that never closes and never says why.
    """

    def __init__(
        self,
        code: str,
        message: str = "Access denied",
        *,
        next_path: str | None = None,
        flow: str = "web",
    ) -> None:
        super().__init__(code, message)
        self.next_path = next_path
        self.flow = flow


#: Key that marks a state payload as a LINK rather than a sign-in, carrying the
#: id of the account the identity will be attached to. Its presence is the only
#: thing that selects the link branch at the callback, and it can only be
#: written by ``begin_link``, which requires a session.
_LINK_KEY = "link_user_id"

#: Namespace + TTL for the desktop PENDING LINK code (AM-6 desktop).
#: Sibling of the exchange code below and deliberately a different namespace:
#: one is redeemed for a token, the other authorises attaching an identity, and
#: they must not be interchangeable. Same 60s reasoning.
_LINK_XC_NAMESPACE = "social_link_xc"
_LINK_XC_TTL_SECONDS = 60

#: Namespace + TTL for the desktop one-time exchange code (AM-5).
#: Sixty seconds is generous for "redirect fires, Tauri window emits, app
#: POSTs" and short enough that a code lifted from shell history or a proxy
#: log is dead before it is useful.
_XC_NAMESPACE = "social_xc"
_XC_TTL_SECONDS = 60


def redirect_uri() -> str:
    explicit = os.environ.get("POCKETPAW_SOCIAL_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("POCKETPAW_PUBLIC_BASE_URL", "http://localhost:8888").rstrip("/")
    return f"{base}/api/v1/auth/social/callback"


def frontend_base_url() -> str:
    """Where the SPA lives.

    The desktop branch bounces through the FRONTEND origin, not the backend:
    /oauth-callback is a SvelteKit route served by Vite/Tauri on :1420, and
    redirecting to the backend origin would land on nothing. Same variable the
    codeconnect GitHub callback already uses.
    """
    return os.environ.get("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420").rstrip("/")


# ---------------------------------------------------------------------------
# Begin
# ---------------------------------------------------------------------------


def _usable_provider(provider_name: str):
    """Resolve a provider this deployment can actually complete a flow with."""
    provider = get_provider(provider_name)
    if provider is None:
        raise ValidationError("social.unknown_provider", f"Unknown provider: {provider_name}")
    if not provider.is_configured():
        # Operational, not a bug: this deployment has no credentials for it.
        raise CloudError(
            503,
            "social.provider_not_configured",
            f"{provider.name} sign-in is not configured on this server",
        )
    return provider


async def _issue_authorize_url(provider, *, extra: dict[str, Any]) -> str:
    """Mint the PKCE pair + state, and return where to send the browser.

    ``extra`` is folded into the state payload. Shared by sign-in and linking
    so the two cannot drift on the parts that make state safe — a link flow
    that quietly lost PKCE or the nonce would still work, and still be wrong.
    """
    verifier, challenge = _oauth_state.pkce_pair()
    nonce = _oauth_state.new_nonce()
    state = await _oauth_state.issue(
        _STATE_NAMESPACE,
        {**extra, "code_verifier": verifier, "nonce": nonce},
    )
    return provider.authorize_url(
        state=state,
        redirect_uri=redirect_uri(),
        code_challenge=challenge,
        nonce=nonce,
    )


async def begin_login(
    provider_name: str, *, flow: str = "web", next_path: str | None = None
) -> str:
    """Build the provider's authorize URL and persist the flow state."""
    provider = _usable_provider(provider_name)
    if flow not in _FLOWS:
        raise ValidationError("social.unknown_flow", f"Unknown flow: {flow}")

    return await _issue_authorize_url(
        provider,
        extra={
            "provider": provider.name,
            "flow": flow,
            "next": next_path or "",
        },
    )


async def begin_link(
    user: _UserDoc,
    provider_name: str,
    *,
    flow: str = "web",
    next_path: str | None = None,
) -> str:
    """Begin an OAuth flow that ATTACHES the result to ``user``.

    ``user`` comes from the route's session dependency, and its id is pinned
    into the state. What happens at the callback then depends on ``flow``,
    because the two clients prove who they are by different means:

      * **web** — the callback carries the session cookie, so it re-checks the
        cookie against the pinned id and attaches there and then.
      * **desktop** — the callback carries NOTHING. The Tauri client
        authenticates with a bearer from localStorage, and a webview opened at
        the provider has no cookie for our origin (verified: the desktop login
        path returns a token and sets no cookie at all). So the callback
        cannot authenticate anyone, does not attach, and instead parks the
        identity behind a one-time code the app redeems from
        ``POST /auth/social/link/complete`` with its bearer.

    Unknown flows are refused rather than defaulted. Silently falling back to
    "web" would send a desktop client down the branch whose session check it
    can never satisfy, and the symptom — a webview that consents successfully
    and then does nothing — looks like a frontend bug for as long as it takes
    someone to read this function.

    The SSO check runs here as well as at the callback. It is not redundant:
    at the callback a refusal is a redirect the user reads after a round trip
    through the provider, whereas here it is an immediate, explainable error
    in the Settings panel before they hand any consent to Google.
    """
    provider = _usable_provider(provider_name)
    if flow not in _FLOWS:
        raise ValidationError("social.unknown_flow", f"Unknown flow: {flow}")

    if await _sso_enforced_for(user):
        raise Forbidden(
            domain.REFUSE_SSO_ENFORCED,
            "Your workspace requires signing in through its identity provider, "
            "so accounts cannot be connected here.",
        )

    return await _issue_authorize_url(
        provider,
        extra={
            "provider": provider.name,
            # Pinned into the state, never read off the callback's query
            # string — a callback URL is attacker-influenced, and this value
            # selects which proof-of-identity the callback demands.
            "flow": flow,
            "next": next_path or "",
            _LINK_KEY: str(user.id),
        },
    )


# ---------------------------------------------------------------------------
# Lookups (pre-tenant by nature)
# ---------------------------------------------------------------------------


async def _find_by_oauth_account(provider: str, account_id: str) -> _UserDoc | None:
    # global-read: resolving "which account owns this provider identity" is a
    # pre-tenant identity question; there is no workspace to scope it to.
    return await _UserDoc.find_one(
        {"oauth_accounts.oauth_name": provider, "oauth_accounts.account_id": account_id}
    )


async def _find_by_email(email: str) -> _UserDoc | None:
    # global-read: same — an email identifies a person across all tenants.
    return await _UserDoc.find_one(_UserDoc.email == email.lower())


async def _sso_enforced_for(user: _UserDoc) -> bool:
    """Whether any workspace this user belongs to mandates SSO.

    Fails CLOSED: if the workspace lookup errors we treat SSO as enforced
    rather than waving the login through, because the failure mode of guessing
    wrong is "the enterprise control we sell has a bypass".
    """
    # WorkspaceMembership.workspace is a STRING id; _id is an ObjectId. Querying
    # $in with the raw strings matches nothing, which would silently disable
    # this guard entirely rather than fail loudly. Caught by
    # test_enforced_sso_refuses_social_sign_in.
    ids: list[PydanticObjectId] = []
    for m in user.workspaces or []:
        try:
            ids.append(PydanticObjectId(m.workspace))
        except Exception:  # noqa: BLE001 — a malformed id cannot match anything
            logger.warning("social: skipping unparseable workspace id on user %s", user.id)
    if not ids:
        return False
    try:
        # global-read: the user's own memberships, resolved by id.
        async for ws in _WorkspaceDoc.find({"_id": {"$in": ids}}):
            cfg = getattr(ws, "sso_config", None)
            if cfg is not None and getattr(cfg, "enforced", False):
                return True
    except Exception:  # noqa: BLE001 — see docstring; fail closed
        logger.exception("social: SSO enforcement check failed; refusing")
        return True
    return False


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


async def complete_callback(
    code: str, state: str, *, session_user: _UserDoc | None = None
) -> dict[str, Any]:
    """Redeem the callback and return ``{mode, user, flow, next}``.

    ``mode`` is ``"login"`` or ``"link"``, decided by the state payload alone —
    the callback's query string is attacker-influenced and says nothing about
    which flow this is.

    ``session_user`` is the account (if any) whose cookie arrived with the
    callback. Ignored for sign-in, where by definition there is no session yet;
    required to match for a link.

    Raises ``Forbidden`` with a ``domain.REFUSE_*`` code when policy refuses;
    the router turns that into the error redirect the dialog renders as
    guidance rather than as a failure.
    """
    payload = await _oauth_state.consume(_STATE_NAMESPACE, state)

    provider_name = str(payload.get("provider") or "")
    provider = get_provider(provider_name)
    if provider is None:
        raise Forbidden("social.invalid_state", "Login state named an unknown provider")

    flow = payload.get("flow") if payload.get("flow") in _FLOWS else "web"
    next_path = str(payload.get("next") or "") or None

    link_user_id = str(payload.get(_LINK_KEY) or "")

    try:
        identity = await provider.exchange(
            code=code,
            redirect_uri=redirect_uri(),
            code_verifier=payload.get("code_verifier"),
            nonce=payload.get("nonce"),
        )
    except Exception as exc:  # noqa: BLE001 — provider / network failure
        if not link_user_id:
            raise
        # A LINK that dies here still has to land somewhere its client can act
        # on. Re-raised as LinkRefused carrying the flow, so a desktop webview
        # gets a URL it closes on instead of the sign-in dialog it cannot use.
        logger.exception("social: provider exchange failed during a %s link", flow)
        raise LinkRefused(
            "social.callback_failed",
            "We couldn't finish connecting that account. Try again.",
            next_path=next_path,
            flow=flow,
        ) from exc

    if link_user_id:
        if flow == "desktop":
            # Attach NOTHING here. This request arrived from a Tauri webview,
            # which holds no cookie for our origin — the desktop client
            # authenticates with a bearer that a provider redirect cannot
            # carry. There is therefore no way to authenticate the acting user
            # at this point, and attaching on the strength of the state alone
            # is exactly the theft the web branch's session check exists to
            # prevent. Park it instead; the app redeems it under its bearer.
            link_code = await _park_pending_link(link_user_id, identity)
            return {
                "mode": "link_pending",
                "user": None,
                "provider": identity.provider,
                "link_code": link_code,
                "flow": "desktop",
                "next": next_path,
            }
        user = await _complete_link(link_user_id, identity, session_user, next_path=next_path)
        return {
            "mode": "link",
            "user": user,
            "provider": identity.provider,
            "flow": "web",
            "next": next_path,
        }

    user = await _resolve_user(identity)
    return {
        "mode": "login",
        "user": user,
        "provider": identity.provider,
        "flow": flow,
        "next": next_path,
    }


async def _complete_link(
    link_user_id: str,
    identity: SocialIdentity,
    session_user: _UserDoc | None,
    *,
    next_path: str | None = None,
) -> _UserDoc:
    """Attach ``identity`` to the account that started this link flow.

    The session check is first and is the whole defence: state is a bearer
    secret, so without it anyone holding a stolen link state could attach
    their own provider account to the victim's and sign in as them
    afterwards. With it, an attacker needs the victim's session too — at
    which point they no longer need this endpoint.
    """
    if session_user is None or str(session_user.id) != link_user_id:
        logger.warning(
            "social: link callback session mismatch (state named %s, session is %s)",
            link_user_id,
            None if session_user is None else session_user.id,
        )
        raise LinkRefused(
            domain.REFUSE_LINK_SESSION_MISMATCH,
            "Sign in and start connecting the account again.",
            next_path=next_path,
        )

    return await _apply_link_policy(session_user, identity, next_path=next_path)


async def _apply_link_policy(
    user: _UserDoc, identity: SocialIdentity, *, next_path: str | None = None
) -> _UserDoc:
    """Decide and perform the attach, for an ALREADY-AUTHENTICATED ``user``.

    Shared by both clients on purpose. Web reaches it from the callback after
    a cookie check; desktop reaches it from ``complete_pending_link`` after a
    bearer check. Everything past "who is this, provably" is identical, and
    keeping it in one function is what stops the desktop path quietly growing
    a weaker version of the takeover and SSO rules.
    """
    owner = await _find_by_oauth_account(identity.provider, identity.account_id)
    decision = domain.decide_link(
        identity=identity,
        owner_user_id=str(owner.id) if owner else None,
        acting_user_id=str(user.id),
    )
    decision = domain.apply_sso_guard(decision, enforced=await _sso_enforced_for(user))

    if decision.refused:
        logger.info(
            "social: refused to link %s identity to user %s reason=%s",
            identity.provider,
            user.id,
            decision.reason,
        )
        await _audit(
            user,
            "auth.social.link_refused",
            identity.provider,
            reason=decision.reason or "",
        )
        raise LinkRefused(
            decision.reason or domain.REFUSE_UNVERIFIED,
            _link_refusal_message(decision),
            next_path=next_path,
        )

    if decision.action == "noop":
        return user

    linked = await _attach_account(user, identity)
    await _audit(linked, "auth.social.linked", identity.provider)
    return linked


# ---------------------------------------------------------------------------
# Desktop link handoff — park at the callback, redeem under the bearer
# ---------------------------------------------------------------------------


async def _park_pending_link(link_user_id: str, identity: SocialIdentity) -> str:
    """Stash a consented-but-unattached identity behind a one-time code.

    Holds the identity fields rather than a re-runnable provider code: the
    authorization code is single-use and already spent by the time we get
    here, so there is nothing to replay even if this record leaked.

    And a leaked code buys nothing on its own. Redemption requires an
    authenticated caller who IS ``link_user_id`` — so unlike the web
    callback, where possession of the state was the only thing standing
    between an attacker and an attach, here possession is not sufficient at
    all. That makes this handoff strictly stronger than the cookie check it
    replaces, not a relaxation of it for desktop's convenience.
    """
    return await _oauth_state.issue(
        _LINK_XC_NAMESPACE,
        {
            _LINK_KEY: link_user_id,
            "provider": identity.provider,
            "account_id": identity.account_id,
            "email": identity.email or "",
            "full_name": identity.full_name or "",
            "avatar": identity.avatar or "",
        },
        ttl_seconds=_LINK_XC_TTL_SECONDS,
    )


async def complete_pending_link(user: _UserDoc, link_code: str) -> dict[str, Any]:
    """Finish a desktop link: redeem the parked identity as ``user``.

    ``user`` comes from the route's ``current_active_user`` dependency, which
    the desktop client satisfies with its bearer. That is the whole point of
    this endpoint existing — it moves the "prove you are the account this link
    was started for" check off the callback, which desktop cannot satisfy, and
    onto a request it can.

    The pinned-id comparison is the same property the web callback enforces
    against the cookie, so neither client can attach an identity to an account
    that is not theirs.
    """
    payload = await _oauth_state.consume(_LINK_XC_NAMESPACE, link_code)

    parked_user_id = str(payload.get(_LINK_KEY) or "")
    if not parked_user_id or parked_user_id != str(user.id):
        logger.warning(
            "social: pending-link redeemed by the wrong account (parked for %s, caller %s)",
            parked_user_id,
            user.id,
        )
        raise LinkRefused(
            domain.REFUSE_LINK_SESSION_MISMATCH,
            "Sign in and start connecting the account again.",
        )

    try:
        identity = SocialIdentity(
            provider=str(payload.get("provider") or ""),
            account_id=str(payload.get("account_id") or ""),
            email=str(payload.get("email") or "") or None,
            full_name=str(payload.get("full_name") or ""),
            avatar=str(payload.get("avatar") or ""),
        )
    except ValueError as exc:
        # SocialIdentity refuses an empty provider / account_id. A payload that
        # cannot rebuild one is a corrupt record, not a policy decision.
        raise LinkRefused(
            "social.invalid_link_code", "That connection attempt is no longer valid."
        ) from exc

    linked = await _apply_link_policy(user, identity)
    return {"user": linked, "provider": identity.provider}


def _link_refusal_message(decision: domain.LinkDecision) -> str:
    if decision.reason == domain.REFUSE_SSO_ENFORCED:
        return "Your workspace requires signing in through its identity provider."
    if decision.reason == domain.REFUSE_IDENTITY_CLAIMED:
        return (
            "That account is already connected to a different PocketPaw user. "
            "Disconnect it there first."
        )
    return (
        "We couldn't confirm a verified email from that account. "
        "Verify your email with the provider, then try again."
    )


# ---------------------------------------------------------------------------
# Connected accounts (AM-6)
# ---------------------------------------------------------------------------


def _has_usable_password(user: _UserDoc) -> bool:
    """Whether this account can actually be signed into with a password.

    NOT ``bool(user.hashed_password)``, and the difference is the bug this
    function exists to prevent. Accounts minted by the social path
    (``_create_user`` below) and by SSO JIT provisioning (``sso/service.py``)
    store an UNUSABLE sentinel — ``!social-only-…`` / ``!sso-only-…`` — rather
    than an empty string, because an empty hash can compare-equal in some
    verifiers. So the naive truthiness check answers "yes, they have a
    password" for exactly the population that has none, and would happily let
    them unlink their only identity and lock themselves out.

    The test is positive rather than a blocklist of known sentinels: pwdlib
    and passlib hashes are Modular Crypt Format and begin with ``$``
    (``$argon2id$…``, ``$2b$…``). Anything else is not something we can verify
    a password against, so a sentinel added later needs no change here. Being
    wrong in this direction costs a user one refusal they can resolve by
    setting a password; being wrong in the other direction is permanent.
    """
    raw = getattr(user, "hashed_password", "") or ""
    return raw.startswith("$")


async def list_identities(user: _UserDoc) -> list[dict[str, Any]]:
    """The provider identities attached to ``user``.

    Returns only what the panel renders. The stored row also holds an
    ``access_token`` field and the provider's account id; neither is echoed,
    because an endpoint that reads a credential store should hand back the
    minimum that answers the question asked.
    """
    return [
        {
            "provider": account.oauth_name,
            "account_email": account.account_email or "",
            "linked_at": getattr(account, "linked_at", None),
        }
        for account in (user.oauth_accounts or [])
    ]


async def unlink_identity(user: _UserDoc, provider_name: str) -> None:
    """Detach a provider identity, unless it is the last way in.

    Note what is NOT consulted: whether the user could sign in through their
    workspace's IdP. An SSO-provisioned member with one linked identity and no
    password is refused here even though SSO would still let them in. That is
    a deliberate over-refusal — resolving it means setting a password, whereas
    guessing wrong the other way means an account nobody can reach.
    """
    decision = domain.decide_unlink(
        provider=provider_name,
        linked_providers=[a.oauth_name for a in (user.oauth_accounts or [])],
        has_password=_has_usable_password(user),
    )

    if not decision.allowed:
        if decision.reason == domain.REFUSE_NOT_LINKED:
            # 404 for the status, but the DOMAIN's code on the wire. Building
            # this as NotFound("social identity", …) produced the code
            # "social identity.not_found" — NotFound derives it as
            # f"{resource}.not_found", so the human-readable resource name
            # became a machine-readable identifier, space and all. Clients then
            # cannot key on the constant the domain defines for exactly this
            # refusal, and the frontend had already grown a second lookup entry
            # to cope. Name the code explicitly instead.
            raise CloudError(
                404,
                domain.REFUSE_NOT_LINKED,
                f"No {provider_name} account is connected to your profile.",
            )
        raise ConflictError(
            domain.REFUSE_LAST_CREDENTIAL,
            "That is the only way you can sign in. Set a password, or connect "
            "another account, before disconnecting this one.",
        )

    user.oauth_accounts = [a for a in (user.oauth_accounts or []) if a.oauth_name != provider_name]
    await user.save()
    logger.info("social: unlinked %s from user %s", provider_name, user.id)
    await _audit(user, "auth.social.unlinked", provider_name)


async def _audit(user: _UserDoc, action: str, provider: str, **extra: str) -> None:
    """Best-effort audit row for a credential change.

    Attaching or removing a way into an account is exactly what an audit trail
    is for. It is skipped for a user with no active workspace because audit
    rows are workspace-scoped and there is no tenant to file it under — the
    ``logger`` calls at each site remain the record in that case.

    ``audit_service.record`` never raises, so no failure here can block the
    credential change that has already been persisted.
    """
    workspace_id = getattr(user, "active_workspace", None)
    if not workspace_id:
        return
    # Local import: auth.core is imported broadly, and the audit package pulls
    # in Beanie models the OSS-only startup path has no use for.
    from pocketpaw_ee.cloud.audit import service as audit_service

    await audit_service.record(
        str(workspace_id),
        str(user.id),
        action,
        target_type="user",
        target_id=str(user.id),
        metadata={"provider": provider, **extra},
    )


async def _resolve_user(identity: SocialIdentity) -> _UserDoc:
    linked = await _find_by_oauth_account(identity.provider, identity.account_id)
    # Only ever look up by an address the provider vouched for. `identity.email`
    # is None unless the adapter verified it, which is what makes this safe.
    by_email = (
        await _find_by_email(identity.email)
        if (linked is None and identity.has_verified_email and identity.email)
        else None
    )

    decision = domain.decide(
        identity=identity,
        linked_user_id=str(linked.id) if linked else None,
        email_user_id=str(by_email.id) if by_email else None,
    )

    target = linked or by_email
    enforced = await _sso_enforced_for(target) if target is not None else False
    decision = domain.apply_sso_guard(decision, enforced=enforced)

    if decision.refused:
        logger.info(
            "social: refused %s identity (provider_account=%s) reason=%s",
            identity.provider,
            identity.account_id,
            decision.reason,
        )
        raise Forbidden(decision.reason or domain.REFUSE_UNVERIFIED, _refusal_message(decision))

    if decision.action == "sign_in":
        assert linked is not None  # decide() only returns sign_in with a link
        return linked

    if decision.action == "link":
        assert by_email is not None
        return await _attach_account(by_email, identity)

    return await _create_user(identity)


def _refusal_message(decision: domain.LinkDecision) -> str:
    if decision.reason == domain.REFUSE_SSO_ENFORCED:
        return "Your workspace requires signing in through its identity provider."
    return (
        "We couldn't confirm a verified email from that account. "
        "Sign in with your password, then connect it from Settings."
    )


async def _attach_account(user: _UserDoc, identity: SocialIdentity) -> _UserDoc:
    """Bind a provider identity to an existing account."""
    already = any(
        a.oauth_name == identity.provider and a.account_id == identity.account_id
        for a in (user.oauth_accounts or [])
    )
    if not already:
        user.oauth_accounts.append(
            _OAuthAccount(
                oauth_name=identity.provider,
                account_id=identity.account_id,
                account_email=identity.email or "",
                linked_at=datetime.now(UTC),
                # We do not keep provider access tokens. Sign-in needs identity,
                # not ongoing API access; storing a token we never use is
                # avoidable breach surface. Repo access is codeconnect's job.
                access_token="",  # noqa: S106 — intentionally empty, not a secret
            )
        )
    if not user.avatar and identity.avatar:
        user.avatar = identity.avatar
    if not user.full_name and identity.full_name:
        user.full_name = identity.full_name
    await user.save()
    return user


async def _create_user(identity: SocialIdentity) -> _UserDoc:
    """Create an account from a provider identity.

    ``is_verified=True`` is honest here and only here: we reached this branch
    because the provider asserted the address is verified.

    ``hashed_password`` gets an unusable sentinel rather than an empty string.
    An empty hash can compare-equal in some verifier implementations, which
    would mean "no password set" reads as "any password works".
    """
    import secrets

    assert identity.email  # decide() only returns create with a verified email

    user = _UserDoc(
        email=identity.email.lower(),
        hashed_password="!social-only-" + secrets.token_urlsafe(48),
        is_active=True,
        is_verified=True,
        is_superuser=False,
        full_name=identity.full_name or "",
        avatar=identity.avatar or "",
        oauth_accounts=[
            _OAuthAccount(
                oauth_name=identity.provider,
                account_id=identity.account_id,
                account_email=identity.email,
                linked_at=datetime.now(UTC),
                access_token="",  # noqa: S106 — see _attach_account
            )
        ],
        last_seen=datetime.now(UTC),
    )
    await user.insert()
    logger.info("social: created account for %s via %s", user.email, identity.provider)
    return user


# ---------------------------------------------------------------------------
# Desktop exchange code (AM-5)
# ---------------------------------------------------------------------------


async def issue_exchange_code(user_id: str) -> str:
    """Mint a single-use code the desktop client trades for a bearer token.

    The redirect carries THIS, never a token. A token in a URL leaks through
    browser history, Referer headers, window titles, and any proxy log on the
    path; a 60-second single-use reference does not, and is worthless the
    instant it has been redeemed.
    """
    return await _oauth_state.issue(
        _XC_NAMESPACE, {"user_id": user_id}, ttl_seconds=_XC_TTL_SECONDS
    )


async def redeem_exchange_code(xc: str) -> _UserDoc:
    """Consume an exchange code and return its user.

    Raises ``Forbidden`` on anything unexpected: unknown, already spent,
    expired, or naming an account that has since been deactivated. The state
    store's GET-then-DEL means two concurrent redemptions cannot both win.
    """
    payload = await _oauth_state.consume(_XC_NAMESPACE, xc)
    user_id = payload.get("user_id")
    if not user_id:
        raise Forbidden("social.invalid_exchange_code", "Exchange code is not valid")

    try:
        user = await _UserDoc.get(PydanticObjectId(str(user_id)))
    except Exception as exc:  # noqa: BLE001 — malformed id is just an invalid code
        raise Forbidden("social.invalid_exchange_code", "Exchange code is not valid") from exc

    # Re-check liveness at redemption: the account may have been deactivated in
    # the seconds between the callback and this call.
    if user is None or not getattr(user, "is_active", False):
        raise Forbidden("social.invalid_exchange_code", "Exchange code is not valid")
    return user

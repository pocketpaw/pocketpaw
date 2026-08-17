"""Social sign-in linking policy — the decision table, as pure functions.

Created 2026-07-29 (AM-4). The IO lives in ``service.py``; the *policy* lives
here so every branch can be tested without a database, and so the rule can be
read in one screen.

The policy, in order:

    1. This provider account is ALREADY linked        -> sign in
    2. No verified email from the provider            -> REFUSE
    3. Verified email matches an existing account     -> link, then sign in
    4. Verified email, no existing account            -> create, link, sign in

Step 1 comes before step 2 deliberately. A returning user whose provider has
since stopped vouching for their address (they removed it, or declined the
`user:email` scope on a re-consent) is still *the same account* — we matched on
the provider's immutable id, not on an email — so locking them out would be
wrong. Verification only gates the step that BINDS an identity to an account
it was not previously bound to.

Step 2 before step 3 is the account-takeover defence. Matching an unverified
address against an existing account is how an attacker attaches victim@corp.com
to their own provider profile and walks into the victim's account.

Updated 2026-08-01 (AM-6) with the SETTINGS-side policy — ``decide_link`` and
``decide_unlink``, for a user who already has a session and is managing their
connected accounts. Two rules there are worth stating up front, because both
are the difference between a feature and a vulnerability:

    * **Linking must not steal.** If the incoming provider identity already
      belongs to a DIFFERENT account, refuse. Re-pointing it would hand the
      attacker whatever that identity could previously sign into, and would
      silently strip a credential from its real owner.
    * **Never remove the last credential.** Unlinking the only way into an
      account locks its owner out permanently, and support cannot undo it.

A note on the cloud code rules: ``domain.py`` objects normally carry required
tenancy fields. These deliberately do not. Sign-in happens BEFORE a tenant is
known — the user may have no workspace at all (they land in the /welcome
funnel) — so there is no ``workspace_id`` to construct with. Tenancy is
enforced downstream, once a session exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pocketpaw_ee.cloud.auth.social.providers.base import SocialIdentity

#: What the caller should do with this identity.
#:
#: The first three are sign-in outcomes from :func:`decide`. ``attach`` and
#: ``noop`` are link outcomes from :func:`decide_link` — a separate pair rather
#: than reusing ``link``/``sign_in`` because the two paths differ in what
#: authorises them: sign-in matched on a provider-verified email, linking is
#: authorised by the caller's existing session.
LinkAction = Literal["sign_in", "link", "create", "attach", "noop", "refuse"]

#: Refusal codes. These reach the frontend as ``?reason=`` on the error
#: redirect, so each maps to a specific piece of UI copy.
REFUSE_UNVERIFIED = "auth.unverified_link"
REFUSE_SSO_ENFORCED = "auth.sso_enforced"

#: The incoming identity is already attached to somebody else's account.
REFUSE_IDENTITY_CLAIMED = "auth.identity_claimed"
#: The callback's session is not the account that started the link.
REFUSE_LINK_SESSION_MISMATCH = "auth.link_session_mismatch"
#: Unlinking this would leave the account with no way to sign in.
REFUSE_LAST_CREDENTIAL = "auth.last_credential"
#: Nothing to unlink — no identity from that provider is attached.
REFUSE_NOT_LINKED = "auth.not_linked"


@dataclass(frozen=True)
class LinkDecision:
    action: LinkAction
    #: The account to act on. None for "create" and for refusals.
    user_id: str | None = None
    #: Set only when ``action == "refuse"``.
    reason: str | None = None

    @property
    def refused(self) -> bool:
        return self.action == "refuse"


def decide(
    *,
    identity: SocialIdentity,
    linked_user_id: str | None,
    email_user_id: str | None,
) -> LinkDecision:
    """Resolve a provider identity to an action.

    ``linked_user_id`` is the account already carrying this
    ``(provider, account_id)`` pair — the strongest possible match, since the
    provider's id is immutable.

    ``email_user_id`` is an account whose email equals the identity's, and it
    MUST have been looked up using ``identity.email``, which adapters only
    populate when the provider vouched for the address. Passing a
    caller-supplied or unverified address here would defeat the whole policy.
    """
    # 1. Already linked. Matched on the provider's immutable id, so the state
    #    of their email today is irrelevant — this is the same person.
    if linked_user_id:
        return LinkDecision(action="sign_in", user_id=linked_user_id)

    # 2. Nothing the provider will vouch for. Binding on an unverified address
    #    is the takeover path, so refuse rather than guess.
    if not identity.has_verified_email:
        return LinkDecision(action="refuse", reason=REFUSE_UNVERIFIED)

    # 3. Verified address belonging to someone we already know.
    if email_user_id:
        return LinkDecision(action="link", user_id=email_user_id)

    # 4. Verified address, nobody has it.
    return LinkDecision(action="create")


@dataclass(frozen=True)
class UnlinkDecision:
    """Whether an identity may be detached, and why not."""

    allowed: bool
    #: Set only when ``allowed`` is False.
    reason: str | None = None


def decide_link(
    *,
    identity: SocialIdentity,
    owner_user_id: str | None,
    acting_user_id: str,
) -> LinkDecision:
    """Resolve "may this signed-in user attach this identity to themselves?".

    ``owner_user_id`` is the account already carrying this
    ``(provider, account_id)`` pair, or None. ``acting_user_id`` is the caller,
    established from their SESSION — never from the request body. A link
    endpoint that takes the target user from the payload is an
    account-takeover primitive, which is why this function has no way to
    express one.

    Unlike :func:`decide`, no email matching happens here at all: the session
    already says who this is, so the identity's address is not a join key and
    cannot be used as one.
    """
    # Already mine. Idempotent — clicking "Connect" twice is not an error, and
    # reporting one would send the UI into a failure state over a no-op.
    if owner_user_id is not None and owner_user_id == acting_user_id:
        return LinkDecision(action="noop", user_id=acting_user_id)

    # Somebody else's. The takeover case: re-pointing the identity would let
    # the claimant sign into THIS account, and would silently strip a
    # credential from the account that legitimately holds it. Neither is ever
    # what the user meant, so refuse instead of guessing which.
    if owner_user_id is not None:
        return LinkDecision(action="refuse", reason=REFUSE_IDENTITY_CLAIMED)

    # No verified address. Not a takeover risk here — the session, not the
    # email, authorises this — but it breaks a system-wide invariant that
    # something else depends on: every row in ``oauth_accounts`` was
    # established from a provider-verified identity. ``decide`` step 1 leans on
    # exactly that when it signs a returning user in on a link alone, without
    # re-checking verification. Admitting an unverified identity through this
    # door would quietly remove the foundation that shortcut stands on.
    if not identity.has_verified_email:
        return LinkDecision(action="refuse", reason=REFUSE_UNVERIFIED)

    return LinkDecision(action="attach", user_id=acting_user_id)


def decide_unlink(
    *,
    provider: str,
    linked_providers: tuple[str, ...] | list[str],
    has_password: bool,
) -> UnlinkDecision:
    """Resolve "may this identity be detached?".

    Refuses to remove the last credential. ``has_password`` MUST come from a
    real check that the stored hash is usable, not from ``bool(hashed_password)``
    — accounts created by the social and SSO paths carry an unusable sentinel
    in that field, so the naive check reads "has a password" for precisely the
    users who have none. See ``service._has_usable_password``.

    Erring toward refusal is deliberate. A false refusal is an inconvenience
    the user resolves by setting a password; a false allow is a permanent
    lockout that support cannot reverse.
    """
    if provider not in linked_providers:
        return UnlinkDecision(allowed=False, reason=REFUSE_NOT_LINKED)

    remaining = [p for p in linked_providers if p != provider]
    if not remaining and not has_password:
        return UnlinkDecision(allowed=False, reason=REFUSE_LAST_CREDENTIAL)

    return UnlinkDecision(allowed=True)


def apply_sso_guard(decision: LinkDecision, *, enforced: bool) -> LinkDecision:
    """Refuse a decision that would bypass a workspace's enforced SSO.

    Applied after ``decide`` because it depends on the workspace of whichever
    account the decision landed on. A workspace that has turned SSO on is
    paying for the guarantee that its members authenticate through the IdP;
    consumer Google must not become the documented way around it.

    ``create`` is never guarded: a brand-new account belongs to no workspace,
    so there is nothing to enforce yet.

    It governs the AM-6 link outcomes (``attach`` / ``noop``) too, and that is
    the point of routing them through here rather than writing a second guard.
    A link is not itself a bypass — the sign-in path re-runs this check every
    time, so an identity attached under enforced SSO still could not be spent.
    But it would be a bypass lying in wait for the day that check regresses,
    and it stores exactly the credential the org turned this control on to
    keep out. Refusing at connect-time also beats connecting an identity that
    then silently never works.
    """
    if not enforced or decision.action in ("refuse", "create"):
        return decision
    return LinkDecision(action="refuse", reason=REFUSE_SSO_ENFORCED)

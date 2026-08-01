"""Social sign-in linking policy (AM-4) — the account-takeover boundary.

This is the highest-stakes table in the feature. Getting row 3 wrong hands an
attacker someone else's account: they attach victim@corp.com to their own
GitHub or Google profile, sign in, and the linker binds them to the victim.

The policy is pure, so every branch is exercised here directly. Reversal cost
is the reason for the coverage: once identities are bound under a wrong policy,
undoing it means unpicking oauth_accounts rows per user.

Extended 2026-08-01 (AM-6) with the settings-side table — ``decide_link`` and
``decide_unlink``. Same argument for the coverage: linking the wrong way hands
over an account, and unlinking the wrong way destroys access to one.
"""

import pytest
from pocketpaw_ee.cloud.auth.social.domain import (
    REFUSE_IDENTITY_CLAIMED,
    REFUSE_LAST_CREDENTIAL,
    REFUSE_NOT_LINKED,
    REFUSE_SSO_ENFORCED,
    REFUSE_UNVERIFIED,
    LinkDecision,
    apply_sso_guard,
    decide,
    decide_link,
    decide_unlink,
)
from pocketpaw_ee.cloud.auth.social.providers.base import SocialIdentity


def ident(*, email: str | None = "dev@corp.com", account_id: str = "acct-1") -> SocialIdentity:
    return SocialIdentity(provider="github", account_id=account_id, email=email)


# ---------------------------------------------------------------------------
# Row 1 — already linked
# ---------------------------------------------------------------------------


def test_an_already_linked_account_signs_in():
    d = decide(identity=ident(), linked_user_id="u1", email_user_id=None)
    assert d == LinkDecision(action="sign_in", user_id="u1")


def test_an_already_linked_account_signs_in_even_with_NO_verified_email():
    # They removed the address, or declined user:email on a re-consent. We
    # matched on the provider's immutable id, not on an email, so this is
    # still the same person and locking them out would be wrong.
    d = decide(identity=ident(email=None), linked_user_id="u1", email_user_id=None)
    assert d.action == "sign_in"
    assert d.user_id == "u1"


def test_the_existing_link_wins_over_an_email_match_on_a_different_account():
    # The provider id is the stronger signal: emails change hands, ids do not.
    d = decide(identity=ident(), linked_user_id="u1", email_user_id="u2")
    assert d.user_id == "u1"


# ---------------------------------------------------------------------------
# Row 2 — no verified email  (THE takeover defence)
# ---------------------------------------------------------------------------


def test_an_unverified_identity_is_refused_when_the_email_matches_an_account():
    # The attack: attacker adds victim@corp.com to their own provider account
    # without verifying it. If this linked, they would land inside u2.
    d = decide(identity=ident(email=None), linked_user_id=None, email_user_id="u2")
    assert d.action == "refuse"
    assert d.reason == REFUSE_UNVERIFIED
    assert d.user_id is None


def test_an_unverified_identity_is_refused_even_with_no_account_to_match():
    # Never silently create an account on an address nobody vouched for -
    # that would squat an email its real owner may later want.
    d = decide(identity=ident(email=None), linked_user_id=None, email_user_id=None)
    assert d.action == "refuse"
    assert d.reason == REFUSE_UNVERIFIED


def test_refusal_never_carries_a_user_id():
    d = decide(identity=ident(email=None), linked_user_id=None, email_user_id="u2")
    assert d.user_id is None
    assert d.refused is True


# ---------------------------------------------------------------------------
# Rows 3 and 4 — the happy paths
# ---------------------------------------------------------------------------


def test_a_verified_email_links_to_the_existing_account():
    d = decide(identity=ident(), linked_user_id=None, email_user_id="u2")
    assert d == LinkDecision(action="link", user_id="u2")


def test_a_verified_email_with_no_account_creates_one():
    d = decide(identity=ident(), linked_user_id=None, email_user_id=None)
    assert d == LinkDecision(action="create", user_id=None)


# ---------------------------------------------------------------------------
# Enforced SSO — social must not be the way around it
# ---------------------------------------------------------------------------


def test_enforced_sso_refuses_a_sign_in():
    d = apply_sso_guard(LinkDecision(action="sign_in", user_id="u1"), enforced=True)
    assert d.action == "refuse"
    assert d.reason == REFUSE_SSO_ENFORCED


def test_enforced_sso_refuses_a_link():
    d = apply_sso_guard(LinkDecision(action="link", user_id="u2"), enforced=True)
    assert d.action == "refuse"
    assert d.reason == REFUSE_SSO_ENFORCED


def test_enforced_sso_does_not_touch_a_create():
    # A brand-new account belongs to no workspace, so there is no enforcement
    # to apply. Refusing here would make signup impossible for everyone the
    # moment any one workspace enabled SSO.
    d = apply_sso_guard(LinkDecision(action="create"), enforced=True)
    assert d.action == "create"


def test_the_guard_is_a_no_op_when_sso_is_not_enforced():
    original = LinkDecision(action="sign_in", user_id="u1")
    assert apply_sso_guard(original, enforced=False) == original


def test_the_guard_does_not_overwrite_an_existing_refusal_reason():
    # An unverified refusal must keep saying "unverified" - the user's next
    # step differs from the SSO case, and the copy is chosen by this code.
    original = LinkDecision(action="refuse", reason=REFUSE_UNVERIFIED)
    assert apply_sso_guard(original, enforced=True).reason == REFUSE_UNVERIFIED


# ---------------------------------------------------------------------------
# Whole-table sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verified", "linked", "by_email", "enforced", "expected_action"),
    [
        # already linked -> always sign in, unless SSO is enforced
        (True, "u1", None, False, "sign_in"),
        (False, "u1", None, False, "sign_in"),
        (True, "u1", "u2", False, "sign_in"),
        (True, "u1", None, True, "refuse"),
        # not linked, unverified -> always refuse
        (False, None, None, False, "refuse"),
        (False, None, "u2", False, "refuse"),
        (False, None, "u2", True, "refuse"),
        # not linked, verified, email matches -> link unless enforced
        (True, None, "u2", False, "link"),
        (True, None, "u2", True, "refuse"),
        # not linked, verified, no match -> create, enforcement irrelevant
        (True, None, None, False, "create"),
        (True, None, None, True, "create"),
    ],
)
def test_full_decision_table(verified, linked, by_email, enforced, expected_action):
    identity = ident(email="dev@corp.com" if verified else None)
    decision = apply_sso_guard(
        decide(identity=identity, linked_user_id=linked, email_user_id=by_email),
        enforced=enforced,
    )
    assert decision.action == expected_action


# ---------------------------------------------------------------------------
# decide_link — the settings-side table (AM-6)
# ---------------------------------------------------------------------------


def test_linking_an_unclaimed_verified_identity_attaches_it():
    d = decide_link(identity=ident(), owner_user_id=None, acting_user_id="u1")
    assert d == LinkDecision(action="attach", user_id="u1")


def test_relinking_my_own_identity_is_a_no_op_not_an_error():
    # Double-click on "Connect", or a retried callback. Reporting a failure
    # here would put the panel in an error state over a state it already has.
    d = decide_link(identity=ident(), owner_user_id="u1", acting_user_id="u1")
    assert d == LinkDecision(action="noop", user_id="u1")


def test_linking_an_identity_owned_by_SOMEONE_ELSE_is_REFUSED():
    # THE takeover case for this endpoint. Re-pointing it would let u1 sign in
    # as u2 afterwards, and would strip u2 of a credential without telling them.
    d = decide_link(identity=ident(), owner_user_id="u2", acting_user_id="u1")
    assert d.action == "refuse"
    assert d.reason == REFUSE_IDENTITY_CLAIMED


def test_the_claimed_check_runs_BEFORE_the_verification_check():
    # Ordering matters for the message, not just the outcome: telling someone
    # "verify your email" when the real problem is that the identity belongs
    # to another account sends them round a loop that can never succeed.
    d = decide_link(identity=ident(email=None), owner_user_id="u2", acting_user_id="u1")
    assert d.reason == REFUSE_IDENTITY_CLAIMED


def test_linking_an_unverified_identity_is_refused():
    # Not a takeover risk here — the session says who this is — but it keeps
    # the invariant that decide() step 1 leans on: every stored oauth_accounts
    # row was established from a provider-verified identity.
    d = decide_link(identity=ident(email=None), owner_user_id=None, acting_user_id="u1")
    assert d.action == "refuse"
    assert d.reason == REFUSE_UNVERIFIED


def test_a_link_refusal_never_carries_a_user_id():
    d = decide_link(identity=ident(email=None), owner_user_id=None, acting_user_id="u1")
    assert d.user_id is None
    assert d.refused is True


def test_no_link_decision_ever_targets_an_account_other_than_the_caller():
    # The invariant over the whole space: whatever happens, the only account
    # this table will act on is the one that asked.
    for owner in (None, "u1", "u2"):
        for email in ("dev@corp.com", None):
            d = decide_link(
                identity=ident(email=email), owner_user_id=owner, acting_user_id="u1"
            )
            assert d.user_id in (None, "u1"), (owner, email)


@pytest.mark.parametrize("action", ["attach", "noop"])
def test_enforced_sso_refuses_a_settings_link(action):
    # A link is not itself a bypass — sign-in re-checks every time — but it is
    # one lying in wait if that check ever regresses, and it stores exactly the
    # credential the org enabled this control to keep out.
    d = apply_sso_guard(LinkDecision(action=action, user_id="u1"), enforced=True)
    assert d.action == "refuse"
    assert d.reason == REFUSE_SSO_ENFORCED


# ---------------------------------------------------------------------------
# decide_unlink — never remove the last credential (AM-6)
# ---------------------------------------------------------------------------


def test_unlinking_one_of_two_identities_is_allowed_without_a_password():
    d = decide_unlink(
        provider="github", linked_providers=["github", "google"], has_password=False
    )
    assert d.allowed is True
    assert d.reason is None


def test_unlinking_the_only_identity_is_allowed_when_a_password_exists():
    d = decide_unlink(provider="github", linked_providers=["github"], has_password=True)
    assert d.allowed is True


def test_unlinking_the_LAST_credential_is_REFUSED():
    # The lockout case. Support cannot undo this, which is why it fails closed.
    d = decide_unlink(provider="github", linked_providers=["github"], has_password=False)
    assert d.allowed is False
    assert d.reason == REFUSE_LAST_CREDENTIAL


def test_unlinking_something_that_was_never_linked_reports_that_specifically():
    # Distinct from the lockout refusal: nothing is at stake, the caller just
    # named a provider they never connected.
    d = decide_unlink(provider="google", linked_providers=["github"], has_password=False)
    assert d.allowed is False
    assert d.reason == REFUSE_NOT_LINKED


def test_no_unlink_is_allowed_that_would_leave_no_way_in():
    # The invariant over the whole space, rather than one example of it.
    for providers in ([], ["github"], ["github", "google"]):
        for has_password in (False, True):
            d = decide_unlink(
                provider="github", linked_providers=providers, has_password=has_password
            )
            if d.allowed:
                remaining = [p for p in providers if p != "github"]
                assert remaining or has_password, (providers, has_password)


def test_no_path_through_the_table_binds_an_unverified_identity():
    # The invariant, stated once over the whole space: an identity the provider
    # will not vouch for can never produce a link or a create.
    for linked in (None, "u1"):
        for by_email in (None, "u2"):
            for enforced in (False, True):
                d = apply_sso_guard(
                    decide(
                        identity=ident(email=None),
                        linked_user_id=linked,
                        email_user_id=by_email,
                    ),
                    enforced=enforced,
                )
                assert d.action in ("sign_in", "refuse"), (linked, by_email, enforced)
                if d.action == "sign_in":
                    # ...and the only way through is an ALREADY-linked account,
                    # which was bound earlier under a verified email.
                    assert linked is not None

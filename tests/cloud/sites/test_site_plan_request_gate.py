# tests/cloud/sites/test_site_plan_request_gate.py — an employee asks for a paid
# site plan; approving is what buys it.
#
# Created 2026-09-01 (feat/sites-plan-purchase-request). Stacked on the
# ``sites.buy_plan`` gate (fix/sites-plan-purchase-authz), which stopped a member
# charging the company card and left them with a 403 and nowhere to go. This is
# the other half: the refusal becomes a request an admin approves.
#
# THE SECURITY PROPERTY, and the reason most of this file exists. Every other
# gated proposal kind re-checks the PROPOSER's role at execute time. This one
# cannot: the proposer is a member who never had the right, which is the premise.
# So the check runs against the APPROVER — ``Action.approved_by``, which the
# instinct router writes from the authenticated identity. Get that backwards and
# the feature refuses every request it exists to serve; leave it out and any
# pending request buys the moment anyone touches it. Both failure modes are
# pinned below.
#
# What each group covers:
#   * propose — records what was asked, canonicalizes the tier, refuses an
#     org-scoped flat at the door (it could only ever fail on approval);
#   * the approver check — the inversion above, in both directions;
#   * the tamper guards — schema, identity hash, missing approver, idempotency;
#   * price drift — reported, never silently charged, and never enforced.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class _FakeStore:
    """Enough of the instinct store for the executor's guards + back-writes.

    ``_db_path`` points nowhere: the outcome back-write is best-effort and
    swallows its own failure, so the executor's behaviour is unchanged by it and
    a test does not need a real SQLite file to observe the decisions.
    """

    def __init__(self, action: Any):
        self._db_path = "/nonexistent/instinct.db"
        self._action = action
        self.failed: list[str] = []
        self.executed: list[str] = []

    async def get_action(self, action_id: str) -> Any:  # noqa: ARG002
        return self._action

    async def mark_failed(self, action_id: str, error: str) -> None:  # noqa: ARG002
        self.failed.append(error)

    async def mark_executed(self, action_id: str, outcome: str = "") -> None:  # noqa: ARG002
        self.executed.append(outcome)


def _action(blob: dict[str, Any], *, approved_by: str = "admin-1", status: str = "approved"):
    return SimpleNamespace(
        id="act-1",
        parameters={"_site_plan_request": blob},
        approved_by=approved_by,
        status=status,
    )


def _blob(**over: Any) -> dict[str, Any]:
    """A well-formed blob, with the identity hash computed to match."""
    from pocketpaw_ee.cloud.site_plan_requests.propose import (
        SITE_PLAN_REQUEST_SCHEMA,
        compute_request_hash,
    )

    base = {
        "kind": "site_plan_request",
        "schema": SITE_PLAN_REQUEST_SCHEMA,
        "workspace_id": "ws-1",
        "pocket_id": "pkt-1",
        "site_plan_key": "staff",
        "monthly_price_usd": 19,
        "requested_by": "member-1",
        "idempotency_key": "k",
        "summary": "s",
        "correlation_id": None,
        "proposed_event_id": None,
    }
    base.update(over)
    base.setdefault(
        "params_hash",
        compute_request_hash(
            str(base["workspace_id"]), str(base["pocket_id"]), str(base["site_plan_key"])
        ),
    )
    return base


def _arm(monkeypatch, *, action, may_buy: bool = True, publish=None) -> dict[str, Any]:
    """Point the executor at doubles and record what reached the purchase.

    Returns a dict the test reads: ``published`` is the kwargs of the publish
    call, or absent when it never happened — which is the assertion that matters
    for every refusal case.
    """
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    seen: dict[str, Any] = {}
    store = _FakeStore(action)
    monkeypatch.setattr(ex, "get_instinct_store", lambda **kw: store, raising=False)
    import pocketpaw.stores as _stores

    monkeypatch.setattr(_stores, "get_instinct_store", lambda **kw: store)

    async def _recheck(workspace_id: str, approver_user_id: str) -> None:
        seen["rechecked"] = (workspace_id, approver_user_id)
        if not may_buy:
            from pocketpaw_ee.guards.rbac import Forbidden

            raise Forbidden("sites.plan_purchase_forbidden", "not an admin")

    monkeypatch.setattr(ex, "_recheck_approver_may_buy", _recheck)

    async def _publish(**kwargs: Any) -> Any:
        seen["published"] = kwargs
        if publish is not None:
            return publish()
        return SimpleNamespace(id="site-1")

    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setattr(sites_service, "publish_pocket", _publish)
    seen["store"] = store
    return seen


# --------------------------------------------------------------------------- #
# Propose — what gets recorded, and what is refused before a card exists
# --------------------------------------------------------------------------- #


def test_the_request_hash_covers_identity_and_not_price():
    """The admin agrees to a site and a tier. The PRICE is allowed to move (the
    catalog owns money), so hashing it would turn an ordinary price change into a
    pile of dead Tray cards — while the three fields that must not move are
    exactly what the hash pins."""
    from pocketpaw_ee.cloud.site_plan_requests.propose import compute_request_hash

    base = compute_request_hash("ws-1", "pkt-1", "staff")
    assert compute_request_hash("ws-1", "pkt-1", "staff") == base
    assert compute_request_hash("ws-2", "pkt-1", "staff") != base
    assert compute_request_hash("ws-1", "pkt-2", "staff") != base
    assert compute_request_hash("ws-1", "pkt-1", "site") != base


async def test_an_org_scoped_flat_cannot_be_requested_for_one_site():
    """``studio`` / ``agency`` cover a whole workspace and are refused by
    ``publish_pocket`` as a ``site_plan_key``. A request for one could only ever
    become a Tray card that fails on approval, so it is refused at the door —
    where the requester is present to be told why."""
    from pocketpaw_ee.cloud.site_plan_requests import propose_site_plan_request

    with pytest.raises(ValueError, match="not a plan a single site"):
        await propose_site_plan_request(
            workspace_id="ws-1",
            pocket_id="pkt-1",
            site_plan_key="agency",
            requested_by="member-1",
        )


async def test_an_unknown_tier_is_refused():
    from pocketpaw_ee.cloud.site_plan_requests import propose_site_plan_request

    with pytest.raises(ValueError, match="not a plan a single site"):
        await propose_site_plan_request(
            workspace_id="ws-1",
            pocket_id="pkt-1",
            site_plan_key="platinum",
            requested_by="member-1",
        )


async def test_tenancy_and_requester_are_mandatory():
    """A blob with no workspace is unexecutable AND ungated — the router's
    tenancy guard fails closed on an empty claim, so a request that could produce
    one must never be built."""
    from pocketpaw_ee.cloud.site_plan_requests import propose_site_plan_request

    with pytest.raises(ValueError, match="workspace_id"):
        await propose_site_plan_request(
            workspace_id="", pocket_id="pkt-1", site_plan_key="site", requested_by="m"
        )
    with pytest.raises(ValueError, match="pocket_id"):
        await propose_site_plan_request(
            workspace_id="ws-1", pocket_id="", site_plan_key="site", requested_by="m"
        )
    with pytest.raises(ValueError, match="requested_by"):
        await propose_site_plan_request(
            workspace_id="ws-1", pocket_id="pkt-1", site_plan_key="site", requested_by=""
        )


# --------------------------------------------------------------------------- #
# THE INVERSION — the approver is checked, not the proposer
# --------------------------------------------------------------------------- #


async def test_approving_checks_the_APPROVER_not_the_requester(monkeypatch):
    """The load-bearing rule. The requester is a member who may not buy — that is
    the premise — so a proposer-side re-check would refuse every request this
    feature creates. The identity put to the check must be the approver's."""
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    action = _action(_blob(requested_by="member-1"), approved_by="admin-9")
    seen = _arm(monkeypatch, action=action)

    await ex.execute_approved_site_plan_request(action)

    assert seen["rechecked"] == ("ws-1", "admin-9"), "the APPROVER's rights decide"
    assert seen["published"]["purchase_authorized"] is True
    assert seen["published"]["site_plan_key"] == "staff"
    # The publish is still attributed to the person who built the site.
    assert seen["published"]["user_id"] == "member-1"


async def test_an_approver_without_buy_rights_does_not_charge(monkeypatch):
    """A cleared ``instinct.approve`` is not by itself a licence to spend. If the
    approver cannot buy, nothing reaches the publish path."""
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    action = _action(_blob())
    seen = _arm(monkeypatch, action=action, may_buy=False)

    await ex.execute_approved_site_plan_request(action)

    assert "published" not in seen, "a denied approver must not buy"
    assert seen["store"].failed, "and the refusal is recorded on the Action"
    assert "sites.buy_plan" in seen["store"].failed[0]


async def test_an_action_with_no_approver_never_buys(monkeypatch):
    """``approved_by`` empty means nothing proves a human with rights accepted
    this. Falling back to the requester would be the whole vulnerability — they
    are, by construction, the person who may not buy."""
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    action = _action(_blob(), approved_by="")
    seen = _arm(monkeypatch, action=action)

    await ex.execute_approved_site_plan_request(action)

    assert "published" not in seen
    assert "rechecked" not in seen, "it must refuse before it even asks"
    assert any("approved_by" in f for f in seen["store"].failed)


def test_the_approver_id_is_read_from_the_authenticated_field():
    """``approved_by`` is written by the router from ``str(user.id)``. A client
    can put anything in ``ApproveRequest.approver``; if the executor ever read
    THAT, a member could approve their own request by naming themselves.

    The decisive case is the FIRST assertion, and it took a mutation to get
    right. An Action carrying BOTH fields with different values is the only
    input that separates the two readings — asserting on an object that has only
    ``approved_by`` passes just as happily against
    ``getattr(action, "approver", ...) or getattr(action, "approved_by", ...)``,
    which is precisely the vulnerable version. That mutation escaped this test
    until this line existed.
    """
    from pocketpaw_ee.cloud.site_plan_requests.executor import _approver_id

    # A forged display label alongside the authenticated field: the authenticated
    # one must win, and the forgery must not appear anywhere in the answer.
    both = SimpleNamespace(approved_by="admin-1", approver="member-1")
    assert _approver_id(both) == "admin-1"

    assert _approver_id(SimpleNamespace(approved_by="admin-1")) == "admin-1"
    assert _approver_id(SimpleNamespace(approved_by=None)) == ""
    # No such attribute at all → empty, which the caller turns into a refusal.
    assert _approver_id(SimpleNamespace()) == ""
    # And a client-controlled field ALONE is never an approver: an Action with no
    # ``approved_by`` was not approved, whatever else it carries.
    assert _approver_id(SimpleNamespace(approver="member-1")) == ""


def test_buying_and_approving_sit_at_the_same_height():
    """The inversion is only safe while an approver necessarily clears the
    purchase bar. If ``sites.buy_plan`` is ever raised above ``instinct.approve``,
    the executor's re-check starts refusing ADMIN approvals — correct, but a
    surprise worth failing a test over rather than discovering in production."""
    from pocketpaw_ee.guards.actions import ACTIONS

    assert ACTIONS["sites.buy_plan"].minimum.level <= ACTIONS["instinct.approve"].minimum.level


# --------------------------------------------------------------------------- #
# Tamper guards
# --------------------------------------------------------------------------- #


async def test_a_moved_tier_after_approval_does_not_buy(monkeypatch):
    """The admin approved a SPECIFIC site on a SPECIFIC tier. An approve-with-
    edits that swapped either must not buy something else — the identity hash is
    recomputed and refuses."""
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    blob = _blob(site_plan_key="site")  # hash computed for 'site'…
    blob["site_plan_key"] = "staff"  # …then the tier was moved under it
    action = _action(blob)
    seen = _arm(monkeypatch, action=action)

    await ex.execute_approved_site_plan_request(action)

    assert "published" not in seen
    assert any("hash mismatch" in f for f in seen["store"].failed)


async def test_a_stale_schema_fails_loud(monkeypatch):
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    action = _action(_blob(schema=99))
    seen = _arm(monkeypatch, action=action)

    await ex.execute_approved_site_plan_request(action)

    assert "published" not in seen
    assert any("schema mismatch" in f for f in seen["store"].failed)


async def test_a_second_approval_does_not_buy_twice(monkeypatch):
    """Bulk re-approve and retries are ordinary. Charging twice for one request
    is not."""
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    blob = _blob()
    blob["outcome"] = {"status": "executed", "response_summary": "done", "executed_at": "t"}
    action = _action(blob)
    seen = _arm(monkeypatch, action=action)

    await ex.execute_approved_site_plan_request(action)

    assert "published" not in seen
    assert not seen["store"].failed, "an idempotent no-op is not a failure"


async def test_a_terminal_action_does_not_re_buy(monkeypatch):
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    action = _action(_blob(), status="executed")
    seen = _arm(monkeypatch, action=action)

    await ex.execute_approved_site_plan_request(action)

    assert "published" not in seen


async def test_a_failed_publish_is_recorded_not_raised(monkeypatch):
    """The executor runs inside the approve response. A publish failure must
    surface on the Action, never as a 500 on the admin's approve click."""
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    def _boom():
        raise RuntimeError("gateway said no")

    action = _action(_blob())
    seen = _arm(monkeypatch, action=action, publish=_boom)

    await ex.execute_approved_site_plan_request(action)  # must not raise

    assert any("gateway said no" in f for f in seen["store"].failed)


# --------------------------------------------------------------------------- #
# Price drift — reported, never enforced, never silent
# --------------------------------------------------------------------------- #


async def test_a_price_that_moved_is_charged_live_and_reported(monkeypatch):
    """The catalog owns the truth about money, so the purchase prices from it —
    but an admin who approved "$7/month" must be able to see they were charged
    something else. Refusing on drift instead would leave a workspace's requests
    dead every time a price changed."""
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    # Quoted at a price the catalog no longer has.
    action = _action(_blob(monthly_price_usd=3))
    seen = _arm(monkeypatch, action=action)

    await ex.execute_approved_site_plan_request(action)

    assert seen["published"]["site_plan_key"] == "staff"
    summary = seen["store"].executed[0]
    assert "price changed since the request" in summary
    assert "$3" in summary


async def test_a_matching_price_says_nothing_about_drift(monkeypatch):
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    live = int(site_plans.site_scoped_tier("staff").monthly_price_usd or 0)
    action = _action(_blob(monthly_price_usd=live))
    seen = _arm(monkeypatch, action=action)

    await ex.execute_approved_site_plan_request(action)

    assert "price changed" not in seen["store"].executed[0]


async def test_a_tier_the_catalog_dropped_does_not_buy(monkeypatch):
    """A request can outlive its tier. Publishing on a rung that no longer exists
    would stamp a plan nothing can price."""
    from pocketpaw_ee.cloud.site_plan_requests import executor as ex

    blob = _blob(site_plan_key="staff")
    action = _action(blob)
    seen = _arm(monkeypatch, action=action)
    monkeypatch.setattr("pocketpaw_ee.cloud.billing.site_plans.site_scoped_tier", lambda k: None)

    await ex.execute_approved_site_plan_request(action)

    assert "published" not in seen
    assert any("no longer a plan" in f for f in seen["store"].failed)


# --------------------------------------------------------------------------- #
# The router's tenancy binding
# --------------------------------------------------------------------------- #


def test_a_request_from_another_workspace_cannot_be_approved():
    """Approving BUYS on the blob's workspace. ``instinct.approve`` only proves
    the caller holds the role SOMEWHERE."""
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.instinct.router import _assert_site_plan_request_workspace

    action = _action(_blob(workspace_id="ws-victim"))
    with pytest.raises(Forbidden):
        _assert_site_plan_request_workspace(action, "ws-attacker")
    # The owning workspace passes.
    _assert_site_plan_request_workspace(action, "ws-victim")


def test_a_workspace_less_request_is_refused_rather_than_waved_through():
    """FAIL-CLOSED on an empty claim. A pass-through would let anyone propose a
    blob with no workspace aimed at a victim's pocket and have any operator in
    any workspace approve the purchase."""
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.instinct.router import _assert_site_plan_request_workspace

    action = _action(_blob(workspace_id=""))
    with pytest.raises(Forbidden):
        _assert_site_plan_request_workspace(action, "ws-1")


def test_the_kind_is_registered_so_edits_cannot_mint_or_move_it():
    """``RESERVED_GATED_PARAM_KEYS`` is what stops an approve-time edit ADDING
    this blob to an innocuous Tray card, or rewriting its tenancy. A gated kind
    that is not in the set is a gated kind in name only."""
    from pocketpaw_ee.instinct.router import RESERVED_GATED_PARAM_KEYS

    assert "_site_plan_request" in RESERVED_GATED_PARAM_KEYS


def test_every_gated_kind_guard_runs_on_the_shared_chokepoint():
    """``_assert_gated_workspaces`` is the one place all four dispatch paths
    (approve / bulk-approve / reject / bulk-reject) call, so a kind wired into
    three of them and missed on the fourth is the bug it exists to prevent."""
    import inspect

    from pocketpaw_ee.instinct import router

    src = inspect.getsource(router._assert_gated_workspaces)
    assert "_assert_site_plan_request_workspace" in src

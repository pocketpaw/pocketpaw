# tests/ee/sites/test_transfer.py — workspace-to-workspace site transfer (sites
# lifecycle wave 3).
#
# Created 2026-09-12 (feat/sites-transfer).
#
# What is under test is the AUTHORIZATION BOUNDARY and the RE-KEY, in that order of
# importance. A transfer is the one sites operation that deliberately crosses a
# tenancy line, so most of these assert a refusal rather than a success — and the
# two that matter most are the ones nobody would think to write: that the SENDER
# alone cannot complete a transfer, and that the RECEIVER cannot accept into a
# workspace they do not belong to by naming it.
#
# The ordering tests exist for the same reason the delete cascade's do: a transfer
# that does the right four things in the wrong order leaves ownership split across
# two tenants, and the window where that is true is the window where either side can
# delete a pocket and strand a live site.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.sites.transfer import (
    OUTCOME_ASSETS_RETAINED,
    OUTCOME_DONE,
    OUTCOME_SKIPPED,
    STATUS_FAILED,
    STATUS_IN_FLIGHT,
    STATUS_NONE,
    STATUS_OFFERED,
    STEP_ASSETS,
    STEP_FINALIZE,
    STEP_OWNERSHIP,
    STEP_RECORDS,
    TRANSFER_STEPS,
    TransferRefused,
    TransferStepFailed,
    check_can_accept,
    check_can_offer,
    run_transfer,
)


class _Settings:
    def __init__(self, allowed: bool = True):
        self.site_transfers_allowed = allowed


class _Site:
    def __init__(self, **kw):
        self.id = "site-1"
        self.workspace = "ws-source"
        self.pocket_id = "pk-1"
        self.owner = "user-sender"
        self.name = "Acme"
        self.url = "https://site-1.paw.dev"
        self.script_name = "site-1"
        self.delete_status = "none"
        self.subscription_status = "none"
        self.transfer_status = STATUS_NONE
        self.transfer_to_workspace = ""
        self.transfer_offered_by = ""
        self.transfer_offered_at = None
        self.transfer_reason = None
        self.transferred_at = None
        self.transfer_ledger: dict[str, str] = {}
        self.identity_workspace = ""
        self.asset_source_prefixes: list[str] = []
        self.__dict__.update(kw)


class _Deps:
    """The side effects, recorded in call order so the ordering can be asserted."""

    def __init__(self, *, fail_on: str | None = None, records_moved: bool = True):
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.records_moved = records_moved
        self.pocket_moves: list[tuple[str, str, str]] = []
        self.record_moves: list[tuple[str, str, str]] = []

    def now(self):
        return datetime(2026, 9, 12, tzinfo=UTC)

    def asset_prefix_for(self, workspace_id, pocket_id):
        return f"site-assets/{workspace_id}/{pocket_id}/"

    async def move_pocket(self, *, pocket_id, destination_workspace_id, new_owner_id):
        if self.fail_on == "move_pocket":
            raise RuntimeError("mongo said no")
        self.calls.append("move_pocket")
        self.pocket_moves.append((pocket_id, destination_workspace_id, new_owner_id))

    async def move_records(self, *, site_id, source_workspace_id, destination_workspace_id):
        if self.fail_on == "move_records":
            raise RuntimeError("mongo said no")
        self.calls.append("move_records")
        self.record_moves.append((site_id, source_workspace_id, destination_workspace_id))
        return self.records_moved


def _saver(saves):
    async def save(site):
        saves.append(dict(site.transfer_ledger))

    return save


# ---------------------------------------------------------------------------
# The send side
# ---------------------------------------------------------------------------


def test_owner_may_offer():
    check_can_offer(
        site=_Site(),
        actor_user_id="user-sender",
        source_settings=_Settings(),
        destination_workspace_id="ws-dest",
    )


def test_a_non_owner_may_not_offer():
    """Owner-only, matching the wave-1 delete guard and Netlify.

    Mutation that must break this: drop the ``site.owner != actor`` branch in
    ``check_can_offer``.
    """
    with pytest.raises(TransferRefused) as exc:
        check_can_offer(
            site=_Site(),
            actor_user_id="user-somebody-else",
            source_settings=_Settings(),
            destination_workspace_id="ws-dest",
        )
    assert exc.value.code == "transfer.not_owner"


def test_workspace_can_forbid_transfers_out():
    """A transfer out is data egress, so an admin can switch it off entirely."""
    with pytest.raises(TransferRefused) as exc:
        check_can_offer(
            site=_Site(),
            actor_user_id="user-sender",
            source_settings=_Settings(allowed=False),
            destination_workspace_id="ws-dest",
        )
    assert exc.value.code == "transfer.blocked_by_workspace"


def test_a_paid_site_cannot_be_moved():
    """THE BILLING GUARD, and it is about the credits rail rather than a gateway.

    A paid site is charged against the SOURCE workspace's credit balance, and the
    renewal sweeper debits whatever workspace the row names. Re-keying the row would
    silently start charging the DESTINATION for a plan its members never bought.

    Mutation that must break this: remove the ``_check_not_paying`` call from
    ``check_can_offer``.
    """
    with pytest.raises(TransferRefused) as exc:
        check_can_offer(
            site=_Site(subscription_status="active"),
            actor_user_id="user-sender",
            source_settings=_Settings(),
            destination_workspace_id="ws-dest",
        )
    assert exc.value.code == "transfer.site_is_paid"


def test_a_deleting_site_cannot_be_moved():
    with pytest.raises(TransferRefused) as exc:
        check_can_offer(
            site=_Site(delete_status="tearing_down"),
            actor_user_id="user-sender",
            source_settings=_Settings(),
            destination_workspace_id="ws-dest",
        )
    assert exc.value.code == "transfer.deleting"


def test_cannot_offer_to_the_workspace_that_already_owns_it():
    with pytest.raises(TransferRefused) as exc:
        check_can_offer(
            site=_Site(),
            actor_user_id="user-sender",
            source_settings=_Settings(),
            destination_workspace_id="ws-source",
        )
    assert exc.value.code == "transfer.same_workspace"


def test_cannot_offer_twice():
    with pytest.raises(TransferRefused) as exc:
        check_can_offer(
            site=_Site(transfer_status=STATUS_OFFERED, transfer_to_workspace="ws-other"),
            actor_user_id="user-sender",
            source_settings=_Settings(),
            destination_workspace_id="ws-dest",
        )
    assert exc.value.code == "transfer.already_offered"


# ---------------------------------------------------------------------------
# The receive side — the half a one-shot transfer would have no check on at all
# ---------------------------------------------------------------------------


def _offered(**kw):
    return _Site(
        transfer_status=STATUS_OFFERED,
        transfer_to_workspace="ws-dest",
        transfer_offered_by="user-sender",
        **kw,
    )


def test_a_member_of_the_destination_may_accept():
    check_can_accept(
        site=_offered(),
        accepting_user_id="user-recipient",
        accepting_workspace_id="ws-dest",
        member_workspace_ids=("ws-dest",),
    )


def test_cannot_accept_into_a_workspace_you_are_not_a_member_of():
    """THE RECEIVING GUARD. Without it, accepting a site needs only a workspace id.

    The request header says which tenant the caller is ASKING to act as; their own
    user record says which ones they may. A caller who is not a member of the
    destination must not be able to pull a site into it by naming it.

    Mutation that must break this: drop the ``member_workspace_ids`` branch in
    ``check_can_accept``.
    """
    with pytest.raises(TransferRefused) as exc:
        check_can_accept(
            site=_offered(),
            accepting_user_id="user-outsider",
            accepting_workspace_id="ws-dest",
            member_workspace_ids=("ws-somewhere-else",),
        )
    assert exc.value.code == "transfer.not_a_member"


def test_cannot_accept_an_offer_addressed_to_another_workspace():
    """A workspace the offer does not name gets ``not_offered``, not ``forbidden``.

    Saying "that site was offered somewhere else" would confirm the site exists to a
    tenant with no business knowing that it does.
    """
    with pytest.raises(TransferRefused) as exc:
        check_can_accept(
            site=_offered(),
            accepting_user_id="user-recipient",
            accepting_workspace_id="ws-a-third-party",
            member_workspace_ids=("ws-a-third-party",),
        )
    assert exc.value.code == "transfer.not_offered"


def test_cannot_accept_when_no_offer_is_open():
    with pytest.raises(TransferRefused) as exc:
        check_can_accept(
            site=_Site(),
            accepting_user_id="user-recipient",
            accepting_workspace_id="ws-dest",
            member_workspace_ids=("ws-dest",),
        )
    assert exc.value.code == "transfer.not_offered"


def test_the_paid_guard_is_rechecked_at_accept():
    """An offer can sit for days, and the site can be upgraded while it does.

    Mutation that must break this: remove the ``_check_not_paying`` call from
    ``check_can_accept`` (the one in ``check_can_offer`` does not cover this).
    """
    with pytest.raises(TransferRefused) as exc:
        check_can_accept(
            site=_offered(subscription_status="active"),
            accepting_user_id="user-recipient",
            accepting_workspace_id="ws-dest",
            member_workspace_ids=("ws-dest",),
        )
    assert exc.value.code == "transfer.site_is_paid"


def test_the_delete_guard_is_rechecked_at_accept():
    with pytest.raises(TransferRefused) as exc:
        check_can_accept(
            site=_offered(delete_status="queued"),
            accepting_user_id="user-recipient",
            accepting_workspace_id="ws-dest",
            member_workspace_ids=("ws-dest",),
        )
    assert exc.value.code == "transfer.deleting"


# ---------------------------------------------------------------------------
# The re-key
# ---------------------------------------------------------------------------


async def test_transfer_rekeys_the_site_and_the_pocket():
    site = _offered()
    deps = _Deps()
    await run_transfer(
        site=site,
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=deps,
        save=_saver([]),
    )
    assert site.workspace == "ws-dest"
    assert site.owner == "user-recipient"
    assert deps.pocket_moves == [("pk-1", "ws-dest", "user-recipient")]


async def test_the_site_id_never_changes():
    """THE INVARIANT THE WHOLE DESIGN IS BUILT AROUND.

    ``_id`` is the Cloudflare Worker's script name and the subdomain the site serves
    at. A transfer that re-derived it would rename a live Worker and move a public
    URL, and every custom-domain route would still point at the old script.
    """
    site = _offered()
    before = site.id
    await run_transfer(
        site=site,
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=_Deps(),
        save=_saver([]),
    )
    assert site.id == before
    assert site.script_name == "site-1"
    assert site.url == "https://site-1.paw.dev"


async def test_the_minting_workspace_is_stamped():
    """Without this stamp the next publish forks the site into two Workers."""
    site = _offered()
    await run_transfer(
        site=site,
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=_Deps(),
        save=_saver([]),
    )
    assert site.identity_workspace == "ws-source"


async def test_a_second_transfer_keeps_the_original_minting_workspace():
    """The id was minted ONCE. Overwriting the stamp with the most recent source
    would make a later publish re-derive an id the live Worker has never been
    called."""
    site = _offered(workspace="ws-second", identity_workspace="ws-source")
    await run_transfer(
        site=site,
        destination_workspace_id="ws-third",
        accepting_user_id="user-recipient",
        deps=_Deps(),
        save=_saver([]),
    )
    assert site.identity_workspace == "ws-source"


async def test_ownership_moves_before_the_customers_data():
    """ORDER IS THE DESIGN HERE TOO, and this is the direction that matters.

    The destination must never hold leads for a site it does not yet own — if the
    transfer then failed and was abandoned, it would keep them.

    Mutation that must break this: swap ``STEP_OWNERSHIP`` and ``STEP_RECORDS`` in
    ``TRANSFER_STEPS``.
    """
    deps = _Deps()
    await run_transfer(
        site=_offered(),
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=deps,
        save=_saver([]),
    )
    assert deps.calls == ["move_pocket", "move_records"]


async def test_leads_are_moved_from_the_minting_workspace():
    """The leads are read from the workspace the site CAME from, which by the time
    this step runs is no longer ``site.workspace`` — that has already been re-keyed."""
    deps = _Deps()
    await run_transfer(
        site=_offered(),
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=deps,
        save=_saver([]),
    )
    assert deps.record_moves == [("site-1", "ws-source", "ws-dest")]


async def test_public_assets_are_retained_and_recorded():
    """They do not move — their key is inside an immutable URL in the live HTML.

    Recording the prefix is what keeps them reclaimable: the delete cascade purges
    ``prefix_for(site.workspace, ...)``, which after a transfer names an empty
    prefix, so without this list the real objects stay world-readable forever.
    """
    site = _offered()
    await run_transfer(
        site=site,
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=_Deps(),
        save=_saver([]),
    )
    assert site.asset_source_prefixes == ["site-assets/ws-source/pk-1/"]
    assert site.transfer_ledger[STEP_ASSETS] == OUTCOME_ASSETS_RETAINED


async def test_finalize_clears_the_offer_and_stamps_the_move():
    site = _offered()
    await run_transfer(
        site=site,
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=_Deps(),
        save=_saver([]),
    )
    assert site.transfer_status == STATUS_NONE
    assert site.transfer_to_workspace == ""
    assert site.transfer_offered_by == ""
    assert site.transferred_at == datetime(2026, 9, 12, tzinfo=UTC)


async def test_every_step_is_recorded_in_the_ledger():
    site = _offered()
    await run_transfer(
        site=site,
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=_Deps(),
        save=_saver([]),
    )
    assert set(site.transfer_ledger) == set(TRANSFER_STEPS)


async def test_nothing_to_move_is_recorded_differently_from_moved():
    """ "There were no leads" and "I moved the leads" are different facts, and an
    operator reading a stalled transfer needs to tell them apart."""
    site = _offered()
    await run_transfer(
        site=site,
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=_Deps(records_moved=False),
        save=_saver([]),
    )
    assert site.transfer_ledger[STEP_RECORDS] == OUTCOME_SKIPPED
    assert site.transfer_ledger[STEP_OWNERSHIP] == OUTCOME_DONE


async def test_the_ledger_is_persisted_after_every_step():
    """A ledger written only at the end records nothing about the crash it exists to
    survive."""
    saves: list[dict] = []
    await run_transfer(
        site=_offered(),
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=_Deps(),
        save=_saver(saves),
    )
    assert len(saves) == len(TRANSFER_STEPS)
    assert [len(s) for s in saves] == [1, 2, 3, 4]


async def test_a_failure_stops_at_that_step_and_keeps_what_ran():
    site = _offered()
    with pytest.raises(TransferStepFailed) as exc:
        await run_transfer(
            site=site,
            destination_workspace_id="ws-dest",
            accepting_user_id="user-recipient",
            deps=_Deps(fail_on="move_records"),
            save=_saver([]),
        )
    assert exc.value.step == STEP_RECORDS
    assert exc.value.reason == "records:runtimeerror"
    assert STEP_OWNERSHIP in site.transfer_ledger
    assert STEP_RECORDS not in site.transfer_ledger
    assert STEP_FINALIZE not in site.transfer_ledger


async def test_a_resumed_transfer_skips_what_the_ledger_records():
    """Resume is a SKIP, not a retry — the same property the delete ledger has."""
    site = _offered()
    site.transfer_ledger = {STEP_OWNERSHIP: OUTCOME_DONE}
    site.workspace = "ws-dest"
    site.identity_workspace = "ws-source"
    deps = _Deps()
    await run_transfer(
        site=site,
        destination_workspace_id="ws-dest",
        accepting_user_id="user-recipient",
        deps=deps,
        save=_saver([]),
    )
    assert deps.pocket_moves == []
    assert deps.calls == ["move_records"]


async def test_the_failure_cause_is_a_token_not_provider_text():
    """``transfer_reason`` is surfaced, so it carries a fixed token the way
    ``build_reason`` and ``delete_reason`` do — never a raw driver message."""

    class _Boom(Exception):
        pass

    class _Deps2(_Deps):
        async def move_pocket(self, **kw):
            raise _Boom("connection refused to 10.0.0.5:27017, user root")

    with pytest.raises(TransferStepFailed) as exc:
        await run_transfer(
            site=_offered(),
            destination_workspace_id="ws-dest",
            accepting_user_id="user-recipient",
            deps=_Deps2(),
            save=_saver([]),
        )
    # ``boom``, not ``_boom``: ``_classify`` strips leading underscores, so a
    # private exception class does not surface a name starting with one.
    assert exc.value.reason == "ownership:boom"
    assert "27017" not in exc.value.reason
    assert "root" not in exc.value.reason


def test_status_values_do_not_collide():
    """A transferred site returns to ``none`` rather than a terminal success value,
    the same choice ``delete_status`` makes."""
    assert len({STATUS_NONE, STATUS_OFFERED, STATUS_IN_FLIGHT, STATUS_FAILED}) == 4
    assert STATUS_NONE == "none"

# tests/ee/test_paw_bar_ledger.py — the paw-bar agent-ledger emitters (AL-2).
# Created: 2026-08-01. AL-1 proved the spine; this file proves the concierge
# funnel that rides on it — conversation → cart → gated ask → approval →
# delivered — actually lands as rows, for the widget's own agent.
#
# The layers, in the order the slice fails if any one is wrong:
#
#   * THE BEATS. Each of the four emitters lands its kind with the right
#     agent_id: the visitor's auto verbs, the delivered decision, the raised
#     handoff, and the conversation's started/takeover/resolved transitions.
#   * MONEY. ``value_cents`` is priced off the widget spec's own catalog — the
#     product's declared price times the quantity added, and the cart total at
#     checkout. This is the only kind that carries value, so a wrong number here
#     is a wrong number on the owner's board.
#   * IDEMPOTENCY. ``UNIQUE(kind, ref)`` absorbs the replays that actually
#     happen: a re-delivered approval, a "started" fired on every turn, a
#     re-applied patch. And, in the other direction, a REPEAT (a second handoff,
#     a second takeover episode, two products added in one turn) must still be
#     two rows — a dedupe key that swallows repeats under-counts silently.
#   * ROUTING. The ledger FILE is routed by the real workspace token, never by
#     the widget OWNER label. Getting this wrong is invisible: the factory raises
#     inside the emitter's own fail-soft guard and the row simply never exists.
#     The recorded-route assertions are the regression guard for it.
#   * THE FAIL-SOFT CONTRACT — the load-bearing one. A ledger store that raises
#     must not cost a visitor their cart, their escape hatch, or their answer. If
#     those tests go red, bookkeeping has started charging visitors for itself.
#
# Store isolation: every store here is built on tmp_path and monkeypatched onto
# the factories the producers lazy-import, so nothing touches ~/.pocketpaw. The
# ledger fake deliberately returns a store at ``tmp_path/agent_ledger.db`` —
# the SAME file AL-1's ``get_agent_ledger_store_beside(instinct.db)`` resolves to
# — so an AL-1 row and an AL-2 row for one action can be read back together and
# the funnel is proven end to end rather than per-emitter.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.paw_bar import ledger  # noqa: E402
from pocketpaw_ee.paw_bar.actions import execute_action  # noqa: E402
from pocketpaw_ee.paw_bar.decision_loop import deliver_customer_decision  # noqa: E402
from pocketpaw_ee.paw_bar.handoff import raise_handoff  # noqa: E402

from pocketpaw.agent_ledger.models import (  # noqa: E402
    ATTR_AGENT_ID,
    ATTR_CART_CURRENCY,
    ATTR_CART_VALUE_CENTS,
    ATTR_CONVERSATION_ID,
    ATTR_HANDOFF_SOURCE,
    ATTR_PRODUCT_ID,
    ATTR_VISITOR_VERB,
    ATTR_WIDGET_ID,
    KIND_ACTION_APPROVED,
    KIND_ACTION_DELIVERED,
    KIND_ACTION_REJECTED,
    KIND_CONVERSATION_STARTED,
    KIND_CONVERSATION_TAKEOVER,
    KIND_HANDOFF_RAISED,
    KIND_HANDOFF_RESOLVED,
    KIND_VISITOR_ACTION,
    SURFACE_PAW_BAR,
    LedgerActor,
)
from pocketpaw.agent_ledger.store import AgentLedgerStore  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402
from pocketpaw.paw_bar.models import (  # noqa: E402
    ConversationState,
    DecisionState,
    PawBarBlock,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore  # noqa: E402

_WS = "ws-1"
_OWNER = "user:maya"  # colon-qualified on purpose — it must NEVER route a store
_REF = "cust-0001"
_AGENT = "agent-xyz"

# --------------------------------------------------------------------------- #
# Builders — the Cedar & Stone shape: a catalog, an auto cart, a gated ask.
# --------------------------------------------------------------------------- #


def _spec(**ov: Any) -> PawBarSpec:
    data: dict[str, Any] = dict(
        widget_id="pp_seed",
        pocket_id="pocket-1",
        blocks=[PawBarBlock(type="text", content="Cedar & Stone")],
        actions=[
            {"verb": "add_to_cart", "policy": "auto", "args": {"product_id": "str", "qty": "int"}},
            {"verb": "checkout", "policy": "auto", "args": {}},
            {"verb": "book_table", "policy": "gated", "args": {"date": "str"}},
        ],
        catalog=[
            {"id": "espresso", "name": "Espresso", "price_cents": 350, "currency": "USD"},
            {"id": "beans", "name": "Beans", "price_cents": 1800, "currency": "USD"},
        ],
        checkout_url="https://cedar.example/checkout?cart={cart_ref}",
    )
    data.update(ov)
    return PawBarSpec(**data)


def _widget(**ov: Any) -> PawBarWidget:
    d: dict[str, Any] = dict(
        pocket_id="pocket-1",
        owner=_OWNER,
        name="Cedar & Stone",
        spec=_spec(),
        allowed_domains=["cedar.example"],
        agent_id=_AGENT,
        workspace_id=_WS,
    )
    d.update(ov)
    return PawBarWidget(**d)


class _DeadFabric:
    """A Fabric store that refuses everything — the handoff's degraded path.

    Keeps these tests off Beanie entirely: ``raise_handoff`` still escalates the
    conversation (the surface that matters here) and returns an empty
    ``handoff_id``, which is exactly the fallback-ref case worth covering.
    """

    async def get_type_by_name(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("fabric is down")

    async def define_type(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("fabric is down")

    async def create_object(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("fabric is down")


@pytest.fixture
def rig(tmp_path: Path, monkeypatch):
    """Isolated paw-bar + instinct + ledger stores on the factories the producers use.

    ``routes`` records every ``workspace_id`` the ledger factory was asked for,
    which is what the routing assertions read: the failure this guards against
    (routing by the OWNER label) is silent by construction, so the test has to
    watch the call rather than the result.
    """
    pp_store = PawBarStore(tmp_path / "paw_bar.db")
    instinct_store = InstinctStore(tmp_path / "instinct.db")
    # The same path AL-1's beside-routing produces for that instinct.db, so both
    # generations of rows are readable through one handle.
    ledger_store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    routes: list[str | None] = []

    def _ledger_factory(*, workspace_id: str | None = None) -> AgentLedgerStore:
        routes.append(workspace_id)
        return ledger_store

    monkeypatch.setattr("pocketpaw.stores.get_paw_bar_store", lambda *a, **k: pp_store)
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: instinct_store)
    monkeypatch.setattr("pocketpaw.stores.get_agent_ledger_store", _ledger_factory)
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: _DeadFabric())
    return SimpleNamespace(
        pp=pp_store,
        instinct=instinct_store,
        ledger=ledger_store,
        routes=routes,
    )


async def _gated_action(rig, widget) -> str:
    """Raise the gated proposal the demo's booking ask produces; return its id."""
    out = await execute_action(widget, _WS, _REF, "book_table", {"date": "Fri"})
    assert out.ok
    return out.result["instinct_action_id"]


# --------------------------------------------------------------------------- #
# Emitter #4 — the visitor's own actions, and the money on them
# --------------------------------------------------------------------------- #


class TestVisitorAction:
    async def test_add_to_cart_lands_an_agent_keyed_priced_row(self, rig) -> None:
        widget = await rig.pp.create_widget(_widget())

        out = await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "espresso"})
        assert out.ok

        rows = await rig.ledger.query(kinds=[KIND_VISITOR_ACTION])
        assert len(rows) == 1
        row = rows[0]
        assert row.agent_id == _AGENT
        assert row.workspace_id == _WS
        assert row.surface == SURFACE_PAW_BAR
        assert row.actor == LedgerActor.VISITOR.value
        # The catalog is the price of record — but a cart add is INTENT, so its
        # money is in attrs and the value column stays empty. Only a checkout
        # becomes attributed value; see test_the_same_money_is_never_attributed_twice.
        assert row.value_cents is None
        assert row.attrs[ATTR_CART_VALUE_CENTS] == 350
        assert row.attrs[ATTR_CART_CURRENCY] == "USD"
        assert row.attrs[ATTR_VISITOR_VERB] == "add_to_cart"
        assert row.attrs[ATTR_PRODUCT_ID] == "espresso"
        assert row.attrs[ATTR_AGENT_ID] == _AGENT
        assert row.attrs[ATTR_WIDGET_ID] == widget.id
        assert row.attrs[ATTR_CONVERSATION_ID] == f"{widget.id}:{_REF}"
        # Ops metrics are federated — never on a ledger row.
        assert not any(k in row.attrs for k in ("tokens", "cost", "latency", "gen_ai.usage"))
        # And the FILE was routed by the real workspace, not the owner label.
        assert rig.routes == [_WS]

    async def test_a_cart_add_records_its_money_as_INTENT_not_as_value(self, rig) -> None:
        """Three of a $3.50 item is $10.50 — in attrs, NOT in ``value_cents``.

        A cart add is intent; only a checkout is money the owner can count. Both
        verbs emit ``paw.visitor.action``, so no kind filter can separate them,
        and ``value_by_currency`` sums every row carrying a value — see the
        double-count test below for what putting both in the column would do.
        """
        widget = await rig.pp.create_widget(_widget())

        await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "espresso", "qty": 3})

        rows = await rig.ledger.query(kinds=[KIND_VISITOR_ACTION])
        assert [r.value_cents for r in rows] == [None]
        assert rows[0].attrs[ATTR_CART_VALUE_CENTS] == 1050
        assert rows[0].attrs[ATTR_CART_CURRENCY] == "USD"

    async def test_the_same_money_is_never_attributed_twice(self, rig) -> None:
        """One $22 coffee added and then bought is $22 attributed, not $44.

        The regression this guards is the two-meters bug in its most dangerous
        form: it inflates the owner's headline revenue number, in the flattering
        direction, while nothing looks broken. Caught in review before either
        slice merged — the emitters would have made it real the day they landed.
        """
        widget = await rig.pp.create_widget(_widget())

        await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "espresso", "qty": 2})
        out = await execute_action(widget, _WS, _REF, "checkout", {})
        assert out.ok

        # 2 × 350 = 700, counted ONCE — at the checkout, not again at the add.
        assert await rig.ledger.value_by_currency() == {"USD": 700}

    async def test_checkout_carries_the_cart_total(self, rig) -> None:
        widget = await rig.pp.create_widget(_widget())
        await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "espresso", "qty": 2})
        await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "beans"})

        out = await execute_action(widget, _WS, _REF, "checkout", {})
        assert out.ok

        rows = await rig.ledger.query(kinds=[KIND_VISITOR_ACTION])
        checkout = [r for r in rows if r.attrs[ATTR_VISITOR_VERB] == "checkout"]
        assert len(checkout) == 1
        # 2 × 350 + 1800, i.e. the cart the checkout link was rendered for.
        assert checkout[0].value_cents == 2500
        assert checkout[0].currency == "USD"

    async def test_two_products_in_one_second_are_two_rows(self, rig) -> None:
        """The concierge can add two items in one turn — the ref must separate them.

        A ref of (conversation, verb, second) would collapse these into one row
        and lose the second product's money with it, which is why the product id
        is part of the key.
        """
        widget = await rig.pp.create_widget(_widget())

        await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "espresso"})
        await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "beans"})

        rows = await rig.ledger.query(kinds=[KIND_VISITOR_ACTION])
        # Two rows, each carrying its own product's price as INTENT. The point of
        # the product-in-the-ref is that the second add is not swallowed as a
        # replay — and it would take its money down with it.
        assert sorted(r.attrs[ATTR_CART_VALUE_CENTS] for r in rows) == [350, 1800]
        assert [r.value_cents for r in rows] == [None, None]

    async def test_a_refused_action_records_nothing(self, rig) -> None:
        """Nothing happened, so nothing is on the board."""
        widget = await rig.pp.create_widget(_widget())

        assert not (await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "no"})).ok
        assert not (await execute_action(widget, _WS, _REF, "checkout", {})).ok  # empty cart

        assert await rig.ledger.query() == []

    async def test_a_gated_verb_is_not_a_visitor_action(self, rig) -> None:
        """A gated ask executes nothing; its beats belong to the Instinct emitter.

        Emitting here too would put one request in the funnel twice.
        """
        widget = await rig.pp.create_widget(_widget())

        await _gated_action(rig, widget)

        assert await rig.ledger.query(kinds=[KIND_VISITOR_ACTION]) == []


# --------------------------------------------------------------------------- #
# Emitter #2 — the delivered beat (and the funnel it closes)
# --------------------------------------------------------------------------- #


class TestDelivered:
    async def test_the_funnel_closes_on_one_action_id(self, rig) -> None:
        """approve (AL-1) and delivered (AL-2) share a ref, an agent, and a file."""
        widget = await rig.pp.create_widget(_widget())
        action_id = await _gated_action(rig, widget)

        approved = await rig.instinct.approve(action_id, approver="user:maya")
        await deliver_customer_decision(approved, declined=False)

        assert (await rig.pp.get_latest_decision(widget.id, _REF)).state == DecisionState.DELIVERED

        counts = await rig.ledger.counts_by_kind(agent_id=_AGENT)
        assert counts[KIND_ACTION_APPROVED] == 1
        assert counts[KIND_ACTION_DELIVERED] == 1

        delivered = (await rig.ledger.query(kinds=[KIND_ACTION_DELIVERED]))[0]
        assert delivered.ref == action_id
        assert delivered.agent_id == _AGENT
        assert delivered.workspace_id == _WS
        assert delivered.actor == LedgerActor.SYSTEM.value
        assert delivered.attrs["paw.decision.actor"] == "user:maya"
        assert delivered.value_cents is None
        # Routed by the widget's REAL workspace, never the ``user:maya`` owner.
        assert _OWNER not in rig.routes
        assert rig.routes[-1] == _WS

    async def test_a_redelivery_does_not_double_count(self, rig) -> None:
        """Replays happen (a retried approve, a sweep re-resolving the row)."""
        widget = await rig.pp.create_widget(_widget())
        action_id = await _gated_action(rig, widget)
        approved = await rig.instinct.approve(action_id)

        await deliver_customer_decision(approved, declined=False)
        await deliver_customer_decision(approved, declined=False)
        await deliver_customer_decision(approved, declined=False)

        assert len(await rig.ledger.query(kinds=[KIND_ACTION_DELIVERED])) == 1

    async def test_a_decline_is_not_a_delivery(self, rig) -> None:
        """A refusal is already counted as ``paw.action.rejected`` by AL-1."""
        widget = await rig.pp.create_widget(_widget())
        action_id = await _gated_action(rig, widget)

        rejected = await rig.instinct.reject(action_id, reason="fully booked")
        await deliver_customer_decision(rejected, declined=True)

        assert (await rig.pp.get_latest_decision(widget.id, _REF)).state == DecisionState.DECLINED
        counts = await rig.ledger.counts_by_kind()
        assert counts.get(KIND_ACTION_DELIVERED, 0) == 0
        assert counts[KIND_ACTION_REJECTED] == 1

    async def test_a_legacy_widget_never_routes_by_its_owner_label(self, rig) -> None:
        """The review blocker, pinned.

        A widget with no ``workspace_id`` scopes its proposal by the OWNER label
        (``user:maya``). Feeding that to the store factory raises inside the
        emitter's guard and the row vanishes with nobody told, so the FILE route
        must fall back to ``None`` (the single-tenant shared ledger) instead.
        """
        widget = await rig.pp.create_widget(_widget(workspace_id=""))
        out = await execute_action(widget, _OWNER, _REF, "book_table", {"date": "Fri"})
        approved = await rig.instinct.approve(out.result["instinct_action_id"])

        await deliver_customer_decision(approved, declined=False)

        assert _OWNER not in rig.routes
        assert rig.routes[-1] is None
        delivered = await rig.ledger.query(kinds=[KIND_ACTION_DELIVERED])
        assert len(delivered) == 1
        # The in-row scope still records what the Action was scoped by, so the
        # delivered row sits in the same bucket as its own approved row.
        assert delivered[0].workspace_id == _OWNER


# --------------------------------------------------------------------------- #
# Emitter #3 — the handoff beats
# --------------------------------------------------------------------------- #


class TestHandoff:
    async def test_a_raised_handoff_is_recorded_for_the_agent(self, rig) -> None:
        widget = await rig.pp.create_widget(_widget())
        await rig.pp.upsert_conversation_on_visitor_turn(widget.id, _REF, _WS)

        outcome = await raise_handoff(
            widget=widget,
            workspace_id=_WS,
            customer_ref=_REF,
            question="Can I change my booking?",
            store=rig.pp,
        )
        assert outcome.ok

        rows = await rig.ledger.query(kinds=[KIND_HANDOFF_RAISED])
        assert len(rows) == 1
        assert rows[0].agent_id == _AGENT
        assert rows[0].workspace_id == _WS
        assert rows[0].actor == LedgerActor.VISITOR.value
        assert rows[0].attrs[ATTR_HANDOFF_SOURCE] == "visitor"
        assert rig.routes == [_WS]

    async def test_an_agent_raised_handoff_records_the_agent_as_the_actor(self, rig) -> None:
        """Same kind, different story: the concierge escalated itself."""
        widget = await rig.pp.create_widget(_widget())

        await raise_handoff(
            widget=widget,
            workspace_id=_WS,
            customer_ref=_REF,
            source="agent",
            store=rig.pp,
        )

        rows = await rig.ledger.query(kinds=[KIND_HANDOFF_RAISED])
        assert rows[0].actor == LedgerActor.AGENT.value
        assert rows[0].attrs[ATTR_HANDOFF_SOURCE] == "agent"

    async def test_a_refused_handoff_records_nothing(self, rig, monkeypatch) -> None:
        """Both owner-visible surfaces failed — nobody was told, so nothing is counted."""
        widget = await rig.pp.create_widget(_widget())

        async def _boom(*a: Any, **k: Any):
            raise RuntimeError("conversation table is gone")

        monkeypatch.setattr(rig.pp, "ensure_conversation", _boom)

        outcome = await raise_handoff(
            widget=widget, workspace_id=_WS, customer_ref=_REF, store=rig.pp
        )
        assert outcome.ok is False
        assert await rig.ledger.query(kinds=[KIND_HANDOFF_RAISED]) == []

    async def test_asking_twice_is_two_rows(self, rig) -> None:
        """A repeat is not a replay: two asks are two rows, or the board hides the
        one thing an owner most wants to see rising."""
        widget = await rig.pp.create_widget(_widget())

        await ledger.emit_handoff_raised(
            widget=widget, workspace_id=_WS, customer_ref=_REF, handoff_id="fab-1"
        )
        await ledger.emit_handoff_raised(
            widget=widget, workspace_id=_WS, customer_ref=_REF, handoff_id="fab-2"
        )
        # ...but the SAME handoff record replayed is absorbed.
        await ledger.emit_handoff_raised(
            widget=widget, workspace_id=_WS, customer_ref=_REF, handoff_id="fab-1"
        )

        assert len(await rig.ledger.query(kinds=[KIND_HANDOFF_RAISED])) == 2


# --------------------------------------------------------------------------- #
# Emitter #5 — the conversation beats, read off the row diff
# --------------------------------------------------------------------------- #


class TestConversation:
    async def test_started_is_recorded_once_per_conversation(self, rig) -> None:
        """Fired on every turn; the ledger's UNIQUE(kind, ref) is what makes it 'new'."""
        widget = await rig.pp.create_widget(_widget())

        for _ in range(3):
            await ledger.emit_conversation_started(
                widget=widget, workspace_id=_WS, customer_ref=_REF
            )
        await ledger.emit_conversation_started(
            widget=widget, workspace_id=_WS, customer_ref="cust-0002"
        )

        rows = await rig.ledger.query(kinds=[KIND_CONVERSATION_STARTED])
        assert len(rows) == 2
        assert {r.ref for r in rows} == {f"{widget.id}:{_REF}", f"{widget.id}:cust-0002"}
        assert rows[0].agent_id == _AGENT
        assert rows[0].actor == LedgerActor.VISITOR.value

    async def test_the_owner_taking_over_an_escalated_thread_records_both_beats(self, rig) -> None:
        """The transition helper reads real rows, not the caller's intent."""
        widget = await rig.pp.create_widget(_widget())
        await rig.pp.upsert_conversation_on_visitor_turn(widget.id, _REF, _WS)
        before = await rig.pp.update_conversation(
            widget.id, _REF, workspace_id=_WS, state=ConversationState.NEEDS_HUMAN.value
        )
        after = await rig.pp.update_conversation(
            widget.id,
            _REF,
            workspace_id=_WS,
            bot_paused=True,
            state=ConversationState.OPEN.value,
        )

        await ledger.emit_conversation_transition(
            widget=widget, workspace_id=_WS, customer_ref=_REF, before=before, after=after
        )

        counts = await rig.ledger.counts_by_kind(agent_id=_AGENT)
        assert counts[KIND_CONVERSATION_TAKEOVER] == 1
        assert counts[KIND_HANDOFF_RESOLVED] == 1
        takeover = (await rig.ledger.query(kinds=[KIND_CONVERSATION_TAKEOVER]))[0]
        assert takeover.actor == LedgerActor.OWNER.value
        assert takeover.workspace_id == _WS
        assert rig.routes == [_WS, _WS]

    async def test_a_patch_that_crosses_nothing_records_nothing(self, rig) -> None:
        widget = await rig.pp.create_widget(_widget())
        await rig.pp.upsert_conversation_on_visitor_turn(widget.id, _REF, _WS)
        before = await rig.pp.get_conversation(widget.id, _REF, workspace_id=_WS)
        after = await rig.pp.update_conversation(widget.id, _REF, workspace_id=_WS, tags=["vip"])

        await ledger.emit_conversation_transition(
            widget=widget, workspace_id=_WS, customer_ref=_REF, before=before, after=after
        )

        assert await rig.ledger.query() == []

    async def test_a_second_takeover_episode_is_a_second_row(self, rig) -> None:
        """The bot hands itself back after the idle window; tomorrow is a new episode."""
        widget = await rig.pp.create_widget(_widget())
        paused = SimpleNamespace(bot_paused=True, bot_paused_at="2026-08-01T09:00:00", state="open")
        resumed = SimpleNamespace(bot_paused=False, bot_paused_at="", state="open")
        later = SimpleNamespace(bot_paused=True, bot_paused_at="2026-08-02T11:30:00", state="open")

        await ledger.emit_conversation_transition(
            widget=widget, workspace_id=_WS, customer_ref=_REF, before=resumed, after=paused
        )
        # The same episode written twice is still one row.
        await ledger.emit_conversation_transition(
            widget=widget, workspace_id=_WS, customer_ref=_REF, before=resumed, after=paused
        )
        await ledger.emit_conversation_transition(
            widget=widget, workspace_id=_WS, customer_ref=_REF, before=resumed, after=later
        )

        assert len(await rig.ledger.query(kinds=[KIND_CONVERSATION_TAKEOVER])) == 2


# --------------------------------------------------------------------------- #
# The slice, end to end — the Cedar & Stone flow this task exists for
# --------------------------------------------------------------------------- #


class TestTheWholeFunnel:
    async def test_one_visitor_leaves_a_complete_trail_for_one_agent(self, rig) -> None:
        """conversation → cart → gated ask → approval → delivered → handoff.

        The AL-2 "done when": every stage of the demo flow is a row, all of them
        keyed to the widget's bound agent, all in one workspace's ledger, with
        the cart's money attributed and no ops metric anywhere.
        """
        widget = await rig.pp.create_widget(_widget())

        await ledger.emit_conversation_started(widget=widget, workspace_id=_WS, customer_ref=_REF)
        await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "beans"})
        action_id = await _gated_action(rig, widget)
        approved = await rig.instinct.approve(action_id, approver="user:maya")
        await deliver_customer_decision(approved, declined=False)
        await raise_handoff(widget=widget, workspace_id=_WS, customer_ref=_REF, store=rig.pp)

        counts = await rig.ledger.counts_by_kind(agent_id=_AGENT, workspace_id=_WS)
        assert counts == {
            KIND_CONVERSATION_STARTED: 1,
            KIND_VISITOR_ACTION: 1,
            KIND_ACTION_APPROVED: 1,  # AL-1's emitter, same file, same agent
            KIND_ACTION_DELIVERED: 1,
            KIND_HANDOFF_RAISED: 1,
        }
        # This visitor filled a cart and never bought, so NOTHING is attributed.
        # An abandoned cart is not revenue, and a board that counted it would tell
        # the owner they earned money they never received. The cart's £/$ is still
        # on the row, as intent, in attrs.
        assert await rig.ledger.value_by_currency(agent_id=_AGENT, workspace_id=_WS) == {}
        cart_row = (await rig.ledger.query(kinds=[KIND_VISITOR_ACTION]))[0]
        assert cart_row.attrs[ATTR_CART_VALUE_CENTS] == 1800
        rows = await rig.ledger.query(agent_id=_AGENT, workspace_id=_WS)
        assert all(r.surface == SURFACE_PAW_BAR for r in rows)
        assert not any(
            k in r.attrs for r in rows for k in ("tokens", "cost", "latency", "gen_ai.usage")
        )


# --------------------------------------------------------------------------- #
# THE fail-soft contract — bookkeeping never costs anyone their turn
# --------------------------------------------------------------------------- #


class _ExplodingLedger:
    async def append(self, row: Any) -> bool:  # noqa: ARG002
        raise RuntimeError("ledger disk is on fire")


class TestFailSoft:
    """Each test here PROVES the substitution took effect before believing the result.

    A fail-soft test that patches a seam the emitter does not actually call
    passes without ever running the guard — it asserts only that a HEALTHY
    emitter is harmless, which is not the claim. So every case below also
    asserts the row did NOT land: if it did, the exploding store was never
    reached and the test is telling us nothing.
    """

    async def test_a_raising_ledger_store_does_not_break_the_visitors_cart(
        self, rig, monkeypatch
    ) -> None:
        """If this goes red, a broken analytics table can empty a visitor's cart."""
        widget = await rig.pp.create_widget(_widget())
        monkeypatch.setattr(
            "pocketpaw.stores.get_agent_ledger_store", lambda **_k: _ExplodingLedger()
        )

        out = await execute_action(widget, _WS, _REF, "add_to_cart", {"product_id": "espresso"})

        assert out.ok
        assert out.cart["total_cents"] == 350
        cart = await rig.pp.get_cart(widget.id, _REF)
        assert cart is not None and cart.total_cents == 350
        # The guard really ran: the emitter reached the exploding store, so no
        # row exists. Without this the test would pass on a missed patch.
        assert await rig.ledger.query(kinds=[KIND_VISITOR_ACTION]) == []

    async def test_an_unresolvable_ledger_store_does_not_break_the_escape_hatch(
        self, rig, monkeypatch
    ) -> None:
        """Fail-CLOSED store resolution meets fail-SOFT emission, and soft wins.

        A workspace the factory refuses (the real cloud-mode behaviour for an
        unscoped call) must never stop a visitor reaching a person.
        """
        from pocketpaw import stores

        widget = await rig.pp.create_widget(_widget())

        def _boom(**_k: Any):
            raise stores.WorkspaceScopeRequired("no workspace resolved")

        monkeypatch.setattr("pocketpaw.stores.get_agent_ledger_store", _boom)

        outcome = await raise_handoff(
            widget=widget, workspace_id=_WS, customer_ref=_REF, store=rig.pp
        )

        assert outcome.ok is True
        conversation = await rig.pp.get_conversation(widget.id, _REF, workspace_id=_WS)
        assert conversation is not None
        assert conversation.state == ConversationState.NEEDS_HUMAN
        assert await rig.ledger.query(kinds=[KIND_HANDOFF_RAISED]) == []

    async def test_a_raising_ledger_store_does_not_break_the_delivery(
        self, rig, monkeypatch
    ) -> None:
        widget = await rig.pp.create_widget(_widget())
        action_id = await _gated_action(rig, widget)
        approved = await rig.instinct.approve(action_id, approver="user:maya")
        monkeypatch.setattr(
            "pocketpaw.stores.get_agent_ledger_store", lambda **_k: _ExplodingLedger()
        )

        await deliver_customer_decision(approved, declined=False)

        decision = await rig.pp.get_latest_decision(widget.id, _REF)
        assert decision.state == DecisionState.DELIVERED
        assert decision.reply
        assert await rig.ledger.query(kinds=[KIND_ACTION_DELIVERED]) == []

    async def test_every_emitter_swallows_a_broken_widget(self, rig, monkeypatch) -> None:
        """A duck-typed / half-built widget is a degraded row, never an exception.

        The emitters are called from a visitor's hot path with whatever the
        caller has; ``None`` is the worst case and it must be survivable.
        """
        emitters = [
            ledger.emit_conversation_started(widget=None, workspace_id=_WS, customer_ref=_REF),
            ledger.emit_conversation_takeover(widget=None, workspace_id=_WS, customer_ref=_REF),
            ledger.emit_handoff_raised(widget=None, workspace_id=_WS, customer_ref=_REF),
            ledger.emit_handoff_resolved(widget=None, workspace_id=_WS, customer_ref=_REF),
            ledger.emit_visitor_action(
                widget=None, workspace_id=_WS, customer_ref=_REF, verb="checkout", spec=None
            ),
            ledger.emit_action_delivered(
                action=None,
                widget=None,
                customer_ref=_REF,
                row_workspace_id=_WS,
                decided_by="user:maya",
            ),
        ]
        for emitter in emitters:
            assert await emitter in (True, False)

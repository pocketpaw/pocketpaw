# tests/cloud/test_paw_bar_backend.py — PR-A: Paw Bar models + store.
# Created: 2026-04-13 — Covers validation caps, domain normalization, token
# rotation, event persistence, and the rate-limit primitives used by PR-B.

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pocketpaw.paw_bar.models import (
    MAX_BLOCKS_PER_SPEC,
    MAX_DOMAINS_PER_WIDGET,
    MAX_ITEMS_PER_LIST,
    PawBarAction,
    PawBarBlock,
    PawBarEvent,
    PawBarEventMapping,
    PawBarListItem,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore


def _spec(widget_id: str = "pp_test") -> PawBarSpec:
    return PawBarSpec(
        widget_id=widget_id,
        pocket_id="pocket-1",
        blocks=[
            PawBarBlock(type="text", content="Today's menu", style="heading"),
            PawBarBlock(
                type="list",
                items=[
                    PawBarListItem(
                        title="Oat Milk Latte",
                        meta="$5 — 34 in stock",
                        action=PawBarAction(event="order_click", payload={"item": "oat_latte"}),
                    ),
                ],
            ),
        ],
    )


def _widget(**overrides) -> PawBarWidget:
    defaults = {
        "pocket_id": "pocket-1",
        "owner": "user:maya",
        "name": "Brew & Co Menu",
        "spec": _spec(),
        "allowed_domains": ["brewco.com"],
        "event_mapping": {
            "order_click": PawBarEventMapping(
                creates="Order",
                fields={"item": "{{ payload.item }}", "customer_ref": "{{ customer_ref }}"},
            ),
        },
    }
    defaults.update(overrides)
    return PawBarWidget(**defaults)


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestBlockCaps:
    def test_list_block_accepts_up_to_the_cap(self) -> None:
        items = [PawBarListItem(title=f"Item {i}") for i in range(MAX_ITEMS_PER_LIST)]
        block = PawBarBlock(type="list", items=items)
        assert len(block.items) == MAX_ITEMS_PER_LIST

    def test_list_block_rejects_past_the_cap(self) -> None:
        items = [PawBarListItem(title=f"Item {i}") for i in range(MAX_ITEMS_PER_LIST + 1)]
        with pytest.raises(ValueError, match="list block accepts at most"):
            PawBarBlock(type="list", items=items)

    def test_spec_rejects_too_many_blocks(self) -> None:
        blocks = [PawBarBlock(type="divider") for _ in range(MAX_BLOCKS_PER_SPEC + 1)]
        with pytest.raises(ValueError, match="spec accepts at most"):
            PawBarSpec(widget_id="pp_x", pocket_id="p", blocks=blocks)


class TestWidgetValidation:
    def test_allowed_domains_are_lowercased_and_deduped(self) -> None:
        widget = _widget(allowed_domains=["BrewCo.com", "brewco.com", " shop.brewco.com "])
        assert widget.allowed_domains == ["brewco.com", "shop.brewco.com"]

    def test_allowed_domains_cap_enforced(self) -> None:
        domains = [f"site{i}.example" for i in range(MAX_DOMAINS_PER_WIDGET + 1)]
        with pytest.raises(ValueError, match="allowed_domains accepts at most"):
            _widget(allowed_domains=domains)

    def test_rate_limit_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="rate limits must be"):
            _widget(rate_limit_per_min=0)
        with pytest.raises(ValueError, match="rate limits must be"):
            _widget(per_customer_limit_per_min=-1)

    def test_access_token_is_generated_and_prefixed(self) -> None:
        widget = _widget()
        assert widget.access_token.startswith("pp_tok_")
        assert len(widget.access_token) > len("pp_tok_") + 20


class TestEventValidation:
    def test_empty_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="event type is required"):
            PawBarEvent(widget_id="pp_x", type="  ", customer_ref="abc")

    def test_type_is_stripped(self) -> None:
        event = PawBarEvent(widget_id="pp_x", type=" order_click ", customer_ref="abc")
        assert event.type == "order_click"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> PawBarStore:
    return PawBarStore(tmp_path / "paw_bar.db")


class TestWidgetCRUD:
    @pytest.mark.asyncio
    async def test_create_and_fetch_widget(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())
        fetched = await store.get_widget(widget.id)
        assert fetched is not None
        assert fetched.owner == "user:maya"
        assert fetched.allowed_domains == ["brewco.com"]
        assert "order_click" in fetched.event_mapping
        assert fetched.event_mapping["order_click"].creates == "Order"

    @pytest.mark.asyncio
    async def test_list_filters_by_pocket_and_owner(self, store: PawBarStore) -> None:
        await store.create_widget(_widget(pocket_id="pocket-1", owner="user:maya"))
        await store.create_widget(_widget(pocket_id="pocket-2", owner="user:priya"))

        by_pocket = await store.list_widgets(pocket_id="pocket-1")
        assert len(by_pocket) == 1
        assert by_pocket[0].pocket_id == "pocket-1"

        by_owner = await store.list_widgets(owner="user:priya")
        assert len(by_owner) == 1
        assert by_owner[0].owner == "user:priya"

    @pytest.mark.asyncio
    async def test_update_spec_replaces_blocks(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())
        new_spec = PawBarSpec(
            widget_id=widget.id,
            pocket_id=widget.pocket_id,
            blocks=[PawBarBlock(type="text", content="Closed today")],
        )
        updated = await store.update_spec(widget.id, new_spec)
        assert updated is not None
        assert len(updated.spec.blocks) == 1
        assert updated.spec.blocks[0].content == "Closed today"

    @pytest.mark.asyncio
    async def test_rotate_token_invalidates_old_token(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())
        original = widget.access_token
        rotated = await store.rotate_token(widget.id)
        assert rotated is not None
        assert rotated.access_token != original
        assert rotated.access_token.startswith("pp_tok_")

    @pytest.mark.asyncio
    async def test_delete_widget_returns_true_then_false(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())
        assert await store.delete_widget(widget.id) is True
        assert await store.delete_widget(widget.id) is False
        assert await store.get_widget(widget.id) is None

    @pytest.mark.asyncio
    async def test_update_missing_widget_returns_none(self, store: PawBarStore) -> None:
        result = await store.update_spec("does_not_exist", _spec())
        assert result is None


# ---------------------------------------------------------------------------
# Event log + rate limit
# ---------------------------------------------------------------------------


class TestEventStore:
    @pytest.mark.asyncio
    async def test_events_are_listed_newest_first(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())
        now = datetime.now()
        await store.record_event(
            PawBarEvent(
                widget_id=widget.id,
                type="order_click",
                customer_ref="cust_a",
                timestamp=now - timedelta(minutes=5),
            ),
        )
        await store.record_event(
            PawBarEvent(
                widget_id=widget.id,
                type="order_click",
                customer_ref="cust_b",
                timestamp=now,
            ),
        )
        events = await store.recent_events(widget.id)
        assert len(events) == 2
        assert events[0].customer_ref == "cust_b"
        assert events[1].customer_ref == "cust_a"

    @pytest.mark.asyncio
    async def test_count_events_since_respects_window(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())
        now = datetime.now()
        await store.record_event(
            PawBarEvent(
                widget_id=widget.id,
                type="order_click",
                customer_ref="cust_a",
                timestamp=now - timedelta(minutes=5),
            ),
        )
        await store.record_event(
            PawBarEvent(
                widget_id=widget.id,
                type="order_click",
                customer_ref="cust_a",
                timestamp=now - timedelta(seconds=20),
            ),
        )

        assert await store.count_events_since(widget.id, now - timedelta(minutes=1)) == 1
        assert await store.count_events_since(widget.id, now - timedelta(minutes=10)) == 2

    @pytest.mark.asyncio
    async def test_within_rate_limit_enforces_overall_and_per_customer(
        self, store: PawBarStore
    ) -> None:
        widget = await store.create_widget(_widget())
        now = datetime.now()
        for i in range(3):
            await store.record_event(
                PawBarEvent(
                    widget_id=widget.id,
                    type="order_click",
                    customer_ref="cust_a",
                    timestamp=now - timedelta(seconds=10 * i),
                ),
            )

        # Overall cap 5, per-customer cap 3 — cust_a is at the per-customer ceiling.
        allowed = await store.within_rate_limit(
            widget.id,
            overall_per_min=5,
            per_customer_per_min=3,
            customer_ref="cust_a",
            now=now,
        )
        assert allowed is False

        # cust_b has no prior events — still accepted.
        allowed_other = await store.within_rate_limit(
            widget.id,
            overall_per_min=5,
            per_customer_per_min=3,
            customer_ref="cust_b",
            now=now,
        )
        assert allowed_other is True

    @pytest.mark.asyncio
    async def test_within_rate_limit_respects_overall_ceiling(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())
        now = datetime.now()
        for i in range(5):
            await store.record_event(
                PawBarEvent(
                    widget_id=widget.id,
                    type="order_click",
                    customer_ref=f"cust_{i}",
                    timestamp=now - timedelta(seconds=5),
                ),
            )
        allowed = await store.within_rate_limit(
            widget.id,
            overall_per_min=5,
            per_customer_per_min=10,
            customer_ref="cust_new",
            now=now,
        )
        assert allowed is False

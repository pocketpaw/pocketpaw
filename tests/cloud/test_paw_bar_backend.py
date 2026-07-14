# tests/cloud/test_paw_bar_backend.py — PR-A: Paw Bar models + store.
# Created: 2026-04-13 — Covers validation caps, domain normalization, token
# rotation, event persistence, and the rate-limit primitives used by PR-B.
# Updated: 2026-07-11 (W4a tenancy seam) — Added TestWorkspaceScoping (two-
# tenant isolation on get/list/update/rotate/delete + legacy ''-row matching),
# TestSpecRevisions (archive-on-update, rollback round-trip, workspace-scoped
# rollback), and TestScopedFabricWrite (the leak fix: _apply_event_mapping
# threads the widget row's workspace_id into get_fabric_store; '' → None keeps
# the single-tenant default store).

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
    async def test_agent_id_round_trips_create_to_get(self, store: PawBarStore) -> None:
        """T3 — a concierge widget's agent binding survives create → get (and the
        default is "" for an unbound widget, like the workspace_id column)."""
        bound = await store.create_widget(_widget(agent_id="agent-123"))
        fetched = await store.get_widget(bound.id)
        assert fetched is not None
        assert fetched.agent_id == "agent-123"

        # It also rides the list read (SELECT * → _row_to_widget).
        listed = await store.list_widgets(pocket_id=bound.pocket_id)
        assert listed and listed[0].agent_id == "agent-123"

        # An unset binding defaults to "" (not None), mirroring workspace_id.
        unbound = await store.create_widget(_widget(pocket_id="pocket-unbound"))
        fetched_unbound = await store.get_widget(unbound.id)
        assert fetched_unbound is not None
        assert fetched_unbound.agent_id == ""

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


# ---------------------------------------------------------------------------
# W4a — in-row workspace scoping (two-tenant isolation)
# ---------------------------------------------------------------------------


class TestWorkspaceScoping:
    @pytest.mark.asyncio
    async def test_get_widget_is_workspace_scoped(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget(workspace_id="w1"))
        assert await store.get_widget(widget.id, workspace_id="w1") is not None
        assert await store.get_widget(widget.id, workspace_id="w2") is None
        # None ⇒ unscoped (backward-compatible internal reads).
        assert await store.get_widget(widget.id) is not None

    @pytest.mark.asyncio
    async def test_legacy_empty_workspace_row_matches_any_scope(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())  # workspace_id defaults to ""
        assert await store.get_widget(widget.id, workspace_id="w1") is not None
        assert await store.get_widget(widget.id, workspace_id="w2") is not None

    @pytest.mark.asyncio
    async def test_list_widgets_is_workspace_scoped(self, store: PawBarStore) -> None:
        await store.create_widget(_widget(workspace_id="w1", owner="user:maya"))
        await store.create_widget(_widget(workspace_id="w2", owner="user:priya"))
        legacy = await store.create_widget(_widget(owner="user:legacy"))  # ""

        w1 = await store.list_widgets(workspace_id="w1")
        assert {w.owner for w in w1} == {"user:maya", "user:legacy"}
        w2 = await store.list_widgets(workspace_id="w2")
        assert {w.owner for w in w2} == {"user:priya", "user:legacy"}
        unscoped = await store.list_widgets()
        assert len(unscoped) == 3
        assert legacy.workspace_id == ""

    @pytest.mark.asyncio
    async def test_update_spec_cross_tenant_returns_none_and_never_mutates(
        self, store: PawBarStore
    ) -> None:
        widget = await store.create_widget(_widget(workspace_id="w1"))
        new_spec = PawBarSpec(
            widget_id=widget.id,
            pocket_id=widget.pocket_id,
            blocks=[PawBarBlock(type="text", content="hijacked")],
        )
        assert await store.update_spec(widget.id, new_spec, workspace_id="w2") is None
        untouched = await store.get_widget(widget.id)
        assert untouched is not None
        assert untouched.spec.blocks[0].content == "Today's menu"

    @pytest.mark.asyncio
    async def test_rotate_token_cross_tenant_returns_none_and_never_mutates(
        self, store: PawBarStore
    ) -> None:
        widget = await store.create_widget(_widget(workspace_id="w1"))
        assert await store.rotate_token(widget.id, workspace_id="w2") is None
        unchanged = await store.get_widget(widget.id)
        assert unchanged is not None
        assert unchanged.access_token == widget.access_token

    @pytest.mark.asyncio
    async def test_delete_widget_cross_tenant_is_a_noop(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget(workspace_id="w1"))
        assert await store.delete_widget(widget.id, workspace_id="w2") is False
        assert await store.get_widget(widget.id) is not None
        # Same-tenant delete still works.
        assert await store.delete_widget(widget.id, workspace_id="w1") is True


# ---------------------------------------------------------------------------
# W4a — spec revisions + rollback
# ---------------------------------------------------------------------------


class TestSpecRevisions:
    @pytest.mark.asyncio
    async def test_update_archives_prior_spec_and_rollback_restores_it(
        self, store: PawBarStore
    ) -> None:
        widget = await store.create_widget(_widget())
        new_spec = PawBarSpec(
            widget_id=widget.id,
            pocket_id=widget.pocket_id,
            blocks=[PawBarBlock(type="text", content="Closed today")],
        )
        await store.update_spec(widget.id, new_spec)

        latest = await store.latest_spec_revision(widget.id)
        assert latest is not None
        revision, archived = latest
        assert revision == 1
        assert archived.blocks[0].content == "Today's menu"

        restored = await store.rollback_spec(widget.id)
        assert restored is not None
        assert restored.spec.blocks[0].content == "Today's menu"

        # The rollback itself archived the replaced spec (monotonic revision),
        # so rolling back again restores "Closed today" — always reversible.
        latest2 = await store.latest_spec_revision(widget.id)
        assert latest2 is not None
        assert latest2[0] == 2
        again = await store.rollback_spec(widget.id)
        assert again is not None
        assert again.spec.blocks[0].content == "Closed today"

    @pytest.mark.asyncio
    async def test_rollback_without_revisions_returns_none(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget())
        assert await store.rollback_spec(widget.id) is None

    @pytest.mark.asyncio
    async def test_rollback_is_workspace_scoped(self, store: PawBarStore) -> None:
        widget = await store.create_widget(_widget(workspace_id="w1"))
        new_spec = PawBarSpec(
            widget_id=widget.id,
            pocket_id=widget.pocket_id,
            blocks=[PawBarBlock(type="text", content="v2")],
        )
        await store.update_spec(widget.id, new_spec, workspace_id="w1")
        assert await store.rollback_spec(widget.id, workspace_id="w2") is None
        current = await store.get_widget(widget.id)
        assert current is not None
        assert current.spec.blocks[0].content == "v2"


# ---------------------------------------------------------------------------
# W4a — the public-path Fabric write is scoped to the widget's workspace
# ---------------------------------------------------------------------------


class _StubFabricObject:
    """Kwarg-eating FabricObject stand-in for the seam tests below."""

    def __init__(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class TestScopedFabricWrite:
    @pytest.mark.asyncio
    async def test_apply_event_mapping_threads_the_widget_workspace(self, monkeypatch) -> None:
        """The cross-tenant leak fix: get_fabric_store must receive the OWNER's
        workspace_id (from the widget row), not be called bare."""
        from unittest.mock import AsyncMock, MagicMock

        import pocketpaw_ee.api as ee_api
        from pocketpaw_ee.paw_bar import router as ppr

        captured: dict[str, object] = {}
        created_obj = MagicMock()
        created_obj.id = "obj_scoped_1"
        fabric = MagicMock()
        fabric.create_object = AsyncMock(return_value=created_obj)

        def spy_get_fabric_store(*, workspace_id: str | None = None):
            captured["workspace_id"] = workspace_id
            return fabric

        monkeypatch.setattr(ee_api, "get_fabric_store", spy_get_fabric_store)
        # NOTE: the real FabricObject requires type_id (the router only passes
        # type_name — a pre-existing gap, unrelated to W4a); stub it so this
        # test isolates the tenancy seam.
        monkeypatch.setattr("pocketpaw.fabric.models.FabricObject", _StubFabricObject)

        widget = _widget(workspace_id="w-owner")
        event = PawBarEvent(
            widget_id=widget.id,
            type="order_click",
            payload={"item": "oat_latte"},
            customer_ref="cust_a",
        )
        obj_id = await ppr._apply_event_mapping(widget, event)
        assert obj_id == "obj_scoped_1"
        assert captured["workspace_id"] == "w-owner"

    @pytest.mark.asyncio
    async def test_legacy_widget_keeps_the_default_store(self, monkeypatch) -> None:
        """An unstamped ('' workspace) widget passes None — single-tenant
        behavior is unchanged."""
        from unittest.mock import AsyncMock, MagicMock

        import pocketpaw_ee.api as ee_api
        from pocketpaw_ee.paw_bar import router as ppr

        captured: dict[str, object] = {"workspace_id": "sentinel"}
        created_obj = MagicMock()
        created_obj.id = "obj_default_1"
        fabric = MagicMock()
        fabric.create_object = AsyncMock(return_value=created_obj)

        def spy_get_fabric_store(*, workspace_id: str | None = None):
            captured["workspace_id"] = workspace_id
            return fabric

        monkeypatch.setattr(ee_api, "get_fabric_store", spy_get_fabric_store)
        monkeypatch.setattr("pocketpaw.fabric.models.FabricObject", _StubFabricObject)

        widget = _widget()  # workspace_id defaults to ""
        event = PawBarEvent(
            widget_id=widget.id,
            type="order_click",
            payload={"item": "oat_latte"},
            customer_ref="cust_a",
        )
        obj_id = await ppr._apply_event_mapping(widget, event)
        assert obj_id == "obj_default_1"
        assert captured["workspace_id"] is None

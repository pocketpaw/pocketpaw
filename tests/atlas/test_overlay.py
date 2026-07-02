# tests/atlas/test_overlay.py — live overlay + fail-closed entitlement filter
# (AT-5). Created: 2026-07-02 (feat/atlas-overlay). Proves, on a synthetic
# store: non-granted entries are ABSENT from search and describe (describe of
# a filtered id answers exactly like an unknown id — no leakage); a RAISING
# provider filters gated entries (fail-closed) and marks connectors
# unavailable when the connected-set resolution raises (annotated, not
# filtered); an ambiguous grant (None) filters; connector cards carry the
# ``available`` annotation and available connectors re-rank above unavailable
# ones at EQUAL score without touching base scoring; the shared store's
# entries are never mutated; the OSS DefaultEntitlementProvider (fed a fake
# registry) gates nothing and reads availability from the registry's durable
# status. MCP handler tests run the REAL packaged store through stubbed
# providers: same query in two contexts returns context-appropriate results,
# the known-ids error listing hides filtered ids, and describe of an
# unavailable connector points at the integrations surface route (looked up
# from the seed, not hard-coded).

import json

import pytest

from pocketpaw.agents.sdk_mcp_atlas import (
    _atlas_describe_handler,
    _atlas_search_handler,
)
from pocketpaw.atlas.model import AtlasEntry, AtlasModel
from pocketpaw.atlas.overlay import (
    DEFAULT_SCOPE_KEY,
    AtlasOverlay,
    DefaultEntitlementProvider,
    EntitlementProvider,
    OverlaidEntry,
)
from pocketpaw.atlas.store import AtlasStore

# ── fixtures ────────────────────────────────────────────────────────────


def _entry(entry_id: str, kind: str, name: str, **kw) -> AtlasEntry:
    return AtlasEntry(
        id=entry_id,
        kind=kind,
        name=name,
        summary=kw.pop("summary", f"{name} summary"),
        narrative=kw.pop("narrative", f"{name} narrative"),
        **kw,
    )


def _synthetic_store() -> AtlasStore:
    """Small store: one primitive, one surface, one sense, three connectors.

    ``alpha_crm`` and ``beta_crm`` share the keyword "crm" so a "crm" query
    scores them EQUALLY (keyword hit each) — the equal-relevance re-rank
    case. Seed order puts beta before alpha to prove the re-sort is doing
    the work, not the base order.
    """
    return AtlasStore(
        AtlasModel(
            entries=[
                _entry("primitive:pocket", "primitive", "Pocket", keywords=["app", "crm"]),
                _entry("surface:integrations", "surface", "Integrations", surface="/x/int"),
                _entry("sense:paw.email.v1", "sense", "Email"),
                _entry("connector:beta_crm", "connector", "Beta CRM", keywords=["crm"]),
                _entry("connector:alpha_crm", "connector", "Alpha CRM", keywords=["crm"]),
                _entry("connector:gamma_pay", "connector", "Gamma Pay", keywords=["payments"]),
            ]
        )
    )


class FakeProvider:
    """Configurable provider: connected set + granted-id predicate."""

    def __init__(self, connected=frozenset(), denied_ids=frozenset(), grant_result=True):
        self._connected = set(connected)
        self._denied = set(denied_ids)
        self._grant_result = grant_result

    def connected_connector_names(self):
        return set(self._connected)

    def is_granted(self, entry):
        if entry.id in self._denied:
            return False
        return self._grant_result


class RaisingGrantProvider:
    def connected_connector_names(self):
        return set()

    def is_granted(self, entry):
        raise RuntimeError("entitlement service down")


class RaisingConnectedProvider:
    def connected_connector_names(self):
        raise RuntimeError("registry unavailable")

    def is_granted(self, entry):
        return True


# ── protocol shape ──────────────────────────────────────────────────────


def test_fake_and_default_providers_satisfy_protocol():
    assert isinstance(FakeProvider(), EntitlementProvider)
    assert isinstance(DefaultEntitlementProvider(), EntitlementProvider)


# ── filtering: absent means absent ──────────────────────────────────────


class TestGrantFiltering:
    def test_denied_entry_absent_from_apply_and_search(self):
        store = _synthetic_store()
        provider = FakeProvider(denied_ids={"connector:alpha_crm"})
        applied_ids = {o.entry.id for o in AtlasOverlay.apply(store.entries, provider)}
        assert "connector:alpha_crm" not in applied_ids
        assert "connector:beta_crm" in applied_ids  # siblings unaffected

        result_ids = [o.entry.id for o in AtlasOverlay.search(store, "crm", provider)]
        assert "connector:alpha_crm" not in result_ids
        assert "connector:beta_crm" in result_ids

    def test_denied_entry_describe_returns_none_like_unknown(self):
        store = _synthetic_store()
        provider = FakeProvider(denied_ids={"connector:alpha_crm"})
        assert AtlasOverlay.describe(store, "connector:alpha_crm", provider) is None
        assert AtlasOverlay.describe(store, "connector:no_such", provider) is None

    def test_visible_ids_excludes_denied(self):
        store = _synthetic_store()
        provider = FakeProvider(denied_ids={"connector:alpha_crm"})
        visible = AtlasOverlay.visible_ids(store, provider)
        assert "connector:alpha_crm" not in visible
        assert "primitive:pocket" in visible

    def test_ambiguous_grant_is_filtered(self):
        """A non-True grant answer (None) is ambiguous → fail-closed."""
        store = _synthetic_store()
        provider = FakeProvider(grant_result=None)
        assert AtlasOverlay.apply(store.entries, provider) == []
        assert AtlasOverlay.describe(store, "primitive:pocket", provider) is None

    def test_filtering_frees_result_slots(self):
        """Filtered entries never eat limit slots — filter happens pre-truncate."""
        store = _synthetic_store()
        provider = FakeProvider(denied_ids={"connector:beta_crm"})
        results = [o.entry.id for o in AtlasOverlay.search(store, "crm", provider, limit=2)]
        assert len(results) == 2
        assert "connector:beta_crm" not in results


# ── fail-closed on provider errors ──────────────────────────────────────


class TestFailClosed:
    def test_raising_grant_filters_everything_it_gates(self):
        store = _synthetic_store()
        provider = RaisingGrantProvider()
        assert AtlasOverlay.search(store, "crm", provider) == []
        assert AtlasOverlay.describe(store, "primitive:pocket", provider) is None
        assert AtlasOverlay.visible_ids(store, provider) == []

    def test_raising_connected_set_marks_connectors_unavailable_not_filtered(self):
        store = _synthetic_store()
        provider = RaisingConnectedProvider()
        overlaid = AtlasOverlay.apply(store.entries, provider)
        connectors = [o for o in overlaid if o.entry.kind == "connector"]
        assert connectors, "connectors stay VISIBLE — availability is not a grant"
        assert all(o.available is False for o in connectors)
        others = [o for o in overlaid if o.entry.kind != "connector"]
        assert all(o.available is None for o in others)


# ── availability annotation + re-ranking ────────────────────────────────


class TestAvailabilityAndRanking:
    def test_connector_annotation_and_os_entries_unannotated(self):
        store = _synthetic_store()
        provider = FakeProvider(connected={"alpha_crm"})
        by_id = {o.entry.id: o for o in AtlasOverlay.apply(store.entries, provider)}
        assert by_id["connector:alpha_crm"].available is True
        assert by_id["connector:beta_crm"].available is False
        assert by_id["primitive:pocket"].available is None
        assert by_id["surface:integrations"].available is None
        assert by_id["sense:paw.email.v1"].available is None

    def test_available_ranks_above_unavailable_at_equal_score(self):
        store = _synthetic_store()
        # Base order for "crm": beta before alpha (seed order, equal score).
        base = [e.id for e in store.search("crm", limit=10)]
        assert base.index("connector:beta_crm") < base.index("connector:alpha_crm")

        provider = FakeProvider(connected={"alpha_crm"})
        overlaid = [o.entry.id for o in AtlasOverlay.search(store, "crm", provider, limit=10)]
        assert overlaid.index("connector:alpha_crm") < overlaid.index("connector:beta_crm")

    def test_rerank_never_overrides_base_relevance(self):
        """An unavailable connector with a HIGHER score stays above an
        available one with a lower score — the re-sort is availability at
        equal relevance only."""
        store = _synthetic_store()
        provider = FakeProvider(connected={"alpha_crm"})
        # "beta crm" → double name hit on "Beta CRM" (score 10) vs Alpha
        # CRM's single name hit (5): beta wins on relevance despite being
        # the unavailable one.
        ids = [o.entry.id for o in AtlasOverlay.search(store, "beta crm", provider, limit=10)]
        assert ids[0] == "connector:beta_crm"  # unavailable but most relevant

    def test_search_scored_backward_compat(self):
        """store.search delegates to search_scored with identical results."""
        store = _synthetic_store()
        assert store.search("crm", limit=3) == [e for _, e in store.search_scored("crm", limit=3)]
        assert store.search("", limit=5) == []
        assert store.search_scored("") == []


# ── no mutation of the shared store ─────────────────────────────────────


def test_overlay_never_mutates_store_entries():
    store = _synthetic_store()
    before = [e.model_dump() for e in store.entries]
    provider = FakeProvider(connected={"alpha_crm"}, denied_ids={"connector:gamma_pay"})
    overlaid = AtlasOverlay.apply(store.entries, provider)
    AtlasOverlay.search(store, "crm", provider)
    assert [e.model_dump() for e in store.entries] == before
    # The annotation lives on the wrapper, never on the entry model.
    assert all(isinstance(o, OverlaidEntry) for o in overlaid)
    assert all("available" not in o.entry.model_dump() for o in overlaid)


# ── DefaultEntitlementProvider (OSS default, real seam shape) ───────────


class _FakeRegistry:
    """Mimics ConnectorRegistry.status(scope_key) durable-state rows."""

    def __init__(self, connected_by_scope):
        self._by_scope = connected_by_scope
        self.calls: list[str] = []

    def status(self, scope_key):
        from pocketpaw.connectors.protocol import ConnectorStatus

        self.calls.append(scope_key)
        connected = self._by_scope.get(scope_key, set())
        return [
            {
                "name": name,
                "display_name": name,
                "icon": "plug",
                "status": ConnectorStatus.CONNECTED
                if name in connected
                else ConnectorStatus.DISCONNECTED,
            }
            for name in ("alpha_crm", "beta_crm", "gamma_pay")
        ]


class TestDefaultProvider:
    def test_reads_connected_names_from_registry_scope(self):
        registry = _FakeRegistry({"ws:t1": {"alpha_crm"}, "default": {"gamma_pay"}})
        p_ws = DefaultEntitlementProvider(scope_key="ws:t1", registry=registry)
        p_local = DefaultEntitlementProvider(registry=registry)
        assert p_ws.connected_connector_names() == {"alpha_crm"}
        assert p_local.connected_connector_names() == {"gamma_pay"}
        assert registry.calls == ["ws:t1", "default"]

    def test_oss_default_gates_nothing(self):
        """OS-level entries (and everything else) are never filtered in OSS."""
        store = _synthetic_store()
        registry = _FakeRegistry({"default": {"alpha_crm"}})
        provider = DefaultEntitlementProvider(registry=registry)
        overlaid = AtlasOverlay.apply(store.entries, provider)
        assert {o.entry.id for o in overlaid} == {e.id for e in store.entries}
        by_id = {o.entry.id: o for o in overlaid}
        assert by_id["connector:alpha_crm"].available is True
        assert by_id["connector:beta_crm"].available is False


# ── MCP tool handlers with stubbed providers (real packaged store) ──────


def _cards(result: dict) -> list[dict]:
    block = next(c for c in result["content"] if c["type"] == "text")
    return json.loads(block["text"])["results"]


def _text_of(result: dict) -> str:
    return next(c for c in result["content"] if c["type"] == "text")["text"]


class TestMcpHandlersWithProvider:
    @pytest.mark.asyncio
    async def test_same_query_two_contexts_returns_context_appropriate_results(self):
        query = {"intent": "stripe invoices payments"}
        ctx_granted = FakeProvider(connected={"stripe"})
        ctx_filtered = FakeProvider(denied_ids={"connector:stripe"})

        out_a = await _atlas_search_handler(query, ctx_granted)
        out_b = await _atlas_search_handler(query, ctx_filtered)
        ids_a = [c["id"] for c in _cards(out_a)]
        ids_b = [c["id"] for c in _cards(out_b)]
        assert "connector:stripe" in ids_a
        assert "connector:stripe" not in ids_b
        assert ids_a != ids_b

    @pytest.mark.asyncio
    async def test_search_cards_carry_available_on_connectors_only(self):
        out = await _atlas_search_handler(
            {"intent": "stripe invoices"}, FakeProvider(connected={"stripe"})
        )
        cards = _cards(out)
        for card in cards:
            if card["kind"] == "connector":
                assert card["available"] is (card["id"] == "connector:stripe")
            else:
                assert "available" not in card

    @pytest.mark.asyncio
    async def test_describe_filtered_entry_is_not_found_without_leak(self):
        provider = FakeProvider(denied_ids={"connector:stripe"})
        out = await _atlas_describe_handler({"id": "connector:stripe"}, provider)
        assert out.get("is_error") is True
        text = _text_of(out)
        assert "unknown atlas id" in text
        assert "connector:stripe" not in text.split("Known ids:")[1]

    @pytest.mark.asyncio
    async def test_describe_unavailable_connector_points_at_integrations(self):
        provider = FakeProvider(connected=set())  # nothing connected
        out = await _atlas_describe_handler({"id": "connector:stripe"}, provider)
        assert not out.get("is_error")
        entry = json.loads(_text_of(out))
        assert entry["available"] is False
        # Route comes from the seed's integrations surface entry.
        from pocketpaw.atlas.store import get_atlas_store

        route = get_atlas_store().describe("surface:integrations").surface
        assert route and route in entry["connect_hint"]

    @pytest.mark.asyncio
    async def test_describe_available_connector_has_no_hint(self):
        provider = FakeProvider(connected={"stripe"})
        out = await _atlas_describe_handler({"id": "connector:stripe"}, provider)
        entry = json.loads(_text_of(out))
        assert entry["available"] is True
        assert "connect_hint" not in entry

    @pytest.mark.asyncio
    async def test_describe_os_entry_unaffected_by_provider(self):
        out = await _atlas_describe_handler({"id": "primitive:instinct"}, FakeProvider())
        entry = json.loads(_text_of(out))
        assert entry["id"] == "primitive:instinct"
        assert "available" not in entry

    @pytest.mark.asyncio
    async def test_raising_provider_fails_closed_through_the_tools(self):
        provider = RaisingGrantProvider()
        out = await _atlas_search_handler({"intent": "approve agent actions"}, provider)
        assert not out.get("is_error")
        assert "No atlas entries matched" in _text_of(out)
        out = await _atlas_describe_handler({"id": "primitive:instinct"}, provider)
        assert out.get("is_error") is True

    @pytest.mark.asyncio
    async def test_no_provider_keeps_global_behavior(self):
        out = await _atlas_search_handler({"intent": "approve agent actions"})
        cards = _cards(out)
        assert "primitive:instinct" in [c["id"] for c in cards[:3]]
        assert all("available" not in c for c in cards)


class TestTenantScopeKey:
    """The backend's scope resolution fails CLOSED on a blank workspace id
    (security audit AT-5 IMPORTANT-1): tenancy attached with an empty
    POCKETPAW_WORKSPACE_ID must resolve to a sentinel scope that matches no
    connector rows — never the shared "default" bucket."""

    def _backend(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        return ClaudeSDKBackend.__new__(ClaudeSDKBackend)

    def test_no_tenancy_env_is_default_scope(self):
        backend = self._backend()
        backend._extra_subprocess_env = {}
        assert backend._tenant_scope_key() == DEFAULT_SCOPE_KEY

    def test_workspace_id_maps_to_ws_scope(self):
        backend = self._backend()
        backend._extra_subprocess_env = {"POCKETPAW_WORKSPACE_ID": "w-123"}
        assert backend._tenant_scope_key() == "ws:w-123"

    def test_blank_workspace_id_fails_closed_to_sentinel(self):
        backend = self._backend()
        for blank in ("", "   "):
            backend._extra_subprocess_env = {"POCKETPAW_WORKSPACE_ID": blank}
            scope = backend._tenant_scope_key()
            assert scope == "ws:__missing-workspace-id__"
            assert scope != DEFAULT_SCOPE_KEY

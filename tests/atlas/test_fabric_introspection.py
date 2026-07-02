# tests/atlas/test_fabric_introspection.py — live Fabric introspection for
# the atlas MCP tools (AT-7). Created: 2026-07-02 (feat/atlas-fabric).
#
# Proves the FabricIntrospector seam end-to-end with a FAKE introspector
# (no pocketpaw_ee required): search surfaces synthetic fabric:<type> cards
# for entity-type / property queries APPENDED after compiled-entry results
# (never displacing them); describe answers fabric:<type> with the live
# schema (properties + links); absent introspector (OSS default) → fabric
# ids are unknown ids and no fabric cards appear; a RAISING introspector is
# treated exactly like an absent one (fail-closed, both tools, no crash).
# Also pins the structural protocol check and the EE adapter: adapter
# construction against a tmp-path WorkspaceFabricStore is import-guarded
# (pytest.importorskip) so the test runs where pocketpaw_ee is installed
# (or via PYTHONPATH=ee) and skips cleanly elsewhere.

import json

import pytest

from pocketpaw.agents.sdk_mcp_atlas import (
    _atlas_describe_handler,
    _atlas_search_handler,
)
from pocketpaw.atlas.fabric import (
    FABRIC_ID_PREFIX,
    FABRIC_KIND,
    FabricIntrospector,
    build_workspace_fabric_introspector,
    describe_fabric_id,
    search_entity_types,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_SCHEMA = {
    "Customer": {
        "name": "Customer",
        "properties": ["churn_risk", "email", "plan"],
        "links": [
            {"name": "competes_with", "from_type": "Customer", "to_type": "Competitor"},
        ],
    },
    "Competitor": {
        "name": "Competitor",
        "properties": ["pricing_page"],
        "links": [
            {"name": "competes_with", "from_type": "Customer", "to_type": "Competitor"},
        ],
    },
}


class FakeIntrospector:
    """In-memory FabricIntrospector — the shape the EE adapter produces."""

    def __init__(self, schema: dict | None = None) -> None:
        self._schema = _SCHEMA if schema is None else schema

    def list_entity_types(self) -> list[str]:
        return sorted(self._schema)

    def describe_entity_type(self, name: str) -> dict | None:
        return self._schema.get(name)


class RaisingIntrospector:
    """Every call raises — must be indistinguishable from an absent one."""

    def list_entity_types(self) -> list[str]:
        raise RuntimeError("fabric backend down")

    def describe_entity_type(self, name: str) -> dict | None:
        raise RuntimeError("fabric backend down")


def _text_of(result: dict) -> str:
    block = next((c for c in result.get("content", []) if c.get("type") == "text"), None)
    assert block is not None, "handler must return a text content block"
    return block["text"]


def _cards_of(result: dict) -> list[dict]:
    return json.loads(_text_of(result))["results"]


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_fake_satisfies_structural_protocol(self):
        assert isinstance(FakeIntrospector(), FabricIntrospector)
        assert isinstance(RaisingIntrospector(), FabricIntrospector)


# ---------------------------------------------------------------------------
# Search — fabric cards
# ---------------------------------------------------------------------------


class TestSearchWithIntrospector:
    @pytest.mark.asyncio
    async def test_entity_type_query_surfaces_fabric_card(self):
        out = await _atlas_search_handler(
            {"intent": "customer churn"}, introspector=FakeIntrospector()
        )
        assert not out.get("is_error")
        cards = _cards_of(out)
        fabric = [c for c in cards if c["id"].startswith(FABRIC_ID_PREFIX)]
        assert fabric, f"expected a fabric card, got ids {[c['id'] for c in cards]}"
        card = fabric[0]
        assert card["id"] == "fabric:Customer"
        assert card["kind"] == FABRIC_KIND
        assert card["name"] == "Customer"
        assert card["summary"]
        assert "properties" not in card, "search cards stay thin; describe carries the schema"

    @pytest.mark.asyncio
    async def test_property_token_match_surfaces_owning_type(self):
        # "churn" only exists as a Customer PROPERTY token, not a type name.
        out = await _atlas_search_handler({"intent": "churn risk"}, introspector=FakeIntrospector())
        cards = _cards_of(out)
        assert any(c["id"] == "fabric:Customer" for c in cards)

    @pytest.mark.asyncio
    async def test_compiled_results_never_displaced(self):
        # An entity type engineered to match a classic compiled query.
        introspector = FakeIntrospector(
            {"Agent": {"name": "Agent", "properties": ["approval_state"], "links": []}}
        )
        baseline = _cards_of(await _atlas_search_handler({"intent": "approve agent actions"}))
        out = _cards_of(
            await _atlas_search_handler(
                {"intent": "approve agent actions"}, introspector=introspector
            )
        )
        # Every compiled card survives, same order, fabric strictly appended.
        compiled = [c for c in out if not c["id"].startswith(FABRIC_ID_PREFIX)]
        assert [c["id"] for c in compiled] == [c["id"] for c in baseline]
        fabric_positions = [i for i, c in enumerate(out) if c["id"].startswith(FABRIC_ID_PREFIX)]
        assert fabric_positions, "the Agent entity type should match this intent"
        assert min(fabric_positions) >= len(compiled), "fabric cards must come last"

    @pytest.mark.asyncio
    async def test_fabric_only_match_still_answers(self):
        # A query with no compiled-entry overlap but a live-ontology hit
        # must return the fabric card, not "No atlas entries matched".
        introspector = FakeIntrospector(
            {"Zylkorb": {"name": "Zylkorb", "properties": [], "links": []}}
        )
        out = await _atlas_search_handler({"intent": "zylkorb"}, introspector=introspector)
        assert not out.get("is_error")
        cards = _cards_of(out)
        assert [c["id"] for c in cards] == ["fabric:Zylkorb"]


class TestSearchWithoutIntrospector:
    @pytest.mark.asyncio
    async def test_no_introspector_no_fabric_cards(self):
        out = await _atlas_search_handler({"intent": "customer churn"})
        assert not out.get("is_error")
        text = _text_of(out)
        if "No atlas entries matched" not in text:
            cards = json.loads(text)["results"]
            assert not any(c["id"].startswith(FABRIC_ID_PREFIX) for c in cards)

    @pytest.mark.asyncio
    async def test_raising_introspector_behaves_like_absent(self):
        raising = await _atlas_search_handler(
            {"intent": "approve agent actions"}, introspector=RaisingIntrospector()
        )
        absent = await _atlas_search_handler({"intent": "approve agent actions"})
        assert not raising.get("is_error")
        assert _cards_of(raising) == _cards_of(absent)


# ---------------------------------------------------------------------------
# Describe — live schema
# ---------------------------------------------------------------------------


class TestDescribe:
    @pytest.mark.asyncio
    async def test_describe_returns_properties_and_links(self):
        out = await _atlas_describe_handler(
            {"id": "fabric:Customer"}, introspector=FakeIntrospector()
        )
        assert not out.get("is_error")
        payload = json.loads(_text_of(out))
        assert payload["id"] == "fabric:Customer"
        assert payload["kind"] == FABRIC_KIND
        assert payload["properties"] == ["churn_risk", "email", "plan"]
        assert payload["links"] == [
            {"name": "competes_with", "from_type": "Customer", "to_type": "Competitor"}
        ]
        assert payload["workspace_scoped"] is True
        assert "narrative" in payload

    @pytest.mark.asyncio
    async def test_unknown_entity_type_falls_through_to_unknown_id(self):
        out = await _atlas_describe_handler(
            {"id": "fabric:Nonexistent"}, introspector=FakeIntrospector()
        )
        assert out.get("is_error") is True
        assert "unknown atlas id" in _text_of(out)

    @pytest.mark.asyncio
    async def test_absent_introspector_fabric_id_is_unknown(self):
        out = await _atlas_describe_handler({"id": "fabric:Customer"})
        assert out.get("is_error") is True
        text = _text_of(out)
        assert "unknown atlas id" in text
        # No fabric ids leak into the known-ids listing.
        assert FABRIC_ID_PREFIX not in text.split("Known ids:")[-1]

    @pytest.mark.asyncio
    async def test_raising_introspector_fabric_id_is_unknown(self):
        out = await _atlas_describe_handler(
            {"id": "fabric:Customer"}, introspector=RaisingIntrospector()
        )
        assert out.get("is_error") is True
        assert "unknown atlas id" in _text_of(out)

    @pytest.mark.asyncio
    async def test_compiled_entries_still_describe_with_introspector(self):
        out = await _atlas_describe_handler(
            {"id": "primitive:fabric"}, introspector=FakeIntrospector()
        )
        assert not out.get("is_error")
        assert json.loads(_text_of(out))["id"] == "primitive:fabric"


# ---------------------------------------------------------------------------
# Helper-level fail-closed behavior
# ---------------------------------------------------------------------------


class TestHelpersFailClosed:
    def test_search_helper_returns_empty_on_raise(self):
        assert search_entity_types(RaisingIntrospector(), "customer") == []

    def test_describe_helper_returns_none_on_raise(self):
        assert describe_fabric_id(RaisingIntrospector(), "fabric:Customer") is None

    def test_describe_helper_ignores_non_fabric_ids(self):
        assert describe_fabric_id(FakeIntrospector(), "primitive:fabric") is None
        assert describe_fabric_id(FakeIntrospector(), "fabric:") is None

    def test_search_helper_tolerates_malformed_shapes(self):
        class Malformed:
            def list_entity_types(self):
                return ["Customer", 7, ""]

            def describe_entity_type(self, name):
                return {"properties": "not-a-list", "links": {"nope": 1}}

        cards = search_entity_types(Malformed(), "customer")
        assert [c["id"] for c in cards] == ["fabric:Customer"]


# ---------------------------------------------------------------------------
# EE adapter (import-guarded — skips when pocketpaw_ee isn't installed)
# ---------------------------------------------------------------------------


class TestEeAdapter:
    def test_builder_returns_none_on_blank_workspace_id(self):
        assert build_workspace_fabric_introspector("") is None
        assert build_workspace_fabric_introspector("   ") is None

    def test_real_adapter_against_tmp_store(self, tmp_path):
        pytest.importorskip("pocketpaw_ee")
        from pocketpaw_ee.fabric import WorkspaceFabricRegistry, WorkspaceFabricStore

        store = WorkspaceFabricStore(tmp_path / "fabric_registry.db")
        store.register_entity_type("ws-1", "Customer")
        store.register_property("ws-1", "Customer", "email")
        store.register_property("ws-1", "Customer", "churn_risk")
        store.register_entity_type("ws-1", "Competitor")
        store.register_link("ws-1", "competes_with", "Customer", "Competitor")
        # Another workspace's ontology must be invisible.
        store.register_entity_type("ws-2", "Secret")

        from pocketpaw.atlas.fabric import RegistryFabricIntrospector

        adapter = RegistryFabricIntrospector(
            WorkspaceFabricRegistry(store=store, workspace_id="ws-1")
        )
        assert isinstance(adapter, FabricIntrospector)
        assert adapter.list_entity_types() == ["Competitor", "Customer"]
        described = adapter.describe_entity_type("Customer")
        assert described == {
            "name": "Customer",
            "properties": ["churn_risk", "email"],
            "links": [{"name": "competes_with", "from_type": "Customer", "to_type": "Competitor"}],
        }
        assert adapter.describe_entity_type("Secret") is None

        # And the tool layer serves it end-to-end.
        cards = search_entity_types(adapter, "customer churn")
        assert cards and cards[0]["id"] == "fabric:Customer"
        payload = describe_fabric_id(adapter, "fabric:Customer")
        assert payload is not None and payload["properties"] == ["churn_risk", "email"]

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
#
# Updated: 2026-08-17 (feat/ast-2-atlas-trust-aggregate, AST-2) — the
# TestSourceTruthAggregate section: describe on a TRACKED type (a real OSS
# FabricStore in tmp_path with two competing ``arr`` statements) carries the
# additive ``source_truth`` roll-up (properties.arr.disputed == 1, winner mix,
# tracked=True); an UNTRACKED type answers tracked=False with ZERO
# get_statements reads and at most one keys lookup (spy proxy); a missing /
# raising source-truth store leaves the key ABSENT and the registry payload
# intact (helper, handler and builder levels); search NEVER calls the
# aggregate; >500 tracked keys → sampled=True (with a rough timing print);
# and the store's type-scoped ``list_statement_keys`` is pinned directly.

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from pocketpaw.agents.sdk_mcp_atlas import (
    _atlas_describe_handler,
    _atlas_search_handler,
)
from pocketpaw.atlas.fabric import (
    FABRIC_ID_PREFIX,
    FABRIC_KIND,
    SOURCE_TRUTH_POINTER,
    SOURCE_TRUTH_SAMPLE_CAP,
    FabricIntrospector,
    RegistryFabricIntrospector,
    build_workspace_fabric_introspector,
    describe_fabric_id,
    describe_fabric_id_async,
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


class TestSearchReadEfficiency:
    """FINDING B — search must not run the full N-describe read path.

    ``search_entity_types`` scores on entity-type NAMES and PROPERTY names
    only; it never uses a type's link count. The live EE adapter's
    ``describe_entity_type`` opens three sqlite connections per type
    (exists + properties + links), so a full describe per type is 3N
    connections/queries per query — and the link count it pays for is
    discarded. Search must fetch properties only.
    """

    def test_search_does_not_call_full_describe(self):
        """Search prefers a properties-only read path over describe_entity_type,
        so it never pays for the per-type link fetch it doesn't score on."""
        calls = {"describe": 0, "properties": 0}

        class CountingIntrospector:
            def list_entity_types(self):
                return ["Customer", "Competitor"]

            def describe_entity_type(self, name):
                calls["describe"] += 1
                return _SCHEMA.get(name)

            def list_entity_properties(self, name):
                calls["properties"] += 1
                return _SCHEMA.get(name, {}).get("properties", [])

        cards = search_entity_types(CountingIntrospector(), "customer churn")
        assert cards and cards[0]["id"] == "fabric:Customer"
        assert calls["describe"] == 0, (
            "search must not call the full describe_entity_type (it fetches "
            f"unused links); describe called {calls['describe']}x"
        )
        assert calls["properties"] >= 1, "search must use the properties-only read path"

    def test_search_card_summary_has_no_link_count(self):
        """The thin search card must not carry a DB-derived link count — that
        would force the link fetch just to print a number. describe still
        carries links for the detail view."""
        cards = search_entity_types(FakeIntrospector(), "customer churn")
        assert cards and cards[0]["id"] == "fabric:Customer"
        assert "links" not in cards[0]["summary"].lower(), (
            f"search card summary should not report links, got: {cards[0]['summary']!r}"
        )

    def test_search_still_falls_back_when_no_properties_path(self):
        """An introspector without ``list_entity_properties`` (older shape)
        must still work — the describe path remains the fallback."""

        class OnlyDescribe:
            def list_entity_types(self):
                return ["Customer"]

            def describe_entity_type(self, name):
                return _SCHEMA.get(name)

        cards = search_entity_types(OnlyDescribe(), "churn risk")
        assert any(c["id"] == "fabric:Customer" for c in cards)


class TestEeAdapterReadPath:
    """FINDING B — the EE adapter exposes a properties-only read path that
    opens fewer connections than a full describe."""

    def test_adapter_properties_only_skips_links(self, tmp_path):
        pytest.importorskip("pocketpaw_ee")
        from pocketpaw_ee.fabric import WorkspaceFabricRegistry, WorkspaceFabricStore

        store = WorkspaceFabricStore(tmp_path / "fabric_registry.db")
        store.register_entity_type("ws-1", "Customer")
        store.register_property("ws-1", "Customer", "email")
        store.register_property("ws-1", "Customer", "churn_risk")

        registry = WorkspaceFabricRegistry(store=store, workspace_id="ws-1")

        # Spy on the registry via a counting proxy (the registry uses __slots__,
        # so its methods can't be monkeypatched in place). The properties-only
        # read path must NOT touch links or run an existence check.
        calls = {"properties": 0, "links": 0, "exists": 0}

        class CountingRegistry:
            def get_entity_properties(self, name):
                calls["properties"] += 1
                return registry.get_entity_properties(name)

            def list_entity_links(self, name):
                calls["links"] += 1
                return registry.list_entity_links(name)

            def entity_type_exists(self, name):
                calls["exists"] += 1
                return registry.entity_type_exists(name)

            def list_entity_types(self):
                return registry.list_entity_types()

        adapter = RegistryFabricIntrospector(CountingRegistry())
        props = adapter.list_entity_properties("Customer")
        assert sorted(props) == ["churn_risk", "email"]
        assert calls["properties"] == 1, "properties-only read hits get_entity_properties once"
        assert calls["links"] == 0, "properties-only read must not fetch links"
        assert calls["exists"] == 0, "properties-only read must not run the existence check"


class TestCamelSplit:
    """FINDING D — the camel split must break consecutive-capital acronyms.

    The old regex only broke lower→upper, so 'APIKey' / 'HTTPServer' /
    'IOError' never split and 'api key' / 'http server' queries missed them.
    """

    def test_acronym_boundary_splits(self):
        from pocketpaw.atlas.fabric import _CAMEL_RE

        def split(text):
            return _CAMEL_RE.sub(" ", text)

        assert split("HTTPServer") == "HTTP Server"
        assert split("APIKey") == "API Key"
        assert split("IOError") == "IO Error"
        # The existing lower->upper boundary still works.
        assert split("CustomerAccount") == "Customer Account"
        # A pure acronym with no trailing word stays intact.
        assert split("HTTP") == "HTTP"

    def test_acronym_type_name_matches_spaced_query(self):
        introspector = FakeIntrospector(
            {"APIKey": {"name": "APIKey", "properties": [], "links": []}}
        )
        cards = search_entity_types(introspector, "api key")
        assert any(c["id"] == "fabric:APIKey" for c in cards), (
            f"'api key' should match the APIKey entity type, got {[c['id'] for c in cards]}"
        )


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


# ---------------------------------------------------------------------------
# AST-2 — the type-level source-truth aggregate on describe
# ---------------------------------------------------------------------------


class _DictRegistry:
    """Registry-shaped fake for RegistryFabricIntrospector (no pocketpaw_ee)."""

    def __init__(self, schema: dict | None = None) -> None:
        self._schema = _SCHEMA if schema is None else schema

    def list_entity_types(self):
        return sorted(self._schema)

    def entity_type_exists(self, name):
        return name in self._schema

    def get_entity_properties(self, name):
        return set(self._schema.get(name, {}).get("properties", []))

    def list_entity_links(self, name):
        return list(self._schema.get(name, {}).get("links", []))


class _CountingStore:
    """Transparent proxy over a real FabricStore that counts the statement
    reads the aggregate may issue (the type-scoped keys lookup and the
    per-key get_statements walk)."""

    def __init__(self, store) -> None:
        self._store = store
        self.calls = {"list_statement_keys": 0, "get_statements": 0}

    def __getattr__(self, name):
        return getattr(self._store, name)

    async def list_statement_keys(self, **kwargs):
        self.calls["list_statement_keys"] += 1
        return await self._store.list_statement_keys(**kwargs)

    async def get_statements(self, *args, **kwargs):
        self.calls["get_statements"] += 1
        return await self._store.get_statements(*args, **kwargs)


async def _fresh_store(tmp_path, monkeypatch):
    """A FabricStore in 'off' mode (create_object writes NO statements, so
    only the statements the test appends explicitly are tracked)."""
    from pocketpaw.fabric.store import FabricStore

    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: "off")
    return FabricStore(tmp_path / "fabric.db")


async def _seed_disputed_customer(store):
    """One Customer whose ``arr`` has two competing OPEN statements from two
    writer classes (connector 120 vs inferred 150) — cross-tier, materially
    different, both open-validity → the resolver reports a dispute with the
    connector as winner. Returns the store's type + object."""
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(obj_type.id, {"name": "Acme", "arr": 120})
    crm = await store.upsert_source("connector_run", connector="crm", run_id="r1")
    agent = await store.upsert_source("agent_session", session_id="s1")
    await store.append_statement(obj.id, "arr", 120, crm.id, "connector")
    await store.append_statement(obj.id, "arr", 150, agent.id, "inferred")
    return obj_type, obj


class TestSourceTruthAggregate:
    @pytest.mark.asyncio
    async def test_tracked_type_reports_dispute_and_winner_mix(self, tmp_path, monkeypatch):
        store = await _fresh_store(tmp_path, monkeypatch)
        await _seed_disputed_customer(store)
        adapter = RegistryFabricIntrospector(_DictRegistry(), source_truth=store)

        payload = await describe_fabric_id_async(adapter, "fabric:Customer")

        assert payload is not None
        # The registry payload is untouched by the additive field.
        assert payload["properties"] == ["churn_risk", "email", "plan"]
        st = payload["source_truth"]
        assert st["tracked"] is True
        assert st["sampled"] is False
        assert st["object_count"] == 1
        assert st["mode"] in ("off", "shadow", "enforce")
        assert st["pointer"] == SOURCE_TRUTH_POINTER
        # Only the TRACKED property appears; "name" (untracked) is absent.
        assert set(st["properties"]) == {"arr"}
        arr = st["properties"]["arr"]
        assert arr["objects"] == 1
        assert arr["disputed"] == 1  # open connector vs inferred rival, different values
        assert arr["stale"] == 0 and arr["aging"] == 0  # both observed moments ago
        assert arr["winner_writer_mix"] == {"connector": 1}  # ladder: connector > inferred

    @pytest.mark.asyncio
    async def test_stale_winner_counted(self, tmp_path, monkeypatch):
        """A lone connector value observed long ago is the (stale) winner —
        the aggregate counts it under ``stale`` and it is not disputed."""
        store = await _fresh_store(tmp_path, monkeypatch)
        obj_type = await store.define_type(name="Customer", properties=[])
        obj = await store.create_object(obj_type.id, {"arr": 1})
        crm = await store.upsert_source("connector_run", connector="crm", run_id="r1")
        long_ago = datetime.now(UTC) - timedelta(days=400)
        await store.append_statement(obj.id, "arr", 1, crm.id, "connector", observed_at=long_ago)
        adapter = RegistryFabricIntrospector(_DictRegistry(), source_truth=store)

        st = (await describe_fabric_id_async(adapter, "fabric:Customer"))["source_truth"]

        assert st["properties"]["arr"] == {
            "objects": 1,
            "disputed": 0,
            "stale": 1,
            "aging": 0,
            "winner_writer_mix": {"connector": 1},
        }

    @pytest.mark.asyncio
    async def test_untracked_type_zero_statement_reads(self, tmp_path, monkeypatch):
        """Type exists in the store but has NO statements: exactly one keys
        lookup (the indexed existence check) and ZERO get_statements."""
        store = await _fresh_store(tmp_path, monkeypatch)
        obj_type = await store.define_type(name="Competitor", properties=[])
        await store.create_object(obj_type.id, {"pricing_page": "x"})
        spy = _CountingStore(store)
        adapter = RegistryFabricIntrospector(_DictRegistry(), source_truth=spy)

        payload = await describe_fabric_id_async(adapter, "fabric:Competitor")

        st = payload["source_truth"]
        assert st == {
            "mode": st["mode"],
            "tracked": False,
            "sampled": False,
            "object_count": 0,
            "properties": {},
            "pointer": SOURCE_TRUTH_POINTER,
        }
        assert spy.calls == {"list_statement_keys": 1, "get_statements": 0}, spy.calls

    @pytest.mark.asyncio
    async def test_type_unknown_to_store_zero_reads_at_all(self, tmp_path, monkeypatch):
        """Registry knows the type, the statement store never saw it: no
        statement read of any kind."""
        store = await _fresh_store(tmp_path, monkeypatch)
        spy = _CountingStore(store)
        adapter = RegistryFabricIntrospector(_DictRegistry(), source_truth=spy)

        st = (await describe_fabric_id_async(adapter, "fabric:Customer"))["source_truth"]

        assert st["tracked"] is False
        assert spy.calls == {"list_statement_keys": 0, "get_statements": 0}, spy.calls

    @pytest.mark.asyncio
    async def test_other_types_statements_do_not_leak_in(self, tmp_path, monkeypatch):
        """Competitor's tracked keys never count toward Customer's roll-up
        (the keys walk is type-scoped, not a full-fabric scan)."""
        store = await _fresh_store(tmp_path, monkeypatch)
        await _seed_disputed_customer(store)
        comp = await store.define_type(name="Competitor", properties=[])
        rival = await store.create_object(comp.id, {"pricing_page": "x"})
        src = await store.upsert_source("document", document_uri="doc://1")
        await store.append_statement(rival.id, "pricing_page", "y", src.id, "agent")
        spy = _CountingStore(store)
        adapter = RegistryFabricIntrospector(_DictRegistry(), source_truth=spy)

        st = (await describe_fabric_id_async(adapter, "fabric:Customer"))["source_truth"]

        assert set(st["properties"]) == {"arr"}
        assert st["object_count"] == 1
        # One keys lookup + one get_statements per tracked Customer key.
        assert spy.calls == {"list_statement_keys": 1, "get_statements": 1}, spy.calls

    @pytest.mark.asyncio
    async def test_no_store_bound_field_absent(self):
        adapter = RegistryFabricIntrospector(_DictRegistry())
        payload = await describe_fabric_id_async(adapter, "fabric:Customer")
        assert payload is not None
        assert "source_truth" not in payload
        assert payload["properties"] == ["churn_risk", "email", "plan"]

    @pytest.mark.asyncio
    async def test_raising_store_field_absent_payload_intact(self):
        class BrokenStore:
            async def get_type_by_name(self, *a, **k):
                raise RuntimeError("fabric.db missing")

        adapter = RegistryFabricIntrospector(_DictRegistry(), source_truth=BrokenStore())
        payload = await describe_fabric_id_async(adapter, "fabric:Customer")
        assert payload is not None
        assert "source_truth" not in payload
        assert payload["properties"] == ["churn_risk", "email", "plan"]

    @pytest.mark.asyncio
    async def test_plain_introspector_without_method_unchanged(self):
        """An introspector lacking the optional method (the two-method fake)
        answers exactly as before — no source_truth, no error."""
        payload = await describe_fabric_id_async(FakeIntrospector(), "fabric:Customer")
        assert payload == describe_fabric_id(FakeIntrospector(), "fabric:Customer")
        assert "source_truth" not in payload

    @pytest.mark.asyncio
    async def test_search_never_calls_the_aggregate(self):
        calls = {"aggregate": 0}

        class Introspector(FakeIntrospector):
            async def entity_type_source_truth(self, name):
                calls["aggregate"] += 1
                return {"tracked": True}

        introspector = Introspector()
        assert search_entity_types(introspector, "customer churn")
        out = await _atlas_search_handler({"intent": "customer churn"}, introspector=introspector)
        assert not out.get("is_error")
        assert calls["aggregate"] == 0, "search must stay properties-only (FINDING B)"

    @pytest.mark.asyncio
    async def test_handler_merges_source_truth_for_fabric_id(self):
        canned = {
            "mode": "shadow",
            "tracked": True,
            "sampled": False,
            "object_count": 41,
            "properties": {"arr": {"objects": 41, "disputed": 3, "stale": 12, "aging": 0}},
            "pointer": SOURCE_TRUTH_POINTER,
        }

        class Introspector(FakeIntrospector):
            async def entity_type_source_truth(self, name):
                assert name == "Customer"
                return canned

        out = await _atlas_describe_handler({"id": "fabric:Customer"}, introspector=Introspector())
        assert not out.get("is_error")
        payload = json.loads(_text_of(out))
        assert payload["source_truth"] == canned
        assert payload["properties"] == ["churn_risk", "email", "plan"]

        # The two-method fake still answers without the field.
        plain = json.loads(
            _text_of(
                await _atlas_describe_handler(
                    {"id": "fabric:Customer"}, introspector=FakeIntrospector()
                )
            )
        )
        assert "source_truth" not in plain

    @pytest.mark.asyncio
    async def test_sample_cap_marks_sampled(self, tmp_path, monkeypatch, capsys):
        """More tracked keys than the cap → sampled=True, walk stops at the
        cap. Seeds cap+10 keys and prints the aggregate's wall-clock so the
        per-describe cost of a saturated type is known."""
        import sqlite3

        store = await _fresh_store(tmp_path, monkeypatch)
        obj_type = await store.define_type(name="Customer", properties=[])
        src = await store.upsert_source("connector_run", connector="crm", run_id="r1")
        objects = 51
        props_per_object = 10  # 51 * 10 = 510 keys > cap (500)
        obj_ids = []
        for i in range(objects):
            obj = await store.create_object(obj_type.id, {"idx": i})
            obj_ids.append(obj.id)
        # Bulk-seed statements straight into the tmp DB (the store's schema is
        # already ensured) — this is fixture speed, not the read under test.
        now = datetime.now(UTC).isoformat()
        rows = [
            (
                f"st-{i}-{p}",
                oid,
                f"p{p}",
                json.dumps(i),
                src.id,
                "connector",
                now,
                now,
                now,
                None,
                "normal",
                None,
                0,
                None,
            )
            for i, oid in enumerate(obj_ids)
            for p in range(props_per_object)
        ]
        with sqlite3.connect(tmp_path / "fabric.db") as db:
            db.executemany(
                "INSERT INTO fabric_statements (id, object_id, property, value, "
                "source_ref_id, writer_class, observed_at, recorded_at, valid_from, "
                "valid_to, rank, rank_reason, pinned, workspace_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        spy = _CountingStore(store)
        adapter = RegistryFabricIntrospector(_DictRegistry(), source_truth=spy)

        t0 = time.perf_counter()
        st = (await describe_fabric_id_async(adapter, "fabric:Customer"))["source_truth"]
        elapsed = time.perf_counter() - t0
        print(
            f"\n[AST-2 sample-cap] {objects * props_per_object} tracked keys → "
            f"walked {SOURCE_TRUTH_SAMPLE_CAP} in {elapsed:.2f}s"
        )

        assert st["tracked"] is True
        assert st["sampled"] is True
        assert sum(p["objects"] for p in st["properties"].values()) == SOURCE_TRUTH_SAMPLE_CAP
        assert spy.calls["list_statement_keys"] == 1
        assert spy.calls["get_statements"] == SOURCE_TRUTH_SAMPLE_CAP


class TestStoreTypeScopedKeys:
    """The store read helper behind the aggregate: ``list_statement_keys``
    with ``type_id`` / ``limit`` (additive kwargs)."""

    @pytest.mark.asyncio
    async def test_type_id_scopes_and_limit_caps(self, tmp_path, monkeypatch):
        store = await _fresh_store(tmp_path, monkeypatch)
        cust_type, cust = await _seed_disputed_customer(store)
        comp = await store.define_type(name="Competitor", properties=[])
        rival = await store.create_object(comp.id, {"pricing_page": "x"})
        src = await store.upsert_source("document", document_uri="doc://1")
        await store.append_statement(rival.id, "pricing_page", "y", src.id, "agent")
        await store.append_statement(rival.id, "hq", "Berlin", src.id, "agent")

        # Unscoped (existing behavior): every tracked key.
        assert len(await store.list_statement_keys()) == 3
        # Type-scoped: only that type's keys.
        assert await store.list_statement_keys(type_id=cust_type.id) == [(cust.id, "arr")]
        comp_keys = await store.list_statement_keys(type_id=comp.id)
        assert comp_keys == [(rival.id, "hq"), (rival.id, "pricing_page")]
        # limit caps the walk.
        assert await store.list_statement_keys(type_id=comp.id, limit=1) == [(rival.id, "hq")]
        # Unknown type → nothing.
        assert await store.list_statement_keys(type_id="nope") == []

    @pytest.mark.asyncio
    async def test_type_scoped_query_uses_indexes(self, tmp_path, monkeypatch):
        """EXPLAIN QUERY PLAN: the type-scoped walk is an indexed SEARCH on
        both sides (idx_objects_type → idx_statements_object[_property]) —
        never a full table SCAN on either."""
        import sqlite3

        store = await _fresh_store(tmp_path, monkeypatch)
        await _seed_disputed_customer(store)
        with sqlite3.connect(tmp_path / "fabric.db") as db:
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT DISTINCT s.object_id, s.property "
                "FROM fabric_statements s JOIN fabric_objects o ON o.id = s.object_id "
                "WHERE o.type_id = ? ORDER BY s.object_id, s.property LIMIT ?",
                ("t", 501),
            ).fetchall()
        text = " | ".join(str(r[3]) for r in plan)
        assert "SCAN" not in text, text
        assert "idx_objects_type" in text, text
        assert "idx_statements_object" in text, text


class TestBuilderBindsSourceTruth:
    def test_builder_degrades_source_truth_alone(self, tmp_path, monkeypatch):
        """get_fabric_store raising must NOT null the introspector: the registry
        payload still answers and only source_truth is absent."""
        pytest.importorskip("pocketpaw_ee")
        import pocketpaw_ee.fabric as ee_fabric

        registry_store = ee_fabric.WorkspaceFabricStore(tmp_path / "fabric_registry.db")
        registry_store.register_entity_type("ws-1", "Customer")
        registry_store.register_property("ws-1", "Customer", "email")
        monkeypatch.setattr(ee_fabric, "WorkspaceFabricStore", lambda: registry_store)

        def boom(**_kw):
            raise RuntimeError("fabric.db unavailable")

        monkeypatch.setattr("pocketpaw.stores.get_fabric_store", boom)

        introspector = build_workspace_fabric_introspector("ws-1")
        assert introspector is not None
        payload = describe_fabric_id(introspector, "fabric:Customer")
        assert payload is not None and payload["properties"] == ["email"]

        import asyncio

        async_payload = asyncio.run(describe_fabric_id_async(introspector, "fabric:Customer"))
        assert async_payload is not None
        assert "source_truth" not in async_payload

    def test_builder_binds_workspace_store(self, tmp_path, monkeypatch):
        """Happy path: the builder hands the introspector the workspace's
        FabricStore, and describe carries source_truth."""
        pytest.importorskip("pocketpaw_ee")
        import pocketpaw_ee.fabric as ee_fabric

        registry_store = ee_fabric.WorkspaceFabricStore(tmp_path / "fabric_registry.db")
        registry_store.register_entity_type("ws-1", "Customer")
        monkeypatch.setattr(ee_fabric, "WorkspaceFabricStore", lambda: registry_store)

        from pocketpaw.fabric.store import FabricStore

        monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: "off")
        fabric_store = FabricStore(tmp_path / "fabric.db")
        seen = {}

        def fake_get_fabric_store(*, workspace_id=None):
            seen["workspace_id"] = workspace_id
            return fabric_store

        monkeypatch.setattr("pocketpaw.stores.get_fabric_store", fake_get_fabric_store)

        introspector = build_workspace_fabric_introspector("ws-1")
        assert introspector is not None
        assert seen == {"workspace_id": "ws-1"}

        import asyncio

        async def _run():
            await _seed_disputed_customer(fabric_store)
            return await describe_fabric_id_async(introspector, "fabric:Customer")

        payload = asyncio.run(_run())
        assert payload["source_truth"]["properties"]["arr"]["disputed"] == 1

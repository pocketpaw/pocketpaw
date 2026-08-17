# tests/atlas/test_overlay_flags.py — flag-aware ``available`` (tri-state
# ``mode``) for the rollout-flagged primitives (AST-3). Created: 2026-08-17
# (feat/ast-3-atlas-flag-aware). Proves, on a synthetic store and through the
# real MCP handlers: ``primitive:source-truth`` reads
# ``settings.fabric_source_truth_mode`` and ``primitive:verify-loop`` reads the
# HIGHER of ``effective_deep_work_verify_mode()`` /
# ``effective_cloud_plan_verify_mode()`` (enforce > shadow > off) — per overlay
# pass, so a monkeypatched setting is seen immediately; ``off`` → ``available
# False`` + ``mode "off"`` and the existing equal-score demotion re-sort ranks
# the entry below an available primitive; ``shadow`` / ``enforce`` → ``available
# True`` with the mode surfaced; every other entry stays ``available None`` /
# ``mode None``; ``available``/``mode`` are DISCOVERY HINTS ONLY — ``is_granted``
# and describe/visible_ids never filter on them (the primitives stay
# describable at every mode); a settings failure fails closed to ``off``
# (still described); describe renders ``mode`` + an ``enable_hint`` (env-var
# pointer, NOT the connector ``connect_hint``) when off and no hint when live;
# search cards carry ``mode`` for the two primitives.

import json

import pytest

from pocketpaw.agents.sdk_mcp_atlas import (
    _atlas_describe_handler,
    _atlas_search_handler,
)
from pocketpaw.atlas.model import AtlasEntry, AtlasModel
from pocketpaw.atlas.overlay import (
    FLAG_ENABLE_HINTS,
    FLAGGED_PRIMITIVE_IDS,
    AtlasOverlay,
    DefaultEntitlementProvider,
    OverlaidEntry,
)
from pocketpaw.atlas.store import AtlasStore
from pocketpaw.config import get_settings

SOURCE_TRUTH = "primitive:source-truth"
VERIFY_LOOP = "primitive:verify-loop"


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def flags(monkeypatch):
    """Pin EVERY flag input on the live settings object (never rely on the
    machine's ambient config.json — the legacy verify bools alone would flip
    the effective mode to ``enforce``). Returns a setter for per-test flips."""

    def _set(
        *,
        fabric: str = "off",
        deep_work: str = "off",
        cloud_plan: str = "off",
        deep_work_legacy: bool = False,
        cloud_plan_legacy: bool = False,
    ) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "fabric_source_truth_mode", fabric)
        monkeypatch.setattr(settings, "deep_work_verify_mode", deep_work)
        monkeypatch.setattr(settings, "cloud_plan_verify_mode", cloud_plan)
        monkeypatch.setattr(settings, "deep_work_verify_loop_enabled", deep_work_legacy)
        monkeypatch.setattr(settings, "cloud_plan_verify_loop_enabled", cloud_plan_legacy)

    _set()
    return _set


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
    """The two flagged primitives + an always-on primitive sharing their
    keywords (equal-score demotion case), a surface, and a connector. Seed
    order puts the flagged primitives FIRST so a re-sort is provably the
    overlay's doing, not seed order."""
    return AtlasStore(
        AtlasModel(
            entries=[
                _entry(SOURCE_TRUTH, "primitive", "Source-truth", keywords=["provenance"]),
                _entry(VERIFY_LOOP, "primitive", "Verify loop", keywords=["verdict"]),
                _entry(
                    "primitive:pocket", "primitive", "Pocket", keywords=["provenance", "verdict"]
                ),
                _entry("surface:integrations", "surface", "Integrations", surface="/x/int"),
                _entry("connector:alpha_crm", "connector", "Alpha CRM", keywords=["crm"]),
            ]
        )
    )


class FakeProvider:
    def __init__(self, connected=frozenset()):
        self._connected = set(connected)

    def connected_connector_names(self):
        return set(self._connected)

    def is_granted(self, entry):
        return True


def _by_id(store: AtlasStore, provider) -> dict[str, OverlaidEntry]:
    return {o.entry.id: o for o in AtlasOverlay.apply(store.entries, provider)}


def _text_of(result: dict) -> str:
    return next(c for c in result["content"] if c["type"] == "text")["text"]


# ── the flag → available/mode mapping ───────────────────────────────────


class TestSourceTruthMode:
    def test_off_is_unavailable_with_mode_off(self, flags):
        by_id = _by_id(_synthetic_store(), FakeProvider())
        assert by_id[SOURCE_TRUTH].available is False
        assert by_id[SOURCE_TRUTH].mode == "off"

    @pytest.mark.parametrize("mode", ["shadow", "enforce"])
    def test_live_modes_are_available_and_surface_the_mode(self, flags, mode):
        flags(fabric=mode)
        by_id = _by_id(_synthetic_store(), FakeProvider())
        assert by_id[SOURCE_TRUTH].available is True
        assert by_id[SOURCE_TRUTH].mode == mode

    def test_setting_is_read_per_overlay_pass(self, flags):
        store = _synthetic_store()
        assert _by_id(store, FakeProvider())[SOURCE_TRUTH].mode == "off"
        flags(fabric="shadow")  # flipped mid-session — no import-time cache
        assert _by_id(store, FakeProvider())[SOURCE_TRUTH].mode == "shadow"


class TestVerifyLoopMode:
    def test_both_off_is_unavailable(self, flags):
        by_id = _by_id(_synthetic_store(), FakeProvider())
        assert by_id[VERIFY_LOOP].available is False
        assert by_id[VERIFY_LOOP].mode == "off"

    def test_deep_work_shadow_alone_is_available_in_shadow(self, flags):
        flags(deep_work="shadow")
        o = _by_id(_synthetic_store(), FakeProvider())[VERIFY_LOOP]
        assert o.available is True
        assert o.mode == "shadow"

    def test_mode_is_the_higher_of_the_two_flags(self, flags):
        flags(deep_work="shadow", cloud_plan="enforce")
        assert _by_id(_synthetic_store(), FakeProvider())[VERIFY_LOOP].mode == "enforce"
        flags(deep_work="enforce", cloud_plan="off")
        assert _by_id(_synthetic_store(), FakeProvider())[VERIFY_LOOP].mode == "enforce"

    def test_legacy_bool_resolves_through_the_effective_reader(self, flags):
        # effective_*_verify_mode() maps the legacy bool to enforce — the
        # overlay must go through the resolver, not the raw mode field.
        flags(cloud_plan_legacy=True)
        assert _by_id(_synthetic_store(), FakeProvider())[VERIFY_LOOP].mode == "enforce"


class TestEverythingElseUnchanged:
    def test_non_flagged_entries_carry_no_mode(self, flags):
        flags(fabric="enforce", deep_work="enforce")
        by_id = _by_id(_synthetic_store(), FakeProvider(connected={"alpha_crm"}))
        assert by_id["primitive:pocket"].available is None
        assert by_id["primitive:pocket"].mode is None
        assert by_id["surface:integrations"].mode is None
        # Connectors keep the connector-only annotation, no mode.
        assert by_id["connector:alpha_crm"].available is True
        assert by_id["connector:alpha_crm"].mode is None

    def test_mode_lives_on_the_wrapper_not_the_entry(self, flags):
        store = _synthetic_store()
        before = [e.model_dump() for e in store.entries]
        AtlasOverlay.apply(store.entries, FakeProvider())
        assert [e.model_dump() for e in store.entries] == before

    def test_settings_failure_fails_closed_to_off_but_still_describes(self, flags, monkeypatch):
        def _boom():
            raise RuntimeError("settings unavailable")

        monkeypatch.setattr("pocketpaw.config.get_settings", _boom)
        store = _synthetic_store()
        o = AtlasOverlay.describe(store, SOURCE_TRUTH, FakeProvider())
        assert o is not None, "a settings failure must never HIDE the primitive"
        assert o.available is False
        assert o.mode == "off"


# ── ranking: off demotes at equal score, unchanged sort logic ────────────


class TestFlagDemotion:
    def test_off_primitive_ranks_below_available_primitive_at_equal_score(self, flags):
        store = _synthetic_store()
        # Base order for "provenance": source-truth before pocket (seed order,
        # equal keyword score).
        base = [e.id for e in store.search("provenance", limit=10)]
        assert base.index(SOURCE_TRUTH) < base.index("primitive:pocket")

        ids = [o.entry.id for o in AtlasOverlay.search(store, "provenance", FakeProvider())]
        assert ids.index("primitive:pocket") < ids.index(SOURCE_TRUTH)
        assert SOURCE_TRUTH in ids, "demoted, NEVER hidden"

    def test_live_primitive_keeps_seed_order(self, flags):
        flags(fabric="shadow")
        store = _synthetic_store()
        ids = [o.entry.id for o in AtlasOverlay.search(store, "provenance", FakeProvider())]
        assert ids.index(SOURCE_TRUTH) < ids.index("primitive:pocket")

    def test_verify_loop_demotes_the_same_way(self, flags):
        store = _synthetic_store()
        ids = [o.entry.id for o in AtlasOverlay.search(store, "verdict", FakeProvider())]
        assert ids.index("primitive:pocket") < ids.index(VERIFY_LOOP)
        flags(deep_work="shadow")
        ids = [o.entry.id for o in AtlasOverlay.search(store, "verdict", FakeProvider())]
        assert ids.index(VERIFY_LOOP) < ids.index("primitive:pocket")


# ── discovery hints only: never an enforcement gate ─────────────────────


class TestDiscoveryOnly:
    @pytest.mark.parametrize("fabric", ["off", "shadow", "enforce"])
    def test_default_provider_grants_regardless_of_mode(self, flags, fabric):
        flags(fabric=fabric)
        provider = DefaultEntitlementProvider()
        store = _synthetic_store()
        for entry_id in FLAGGED_PRIMITIVE_IDS:
            assert provider.is_granted(store.describe(entry_id)) is True

    def test_off_primitives_are_still_described_and_listed(self, flags):
        store = _synthetic_store()
        provider = FakeProvider()
        for entry_id in FLAGGED_PRIMITIVE_IDS:
            assert AtlasOverlay.describe(store, entry_id, provider) is not None
            assert entry_id in AtlasOverlay.visible_ids(store, provider)

    def test_flagged_ids_and_hints_are_paired(self):
        assert set(FLAGGED_PRIMITIVE_IDS) == {SOURCE_TRUTH, VERIFY_LOOP}
        assert set(FLAG_ENABLE_HINTS) == set(FLAGGED_PRIMITIVE_IDS)
        assert "POCKETPAW_FABRIC_SOURCE_TRUTH_MODE" in FLAG_ENABLE_HINTS[SOURCE_TRUTH]
        assert "deep_work_verify_mode" in FLAG_ENABLE_HINTS[VERIFY_LOOP]
        assert "cloud_plan_verify_mode" in FLAG_ENABLE_HINTS[VERIFY_LOOP]


# ── describe / search rendering through the real MCP handlers ───────────


class TestDescribeRendering:
    @pytest.mark.asyncio
    async def test_describe_source_truth_off_shows_mode_and_enable_pointer(self, flags):
        out = await _atlas_describe_handler({"id": SOURCE_TRUTH}, FakeProvider())
        assert not out.get("is_error")
        payload = json.loads(_text_of(out))
        assert payload["available"] is False
        assert payload["mode"] == "off"
        assert "POCKETPAW_FABRIC_SOURCE_TRUTH_MODE=shadow|enforce" in payload["enable_hint"]
        assert "docs/atlas.md" in payload["enable_hint"]
        # The connector pointer must NOT leak onto a primitive.
        assert "connect_hint" not in payload

    @pytest.mark.asyncio
    async def test_describe_source_truth_shadow_reads_live(self, flags):
        flags(fabric="shadow")
        payload = json.loads(
            _text_of(await _atlas_describe_handler({"id": SOURCE_TRUTH}, FakeProvider()))
        )
        assert payload["available"] is True
        assert payload["mode"] == "shadow"
        assert "enable_hint" not in payload

    @pytest.mark.asyncio
    async def test_describe_verify_loop_off_then_enforce(self, flags):
        payload = json.loads(
            _text_of(await _atlas_describe_handler({"id": VERIFY_LOOP}, FakeProvider()))
        )
        assert payload["available"] is False and payload["mode"] == "off"
        assert "deep_work_verify_mode" in payload["enable_hint"]
        flags(cloud_plan="enforce")
        payload = json.loads(
            _text_of(await _atlas_describe_handler({"id": VERIFY_LOOP}, FakeProvider()))
        )
        assert payload["available"] is True and payload["mode"] == "enforce"
        assert "enable_hint" not in payload

    @pytest.mark.asyncio
    async def test_describe_other_primitive_untouched(self, flags):
        payload = json.loads(
            _text_of(await _atlas_describe_handler({"id": "primitive:instinct"}, FakeProvider()))
        )
        assert "available" not in payload
        assert "mode" not in payload

    @pytest.mark.asyncio
    async def test_search_cards_carry_mode_for_flagged_primitives(self, flags):
        flags(fabric="enforce")
        out = await _atlas_search_handler({"intent": "is this disputed"}, FakeProvider())
        cards = {c["id"]: c for c in json.loads(_text_of(out))["results"]}
        assert cards[SOURCE_TRUTH]["available"] is True
        assert cards[SOURCE_TRUTH]["mode"] == "enforce"
        assert all("mode" not in c for i, c in cards.items() if i not in FLAGGED_PRIMITIVE_IDS)

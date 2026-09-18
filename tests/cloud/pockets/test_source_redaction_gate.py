# tests/cloud/pockets/test_source_redaction_gate.py — proves SF-2: a Paw Site's
# authored ``source`` does not reach the wire when the gate applies, and DOES
# still reach every consumer that needs it to build, preview or edit the site.
#
# The gate is three conditions ANDed in ``pockets.service._source_visible_for_doc``:
# the pocket was born after the flip (``Pocket.source_gated``, stamped at create
# from ``sites_source_gate_enabled``), the setting is still on, and the WORKSPACE
# is not entitled (``Entitlements.site_source_visible``).
#
# WHAT A REVIEWER SHOULD CHECK, in order:
#   (a) the cohort — a pocket born before the flip keeps source forever, one born
#       after it on a free workspace does not, and a paid workspace is untouched;
#   (b) the reach — the chokepoint is ``pocket_to_wire_dict``, so gating one
#       expression must also cover the gallery list, the PATCH and spec-merge
#       WRITE responses and the WebSocket broadcast. Those three are what a
#       read-only, route-level gate misses, and they each get a test;
#   (c) the share link — stripped unconditionally, on every tier (D4);
#   (d) the editor — ``pockets_service.get`` (the PIPELINE reader, deliberately
#       ungated) and everything routed through it still sees the real file map.
#       A gate that blanks the preview is worse than no gate at all;
#   (e) the flag — ``sourceVisible`` publishes the EFFECTIVE answer beside the
#       payload, because the client can see only half the rule it comes from;
#   (f) the signature — ``source_visible`` is required. A default is the bug.
#
# Every test drives the real resolver with ``get_workspace_plan`` /
# ``get_workspace_overrides`` monkeypatched, which is the pattern the whole tree
# uses on it, over the shared mongomock ``mongo_db`` fixture. Nothing here reads
# or writes a Site document, on purpose: the gate is deliberately keyed off the
# WORKSPACE plan, so a pocket with no Site row must resolve like any other rather
# than raise — which is asserted, not assumed.
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.models.pocket import Pocket as PocketDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import (
    CreatePocketRequest,
    UpdatePocketRequest,
    pocket_to_wire_dict,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("mongo_db")]

FREE_WS = "ws_sf2_free"
PAID_WS = "ws_sf2_paid"
USER = "user_sf2"

SOURCE_MAP = {
    "src/routes/+page.svelte": "<h1>Invoices, paid faster</h1>\n",
    "src/routes/+page.ts": "export const prerender = true;\n",
    "src/app.css": ":root { --ink: #17130f; }\n",
}

RIPPLE_SPEC: dict[str, Any] = {"ui": {"type": "flex", "children": []}, "state": {}}


@pytest.fixture
def plan(monkeypatch: pytest.MonkeyPatch):
    """Pin what plan a workspace resolves to. ``free`` withholds source; every
    paid rung grants it. Patched at ``workspace.service`` because that is the one
    seam ``resolve_entitlements`` reads and the whole tree already mocks."""

    def _plan(mapping: dict[str, str | None]) -> None:
        import pocketpaw_ee.cloud.workspace.service as ws_svc

        async def _get_plan(workspace_id: str) -> str | None:
            return mapping.get(workspace_id, "free")

        monkeypatch.setattr(ws_svc, "get_workspace_plan", _get_plan)
        monkeypatch.setattr(ws_svc, "get_workspace_overrides", AsyncMock(return_value=None))

    _plan({FREE_WS: "free", PAID_WS: "pro"})
    return _plan


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    """Turn ``sites_source_gate_enabled`` on or off for the duration of a test.

    The settings object is lru_cached, so every lazy ``get_settings()`` inside
    the service sees the same instance this mutates. monkeypatch restores it."""
    from pocketpaw.config import get_settings

    settings = get_settings()

    def _set(on: bool) -> None:
        monkeypatch.setattr(settings, "sites_source_gate_enabled", on)

    return _set


async def _make_site_pocket(workspace_id: str, *, name: str = "Site") -> dict:
    """A svelte-track site pocket, created through the REST create path so the
    cohort stamp is applied exactly as production applies it."""
    return await pockets_service.create(
        workspace_id,
        USER,
        CreatePocketRequest(
            name=name,
            type="site",
            pattern="landing",
            engine="svelte",
            source=dict(SOURCE_MAP),
            ripple_spec=dict(RIPPLE_SPEC),
        ),
    )


# ---------------------------------------------------------------------------
# (a) The cohort — who is gated, and who is permanently not.
# ---------------------------------------------------------------------------


async def test_a_new_free_pocket_is_redacted(gate, plan) -> None:
    """Acceptance 1, first half: a pocket created while the gate is on, in a free
    workspace, serves no source."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)

    assert created["source"] is None
    fetched = await pockets_service.get_for_wire(created["_id"], USER)
    assert fetched["source"] is None


async def test_a_existing_free_pocket_keeps_its_source(gate, plan) -> None:
    """Acceptance 1, second half, and the whole of D3. A pocket created BEFORE
    the flip is outside the cohort forever — switching the gate on afterwards
    must not take source away from somebody who already had it."""
    gate(False)
    created = await _make_site_pocket(FREE_WS, name="Pre-flip site")
    assert created["source"] == SOURCE_MAP

    gate(True)  # the flip happens now, with the pocket already in the DB
    fetched = await pockets_service.get_for_wire(created["_id"], USER)
    assert fetched["source"] == SOURCE_MAP


async def test_a_paid_workspace_is_unaffected(gate, plan) -> None:
    """Acceptance 2. Same flip, same create path, a plan that grants the
    capability — nothing is withheld."""
    gate(True)
    created = await _make_site_pocket(PAID_WS)

    assert created["source"] == SOURCE_MAP
    fetched = await pockets_service.get_for_wire(created["_id"], USER)
    assert fetched["source"] == SOURCE_MAP


async def test_a_cohort_stamp_is_persisted_not_inferred(gate, plan) -> None:
    """The cohort is a STORED flag, not a ``created_at`` comparison. Read it off
    the document: a date-derived cohort would leave nothing here to read, and
    would silently re-classify the row if anyone edited the timestamp."""
    gate(True)
    gated = await _make_site_pocket(FREE_WS)
    gate(False)
    ungated = await _make_site_pocket(FREE_WS, name="After revert")

    assert (await PocketDoc.get(gated["_id"])).source_gated is True
    assert (await PocketDoc.get(ungated["_id"])).source_gated is False


async def test_a_turning_the_gate_back_off_restores_source(gate, plan) -> None:
    """The rollout is reversible in BOTH directions. The stamp alone does not
    withhold anything — the live setting is ANDed with it — so flipping the
    setting back off gives every stamped pocket its source back with no
    migration and no second flag to remember."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)
    assert created["source"] is None

    gate(False)
    assert (await pockets_service.get_for_wire(created["_id"], USER))["source"] == SOURCE_MAP


async def test_a_gate_ships_disabled(plan) -> None:
    """The setting defaults OFF, so merging this does not change behaviour for
    anyone. Turning it on is a separate, revertible operational step."""
    from pocketpaw.config import Settings

    assert Settings.model_fields["sites_source_gate_enabled"].default is False
    assert PocketDoc.model_fields["source_gated"].default is False


async def test_a_pocket_with_no_site_row_resolves_denied_not_raising(gate, plan) -> None:
    """Acceptance 4. Nothing in this gate reads the Site collection — it is keyed
    off the WORKSPACE plan precisely because ``create_draft_site`` leaves a draft's
    Site row on the free floor. A pocket with no Site document at all must
    therefore resolve to withheld, calmly, rather than raise."""
    from pocketpaw_ee.cloud.models.site import Site

    gate(True)
    created = await _make_site_pocket(FREE_WS)

    assert await Site.find({"pocket_id": created["_id"]}).count() == 0
    assert (await pockets_service.get_for_wire(created["_id"], USER))["source"] is None


async def test_a_only_source_is_redacted(gate, plan) -> None:
    """Redact ``source`` and nothing else. ``rippleSpec`` is a separate authoring
    track and explicitly outside this gate; the identity fields are what the
    gallery renders a card from and must survive."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)
    wire = await pockets_service.get_for_wire(created["_id"], USER)

    assert wire["source"] is None
    assert wire["rippleSpec"] is not None
    assert wire["rippleSpec"]["ui"]["type"] == "flex"
    assert wire["name"] == "Site"
    assert wire["engine"] == "svelte"
    assert wire["pattern"] == "landing"


# ---------------------------------------------------------------------------
# (b) The reach — the three paths a route-level gate misses.
# ---------------------------------------------------------------------------


async def test_b_gallery_list_with_an_unpublished_site_pocket(gate, plan) -> None:
    """The gallery excludes PUBLISHED site pockets via ``site_pocket_ids``, but an
    unpublished one has no Site document and so is never excluded — it is listed
    like any other pocket. Assert its row carries no source.

    Note for a future reader: ``list_pockets`` ALSO projects ``source`` out of its
    Mongo query for weight reasons, so this row would be sourceless even with the
    gate off. Both belts are deliberate and this test pins the outcome, not which
    belt produced it — if the projection is ever widened, this must still hold."""
    gate(True)
    created = await _make_site_pocket(FREE_WS, name="Unpublished site")

    rows = await pockets_service.list_pockets(FREE_WS, USER)
    row = next(r for r in rows if r["_id"] == created["_id"])
    assert row["source"] is None


async def test_b_patch_write_response_is_redacted(gate, plan) -> None:
    """A WRITE echoes the full wire dict back. A read-only gate that only covered
    ``GET`` would leak the whole source map on every save."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)

    updated = await pockets_service.update(
        created["_id"], USER, UpdatePocketRequest(name="Renamed")
    )
    assert updated["name"] == "Renamed"
    assert updated["source"] is None


async def test_b_spec_merge_write_response_is_redacted(gate, plan) -> None:
    """The other write that returns a full wire dict."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)

    merged = await pockets_service.merge_spec(
        FREE_WS,
        USER,
        created["_id"],
        {"merge": {"state": {"greeting": "hi"}}},
    )
    assert merged["ok"] is True
    assert merged["pocket"]["source"] is None


async def test_b_websocket_broadcast_payload_is_redacted(gate, plan, recording_bus) -> None:
    """The socket path bypasses the dependency layer entirely, so no route-level
    gate would ever have reached it. It is covered here only because
    ``_pocket_event_payload`` builds its payload through the same serializer."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)

    payloads = [
        e.data["pocket"]
        for e in recording_bus.events
        if isinstance(getattr(e, "data", None), dict) and "pocket" in e.data
    ]
    assert payloads, "expected a pocket.* broadcast from create"
    assert all(p["source"] is None for p in payloads)
    assert any(p["_id"] == created["_id"] for p in payloads)


async def test_b_websocket_broadcast_is_unredacted_when_paid(gate, plan, recording_bus) -> None:
    """The broadcast's counterpart — it is gated, not blanket-stripped."""
    gate(True)
    created = await _make_site_pocket(PAID_WS)

    payload = next(
        e.data["pocket"]
        for e in recording_bus.events
        if isinstance(getattr(e, "data", None), dict)
        and "pocket" in e.data
        and e.data["pocket"]["_id"] == created["_id"]
    )
    assert payload["source"] == SOURCE_MAP


# ---------------------------------------------------------------------------
# (c) D4 — share links never serve source.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workspace_id", [FREE_WS, PAID_WS])
async def test_c_share_link_strips_source_on_every_tier(gate, plan, workspace_id) -> None:
    """D4. The token is a forwardable bearer credential with no user behind it, so
    there is no entitlement to resolve and the OWNER's is the wrong question.
    Stripped unconditionally: paid, free, and both cohorts."""
    gate(False)  # the pocket predates the flip, so only D4 can be doing this
    created = await _make_site_pocket(workspace_id)
    link = await pockets_service.generate_share_link(created["_id"], USER, "edit")

    shared = await pockets_service.access_via_share_link(link["token"])
    assert shared["_id"] == created["_id"]
    assert shared["source"] is None
    # The rest of the pocket is untouched — this strips source, it does not
    # break the share link.
    assert shared["rippleSpec"]["ui"]["type"] == "flex"


async def test_c_share_link_ignores_access_level(gate, plan) -> None:
    """``share_link_access`` is NOT consulted. It is enforced nowhere, so reading
    it here would imply a guarantee that does not exist — an ``edit`` link is
    stripped exactly like a ``view`` one."""
    gate(False)
    created = await _make_site_pocket(PAID_WS)
    for access in ("view", "comment", "edit"):
        link = await pockets_service.generate_share_link(created["_id"], USER, access)
        assert (await pockets_service.access_via_share_link(link["token"]))["source"] is None


# ---------------------------------------------------------------------------
# (d) The editor must keep working. This is the acceptance risk.
# ---------------------------------------------------------------------------


async def test_d_edit_lane_still_reads_source(gate, plan) -> None:
    """Acceptance 3, first half. ``pockets_service.get`` is the pipeline's reader
    — publish, the generator, the dev server and every ``set_*_source_file`` edit
    tool reach a pocket's source map through it. It is NOT gated, on purpose:
    withholding source there would not hide a site, it would stop the site being
    built, for exactly the cohort the gate targets."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)

    internal = await pockets_service.get(created["_id"], USER)
    assert internal["source"] == SOURCE_MAP


async def test_d_the_two_readers_split_only_on_source(gate, plan) -> None:
    """The split is by AUDIENCE and nothing else. Same pocket, same caller: the
    two readers must agree on every field except the two that ARE the gate —
    ``source`` and the ``sourceVisible`` that reports on it. Any third difference
    means the gate has quietly become a second, divergent serialization."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)

    pipeline = await pockets_service.get(created["_id"], USER)
    wire = await pockets_service.get_for_wire(created["_id"], USER)

    assert pipeline["source"] == SOURCE_MAP
    assert pipeline["sourceVisible"] is True
    assert wire["source"] is None
    assert wire["sourceVisible"] is False

    gated_keys = {"source", "sourceVisible"}
    assert {k: v for k, v in pipeline.items() if k not in gated_keys} == {
        k: v for k, v in wire.items() if k not in gated_keys
    }


async def test_d_get_for_wire_enforces_the_same_tenancy(gate, plan) -> None:
    """The gated reader is not a second access path. It shares ``_fetch_readable``
    with ``get``, so a caller who could not read the pocket still cannot."""
    from pocketpaw_ee.cloud._core.errors import Forbidden

    gate(True)
    created = await pockets_service.create(
        FREE_WS,
        USER,
        CreatePocketRequest(
            name="Private site",
            type="site",
            engine="svelte",
            source=dict(SOURCE_MAP),
            visibility="private",
        ),
    )
    with pytest.raises(Forbidden):
        await pockets_service.get_for_wire(created["_id"], "someone_else")


async def test_d_preview_pocket_still_serves_draft_content(gate, plan) -> None:
    """Acceptance 3, second half. The Preview tab IS the editor. It reads through
    the ungated ``get`` and then prefers the draft ``ArtifactVersion`` snapshot —
    a SECOND copy of the same source, in the versions spine, reached by a
    different code path. Both must survive the gate."""
    from pocketpaw_ee.sites import service as sites_service

    gate(True)
    created = await _make_site_pocket(FREE_WS)

    preview = await sites_service.preview_pocket(
        workspace_id=FREE_WS, user_id=USER, pocket_id=created["_id"]
    )
    assert preview.engine == "svelte"
    assert preview.content == SOURCE_MAP


async def test_d_draft_version_snapshot_survives_a_gated_edit(gate, plan) -> None:
    """The versions spine holds its own copy of source and is written by the edit
    lane. Edit a file on a gated pocket and the preview reflects it — proving the
    draft path is live, not just that the fallback happened to match."""
    from pocketpaw_ee.sites import service as sites_service

    gate(True)
    created = await _make_site_pocket(FREE_WS)

    await pockets_service.set_svelte_source_file(
        created["_id"],
        USER,
        component_path="src/app.css",
        new_source=":root { --ink: #000; }\n",
    )
    preview = await sites_service.preview_pocket(
        workspace_id=FREE_WS, user_id=USER, pocket_id=created["_id"]
    )
    assert preview.content is not None
    assert preview.content["src/app.css"] == ":root { --ink: #000; }\n"


# ---------------------------------------------------------------------------
# (e) ``sourceVisible`` — the effective answer, published.
#
# The client cannot re-derive the gate: it is the workspace capability AND the
# pocket's ``source_gated`` cohort stamp, and only the capability is on any wire.
# Gating a Code tab on the capability alone would hide it on GRANDFATHERED
# pockets whose source we still send. So the resolved answer travels with the
# payload, off the same argument, and the tests below pin that they agree.
# ---------------------------------------------------------------------------


async def test_e_flag_tracks_the_payload_in_both_directions(gate, plan) -> None:
    """The whole contract in one test: gated means ``false`` AND no source;
    ungated means ``true`` AND source present."""
    gate(True)
    gated = await _make_site_pocket(FREE_WS, name="Gated")
    allowed = await _make_site_pocket(PAID_WS, name="Allowed")

    gated_wire = await pockets_service.get_for_wire(gated["_id"], USER)
    assert gated_wire["sourceVisible"] is False
    assert gated_wire["source"] is None

    allowed_wire = await pockets_service.get_for_wire(allowed["_id"], USER)
    assert allowed_wire["sourceVisible"] is True
    assert allowed_wire["source"] == SOURCE_MAP


async def test_e_flag_is_true_for_a_grandfathered_pocket(gate, plan) -> None:
    """The case the flag exists for. A free workspace resolves the CAPABILITY
    False, but this pocket predates the flip and its source is still served — so
    a client gating on ``GET /entitlements`` alone would hide a Code tab over
    content that is right there. The flag says ``true`` and the payload agrees."""
    gate(False)
    created = await _make_site_pocket(FREE_WS, name="Grandfathered")
    gate(True)

    wire = await pockets_service.get_for_wire(created["_id"], USER)
    assert wire["sourceVisible"] is True
    assert wire["source"] == SOURCE_MAP


async def test_e_flag_is_honest_in_the_gallery_list(gate, plan) -> None:
    """``list_pockets`` projects ``source`` out of its Mongo query, so a gated row
    arrives with no source to redact. The flag must still report ``false``, or the
    gallery shows a Code tab that disappears when the pocket is opened — the flag
    and the single-pocket read disagreeing about the same pocket."""
    gate(True)
    created = await _make_site_pocket(FREE_WS, name="Listed site")

    rows = await pockets_service.list_pockets(FREE_WS, USER)
    row = next(r for r in rows if r["_id"] == created["_id"])
    single = await pockets_service.get_for_wire(created["_id"], USER)

    assert row["sourceVisible"] is False
    assert row["sourceVisible"] == single["sourceVisible"]


async def test_e_list_resolves_the_entitlement_once_for_the_page(gate, plan, monkeypatch) -> None:
    """Every row of a gallery belongs to one workspace, so the entitlement is
    resolved once and handed down. Counting the calls is the point: a per-row
    resolve puts two workspace lookups behind every card."""
    import pocketpaw_ee.cloud.entitlements.service as ent_svc

    gate(True)
    for i in range(4):
        await _make_site_pocket(FREE_WS, name=f"Site {i}")

    calls = 0
    real = ent_svc.resolve_entitlements

    async def _counting(workspace_id: str):
        nonlocal calls
        calls += 1
        return await real(workspace_id)

    monkeypatch.setattr(ent_svc, "resolve_entitlements", _counting)

    rows = await pockets_service.list_pockets(FREE_WS, USER)
    assert len(rows) >= 4
    assert all(r["sourceVisible"] is False for r in rows)
    assert calls == 1, f"expected one resolve for the whole page, got {calls}"


async def test_e_share_link_reports_the_flag_false(gate, plan) -> None:
    """D4 strips unconditionally, so the flag says ``false`` on every tier — which
    is the truth for that viewer: they genuinely cannot see this source, whatever
    the owning workspace pays. A share-link client reads the same field as any
    other and needs no special case."""
    gate(False)
    created = await _make_site_pocket(PAID_WS)
    link = await pockets_service.generate_share_link(created["_id"], USER, "view")

    shared = await pockets_service.access_via_share_link(link["token"])
    assert shared["sourceVisible"] is False
    assert shared["source"] is None


async def test_e_broadcast_and_write_responses_carry_the_flag(gate, plan, recording_bus) -> None:
    """The flag rides every path the payload does, so a client updating from a
    socket event or a save response re-reads it rather than going stale."""
    gate(True)
    created = await _make_site_pocket(FREE_WS)

    updated = await pockets_service.update(
        created["_id"], USER, UpdatePocketRequest(name="Renamed")
    )
    assert updated["sourceVisible"] is False

    payload = next(
        e.data["pocket"]
        for e in recording_bus.events
        if isinstance(getattr(e, "data", None), dict)
        and "pocket" in e.data
        and e.data["pocket"]["_id"] == created["_id"]
    )
    assert payload["sourceVisible"] is False


async def test_e_flag_cannot_be_computed_apart_from_the_payload(gate, plan) -> None:
    """Driven on the serializer directly, both ways, so the agreement is pinned
    independently of anything that resolves the boolean. A ``true`` beside a
    withheld payload is the failure this field exists to prevent."""
    gate(True)
    created = await _make_site_pocket(PAID_WS)
    domain = pockets_service._pocket_to_domain(await PocketDoc.get(created["_id"]))

    for visible in (True, False):
        wire = pocket_to_wire_dict(domain, source_visible=visible)
        assert wire["sourceVisible"] is visible
        assert (wire["source"] is not None) is visible


# ---------------------------------------------------------------------------
# (f) The signature itself is the guard.
# ---------------------------------------------------------------------------


async def test_e_source_visible_is_required_and_has_no_default() -> None:
    """``pocket_to_wire_dict`` is pure and synchronous over a frozen domain
    object, so it cannot resolve an entitlement itself — the answer must come
    from the caller. A DEFAULT would decide for every call site that forgot to
    pass one, and the only default that does not break the build pipeline is a
    fail-OPEN one. Requiring it turns a missed call site into a TypeError at
    import instead of a silent leak. Do not add a default to make a call site or
    a test shorter."""
    param = inspect.signature(pocket_to_wire_dict).parameters["source_visible"]

    assert param.default is inspect.Parameter.empty, (
        "source_visible must have NO default — a defaulted gate fails open, "
        "which is the entire bug class SF-2 exists to close"
    )
    assert param.kind is inspect.Parameter.KEYWORD_ONLY

    with pytest.raises(TypeError):
        pocket_to_wire_dict(object())  # type: ignore[call-arg]


async def test_e_serializer_redacts_only_when_told_to(gate, plan) -> None:
    """The chokepoint itself, driven directly on both settings, so the redaction
    is pinned independently of everything that resolves the boolean."""
    gate(True)
    created = await _make_site_pocket(PAID_WS)
    doc = await PocketDoc.get(created["_id"])
    domain = pockets_service._pocket_to_domain(doc)

    assert pocket_to_wire_dict(domain, source_visible=True)["source"] == SOURCE_MAP
    assert pocket_to_wire_dict(domain, source_visible=False)["source"] is None

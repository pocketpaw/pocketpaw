# tests/cloud/pockets/test_template_needs.py
# Created: 2026-06-08 (feat/sense-template-needs, Sense tier chunk 6a) — pins
# the pocket-create path's handling of a template's declared ``needs:`` Senses.
# A vertical template can require provider-agnostic capabilities (e.g.
# paw.payments.v1). At create() the service asks the EE Sense resolver whether
# an enabled connector fills each; ids with no provider are collected and:
#   - surfaced as ``missing_senses`` on the pocket.created event (the seam the
#     prompt-to-connect UX consumes), and
#   - NEVER block creation (mirrors the strict=False template tolerance).
#
# Tests mock both the OSS ``load_template`` (so no real template file is needed)
# and the EE ``resolve`` (so no connector registry / Mongo connector state is
# needed). Uses the shared ``mongo_db`` + autouse RecordingBus fixtures from
# tests/cloud/conftest.py so create()'s insert + emit() succeed and the emitted
# event is inspectable via the ``recording_bus`` fixture.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest
from pocketpaw_ee.cloud.senses.resolver import ResolvedSense

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_needs"
_USER = "user_needs"


def _loaded_with_needs(needs: list[str]) -> dict:
    """A minimal ``load_template`` result shape: {"meta": {...}, ...}.

    ``_check_template_needs`` only reads ``loaded["meta"]["needs"]``, so the
    rest of the meta can be empty for these tests.
    """
    return {"meta": {"needs": needs}, "ripple_spec": None}


# ---------------------------------------------------------------------------
# _check_template_needs — the pure-ish helper (resolve mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_returns_empty_when_all_senses_resolve() -> None:
    """Every declared sense has an enabled provider → no missing ids."""
    loaded = _loaded_with_needs(["paw.email.v1", "paw.payments.v1"])
    resolved = ResolvedSense(sense_id="x", connector_name="gmail")
    with patch(
        "pocketpaw_ee.cloud.senses.resolver.resolve",
        new=AsyncMock(return_value=resolved),
    ):
        missing = await pockets_service._check_template_needs(loaded, _WS)
    assert missing == []


@pytest.mark.asyncio
async def test_check_returns_missing_ids_when_some_resolve_none() -> None:
    """A sense with no enabled provider (resolve → None) is collected."""
    loaded = _loaded_with_needs(["paw.email.v1", "paw.payments.v1"])

    async def _fake_resolve(sense_id: str, workspace_id: str, **_kw):
        # email is wired, payments is not
        if sense_id == "paw.email.v1":
            return ResolvedSense(sense_id=sense_id, connector_name="gmail")
        return None

    with patch("pocketpaw_ee.cloud.senses.resolver.resolve", new=_fake_resolve):
        missing = await pockets_service._check_template_needs(loaded, _WS)
    assert missing == ["paw.payments.v1"]


@pytest.mark.asyncio
async def test_check_guards_none_loaded() -> None:
    """A None loaded result (stale/unknown slug) yields no missing ids."""
    assert await pockets_service._check_template_needs(None, _WS) == []


@pytest.mark.asyncio
async def test_check_guards_no_needs() -> None:
    """A template with no needs yields no missing ids."""
    assert await pockets_service._check_template_needs(_loaded_with_needs([]), _WS) == []
    assert await pockets_service._check_template_needs({"meta": {}}, _WS) == []


@pytest.mark.asyncio
async def test_check_swallows_resolver_error() -> None:
    """A resolver exception never propagates — the offending id is skipped,
    create() must not blow up on a resolver hiccup."""
    loaded = _loaded_with_needs(["paw.email.v1"])
    with patch(
        "pocketpaw_ee.cloud.senses.resolver.resolve",
        new=AsyncMock(side_effect=RuntimeError("registry down")),
    ):
        missing = await pockets_service._check_template_needs(loaded, _WS)
    assert missing == []


# ---------------------------------------------------------------------------
# create() — surfaces missing_senses on the event, NEVER blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_not_blocked_and_emits_missing_senses(recording_bus) -> None:
    """Creating a pocket from a template with an unfilled need still succeeds
    AND attaches the missing sense id to the pocket.created event."""
    loaded = _loaded_with_needs(["paw.payments.v1"])

    with (
        patch(
            "pocketpaw.bundled_templates.load_template",
            return_value=loaded,
        ),
        # No compile output → rippleSpec untouched (we only care about needs).
        patch.object(pockets_service, "_compile_template_to_runtime_dict", return_value=None),
        patch(
            "pocketpaw_ee.cloud.senses.resolver.resolve",
            new=AsyncMock(return_value=None),
        ),
    ):
        wire = await pockets_service.create(
            _WS,
            _USER,
            CreatePocketRequest(name="Invoice chaser", template_slug="invoice-chaser"),
        )

    # Creation was NOT blocked.
    assert wire["name"] == "Invoice chaser"
    assert wire["_id"]

    # The pocket.created event carries the missing sense for the UX.
    created = [e for e in recording_bus.events if e.EVENT_TYPE == "pocket.created"]
    assert len(created) == 1
    assert created[0].data.get("missing_senses") == ["paw.payments.v1"]


@pytest.mark.asyncio
async def test_create_omits_missing_senses_when_all_resolve(recording_bus) -> None:
    """When every need resolves, the event carries no missing_senses key."""
    loaded = _loaded_with_needs(["paw.email.v1"])

    with (
        patch("pocketpaw.bundled_templates.load_template", return_value=loaded),
        patch.object(pockets_service, "_compile_template_to_runtime_dict", return_value=None),
        patch(
            "pocketpaw_ee.cloud.senses.resolver.resolve",
            new=AsyncMock(
                return_value=ResolvedSense(sense_id="paw.email.v1", connector_name="gmail")
            ),
        ),
    ):
        wire = await pockets_service.create(
            _WS,
            _USER,
            CreatePocketRequest(name="Mail thing", template_slug="mail-thing"),
        )

    assert wire["name"] == "Mail thing"
    created = [e for e in recording_bus.events if e.EVENT_TYPE == "pocket.created"]
    assert len(created) == 1
    assert "missing_senses" not in created[0].data


@pytest.mark.asyncio
async def test_create_without_template_has_no_missing_senses(recording_bus) -> None:
    """A plain pocket (no template_slug) never carries missing_senses."""
    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name="plain"))
    assert wire["name"] == "plain"
    created = [e for e in recording_bus.events if e.EVENT_TYPE == "pocket.created"]
    assert len(created) == 1
    assert "missing_senses" not in created[0].data

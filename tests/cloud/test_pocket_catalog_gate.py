# tests/cloud/test_pocket_catalog_gate.py
# Created: 2026-05-22 (Increment 5) — service-level tests for the
# catalog-as-allowlist ingest gate wiring in pockets/service.py: the
# embed-ingest audit-log entry and the strict/logged _gate_catalog
# dispatch. EE-gated (tests/cloud/conftest.py + the explicit
# importorskip below) so the OSS-only CI scope skips this file.
"""Tests for the pockets-service catalog gate + embed-ingest audit log."""

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.pockets import service as pockets_service  # noqa: E402
from pocketpaw_ee.cloud.ripple_validator import CatalogViolationError  # noqa: E402

from pocketpaw.security.audit import get_audit_logger  # noqa: E402

# `asyncio_mode = "auto"` (see pyproject) runs the async tests below
# without an explicit marker; the sync tests run as-is.


def _capture_audit() -> tuple[list[dict], callable]:
    """Register a one-shot capture callback on the singleton audit logger.

    Returns ``(captured_list, deregister)``. The audit logger has no
    public deregister, so the returned cleanup pops the callback off the
    private ``_callbacks`` list.
    """
    captured: list[dict] = []
    logger = get_audit_logger()

    def _cb(event_dict: dict) -> None:
        captured.append(event_dict)

    logger.on_log(_cb)

    def _deregister() -> None:
        try:
            logger._callbacks.remove(_cb)
        except ValueError:
            pass

    return captured, _deregister


# ---------------------------------------------------------------------------
# _audit_embed_ingest — fires only when the spec carries an embed node
# ---------------------------------------------------------------------------


def test_audit_embed_ingest_fires_for_embed_bearing_spec() -> None:
    captured, deregister = _capture_audit()
    try:
        spec = {
            "ui": {
                "type": "flex",
                "children": [
                    {
                        "type": "embed",
                        "props": {"mode": "url", "url": "https://codepen.io/x"},
                    }
                ],
            }
        }
        pockets_service._audit_embed_ingest(spec, actor="u1", workspace_id="w1", pocket_id="p1")
    finally:
        deregister()

    embed_events = [e for e in captured if e.get("action") == "pocket.embed_ingest"]
    assert len(embed_events) == 1
    ctx = embed_events[0].get("context", {})
    assert ctx.get("embed_count") == 1
    assert ctx.get("embed_urls") == ["https://codepen.io/x"]
    assert ctx.get("pocket_id") == "p1"


def test_audit_embed_ingest_silent_for_embed_free_spec() -> None:
    captured, deregister = _capture_audit()
    try:
        spec = {"ui": {"type": "flex", "children": [{"type": "stat", "props": {}}]}}
        pockets_service._audit_embed_ingest(spec, actor="u1", workspace_id="w1", pocket_id="p1")
    finally:
        deregister()

    assert [e for e in captured if e.get("action") == "pocket.embed_ingest"] == []


# ---------------------------------------------------------------------------
# _gate_catalog — strict raises, logged does not; embed always audited
# ---------------------------------------------------------------------------


async def test_gate_catalog_strict_raises_on_unknown_type(monkeypatch) -> None:
    # Stub the manifest fetch so the gate has a deterministic allow-list
    # without a network round-trip.
    async def _fake_allowed() -> list[str]:
        return ["flex", "stat"]

    monkeypatch.setattr(pockets_service, "_catalog_allowed_types", _fake_allowed)

    spec = {"ui": {"type": "flex", "children": [{"type": "ghost-widget", "props": {}}]}}
    with pytest.raises(CatalogViolationError):
        await pockets_service._gate_catalog(spec, strict=True, actor="u1", workspace_id="w1")


async def test_gate_catalog_logged_does_not_raise(monkeypatch) -> None:
    async def _fake_allowed() -> list[str]:
        return ["flex", "stat"]

    monkeypatch.setattr(pockets_service, "_catalog_allowed_types", _fake_allowed)

    spec = {"ui": {"type": "flex", "children": [{"type": "ghost-widget", "props": {}}]}}
    # Must not raise — logged mode records the violation only.
    await pockets_service._gate_catalog(spec, strict=False, actor="u1", workspace_id="w1")


async def test_gate_catalog_skipped_when_manifest_unavailable(monkeypatch) -> None:
    """When the widget manifest can't be fetched the gate is a no-op —
    a violating spec must NOT raise."""

    async def _no_manifest() -> None:
        return None

    monkeypatch.setattr(pockets_service, "_catalog_allowed_types", _no_manifest)

    spec = {"ui": {"type": "ghost-widget", "props": {}}}
    # Best-effort: no manifest → no gate → no raise.
    await pockets_service._gate_catalog(spec, strict=True, actor="u1", workspace_id="w1")


async def test_gate_catalog_audits_embed_even_when_manifest_unavailable(
    monkeypatch,
) -> None:
    async def _no_manifest() -> None:
        return None

    monkeypatch.setattr(pockets_service, "_catalog_allowed_types", _no_manifest)

    captured, deregister = _capture_audit()
    try:
        spec = {
            "ui": {
                "type": "embed",
                "props": {"mode": "url", "url": "https://www.figma.com/file/x"},
            }
        }
        await pockets_service._gate_catalog(
            spec, strict=True, actor="u1", workspace_id="w1", pocket_id="p9"
        )
    finally:
        deregister()

    # The embed audit fires regardless of whether the manifest was reachable.
    assert any(e.get("action") == "pocket.embed_ingest" for e in captured)

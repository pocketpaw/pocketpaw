# tests/cloud/test_template_resolution.py
# Created: 2026-05-28 (feat/wave-3e-template-slug) — pins RFC 03 v2
# template-slug wiring + compile-on-install + the new
# ``resolve_pocket_template`` service helper.
#
# What this pins:
#
#   * ``Pocket.template_slug`` is an optional Beanie field — legacy
#     pockets (no slug) read back as ``None``.
#   * ``service.create`` with a ``template_slug`` loads + compiles the
#     bundled template and MERGES the compile output into rippleSpec
#     (option B — user-customized keys survive). Without a slug, the
#     behaviour is unchanged from before Wave 3e.
#   * ``service.update`` with a ``template_slug`` triggers a
#     recompile + merge against the current rippleSpec.
#   * ``resolve_pocket_template`` returns a typed ``PocketTemplate``
#     for a pocket with a known slug, ``None`` for an unknown slug,
#     ``None`` for a slug-less pocket, and ``None`` for a pocket in a
#     different workspace (tenant isolation).
#   * ``dispatch_bulk_action`` (library entry point) succeeds
#     end-to-end when given a resolved template — closing the Wave 3b
#     gap.
#   * ``temporal_scheduler.run_one_pass`` no longer skips every pocket
#     — pockets that DO carry a resolvable template feed
#     ``temporal_dispatcher.sweep_pocket`` — closing the Wave 3d gap.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.models.pocket_backend import (
    AllowedWrite as _AllowedWriteDoc,
)
from pocketpaw_ee.cloud.models.pocket_backend import (
    PocketBackendCredential as _BackendCredentialDoc,
)
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import (
    CreatePocketRequest,
    UpdatePocketRequest,
)

from pocketpaw.bundled_templates import (
    PocketTemplate,
    install_bundled_templates,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    """Install the bundled templates into a tmp dir so the loader can read
    them without depending on the host's ``~/.pocketpaw/templates``.

    Returns the install root the resolver / loader call points at."""
    install_bundled_templates(destination_root=tmp_path)
    return tmp_path


@pytest.fixture
def patched_loader(monkeypatch, templates_dir: Path):
    """Patch ``load_template`` everywhere the service uses it to point at
    the tmp templates dir. The service imports ``load_template`` lazily
    inside ``create`` / ``update``; the lazy ``from pocketpaw.bundled_templates
    import load_template`` resolves against the package's module-level
    attribute, so patching the package symbol catches every lazy import."""
    from pocketpaw.bundled_templates import loader as loader_mod

    real = loader_mod.load_template
    install_root = templates_dir

    def _patched(slug: str, *, templates_dir: Path | None = None, strict: bool = False):
        return real(slug, templates_dir=templates_dir or install_root, strict=strict)

    monkeypatch.setattr(loader_mod, "load_template", _patched)
    import pocketpaw.bundled_templates as bt_pkg

    monkeypatch.setattr(bt_pkg, "load_template", _patched)
    return install_root


# ---------------------------------------------------------------------------
# Beanie field — pocket reads back template_slug
# ---------------------------------------------------------------------------


async def test_pocket_template_slug_field_persists() -> None:
    """A new Pocket Beanie doc accepts ``template_slug`` and reads it back."""
    doc = _PocketDoc(
        workspace="w1",
        name="t",
        owner="u1",
        template_slug="todo-task-tracker",
    )
    await doc.insert()

    fetched = await _PocketDoc.get(doc.id)
    assert fetched is not None
    assert fetched.template_slug == "todo-task-tracker"


async def test_pocket_template_slug_defaults_to_none() -> None:
    """A pocket created without a slug reads back ``None`` — Mongo
    doesn't need a migration for adding an optional field."""
    doc = _PocketDoc(workspace="w1", name="t", owner="u1")
    await doc.insert()

    fetched = await _PocketDoc.get(doc.id)
    assert fetched is not None
    assert fetched.template_slug is None


# ---------------------------------------------------------------------------
# service.create — compile-on-install + merge
# ---------------------------------------------------------------------------


async def test_create_with_template_slug_compiles_and_merges(patched_loader) -> None:
    """``create`` with a ``template_slug`` populates ``rippleSpec`` from
    the OSS compile pass and persists the slug on the pocket."""
    body = CreatePocketRequest(name="My Todo", template_slug="todo-task-tracker")
    pocket = await pockets_service.create("w1", "u1", body)

    assert pocket["templateSlug"] == "todo-task-tracker"
    spec = pocket["rippleSpec"]
    assert isinstance(spec, dict)
    # The compile pass surfaces template fields (``state``, ``name`` of
    # the template, etc.) onto the spec.
    assert spec.get("name") == "todo-task-tracker"


async def test_create_without_template_slug_skips_compile() -> None:
    """A pocket created without a slug behaves identically to pre-Wave-3e:
    no compile, no rippleSpec injected by the service."""
    body = CreatePocketRequest(name="cold pocket")
    pocket = await pockets_service.create("w1", "u1", body)
    assert pocket["templateSlug"] is None
    assert pocket["rippleSpec"] is None


async def test_create_with_unknown_slug_keeps_slug_and_leaves_spec_unchanged(monkeypatch) -> None:
    """An unknown slug doesn't break create. The pocket is persisted
    with the slug intact so a later resolver can retry; the rippleSpec
    is left at whatever the caller passed (None in this test)."""
    # No patched_loader fixture here — ``load_template`` runs against the
    # real default dir which doesn't carry ``does-not-exist``.
    body = CreatePocketRequest(name="bad slug", template_slug="does-not-exist")
    pocket = await pockets_service.create("w1", "u1", body)
    assert pocket["templateSlug"] == "does-not-exist"
    assert pocket["rippleSpec"] is None


async def test_create_with_template_preserves_user_supplied_ui(patched_loader) -> None:
    """Option B merge: a caller-supplied rippleSpec key (e.g. a ``ui``
    tree) that the compile pass doesn't produce survives the merge."""
    user_ui = {"id": "n_root", "type": "stack", "children": []}
    body = CreatePocketRequest(
        name="custom canvas",
        template_slug="todo-task-tracker",
        ripple_spec={"ui": user_ui},
    )
    pocket = await pockets_service.create("w1", "u1", body)
    spec = pocket["rippleSpec"]
    assert spec.get("ui", {}).get("type") == "stack"
    # The compile pass still landed its keys onto the spec.
    assert "state" in spec


# ---------------------------------------------------------------------------
# service.update — recompile-on-set
# ---------------------------------------------------------------------------


async def test_update_with_template_slug_recompiles(patched_loader) -> None:
    """``update`` with a new slug triggers a recompile + merge."""
    create_body = CreatePocketRequest(name="initial")
    pocket = await pockets_service.create("w1", "u1", create_body)
    pocket_id = pocket["_id"]

    update_body = UpdatePocketRequest(template_slug="kanban-board")
    updated = await pockets_service.update(pocket_id, "u1", update_body)
    assert updated["templateSlug"] == "kanban-board"
    spec = updated["rippleSpec"]
    assert isinstance(spec, dict)
    assert spec.get("name") == "kanban-board"


async def test_update_without_template_slug_leaves_slug_alone(patched_loader) -> None:
    """An update that doesn't touch ``template_slug`` keeps the slug
    and the rippleSpec from the prior create."""
    create_body = CreatePocketRequest(name="hold", template_slug="todo-task-tracker")
    pocket = await pockets_service.create("w1", "u1", create_body)
    pocket_id = pocket["_id"]

    update_body = UpdatePocketRequest(description="new description")
    updated = await pockets_service.update(pocket_id, "u1", update_body)
    assert updated["templateSlug"] == "todo-task-tracker"
    assert updated["description"] == "new description"


# ---------------------------------------------------------------------------
# resolve_pocket_template
# ---------------------------------------------------------------------------


async def test_resolve_returns_template_for_known_slug(templates_dir: Path) -> None:
    """A pocket with a known slug resolves to a typed ``PocketTemplate``."""
    doc = _PocketDoc(workspace="w1", name="t", owner="u1", template_slug="todo-task-tracker")
    await doc.insert()

    template = await pockets_service.resolve_pocket_template(
        "w1", str(doc.id), templates_dir=templates_dir
    )
    assert isinstance(template, PocketTemplate)
    assert template.name == "todo-task-tracker"


async def test_resolve_returns_none_for_pocket_without_slug(templates_dir: Path) -> None:
    """A pocket with no ``template_slug`` returns ``None`` — not an error."""
    doc = _PocketDoc(workspace="w1", name="t", owner="u1")
    await doc.insert()
    template = await pockets_service.resolve_pocket_template(
        "w1", str(doc.id), templates_dir=templates_dir
    )
    assert template is None


async def test_resolve_returns_none_for_unknown_slug(templates_dir: Path) -> None:
    """A slug the on-disk install doesn't carry returns ``None`` — the
    loader's strict=False mode handles it gracefully."""
    doc = _PocketDoc(workspace="w1", name="t", owner="u1", template_slug="does-not-exist")
    await doc.insert()
    template = await pockets_service.resolve_pocket_template(
        "w1", str(doc.id), templates_dir=templates_dir
    )
    assert template is None


async def test_resolve_returns_none_for_unknown_pocket(templates_dir: Path) -> None:
    """A pocket_id that doesn't exist returns ``None`` (not an error)."""
    from beanie import PydanticObjectId

    fake_id = str(PydanticObjectId())
    template = await pockets_service.resolve_pocket_template(
        "w1", fake_id, templates_dir=templates_dir
    )
    assert template is None


async def test_resolve_tenant_isolation(templates_dir: Path) -> None:
    """A pocket in workspace A is invisible to a resolver call from
    workspace B — Rule 7 (tenant filter on every read)."""
    doc = _PocketDoc(workspace="w1", name="t", owner="u1", template_slug="todo-task-tracker")
    await doc.insert()
    template = await pockets_service.resolve_pocket_template(
        "w-other", str(doc.id), templates_dir=templates_dir
    )
    assert template is None


# ---------------------------------------------------------------------------
# Wave 3b closure — dispatch_bulk_action via the resolver works end-to-end
# ---------------------------------------------------------------------------


def _bulk_template() -> PocketTemplate:
    """Minimal v2 template carrying ONE bulk action."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "wave-3e-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "test",
            "description": "Wave 3e end-to-end fixture",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "columns": [{"field": "value", "widget": "number"}],
                "id_field": "id",
            },
            "actions": [
                {
                    "name": "mark_done",
                    "label": "Done",
                    "kind": "bulk",
                    "instinct_policy": "auto",
                }
            ],
        }
    )


async def _make_pocket_for_dispatch(
    *,
    workspace: str = "w1",
    owner: str = "u1",
) -> str:
    """Insert a pocket with a rippleSpec carrying the ``mark_done`` write
    binding plus an allowlisted backend so ``dispatch_bulk_action``
    can fan out without 500ing on missing creds."""
    doc = _PocketDoc(
        workspace=workspace,
        name="bulk-pocket",
        owner=owner,
        rippleSpec={
            "actions": {
                "mark_done": {
                    "kind": "write_binding",
                    "method": "POST",
                    "path": "/items",
                }
            }
        },
        visibility="workspace",
    )
    await doc.insert()
    await _BackendCredentialDoc(
        pocket_id=str(doc.id),
        workspace_id=workspace,
        base_url="https://example.test",
        auth_type="none",
        auth_header=None,
        encrypted_token=None,
        nonce=None,
        salt=None,
        allowed_writes=[
            _AllowedWriteDoc.model_validate({"method": "POST", "path_pattern": "/items*"})
        ],
    ).insert()
    return str(doc.id)


async def test_dispatch_bulk_action_runs_with_resolved_template(monkeypatch) -> None:
    """The Wave 3b gap closure: ``dispatch_bulk_action`` succeeds for a
    pocket+template pair. The route's resolver-fed call no longer
    returns ``bulk_action.template_resolver_pending``."""
    pocket_id = await _make_pocket_for_dispatch()

    calls: list[dict] = []

    async def _stub_run_action(**kwargs: Any) -> dict:
        calls.append(kwargs)
        return {
            "ok": True,
            "action": kwargs["action"],
            "status": 200,
            "response": {"ok": True},
            "on_success": [],
            "on_error": [],
        }

    from pocketpaw_ee.cloud.pockets import action_executor

    monkeypatch.setattr(action_executor, "run_action", _stub_run_action)

    body = {
        "pocket_id": pocket_id,
        "action_name": "mark_done",
        "rows": [{"id": "r1", "value": 1}, {"id": "r2", "value": 2}],
    }
    result = await pockets_service.dispatch_bulk_action(
        "w1",
        "u1",
        body,
        template=_bulk_template(),
        now=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
    )
    assert result["total_rows"] == 2
    assert len(result["executions"]) == 2
    assert result["batch_approval_id"] is None
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Wave 3d closure — temporal scheduler no longer skips every pocket
# ---------------------------------------------------------------------------


async def test_temporal_scheduler_uses_resolver(monkeypatch, templates_dir: Path) -> None:
    """The scheduler's ``_resolve_pocket_template_and_rows`` now hits
    ``resolve_pocket_template`` instead of returning ``(None, [])``
    unconditionally. A pocket with a known slug receives a non-None
    template at the scheduler seam."""
    pocket_id = await _make_pocket_for_dispatch()
    # Stamp the slug onto the pocket so the resolver finds it.
    doc = await _PocketDoc.get(pocket_id)
    assert doc is not None
    doc.template_slug = "todo-task-tracker"
    # Ensure ``rippleSpec.sources`` exists so the scheduler's scan picks
    # the pocket up (the scan filters on a non-empty sources dict in v0).
    spec = dict(doc.rippleSpec or {})
    spec["sources"] = {
        "items": {
            "method": "GET",
            "path": "/items",
            "bind": "state.items",
            "refresh": ["interval"],
            "refresh_interval_seconds": 3600,
        }
    }
    doc.rippleSpec = spec
    await doc.save()

    # The scheduler calls ``pockets_service.resolve_pocket_template``
    # which itself calls ``load_template`` — no ``templates_dir`` kwarg
    # threads through that path, so we patch the loader to default to
    # the tmp install root.
    from pocketpaw.bundled_templates import loader as loader_mod

    real_load = loader_mod.load_template
    install_root = templates_dir

    def _redirect(slug: str, *, templates_dir: Path | None = None, strict: bool = False):
        return real_load(slug, templates_dir=templates_dir or install_root, strict=strict)

    monkeypatch.setattr(loader_mod, "load_template", _redirect)
    import pocketpaw.bundled_templates as bt_pkg

    monkeypatch.setattr(bt_pkg, "load_template", _redirect)

    from pocketpaw_ee.cloud._core import temporal_scheduler

    resolved_calls: list[tuple[str, str, object]] = []

    async def _spy_sweep(workspace_id: str, pocket_id: str, *, template, rows):
        from pocketpaw_ee.cloud.temporal_sweeps.domain import SweepDispatchResult

        resolved_calls.append((workspace_id, pocket_id, template))
        return SweepDispatchResult(
            pocket_id=pocket_id,
            edges_fired=0,
            blocked=0,
            escalated=0,
            errors=0,
            sweep_duration_ms=0,
        )

    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    monkeypatch.setattr(temporal_dispatcher, "sweep_pocket", _spy_sweep)

    visited = await temporal_scheduler.run_one_pass()
    assert visited == 1
    assert len(resolved_calls) == 1
    _ws, _pid, template = resolved_calls[0]
    assert isinstance(template, PocketTemplate)
    assert template.name == "todo-task-tracker"

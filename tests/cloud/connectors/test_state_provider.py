# test_state_provider.py — connector-store-unification CS-3 — cloud state store.
# Created: 2026-06-12 — Locks the cloud restart-survival contract: a fresh
#   process (fresh ConnectorRegistry) with only a seeded WorkspaceConnector doc
#   executes through registry.ensure_connected with NO prior /connect call,
#   for both ws:<workspace_id> and pocket:<pocket_id> scope keys. Also pins
#   the entry-point wiring (the registry's DEFAULT store is the cloud store
#   when pocketpaw-ee is installed), the store's namespacing/ownership rules
#   (set mirrors config onto existing rows only; delete on namespaced keys is
#   a no-op; non-namespaced keys delegate to the file store), and the
#   stale-adapter drops on update_config / disable_connector.
# Updated: 2026-06-12 (PR #1449 review fix) — added the credential-provenance
#   test: a disabled row's credentials must never reach the legacy fallback's
#   connect() (the fallback doc read now filters enabled == True), so disable
#   revokes on the HTTP execute path too, not just the durable seam.
# Updated: 2026-06-28 (AW-2 connector multi-host egress allow-list) — added
#   TestCloudStoreAllowedHosts: _get_cloud folds the row's ``allowed_hosts``
#   field into the returned config dict (so the per-workspace egress additions
#   reach the adapter's connect() via the config channel), and _set_cloud
#   strips the key back out before persisting (the dedicated field stays the
#   single source of truth; the round-trip never duplicates it into config).

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.dto import (
    ExecuteActionRequest,
    UpdateConnectorConfigRequest,
)
from pocketpaw_ee.cloud.connectors.state_provider import CloudConnectorStateStore
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

from pocketpaw.connectors.protocol import ActionResult
from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.state_store import FileConnectorStateStore


async def _seed_doc(
    *,
    workspace: str = "ws-1",
    name: str = "github",
    scope: str = "workspace",
    pocket_id: str | None = None,
    enabled: bool = True,
    config: dict | None = None,
    allowed_hosts: list[str] | None = None,
) -> WorkspaceConnector:
    doc = WorkspaceConnector(
        workspace=workspace,
        name=name,
        enabled=enabled,
        scope=scope,
        pocket_id=pocket_id,
        config=config if config is not None else {"GITHUB_TOKEN": "ghp_test"},
        allowed_hosts=allowed_hosts if allowed_hosts is not None else [],
    )
    await doc.insert()
    return doc


@pytest.fixture
def cloud_store(tmp_path) -> CloudConnectorStateStore:
    """Cloud store with a hermetic file fallback (never the real ~/.pocketpaw)."""
    return CloudConnectorStateStore(
        file_fallback=FileConnectorStateStore(base_dir=tmp_path / "file-state")
    )


@pytest.fixture
def fresh_registry(cloud_store, monkeypatch) -> ConnectorRegistry:
    """A registry simulating a FRESH PROCESS, installed as the service's
    singleton: no live adapters, durable state only in the cloud store."""
    reg = ConnectorRegistry(Path("connectors"), state_store=cloud_store)
    monkeypatch.setattr(connectors_service, "_registry", reg)
    return reg


# ---------------------------------------------------------------------------
# The CS-3 behavior contract: seeded doc → fresh process → execute works.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_execute_rehydrates_workspace_row_without_connect(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,
):
    """A fresh process with only a seeded workspace-scope doc executes
    successfully — registry.ensure_connected rehydrates from the doc's config,
    no /connect call ever happens in this process."""
    await _seed_doc(scope="workspace")
    assert fresh_registry.get_adapter("ws:ws-1", "github") is None

    with patch(
        "pocketpaw.connectors.yaml_engine.DirectRESTAdapter.execute",
        return_value=ActionResult(success=True, data=[{"number": 7}], records_affected=1),
    ):
        resp = await connectors_service.execute(
            "ws-1",
            "github",
            ExecuteActionRequest(action="list_issues", params={"owner": "a", "repo": "b"}),
            user_id="u-1",
        )

    assert resp.success is True
    assert resp.execution_mode == "cloud"
    # Proof the DURABLE path ran (not the legacy one-shot fallback): the
    # registry now holds the rehydrated adapter under the ws: scope key.
    assert fresh_registry.get_adapter("ws:ws-1", "github") is not None


@pytest.mark.asyncio
async def test_cloud_execute_rehydrates_pocket_row_without_connect(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,
):
    """Same restart contract for a pocket-bound row, keyed pocket:<pocket_id>."""
    await _seed_doc(scope="pocket", pocket_id="pk-1")

    with patch(
        "pocketpaw.connectors.yaml_engine.DirectRESTAdapter.execute",
        return_value=ActionResult(success=True, data=[], records_affected=0),
    ):
        resp = await connectors_service.execute(
            "ws-1",
            "github",
            ExecuteActionRequest(
                action="list_issues",
                params={"owner": "a", "repo": "b"},
                scope="pocket",
                pocket_id="pk-1",
            ),
            user_id="u-1",
        )

    assert resp.success is True
    assert fresh_registry.get_adapter("pocket:pk-1", "github") is not None


@pytest.mark.asyncio
async def test_pocket_key_is_tenant_gated(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,
):
    """A pocket_id bound in ANOTHER workspace must not select that tenant's
    config — the bind check fails and the caller falls back to its own ws row
    (which doesn't exist here), so no pocket-keyed adapter appears."""
    await _seed_doc(workspace="ws-OTHER", scope="pocket", pocket_id="pk-foreign")

    with patch(
        "pocketpaw.connectors.yaml_engine.DirectRESTAdapter.execute",
        return_value=ActionResult(success=True, data=[], records_affected=0),
    ):
        await connectors_service.execute(
            "ws-1",
            "github",
            ExecuteActionRequest(
                action="list_issues",
                params={"owner": "a", "repo": "b"},
                scope="pocket",
                pocket_id="pk-foreign",
            ),
            user_id="u-1",
        )

    assert fresh_registry.get_adapter("pocket:pk-foreign", "github") is None


# ---------------------------------------------------------------------------
# Entry-point wiring — the registry's DEFAULT store is the cloud store.
# ---------------------------------------------------------------------------


def test_registry_default_store_comes_from_entry_point():
    """With pocketpaw-ee installed, a registry built with no explicit store
    picks up CloudConnectorStateStore via pocketpaw.connector_state_stores."""
    from pocketpaw._registry import clear_cache, first

    clear_cache()
    provider = first("pocketpaw.connector_state_stores")
    assert provider is not None

    reg = ConnectorRegistry(Path("connectors"))
    assert isinstance(reg._state_store, CloudConnectorStateStore)


# ---------------------------------------------------------------------------
# Store unit contracts — namespacing, ownership, file delegation.
# ---------------------------------------------------------------------------


class TestCloudStoreGet:
    @pytest.mark.asyncio
    async def test_ws_key_returns_enabled_row_config(self, mongo_db, cloud_store):  # noqa: ARG002
        await _seed_doc(config={"GITHUB_TOKEN": "x"})
        assert await cloud_store.get("github", "ws:ws-1") == {"GITHUB_TOKEN": "x"}

    @pytest.mark.asyncio
    async def test_ws_key_is_workspace_scoped(self, mongo_db, cloud_store):  # noqa: ARG002
        await _seed_doc(workspace="ws-OTHER")
        assert await cloud_store.get("github", "ws:ws-1") is None

    @pytest.mark.asyncio
    async def test_disabled_row_reads_as_absent(self, mongo_db, cloud_store):  # noqa: ARG002
        await _seed_doc(enabled=False)
        assert await cloud_store.get("github", "ws:ws-1") is None

    @pytest.mark.asyncio
    async def test_pocket_key_requires_pocket_scope_binding(
        self,
        mongo_db,  # noqa: ARG002
        cloud_store,
    ):
        await _seed_doc(scope="pocket", pocket_id="pk-1", config={"GITHUB_TOKEN": "x"})
        assert await cloud_store.get("github", "pocket:pk-1") == {"GITHUB_TOKEN": "x"}
        assert await cloud_store.get("github", "pocket:pk-2") is None

    @pytest.mark.asyncio
    async def test_empty_config_is_still_a_binding(self, mongo_db, cloud_store):  # noqa: ARG002
        """CLI / no-cred connectors persist config={} — that must read as a
        valid binding ({}), never coerced to None."""
        await _seed_doc(config={})
        assert await cloud_store.get("github", "ws:ws-1") == {}

    def test_plain_key_delegates_to_file_store(self, cloud_store):
        cloud_store.set("testsvc", "default", {"k": "v"})
        assert cloud_store.get("testsvc", "default") == {"k": "v"}
        cloud_store.delete("testsvc", "default")
        assert cloud_store.get("testsvc", "default") is None


class TestCloudStoreOwnership:
    @pytest.mark.asyncio
    async def test_set_mirrors_config_onto_existing_row(
        self,
        mongo_db,  # noqa: ARG002
        cloud_store,
    ):
        doc = await _seed_doc(config={"GITHUB_TOKEN": "old"})
        await cloud_store.set("github", "ws:ws-1", {"GITHUB_TOKEN": "new"})
        refreshed = await WorkspaceConnector.get(doc.id)
        assert refreshed.config == {"GITHUB_TOKEN": "new"}

    @pytest.mark.asyncio
    async def test_set_never_creates_a_row(self, mongo_db, cloud_store):  # noqa: ARG002
        await cloud_store.set("github", "ws:ws-1", {"GITHUB_TOKEN": "x"})
        assert await WorkspaceConnector.find_one(WorkspaceConnector.name == "github") is None

    @pytest.mark.asyncio
    async def test_delete_on_namespaced_key_is_noop(self, mongo_db, cloud_store):  # noqa: ARG002
        """The WorkspaceConnector lifecycle belongs to enable/disable_connector
        — a registry-level delete must not destroy the durable row."""
        await _seed_doc()
        cloud_store.delete("github", "ws:ws-1")
        assert await cloud_store.get("github", "ws:ws-1") is not None

    def test_list_returns_file_rows_only(self, cloud_store):
        cloud_store.set("testsvc", "default", {"k": "v"})
        assert cloud_store.list() == [("testsvc", "default")]


class TestCloudStoreAllowedHosts:
    """AW-2 — the per-workspace egress allow-list field rides the config channel
    on read and is stripped back out on write."""

    @pytest.mark.asyncio
    async def test_get_folds_allowed_hosts_into_config(
        self,
        mongo_db,  # noqa: ARG002 — wires Beanie
        cloud_store,
    ):
        await _seed_doc(
            config={"GITHUB_TOKEN": "x"},
            allowed_hosts=["mirror.example.com", "cdn.example.com"],
        )
        got = await cloud_store.get("github", "ws:ws-1")
        # The dedicated field is folded into config so it reaches connect().
        assert got == {
            "GITHUB_TOKEN": "x",
            "allowed_hosts": ["mirror.example.com", "cdn.example.com"],
        }

    @pytest.mark.asyncio
    async def test_get_omits_key_when_no_allowed_hosts(
        self,
        mongo_db,  # noqa: ARG002
        cloud_store,
    ):
        # Empty allowed_hosts → no key added (config stays byte-identical).
        await _seed_doc(config={"GITHUB_TOKEN": "x"}, allowed_hosts=[])
        assert await cloud_store.get("github", "ws:ws-1") == {"GITHUB_TOKEN": "x"}

    @pytest.mark.asyncio
    async def test_set_strips_allowed_hosts_from_persisted_config(
        self,
        mongo_db,  # noqa: ARG002
        cloud_store,
    ):
        # A round-trip (get folds in, set writes back) must NOT duplicate the
        # key into the stored config blob — the field stays the source of truth.
        doc = await _seed_doc(config={"GITHUB_TOKEN": "x"}, allowed_hosts=["mirror.example.com"])
        folded = await cloud_store.get("github", "ws:ws-1")
        assert "allowed_hosts" in folded  # present on read
        await cloud_store.set("github", "ws:ws-1", folded)
        refreshed = await WorkspaceConnector.get(doc.id)
        # Stored config has the key stripped; the dedicated field is untouched.
        assert refreshed.config == {"GITHUB_TOKEN": "x"}
        assert refreshed.allowed_hosts == ["mirror.example.com"]

    @pytest.mark.asyncio
    async def test_get_degrades_softly_when_db_unavailable(self, cloud_store, monkeypatch):
        """A failing Beanie read (uninitialized DB, partial install) must
        degrade to 'no persisted config', not crash connector support."""

        def _boom(*_args, **_kwargs):
            raise RuntimeError("beanie not initialized")

        monkeypatch.setattr(WorkspaceConnector, "find_one", _boom)
        assert await cloud_store.get("github", "ws:ws-1") is None


# ---------------------------------------------------------------------------
# Stale-adapter drops — config writes / disable invalidate live connections.
# ---------------------------------------------------------------------------


async def _connect_once(fresh_registry: ConnectorRegistry) -> None:
    """Prime a live adapter under the ws: key via the durable path."""
    with patch(
        "pocketpaw.connectors.yaml_engine.DirectRESTAdapter.execute",
        return_value=ActionResult(success=True, data=[], records_affected=0),
    ):
        await connectors_service.execute(
            "ws-1",
            "github",
            ExecuteActionRequest(action="list_issues", params={"owner": "a", "repo": "b"}),
            user_id="u-1",
        )
    assert fresh_registry.get_adapter("ws:ws-1", "github") is not None


@pytest.mark.asyncio
async def test_update_config_drops_live_adapter(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,
):
    await _seed_doc()
    await _connect_once(fresh_registry)

    await connectors_service.update_config(
        "ws-1",
        "github",
        UpdateConnectorConfigRequest(config={"GITHUB_TOKEN": "rotated"}),
    )

    # The stale adapter is gone; the next execute rehydrates with new config.
    assert fresh_registry.get_adapter("ws:ws-1", "github") is None


@pytest.mark.asyncio
async def test_disable_drops_live_adapter(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,
):
    await _seed_doc()
    await _connect_once(fresh_registry)

    await connectors_service.disable_connector("ws-1", "github")

    assert fresh_registry.get_adapter("ws:ws-1", "github") is None
    # And the durable read now reports no binding (enabled filter).
    assert await fresh_registry._state_store.get("github", "ws:ws-1") is None


@pytest.mark.asyncio
async def test_disabled_row_credentials_never_reach_fallback_connect(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,
):
    """Disable must revoke on EVERY execute path (PR #1449 review fix).

    The durable seam already refuses disabled rows (enabled filter in the
    cloud store), but the legacy one-shot fallback used to re-read the doc
    WITHOUT an enabled filter — a disabled row's credentials still connected
    and executed via the HTTP router. Provenance check: seed a DISABLED row
    with credentials, spy on every config that reaches connect(), and assert
    the credentials never appear — the fallback gets {} and real adapters
    fail to connect.
    """
    from pocketpaw.connectors.yaml_engine import DirectRESTAdapter

    await _seed_doc(enabled=False, config={"GITHUB_TOKEN": "ghp_revoked"})

    connect_configs: list[dict] = []
    original_connect = DirectRESTAdapter.connect

    async def _spy_connect(self, pocket_id, config):
        connect_configs.append(dict(config))
        return await original_connect(self, pocket_id, config)

    with (
        patch.object(DirectRESTAdapter, "connect", _spy_connect),
        patch.object(
            DirectRESTAdapter,
            "execute",
            return_value=ActionResult(success=True, data=[], records_affected=0),
        ),
    ):
        await connectors_service.execute(
            "ws-1",
            "github",
            ExecuteActionRequest(action="list_issues", params={"owner": "a", "repo": "b"}),
            user_id="u-1",
        )

    # The disabled row's credentials never reached any connect() call: the
    # durable seam refused the row, and the fallback's enabled-filtered doc
    # read fell through to {} config.
    assert connect_configs == [{}]
    assert all("GITHUB_TOKEN" not in c for c in connect_configs)
    # Nothing was cached under the durable key either.
    assert fresh_registry.get_adapter("ws:ws-1", "github") is None

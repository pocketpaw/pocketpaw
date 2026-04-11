# Tests for the Firebase connector — YAML parsing, adapter connect/execute, error handling.
# Created: 2026-04-01

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.connectors.firebase_adapter import FirebaseAdapter
from pocketpaw.connectors.protocol import ConnectorStatus, TrustLevel
from pocketpaw.connectors.yaml_engine import parse_connector_yaml

# ---------------------------------------------------------------------------
# YAML Parsing
# ---------------------------------------------------------------------------

CONNECTORS_DIR = Path(__file__).resolve().parent.parent / "connectors"


class TestFirebaseYAML:
    """Test that the firebase.yaml connector definition parses correctly."""

    def test_yaml_parses(self):
        defn = parse_connector_yaml(CONNECTORS_DIR / "firebase.yaml")
        assert defn.name == "firebase"
        assert defn.display_name == "Firebase"
        assert defn.type == "cloud"
        assert defn.icon == "flame"

    def test_yaml_auth_method_is_none(self):
        defn = parse_connector_yaml(CONNECTORS_DIR / "firebase.yaml")
        assert defn.auth["method"] == "none"

    def test_yaml_has_firebase_project_credential(self):
        defn = parse_connector_yaml(CONNECTORS_DIR / "firebase.yaml")
        creds = defn.auth["credentials"]
        assert len(creds) == 1
        assert creds[0]["name"] == "FIREBASE_PROJECT"
        assert creds[0]["required"] is False

    def test_yaml_action_count(self):
        defn = parse_connector_yaml(CONNECTORS_DIR / "firebase.yaml")
        # We defined 16 actions in the YAML
        assert len(defn.actions) == 16

    def test_yaml_all_actions_are_local(self):
        defn = parse_connector_yaml(CONNECTORS_DIR / "firebase.yaml")
        for action in defn.actions:
            assert action["method"] == "LOCAL", f"{action['name']} should be LOCAL"

    def test_yaml_destructive_actions_have_trust_levels(self):
        defn = parse_connector_yaml(CONNECTORS_DIR / "firebase.yaml")
        action_map = {a["name"]: a for a in defn.actions}

        # Confirm-level actions
        assert action_map["firestore_delete"]["trust_level"] == "confirm"
        assert action_map["firestore_export"]["trust_level"] == "confirm"

        # Restricted-level actions
        assert action_map["hosting_deploy"]["trust_level"] == "restricted"
        assert action_map["functions_deploy"]["trust_level"] == "restricted"
        assert action_map["auth_import_users"]["trust_level"] == "restricted"

    def test_yaml_read_actions_are_auto(self):
        defn = parse_connector_yaml(CONNECTORS_DIR / "firebase.yaml")
        action_map = {a["name"]: a for a in defn.actions}
        auto_actions = [
            "list_projects",
            "get_project",
            "firestore_list_collections",
            "firestore_databases_list",
            "firestore_get",
            "auth_list_users",
            "hosting_list_sites",
            "functions_list",
            "functions_log",
            "remoteconfig_get",
            "extensions_list",
        ]
        for name in auto_actions:
            assert action_map[name]["trust_level"] == "auto", f"{name} should be auto"


# ---------------------------------------------------------------------------
# Helper to create a mock subprocess
# ---------------------------------------------------------------------------


def _make_mock_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Create a mock asyncio.subprocess result."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    return proc


# ---------------------------------------------------------------------------
# Adapter Unit Tests
# ---------------------------------------------------------------------------


class TestFirebaseAdapterConnect:
    """Test FirebaseAdapter.connect() with mocked subprocess calls."""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        adapter = FirebaseAdapter()
        projects_response = json.dumps(
            {
                "status": "success",
                "result": [
                    {"projectId": "my-project", "displayName": "My Project"},
                ],
            }
        )

        with (
            patch("shutil.which", return_value="/usr/bin/firebase"),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=_make_mock_proc(
                    stdout=projects_response,
                ),
            ),
        ):
            result = await adapter.connect("pocket-1", {})

        assert result.success is True
        assert result.status == ConnectorStatus.CONNECTED
        assert "Firebase CLI" in result.message

    @pytest.mark.asyncio
    async def test_connect_with_project(self):
        adapter = FirebaseAdapter()
        projects_response = json.dumps(
            {
                "status": "success",
                "result": [{"projectId": "my-proj"}],
            }
        )

        with (
            patch("shutil.which", return_value="/usr/bin/firebase"),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=_make_mock_proc(
                    stdout=projects_response,
                ),
            ),
        ):
            result = await adapter.connect("pocket-1", {"FIREBASE_PROJECT": "my-proj"})

        assert result.success is True
        assert "my-proj" in result.message

    @pytest.mark.asyncio
    async def test_connect_firebase_not_installed(self):
        adapter = FirebaseAdapter()

        with patch("shutil.which", return_value=None):
            result = await adapter.connect("pocket-1", {})

        assert result.success is False
        assert result.status == ConnectorStatus.ERROR
        assert "not found" in result.message.lower()

    @pytest.mark.asyncio
    async def test_connect_not_logged_in(self):
        adapter = FirebaseAdapter()
        error_response = json.dumps(
            {
                "status": "error",
                "error": {"message": "Authentication required. Please login."},
            }
        )

        with (
            patch("shutil.which", return_value="/usr/bin/firebase"),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=_make_mock_proc(
                    returncode=1,
                    stdout=error_response,
                ),
            ),
        ):
            result = await adapter.connect("pocket-1", {})

        assert result.success is False
        assert result.status == ConnectorStatus.ERROR


class TestFirebaseAdapterActions:
    """Test the actions() method returns proper schemas."""

    @pytest.mark.asyncio
    async def test_actions_returns_schemas(self):
        adapter = FirebaseAdapter()
        schemas = await adapter.actions()
        assert len(schemas) == 16
        names = {s.name for s in schemas}
        assert "list_projects" in names
        assert "firestore_get" in names
        assert "functions_deploy" in names

    @pytest.mark.asyncio
    async def test_actions_trust_levels(self):
        adapter = FirebaseAdapter()
        schemas = await adapter.actions()
        schema_map = {s.name: s for s in schemas}
        assert schema_map["list_projects"].trust_level == TrustLevel.AUTO
        assert schema_map["firestore_delete"].trust_level == TrustLevel.CONFIRM
        assert schema_map["hosting_deploy"].trust_level == TrustLevel.RESTRICTED

    @pytest.mark.asyncio
    async def test_all_actions_are_local_method(self):
        adapter = FirebaseAdapter()
        schemas = await adapter.actions()
        for s in schemas:
            assert s.method == "LOCAL", f"{s.name} should have method LOCAL"


class TestFirebaseAdapterExecute:
    """Test execute() dispatches to the right CLI commands."""

    async def _connected_adapter(self) -> FirebaseAdapter:
        """Return an adapter that has been connected (mocked)."""
        adapter = FirebaseAdapter()
        adapter._connected = True
        adapter._firebase_bin = "firebase"
        return adapter

    @pytest.mark.asyncio
    async def test_execute_not_connected(self):
        adapter = FirebaseAdapter()
        result = await adapter.execute("list_projects", {})
        assert result.success is False
        assert "Not connected" in result.error

    @pytest.mark.asyncio
    async def test_execute_unknown_action(self):
        adapter = await self._connected_adapter()
        result = await adapter.execute("nonexistent_action", {})
        assert result.success is False
        assert "Unknown action" in result.error

    @pytest.mark.asyncio
    async def test_list_projects(self):
        adapter = await self._connected_adapter()
        projects = [
            {"projectId": "proj-1", "displayName": "Project One"},
            {"projectId": "proj-2", "displayName": "Project Two"},
        ]
        response = json.dumps({"status": "success", "result": projects})

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_mock_proc(
                stdout=response,
            ),
        ):
            result = await adapter.execute("list_projects", {})

        assert result.success is True
        assert len(result.data) == 2
        assert result.records_affected == 2

    @pytest.mark.asyncio
    async def test_firestore_get(self):
        adapter = await self._connected_adapter()
        doc = {"name": "users/abc", "fields": {"email": {"stringValue": "a@b.com"}}}
        response = json.dumps({"status": "success", "result": doc})

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_mock_proc(
                stdout=response,
            ),
        ):
            result = await adapter.execute("firestore_get", {"path": "users/abc"})

        assert result.success is True
        assert result.data["name"] == "users/abc"

    @pytest.mark.asyncio
    async def test_firestore_get_requires_path(self):
        adapter = await self._connected_adapter()
        result = await adapter.execute("firestore_get", {})
        assert result.success is False
        assert "path is required" in result.error

    @pytest.mark.asyncio
    async def test_firestore_delete(self):
        adapter = await self._connected_adapter()
        response = json.dumps({"status": "success", "result": {}})

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_mock_proc(
                stdout=response,
            ),
        ):
            result = await adapter.execute(
                "firestore_delete",
                {
                    "path": "users/abc",
                    "recursive": True,
                },
            )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_functions_log(self):
        adapter = await self._connected_adapter()
        logs = [{"timestamp": "2026-04-01T00:00:00Z", "message": "Hello"}]
        response = json.dumps({"status": "success", "result": logs})

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_mock_proc(
                stdout=response,
            ),
        ):
            result = await adapter.execute("functions_log", {"limit": 10})

        assert result.success is True
        assert result.records_affected == 1

    @pytest.mark.asyncio
    async def test_hosting_list_sites(self):
        adapter = await self._connected_adapter()
        sites = [{"name": "my-site", "defaultUrl": "https://my-site.web.app"}]
        response = json.dumps({"status": "success", "result": sites})

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_mock_proc(
                stdout=response,
            ),
        ):
            result = await adapter.execute("hosting_list_sites", {})

        assert result.success is True
        assert result.records_affected == 1

    @pytest.mark.asyncio
    async def test_command_failure_returns_error(self):
        adapter = await self._connected_adapter()

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_mock_proc(
                returncode=1,
                stderr="Error: project not found",
            ),
        ):
            result = await adapter.execute("list_projects", {})

        assert result.success is False
        assert "project not found" in result.error

    @pytest.mark.asyncio
    async def test_command_timeout(self):
        adapter = await self._connected_adapter()

        async def slow_communicate():
            await asyncio.sleep(100)
            return (b"", b"")

        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = slow_communicate

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await adapter.execute("list_projects", {})

        assert result.success is False
        assert "timed out" in result.error.lower()


class TestFirebaseAdapterSyncSchema:
    """Test sync/schema methods (not applicable for CLI wrapper)."""

    @pytest.mark.asyncio
    async def test_sync_returns_not_supported(self):
        adapter = FirebaseAdapter()
        result = await adapter.sync("pocket-1")
        assert result.success is False
        assert "not supported" in result.error.lower()

    @pytest.mark.asyncio
    async def test_schema_returns_manual(self):
        adapter = FirebaseAdapter()
        schema = await adapter.schema()
        assert schema["schedule"] == "manual"
        assert schema["table"] is None


# ---------------------------------------------------------------------------
# Registry Integration
# ---------------------------------------------------------------------------


class TestFirebaseRegistry:
    """Test that the Firebase adapter is registered in the connector registry."""

    def test_firebase_in_cli_connectors(self):
        from pocketpaw.connectors.registry import _CLI_CONNECTORS

        assert "firebase" in _CLI_CONNECTORS

    def test_create_native_adapter_returns_firebase(self):
        from pocketpaw.connectors.registry import _create_native_adapter

        adapter = _create_native_adapter("firebase")
        assert adapter is not None
        assert adapter.name == "firebase"

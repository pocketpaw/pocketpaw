# Tests for GCP connector — YAML parsing, adapter connect, execute, error handling.
# Created: 2026-04-01

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.connectors.protocol import ConnectorStatus, TrustLevel
from pocketpaw.connectors.yaml_engine import parse_connector_yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONNECTORS_DIR = Path(__file__).resolve().parent.parent / "connectors"
GCP_YAML = CONNECTORS_DIR / "gcp.yaml"


@pytest.fixture
def gcp_definition():
    """Parse the gcp.yaml connector definition."""
    return parse_connector_yaml(GCP_YAML)


@pytest.fixture
def gcp_adapter(gcp_definition):
    """Create a GCPAdapter with the parsed definition."""
    from pocketpaw.connectors.gcp_adapter import GCPAdapter

    return GCPAdapter(definition=gcp_definition)


# ---------------------------------------------------------------------------
# YAML Parsing Tests
# ---------------------------------------------------------------------------


class TestYAMLParsing:
    def test_yaml_exists(self):
        assert GCP_YAML.exists(), f"gcp.yaml not found at {GCP_YAML}"

    def test_basic_fields(self, gcp_definition):
        assert gcp_definition.name == "gcp"
        assert gcp_definition.display_name == "Google Cloud Platform"
        assert gcp_definition.type == "cloud"
        assert gcp_definition.icon == "cloud"

    def test_auth_method(self, gcp_definition):
        assert gcp_definition.auth["method"] == "none"
        creds = gcp_definition.auth["credentials"]
        names = {c["name"] for c in creds}
        assert "GCP_PROJECT" in names
        assert "GCP_REGION" in names
        # Both are optional
        for c in creds:
            assert c.get("required") is False or c.get("required") is None

    def test_action_count(self, gcp_definition):
        # We defined 21 actions total
        assert len(gcp_definition.actions) == 20

    def test_action_names(self, gcp_definition):
        names = {a["name"] for a in gcp_definition.actions}
        expected = {
            "list_projects",
            "get_project",
            "storage_list_buckets",
            "storage_list_objects",
            "storage_get_object",
            "storage_copy",
            "storage_delete",
            "pubsub_list_topics",
            "pubsub_list_subscriptions",
            "pubsub_publish",
            "run_list_services",
            "run_describe_service",
            "run_list_revisions",
            "secrets_list",
            "secrets_get",
            "secrets_create",
            "logs_read",
            "compute_list_instances",
            "compute_describe_instance",
            "iam_list_accounts",
        }
        assert expected.issubset(names)

    def test_trust_levels(self, gcp_definition):
        trust_map = {a["name"]: a.get("trust_level", "confirm") for a in gcp_definition.actions}
        # Read-only actions should be auto
        assert trust_map["list_projects"] == "auto"
        assert trust_map["storage_list_buckets"] == "auto"
        assert trust_map["compute_list_instances"] == "auto"
        # Write actions need confirmation
        assert trust_map["storage_copy"] == "confirm"
        assert trust_map["pubsub_publish"] == "confirm"
        assert trust_map["secrets_get"] == "confirm"
        # Destructive actions are restricted
        assert trust_map["storage_delete"] == "restricted"
        assert trust_map["secrets_create"] == "restricted"

    def test_all_actions_are_local(self, gcp_definition):
        for act in gcp_definition.actions:
            assert act["method"] == "LOCAL"

    def test_required_params(self, gcp_definition):
        """Actions that need params should mark them required."""
        by_name = {a["name"]: a for a in gcp_definition.actions}
        assert by_name["get_project"]["params"]["project_id"]["required"] is True
        assert by_name["storage_list_objects"]["params"]["bucket"]["required"] is True
        assert by_name["storage_delete"]["params"]["bucket"]["required"] is True


# ---------------------------------------------------------------------------
# Adapter Tests (mocked subprocess)
# ---------------------------------------------------------------------------


def _mock_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a mock asyncio subprocess."""
    proc = MagicMock()
    proc.returncode = returncode

    async def communicate():
        return (stdout.encode(), stderr.encode())

    proc.communicate = communicate
    return proc


class TestAdapterConnect:
    @pytest.mark.asyncio
    async def test_connect_success(self, gcp_adapter):
        auth_response = json.dumps([{"account": "test@example.com", "status": "ACTIVE"}])
        with patch("pocketpaw.connectors.gcp_adapter._find_gcloud", return_value="/usr/bin/gcloud"):
            with patch(
                "asyncio.create_subprocess_exec", return_value=_mock_process(stdout=auth_response)
            ):
                result = await gcp_adapter.connect("pocket-1", {})
        assert result.success is True
        assert result.status == ConnectorStatus.CONNECTED
        assert "test@example.com" in result.message

    @pytest.mark.asyncio
    async def test_connect_with_project(self, gcp_adapter):
        auth_response = json.dumps([{"account": "dev@corp.com", "status": "ACTIVE"}])
        with patch("pocketpaw.connectors.gcp_adapter._find_gcloud", return_value="/usr/bin/gcloud"):
            with patch(
                "asyncio.create_subprocess_exec", return_value=_mock_process(stdout=auth_response)
            ):
                result = await gcp_adapter.connect("pocket-1", {"GCP_PROJECT": "my-project"})
        assert result.success is True
        assert "my-project" in result.message

    @pytest.mark.asyncio
    async def test_connect_no_gcloud(self, gcp_adapter):
        with patch("pocketpaw.connectors.gcp_adapter._find_gcloud", return_value=None):
            result = await gcp_adapter.connect("pocket-1", {})
        assert result.success is False
        assert "not found" in result.message.lower()

    @pytest.mark.asyncio
    async def test_connect_not_authenticated(self, gcp_adapter):
        # No active account
        auth_response = json.dumps([{"account": "old@test.com", "status": "DISABLED"}])
        with patch("pocketpaw.connectors.gcp_adapter._find_gcloud", return_value="/usr/bin/gcloud"):
            with patch(
                "asyncio.create_subprocess_exec", return_value=_mock_process(stdout=auth_response)
            ):
                result = await gcp_adapter.connect("pocket-1", {})
        assert result.success is False
        assert "gcloud auth login" in result.message

    @pytest.mark.asyncio
    async def test_connect_gcloud_error(self, gcp_adapter):
        with patch("pocketpaw.connectors.gcp_adapter._find_gcloud", return_value="/usr/bin/gcloud"):
            with patch(
                "asyncio.create_subprocess_exec",
                return_value=_mock_process(stderr="ERROR: some failure", returncode=1),
            ):
                result = await gcp_adapter.connect("pocket-1", {})
        assert result.success is False
        assert "error" in result.message.lower()


class TestAdapterExecute:
    @pytest.mark.asyncio
    async def _connect_adapter(self, adapter):
        """Helper to connect an adapter with mocked gcloud."""
        auth_response = json.dumps([{"account": "test@example.com", "status": "ACTIVE"}])
        with patch("pocketpaw.connectors.gcp_adapter._find_gcloud", return_value="/usr/bin/gcloud"):
            with patch(
                "asyncio.create_subprocess_exec", return_value=_mock_process(stdout=auth_response)
            ):
                await adapter.connect("pocket-1", {"GCP_PROJECT": "test-proj"})

    @pytest.mark.asyncio
    async def test_execute_not_connected(self, gcp_adapter):
        result = await gcp_adapter.execute("list_projects", {})
        assert result.success is False
        assert "Not connected" in result.error

    @pytest.mark.asyncio
    async def test_execute_unknown_action(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        result = await gcp_adapter.execute("nonexistent_action", {})
        assert result.success is False
        assert "Unknown action" in result.error

    @pytest.mark.asyncio
    async def test_list_projects(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        projects = json.dumps(
            [
                {"projectId": "proj-1", "name": "Project One"},
                {"projectId": "proj-2", "name": "Project Two"},
            ]
        )
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=projects)):
            result = await gcp_adapter.execute("list_projects", {})
        assert result.success is True
        assert len(result.data) == 2
        assert result.records_affected == 2

    @pytest.mark.asyncio
    async def test_get_project_missing_param(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        # get_project with no project set and no param
        gcp_adapter._project = None
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="{}")):
            result = await gcp_adapter.execute("get_project", {})
        assert result.success is False
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_storage_list_buckets(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        buckets = json.dumps([{"name": "bucket-1"}, {"name": "bucket-2"}])
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=buckets)):
            result = await gcp_adapter.execute("storage_list_buckets", {})
        assert result.success is True
        assert len(result.data) == 2

    @pytest.mark.asyncio
    async def test_storage_get_object(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        content = "Hello, world!"
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=content)):
            result = await gcp_adapter.execute(
                "storage_get_object", {"bucket": "my-bucket", "path": "test.txt"}
            )
        assert result.success is True
        assert result.data["content"] == content

    @pytest.mark.asyncio
    async def test_storage_get_object_missing_params(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        result = await gcp_adapter.execute("storage_get_object", {"bucket": "my-bucket"})
        assert result.success is False
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_compute_list_instances(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        instances = json.dumps([{"name": "vm-1", "zone": "us-central1-a", "status": "RUNNING"}])
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=instances)):
            result = await gcp_adapter.execute("compute_list_instances", {})
        assert result.success is True
        assert result.data[0]["name"] == "vm-1"

    @pytest.mark.asyncio
    async def test_gcloud_command_failure(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_process(stderr="ERROR: permission denied", returncode=1),
        ):
            result = await gcp_adapter.execute("list_projects", {})
        assert result.success is False
        assert "permission denied" in result.error.lower()

    @pytest.mark.asyncio
    async def test_empty_output(self, gcp_adapter):
        await self._connect_adapter(gcp_adapter)
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="")):
            result = await gcp_adapter.execute("secrets_list", {})
        assert result.success is True
        assert result.data == []


class TestAdapterActions:
    @pytest.mark.asyncio
    async def test_actions_from_yaml(self, gcp_adapter):
        schemas = await gcp_adapter.actions()
        assert len(schemas) == 20
        names = {s.name for s in schemas}
        assert "list_projects" in names
        assert "iam_list_accounts" in names

    @pytest.mark.asyncio
    async def test_actions_trust_levels(self, gcp_adapter):
        schemas = await gcp_adapter.actions()
        by_name = {s.name: s for s in schemas}
        assert by_name["list_projects"].trust_level == TrustLevel.AUTO
        assert by_name["storage_copy"].trust_level == TrustLevel.CONFIRM
        assert by_name["storage_delete"].trust_level == TrustLevel.RESTRICTED

    @pytest.mark.asyncio
    async def test_actions_fallback_without_yaml(self):
        from pocketpaw.connectors.gcp_adapter import GCPAdapter

        adapter = GCPAdapter(definition=None)
        schemas = await adapter.actions()
        # Hardcoded fallback has fewer actions
        assert len(schemas) >= 6
        names = {s.name for s in schemas}
        assert "list_projects" in names


class TestAdapterDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect(self, gcp_adapter):
        result = await gcp_adapter.disconnect("pocket-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_sync_not_supported(self, gcp_adapter):
        result = await gcp_adapter.sync("pocket-1")
        assert result.success is False


class TestRegistryIntegration:
    def test_gcp_in_cli_connectors(self):
        from pocketpaw.connectors.registry import _CLI_CONNECTORS

        assert "gcp" in _CLI_CONNECTORS

    def test_create_native_adapter_returns_gcp(self):
        from pocketpaw.connectors.registry import _create_native_adapter

        adapter = _create_native_adapter("gcp")
        assert adapter is not None
        assert adapter.name == "gcp"

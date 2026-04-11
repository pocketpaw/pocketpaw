# GCP adapter — native Python connector wrapping the gcloud CLI.
# Created: 2026-04-01
# Shells out to gcloud via asyncio.create_subprocess_exec with --format=json.
# Supports optional GCP_PROJECT and GCP_REGION credentials.

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorStatus,
    SyncResult,
    TrustLevel,
)
from pocketpaw.connectors.yaml_engine import ConnectorDef

# Default gcloud binary paths to search
_GCLOUD_PATHS = [
    "/opt/homebrew/share/google-cloud-sdk/bin/gcloud",
    "/usr/local/bin/gcloud",
    "/usr/bin/gcloud",
    "/snap/bin/gcloud",
]

# Per-command timeout in seconds
_CMD_TIMEOUT = 30


def _find_gcloud() -> str | None:
    """Locate the gcloud binary on the system."""
    # Check PATH first
    found = shutil.which("gcloud")
    if found:
        return found
    # Check common install locations
    for p in _GCLOUD_PATHS:
        if Path(p).is_file():
            return p
    return None


class GCPAdapter:
    """Google Cloud Platform connector via gcloud CLI.

    Implements ConnectorProtocol by shelling out to the gcloud CLI
    and parsing JSON output. Each action maps to a specific gcloud
    subcommand.
    """

    def __init__(self, definition: ConnectorDef | None = None) -> None:
        self._def = definition
        self._gcloud: str | None = None
        self._project: str | None = None
        self._region: str | None = None
        self._connected = False

    @property
    def name(self) -> str:
        return "gcp"

    @property
    def display_name(self) -> str:
        return "Google Cloud Platform"

    # -- ConnectorProtocol methods --

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        """Verify gcloud is installed and authenticated."""
        self._gcloud = _find_gcloud()
        if not self._gcloud:
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=(
                    "gcloud CLI not found. Install the Google Cloud SDK: "
                    "https://cloud.google.com/sdk/docs/install"
                ),
            )

        # Store optional project/region overrides
        self._project = config.get("GCP_PROJECT") or None
        self._region = config.get("GCP_REGION") or None

        # Verify authentication
        try:
            result = await self._run_gcloud(["auth", "list", "--format=json"])
            accounts = json.loads(result)
            active = [a for a in accounts if a.get("status") == "ACTIVE"]
            if not active:
                return ConnectionResult(
                    success=False,
                    connector_name=self.name,
                    status=ConnectorStatus.ERROR,
                    message="No active gcloud account. Run: gcloud auth login",
                )
            account_email = active[0].get("account", "unknown")
        except Exception as e:
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=f"gcloud auth check failed: {e}",
            )

        self._connected = True
        project_msg = f" (project: {self._project})" if self._project else ""
        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
            message=f"Connected as {account_email}{project_msg}",
        )

    async def disconnect(self, pocket_id: str) -> bool:
        """No-op — gcloud auth persists outside PocketPaw."""
        self._connected = False
        return True

    async def actions(self) -> list[ActionSchema]:
        """Return action schemas from the YAML definition if available."""
        if self._def:
            schemas = []
            for act in self._def.actions:
                params = {}
                for key, val in act.get("params", {}).items():
                    params[key] = val
                schemas.append(
                    ActionSchema(
                        name=act["name"],
                        description=act.get("description", ""),
                        method=act.get("method", "LOCAL"),
                        parameters=params,
                        trust_level=TrustLevel(act.get("trust_level", "confirm")),
                    )
                )
            return schemas
        # Fallback: return hardcoded action list
        return self._hardcoded_actions()

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        """Execute a gcloud CLI command for the given action."""
        if not self._connected:
            return ActionResult(success=False, error="Not connected")
        if not self._gcloud:
            return ActionResult(success=False, error="gcloud CLI not found")

        handler = self._ACTION_MAP.get(action)
        if not handler:
            return ActionResult(success=False, error=f"Unknown action: {action}")

        try:
            return await handler(self, params)
        except TimeoutError:
            return ActionResult(success=False, error=f"Command timed out after {_CMD_TIMEOUT}s")
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=False, connector_name=self.name, error="Sync not supported")

    async def schema(self) -> dict[str, Any]:
        return {"table": None, "mapping": {}, "schedule": "manual"}

    # -- Internal helpers --

    async def _run_gcloud(self, args: list[str], timeout: float = _CMD_TIMEOUT) -> str:
        """Execute a gcloud command and return stdout."""
        cmd = [self._gcloud or "gcloud"] + args
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode != 0:
            err_msg = stderr.decode().strip() if stderr else f"Exit code {proc.returncode}"
            raise RuntimeError(f"gcloud error: {err_msg}")

        return stdout.decode()

    def _base_flags(
        self, *, include_project: bool = True, include_region: bool = False
    ) -> list[str]:
        """Build common flags (--project, --region, --format=json)."""
        flags = ["--format=json"]
        if include_project and self._project:
            flags.extend(["--project", self._project])
        if include_region and self._region:
            flags.extend(["--region", self._region])
        return flags

    def _parse_json(self, raw: str) -> Any:
        """Parse JSON output from gcloud, returning empty list on empty output."""
        stripped = raw.strip()
        if not stripped:
            return []
        return json.loads(stripped)

    # -- Action handlers --

    async def _list_projects(self, params: dict[str, Any]) -> ActionResult:
        out = await self._run_gcloud(["projects", "list", "--format=json"])
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _get_project(self, params: dict[str, Any]) -> ActionResult:
        pid = params.get("project_id", self._project or "")
        if not pid:
            return ActionResult(success=False, error="project_id is required")
        out = await self._run_gcloud(["projects", "describe", pid, "--format=json"])
        data = self._parse_json(out)
        return ActionResult(success=True, data=data, records_affected=1)

    async def _storage_list_buckets(self, params: dict[str, Any]) -> ActionResult:
        flags = self._base_flags()
        out = await self._run_gcloud(
            ["storage", "ls", "--json"] if False else ["storage", "buckets", "list"] + flags
        )
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _storage_list_objects(self, params: dict[str, Any]) -> ActionResult:
        bucket = params.get("bucket", "")
        if not bucket:
            return ActionResult(success=False, error="bucket is required")
        prefix = params.get("prefix", "")
        uri = f"gs://{bucket}/{prefix}" if prefix else f"gs://{bucket}"
        flags = self._base_flags()
        out = await self._run_gcloud(["storage", "objects", "list", uri] + flags)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _storage_get_object(self, params: dict[str, Any]) -> ActionResult:
        bucket = params.get("bucket", "")
        path = params.get("path", "")
        if not bucket or not path:
            return ActionResult(success=False, error="bucket and path are required")
        out = await self._run_gcloud(["storage", "cat", f"gs://{bucket}/{path}"])
        return ActionResult(success=True, data={"content": out}, records_affected=1)

    async def _storage_copy(self, params: dict[str, Any]) -> ActionResult:
        src = params.get("src", "")
        dest = params.get("dest", "")
        if not src or not dest:
            return ActionResult(success=False, error="src and dest are required")
        out = await self._run_gcloud(["storage", "cp", src, dest])
        return ActionResult(success=True, data={"output": out.strip()}, records_affected=1)

    async def _storage_delete(self, params: dict[str, Any]) -> ActionResult:
        bucket = params.get("bucket", "")
        path = params.get("path", "")
        if not bucket or not path:
            return ActionResult(success=False, error="bucket and path are required")
        out = await self._run_gcloud(["storage", "rm", f"gs://{bucket}/{path}"])
        return ActionResult(success=True, data={"output": out.strip()}, records_affected=1)

    async def _pubsub_list_topics(self, params: dict[str, Any]) -> ActionResult:
        flags = self._base_flags()
        out = await self._run_gcloud(["pubsub", "topics", "list"] + flags)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _pubsub_list_subscriptions(self, params: dict[str, Any]) -> ActionResult:
        flags = self._base_flags()
        out = await self._run_gcloud(["pubsub", "subscriptions", "list"] + flags)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _pubsub_publish(self, params: dict[str, Any]) -> ActionResult:
        topic = params.get("topic", "")
        message = params.get("message", "")
        if not topic or not message:
            return ActionResult(success=False, error="topic and message are required")
        flags = self._base_flags()
        out = await self._run_gcloud(
            ["pubsub", "topics", "publish", topic, f"--message={message}"] + flags
        )
        data = self._parse_json(out)
        return ActionResult(success=True, data=data, records_affected=1)

    async def _run_list_services(self, params: dict[str, Any]) -> ActionResult:
        flags = self._base_flags(include_region=True)
        out = await self._run_gcloud(["run", "services", "list"] + flags)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _run_describe_service(self, params: dict[str, Any]) -> ActionResult:
        name = params.get("name", "")
        if not name:
            return ActionResult(success=False, error="name is required")
        flags = self._base_flags(include_region=True)
        out = await self._run_gcloud(["run", "services", "describe", name] + flags)
        data = self._parse_json(out)
        return ActionResult(success=True, data=data, records_affected=1)

    async def _run_list_revisions(self, params: dict[str, Any]) -> ActionResult:
        flags = self._base_flags(include_region=True)
        out = await self._run_gcloud(["run", "revisions", "list"] + flags)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _secrets_list(self, params: dict[str, Any]) -> ActionResult:
        flags = self._base_flags()
        out = await self._run_gcloud(["secrets", "list"] + flags)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _secrets_get(self, params: dict[str, Any]) -> ActionResult:
        name = params.get("name", "")
        if not name:
            return ActionResult(success=False, error="name is required")
        _flags = self._base_flags()
        # secrets access doesn't support --format=json, returns raw value
        raw_flags = [f"--project={self._project}"] if self._project else []
        out = await self._run_gcloud(
            ["secrets", "versions", "access", "latest", f"--secret={name}"] + raw_flags
        )
        return ActionResult(
            success=True, data={"secret": name, "value": out.strip()}, records_affected=1
        )

    async def _secrets_create(self, params: dict[str, Any]) -> ActionResult:
        name = params.get("name", "")
        if not name:
            return ActionResult(success=False, error="name is required")
        flags = self._base_flags()
        out = await self._run_gcloud(
            ["secrets", "create", name, "--replication-policy=automatic"] + flags
        )
        return ActionResult(
            success=True, data={"created": name, "output": out.strip()}, records_affected=1
        )

    async def _logs_read(self, params: dict[str, Any]) -> ActionResult:
        log_filter = params.get("filter", "")
        limit = params.get("limit", 50)
        flags = self._base_flags()
        cmd = ["logging", "read"]
        if log_filter:
            cmd.append(log_filter)
        cmd.extend([f"--limit={limit}"] + flags)
        out = await self._run_gcloud(cmd)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _compute_list_instances(self, params: dict[str, Any]) -> ActionResult:
        flags = self._base_flags()
        out = await self._run_gcloud(["compute", "instances", "list"] + flags)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    async def _compute_describe_instance(self, params: dict[str, Any]) -> ActionResult:
        name = params.get("name", "")
        if not name:
            return ActionResult(success=False, error="name is required")
        zone = params.get("zone", "")
        flags = self._base_flags()
        cmd = ["compute", "instances", "describe", name]
        if zone:
            cmd.extend(["--zone", zone])
        cmd.extend(flags)
        out = await self._run_gcloud(cmd)
        data = self._parse_json(out)
        return ActionResult(success=True, data=data, records_affected=1)

    async def _iam_list_accounts(self, params: dict[str, Any]) -> ActionResult:
        flags = self._base_flags()
        out = await self._run_gcloud(["iam", "service-accounts", "list"] + flags)
        data = self._parse_json(out)
        return ActionResult(
            success=True, data=data, records_affected=len(data) if isinstance(data, list) else 1
        )

    # Action name → handler method mapping
    _ACTION_MAP: dict[str, Any] = {
        "list_projects": _list_projects,
        "get_project": _get_project,
        "storage_list_buckets": _storage_list_buckets,
        "storage_list_objects": _storage_list_objects,
        "storage_get_object": _storage_get_object,
        "storage_copy": _storage_copy,
        "storage_delete": _storage_delete,
        "pubsub_list_topics": _pubsub_list_topics,
        "pubsub_list_subscriptions": _pubsub_list_subscriptions,
        "pubsub_publish": _pubsub_publish,
        "run_list_services": _run_list_services,
        "run_describe_service": _run_describe_service,
        "run_list_revisions": _run_list_revisions,
        "secrets_list": _secrets_list,
        "secrets_get": _secrets_get,
        "secrets_create": _secrets_create,
        "logs_read": _logs_read,
        "compute_list_instances": _compute_list_instances,
        "compute_describe_instance": _compute_describe_instance,
        "iam_list_accounts": _iam_list_accounts,
    }

    def _hardcoded_actions(self) -> list[ActionSchema]:
        """Fallback action list when no YAML definition is loaded."""
        return [
            ActionSchema(
                name="list_projects",
                description="List GCP projects",
                method="LOCAL",
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="get_project",
                description="Describe a GCP project",
                method="LOCAL",
                parameters={"project_id": {"type": "string", "required": True}},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="storage_list_buckets",
                description="List Cloud Storage buckets",
                method="LOCAL",
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="storage_list_objects",
                description="List objects in a bucket",
                method="LOCAL",
                parameters={"bucket": {"type": "string", "required": True}},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="compute_list_instances",
                description="List Compute Engine instances",
                method="LOCAL",
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="iam_list_accounts",
                description="List IAM service accounts",
                method="LOCAL",
                trust_level=TrustLevel.AUTO,
            ),
        ]

# Firebase CLI adapter — wraps firebase-tools CLI for PocketPaw connectors.
# Created: 2026-04-01
# Shells out to the firebase CLI using asyncio.create_subprocess_exec.
# Supports project management, Firestore, Auth, Hosting, Functions,
# Remote Config, and Extensions via the --json flag for structured output.

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorStatus,
    SyncResult,
    TrustLevel,
)

# Default path to the firebase CLI binary. Overridden via FIREBASE_CLI_PATH if needed.
_DEFAULT_FIREBASE_BIN = "firebase"

# Timeout per CLI command in seconds.
_COMMAND_TIMEOUT = 30


class FirebaseAdapter:
    """Native connector adapter that wraps the Firebase CLI.

    Instead of making REST calls, this adapter shells out to `firebase` commands
    with --json for machine-readable output. Auth is handled by the CLI itself
    (firebase login), not by PocketPaw credentials.
    """

    def __init__(self) -> None:
        self._project: str | None = None
        self._firebase_bin: str = _DEFAULT_FIREBASE_BIN
        self._connected = False

    @property
    def name(self) -> str:
        return "firebase"

    @property
    def display_name(self) -> str:
        return "Firebase"

    async def _run_cmd(
        self,
        *args: str,
        timeout: float = _COMMAND_TIMEOUT,
    ) -> tuple[bool, Any]:
        """Run a firebase CLI command and return (success, parsed_output).

        Adds --json and --non-interactive flags automatically.
        Adds --project flag if a project is configured.
        """
        cmd = [self._firebase_bin, *args, "--json", "--non-interactive"]

        if self._project:
            cmd.extend(["--project", self._project])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                # Try to parse JSON error from stdout first (firebase --json wraps errors)
                error_msg = (
                    stderr_text or stdout_text or f"Command exited with code {proc.returncode}"
                )
                try:
                    parsed = json.loads(stdout_text)
                    if isinstance(parsed, dict) and parsed.get("error"):
                        error_msg = parsed["error"].get("message", str(parsed["error"]))
                except (json.JSONDecodeError, AttributeError):
                    pass
                return False, error_msg

            # Parse JSON output
            if stdout_text:
                try:
                    parsed = json.loads(stdout_text)
                    # Firebase --json wraps results in {"status": "success", "result": ...}
                    if isinstance(parsed, dict) and "result" in parsed:
                        return True, parsed["result"]
                    return True, parsed
                except json.JSONDecodeError:
                    return True, stdout_text
            return True, None

        except TimeoutError:
            return False, f"Command timed out after {timeout}s"
        except FileNotFoundError:
            return False, (
                f"Firebase CLI not found at '{self._firebase_bin}'. "
                "Install with: npm install -g firebase-tools"
            )
        except Exception as e:
            return False, str(e)

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        """Verify firebase CLI is installed and authenticated."""
        # Allow overriding the firebase binary path
        self._firebase_bin = config.get("FIREBASE_CLI_PATH", _DEFAULT_FIREBASE_BIN)

        # Check if firebase binary exists
        if shutil.which(self._firebase_bin) is None:
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=(
                    f"Firebase CLI not found at '{self._firebase_bin}'. "
                    "Install with: npm install -g firebase-tools"
                ),
            )

        # Store project if provided
        self._project = config.get("FIREBASE_PROJECT") or None

        # Verify auth by listing projects
        success, data = await self._run_cmd("projects:list")

        if not success:
            # Check if it's an auth issue
            error_str = str(data).lower()
            if "auth" in error_str or "login" in error_str or "credential" in error_str:
                return ConnectionResult(
                    success=False,
                    connector_name=self.name,
                    status=ConnectorStatus.ERROR,
                    message="Firebase CLI not authenticated. Run: firebase login",
                )
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=f"Firebase CLI error: {data}",
            )

        self._connected = True
        project_msg = f" (project: {self._project})" if self._project else ""
        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
            message=f"Connected to Firebase CLI{project_msg}",
        )

    async def disconnect(self, pocket_id: str) -> bool:
        """Disconnect — no-op since CLI handles its own auth state."""
        self._connected = False
        self._project = None
        return True

    async def actions(self) -> list[ActionSchema]:
        """Return the action schemas for all supported Firebase operations."""
        return [
            # Project Management
            ActionSchema(
                name="list_projects",
                description="List all Firebase projects you have access to",
                method="LOCAL",
                parameters={},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="get_project",
                description="Get details of a specific Firebase project",
                method="LOCAL",
                parameters={
                    "project_id": {"type": "string", "required": True},
                },
                trust_level=TrustLevel.AUTO,
            ),
            # Firestore
            ActionSchema(
                name="firestore_list_collections",
                description="List Firestore indexes and collection info",
                method="LOCAL",
                parameters={
                    "database": {"type": "string"},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="firestore_databases_list",
                description="List all Firestore databases in the project",
                method="LOCAL",
                parameters={},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="firestore_get",
                description="Get a Firestore document or collection at the given path",
                method="LOCAL",
                parameters={
                    "path": {"type": "string", "required": True},
                    "database": {"type": "string"},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="firestore_delete",
                description="Delete a Firestore document at the given path",
                method="LOCAL",
                parameters={
                    "path": {"type": "string", "required": True},
                    "recursive": {"type": "boolean", "default": False},
                    "database": {"type": "string"},
                },
                trust_level=TrustLevel.CONFIRM,
            ),
            ActionSchema(
                name="firestore_export",
                description="Export Firestore data to a GCS bucket",
                method="LOCAL",
                parameters={
                    "destination": {"type": "string", "required": True},
                    "collection_ids": {"type": "string"},
                    "database": {"type": "string"},
                },
                trust_level=TrustLevel.CONFIRM,
            ),
            # Auth
            ActionSchema(
                name="auth_list_users",
                description="Export user accounts from Firebase Auth",
                method="LOCAL",
                parameters={
                    "format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="auth_import_users",
                description="Import users into Firebase Auth from a data file",
                method="LOCAL",
                parameters={
                    "data_file": {"type": "string", "required": True},
                },
                trust_level=TrustLevel.RESTRICTED,
            ),
            # Hosting
            ActionSchema(
                name="hosting_list_sites",
                description="List all Firebase Hosting sites",
                method="LOCAL",
                parameters={},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="hosting_deploy",
                description="Deploy to Firebase Hosting",
                method="LOCAL",
                parameters={
                    "site": {"type": "string"},
                },
                trust_level=TrustLevel.RESTRICTED,
            ),
            # Functions
            ActionSchema(
                name="functions_list",
                description="List all deployed Cloud Functions",
                method="LOCAL",
                parameters={},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="functions_log",
                description="View recent Cloud Functions logs",
                method="LOCAL",
                parameters={
                    "function_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="functions_deploy",
                description="Deploy Cloud Functions",
                method="LOCAL",
                parameters={
                    "function_name": {"type": "string"},
                },
                trust_level=TrustLevel.RESTRICTED,
            ),
            # Remote Config
            ActionSchema(
                name="remoteconfig_get",
                description="Get the Remote Config template",
                method="LOCAL",
                parameters={
                    "version_number": {"type": "string"},
                },
                trust_level=TrustLevel.AUTO,
            ),
            # Extensions
            ActionSchema(
                name="extensions_list",
                description="List installed Firebase Extensions",
                method="LOCAL",
                parameters={},
                trust_level=TrustLevel.AUTO,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        """Execute a Firebase CLI action."""
        if not self._connected:
            return ActionResult(success=False, error="Not connected")

        handler = self._action_handlers().get(action)
        if not handler:
            return ActionResult(success=False, error=f"Unknown action: {action}")

        try:
            return await handler(params)
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    def _action_handlers(self) -> dict[str, Any]:
        """Map action names to handler methods."""
        return {
            "list_projects": self._list_projects,
            "get_project": self._get_project,
            "firestore_list_collections": self._firestore_list_collections,
            "firestore_databases_list": self._firestore_databases_list,
            "firestore_get": self._firestore_get,
            "firestore_delete": self._firestore_delete,
            "firestore_export": self._firestore_export,
            "auth_list_users": self._auth_list_users,
            "auth_import_users": self._auth_import_users,
            "hosting_list_sites": self._hosting_list_sites,
            "hosting_deploy": self._hosting_deploy,
            "functions_list": self._functions_list,
            "functions_log": self._functions_log,
            "functions_deploy": self._functions_deploy,
            "remoteconfig_get": self._remoteconfig_get,
            "extensions_list": self._extensions_list,
        }

    # -- Project Management -------------------------------------------------------

    async def _list_projects(self, params: dict[str, Any]) -> ActionResult:
        success, data = await self._run_cmd("projects:list")
        if not success:
            return ActionResult(success=False, error=str(data))
        records = len(data) if isinstance(data, list) else 1
        return ActionResult(success=True, data=data, records_affected=records)

    async def _get_project(self, params: dict[str, Any]) -> ActionResult:
        project_id = params.get("project_id", "")
        if not project_id:
            return ActionResult(success=False, error="project_id is required")
        # Use apps:list scoped to project to get project info
        old_project = self._project
        self._project = project_id
        success, data = await self._run_cmd("projects:list")
        self._project = old_project
        if not success:
            return ActionResult(success=False, error=str(data))
        # Filter to the requested project
        if isinstance(data, list):
            match = [p for p in data if p.get("projectId") == project_id]
            if match:
                return ActionResult(success=True, data=match[0], records_affected=1)
        return ActionResult(success=True, data=data, records_affected=1)

    # -- Firestore ----------------------------------------------------------------

    async def _firestore_list_collections(self, params: dict[str, Any]) -> ActionResult:
        cmd = ["firestore:indexes"]
        db = params.get("database")
        if db:
            cmd.extend(["--database", db])
        success, data = await self._run_cmd(*cmd)
        if not success:
            return ActionResult(success=False, error=str(data))
        records = len(data) if isinstance(data, list) else 1
        return ActionResult(success=True, data=data, records_affected=records)

    async def _firestore_databases_list(self, params: dict[str, Any]) -> ActionResult:
        success, data = await self._run_cmd("firestore:databases:list")
        if not success:
            return ActionResult(success=False, error=str(data))
        records = len(data) if isinstance(data, list) else 1
        return ActionResult(success=True, data=data, records_affected=records)

    async def _firestore_get(self, params: dict[str, Any]) -> ActionResult:
        path = params.get("path", "")
        if not path:
            return ActionResult(success=False, error="path is required")
        cmd = ["firestore:get", path]
        db = params.get("database")
        if db:
            cmd.extend(["--database", db])
        success, data = await self._run_cmd(*cmd)
        if not success:
            return ActionResult(success=False, error=str(data))
        return ActionResult(success=True, data=data, records_affected=1)

    async def _firestore_delete(self, params: dict[str, Any]) -> ActionResult:
        path = params.get("path", "")
        if not path:
            return ActionResult(success=False, error="path is required")
        cmd = ["firestore:delete", path, "--force"]
        if params.get("recursive"):
            cmd.append("--recursive")
        db = params.get("database")
        if db:
            cmd.extend(["--database", db])
        success, data = await self._run_cmd(*cmd)
        if not success:
            return ActionResult(success=False, error=str(data))
        return ActionResult(success=True, data=data, records_affected=1)

    async def _firestore_export(self, params: dict[str, Any]) -> ActionResult:
        destination = params.get("destination", "")
        if not destination:
            return ActionResult(success=False, error="destination is required")
        cmd = ["firestore:export", destination]
        collection_ids = params.get("collection_ids")
        if collection_ids:
            cmd.extend(["--collection-ids", collection_ids])
        db = params.get("database")
        if db:
            cmd.extend(["--database", db])
        success, data = await self._run_cmd(*cmd, timeout=120)
        if not success:
            return ActionResult(success=False, error=str(data))
        return ActionResult(success=True, data=data, records_affected=1)

    # -- Authentication -----------------------------------------------------------

    async def _auth_list_users(self, params: dict[str, Any]) -> ActionResult:
        fmt = params.get("format", "json")
        cmd = ["auth:export", "--format", fmt]
        success, data = await self._run_cmd(*cmd)
        if not success:
            return ActionResult(success=False, error=str(data))
        records = len(data.get("users", [])) if isinstance(data, dict) else 1
        return ActionResult(success=True, data=data, records_affected=records)

    async def _auth_import_users(self, params: dict[str, Any]) -> ActionResult:
        data_file = params.get("data_file", "")
        if not data_file:
            return ActionResult(success=False, error="data_file is required")
        cmd = ["auth:import", data_file]
        success, data = await self._run_cmd(*cmd)
        if not success:
            return ActionResult(success=False, error=str(data))
        return ActionResult(success=True, data=data, records_affected=1)

    # -- Hosting ------------------------------------------------------------------

    async def _hosting_list_sites(self, params: dict[str, Any]) -> ActionResult:
        success, data = await self._run_cmd("hosting:sites:list")
        if not success:
            return ActionResult(success=False, error=str(data))
        records = len(data) if isinstance(data, list) else 1
        return ActionResult(success=True, data=data, records_affected=records)

    async def _hosting_deploy(self, params: dict[str, Any]) -> ActionResult:
        cmd = ["deploy", "--only", "hosting"]
        site = params.get("site")
        if site:
            cmd.extend(["--only", f"hosting:{site}"])
        success, data = await self._run_cmd(*cmd, timeout=120)
        if not success:
            return ActionResult(success=False, error=str(data))
        return ActionResult(success=True, data=data, records_affected=1)

    # -- Cloud Functions ----------------------------------------------------------

    async def _functions_list(self, params: dict[str, Any]) -> ActionResult:
        success, data = await self._run_cmd("functions:list")
        if not success:
            return ActionResult(success=False, error=str(data))
        records = len(data) if isinstance(data, list) else 1
        return ActionResult(success=True, data=data, records_affected=records)

    async def _functions_log(self, params: dict[str, Any]) -> ActionResult:
        cmd = ["functions:log"]
        limit = params.get("limit", 50)
        cmd.extend(["--limit", str(limit)])
        fn_name = params.get("function_name")
        if fn_name:
            cmd.extend(["--only", fn_name])
        success, data = await self._run_cmd(*cmd)
        if not success:
            return ActionResult(success=False, error=str(data))
        records = len(data) if isinstance(data, list) else 1
        return ActionResult(success=True, data=data, records_affected=records)

    async def _functions_deploy(self, params: dict[str, Any]) -> ActionResult:
        cmd = ["deploy", "--only", "functions"]
        fn_name = params.get("function_name")
        if fn_name:
            cmd = ["deploy", "--only", f"functions:{fn_name}"]
        success, data = await self._run_cmd(*cmd, timeout=120)
        if not success:
            return ActionResult(success=False, error=str(data))
        return ActionResult(success=True, data=data, records_affected=1)

    # -- Remote Config ------------------------------------------------------------

    async def _remoteconfig_get(self, params: dict[str, Any]) -> ActionResult:
        cmd = ["remoteconfig:get"]
        version = params.get("version_number")
        if version:
            cmd.extend(["--version-number", str(version)])
        success, data = await self._run_cmd(*cmd)
        if not success:
            return ActionResult(success=False, error=str(data))
        return ActionResult(success=True, data=data, records_affected=1)

    # -- Extensions ---------------------------------------------------------------

    async def _extensions_list(self, params: dict[str, Any]) -> ActionResult:
        success, data = await self._run_cmd("ext:list")
        if not success:
            return ActionResult(success=False, error=str(data))
        records = len(data) if isinstance(data, list) else 1
        return ActionResult(success=True, data=data, records_affected=records)

    # -- Sync / Schema (not applicable for CLI wrapper) ---------------------------

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=False, connector_name=self.name, error="Sync not supported")

    async def schema(self) -> dict[str, Any]:
        return {"table": None, "mapping": {}, "schedule": "manual"}

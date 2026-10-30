"""WorkspaceVm document — the workspace→Daytona-VM mapping (DB-backed).

Created 2026-07-15 (fix/workspace-vm-map-to-db): moves the workspace-level VM
mapping out of a local JSON file (``~/.pocketpaw/daytona_workspace_vm_map.json``,
via ``ee.cloud.daytona.store``) and into MongoDB. A local-first JSON artifact
does not belong in multi-tenant cloud — every read must be tenant-scoped and
survive across processes/replicas. One VM per workspace (unique index on
``workspace``).

Only ``ee.cloud.daytona.store`` imports this doc class directly — store.py is
the module that owns this collection (ee/cloud Rule 2 spirit; store.py is a
persistence helper, not a full 4-file entity). ``config`` mirrors the legacy
JSON shape (cpu/memory/disk/root_dir/auto_stop_interval) so the one-time
migration can copy entries across without transformation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class WorkspaceVm(Document):
    workspace: Indexed(str)  # type: ignore[valid-type]  # unique — one VM per workspace
    sandbox_id: str
    sandbox_name: str
    config: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "workspace_vms"
        indexes = [
            # One VM per workspace — the registry key.
            IndexModel([("workspace", 1)], unique=True, name="workspace_unique"),
        ]

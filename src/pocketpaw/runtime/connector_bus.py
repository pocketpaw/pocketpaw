# Connector bus listener — runs CLI connectors on the user's host.
# Created: 2026-05-03 — Phase 1 PR-8.
#
# Subscribes to the cloud's `connector.exec.requested` event. When
# the cloud router dispatches a local-mode action (firebase, gcp, …),
# this listener runs the matching adapter on the local machine and
# publishes `connector.exec.completed` with the result.
#
# In single-process pocketpaw deployments (the captain's primary
# shape — `mount_cloud(app)` runs inside the same FastAPI app as the
# local runtime), the bus is in-process and the round-trip is direct.
# In multi-tenant cloud deployments the bus must be cross-process
# (RedisBus, Task 33) — the contract here is identical, only the
# transport changes.

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from ee.cloud.shared.events import event_bus
from pocketpaw.connectors.registry import ConnectorRegistry, _create_native_adapter

logger = logging.getLogger(__name__)

# Topics shared with ee/cloud/connectors/service.py
EXEC_REQUESTED = "connector.exec.requested"
EXEC_COMPLETED = "connector.exec.completed"

# Default timeout per CLI invocation. CLI ops (firebase deploy, gcloud
# pubsub publish) can take ~30-60s in practice; cap so a stuck command
# doesn't block the listener forever.
_EXEC_TIMEOUT_S = 60


_registered = False


def register_listener(connectors_dir: Path | None = None) -> None:
    """Register the connector exec listener on the cloud event bus.

    Idempotent — safe to call multiple times during boot.

    Call from app startup once the cloud is mounted. In the captain's
    local-first shape that's right after ``mount_cloud(app)`` in
    pocketpaw's dashboard.py.
    """
    global _registered
    if _registered:
        return
    handler = _build_handler(connectors_dir or Path("connectors"))
    event_bus.subscribe(EXEC_REQUESTED, handler)
    _registered = True
    logger.info("connector bus listener registered on %s", EXEC_REQUESTED)


def _build_handler(connectors_dir: Path):
    """Build the async handler closed over a registry instance."""
    registry = ConnectorRegistry(connectors_dir)

    async def handler(payload: dict[str, Any]) -> None:
        request_id = payload.get("request_id", "")
        connector = payload.get("connector", "")
        action = payload.get("action", "")
        params = payload.get("params") or {}
        requires_binary = payload.get("requires_binary")

        if not connector or not action:
            await _emit_completed(request_id, success=False, error="malformed exec request")
            return

        # Fail fast if the required binary is missing on this host.
        if requires_binary and shutil.which(requires_binary) is None:
            await _emit_completed(
                request_id,
                success=False,
                error=f"connector.binary_missing: {requires_binary} not found on PATH",
                connector=connector,
                action=action,
            )
            return

        adapter = _create_native_adapter(connector)
        if adapter is None:
            # Fall back to the YAML registry for connectors without a
            # native Python class (rare for local-mode actions).
            defn = registry.get_definition(connector)
            if defn is None:
                await _emit_completed(
                    request_id,
                    success=False,
                    error=f"connector.not_found: {connector}",
                )
                return
            from pocketpaw.connectors.yaml_engine import DirectRESTAdapter

            adapter = DirectRESTAdapter(defn)

        # CLI adapters expect connect() to be called before execute().
        try:
            if not getattr(adapter, "_connected", False):
                await adapter.connect(payload.get("scope", "default"), {})
        except Exception as exc:  # noqa: BLE001
            await _emit_completed(
                request_id,
                success=False,
                error=f"connect failed: {exc}",
                connector=connector,
                action=action,
            )
            return

        try:
            result = await asyncio.wait_for(
                adapter.execute(action, params),
                timeout=_EXEC_TIMEOUT_S,
            )
        except TimeoutError:
            await _emit_completed(
                request_id,
                success=False,
                error=f"action timed out after {_EXEC_TIMEOUT_S}s",
                connector=connector,
                action=action,
            )
            return
        except Exception as exc:  # noqa: BLE001
            await _emit_completed(
                request_id,
                success=False,
                error=str(exc),
                connector=connector,
                action=action,
            )
            return

        await _emit_completed(
            request_id,
            success=result.success,
            data=result.data,
            error=result.error,
            records_affected=result.records_affected,
            connector=connector,
            action=action,
        )

    return handler


async def _emit_completed(
    request_id: str,
    *,
    success: bool,
    data: Any = None,
    error: str | None = None,
    records_affected: int = 0,
    connector: str = "",
    action: str = "",
) -> None:
    await event_bus.emit(
        EXEC_COMPLETED,
        {
            "request_id": request_id,
            "connector": connector,
            "action": action,
            "success": success,
            "data": data,
            "error": error,
            "records_affected": records_affected,
        },
    )


def reset_for_tests() -> None:
    """Test-only — clear the registered flag so a fresh handler can subscribe."""
    global _registered
    _registered = False

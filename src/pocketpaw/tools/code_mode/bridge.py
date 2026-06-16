# Code Mode RPC bridge — a local Unix-domain-socket server that dispatches a
# code-mode script's tool calls through the EXISTING ToolRegistry chokepoint.
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.
#
# The sandbox child (running the agent's script) connects to this UDS, sends
# ``{"name", "args"}`` newline-delimited JSON, and reads back ``{"ok",
# "result"|"error"}``. Every call:
#   1. RE-VALIDATES the read-safe allowlist via code_mode.safety — the bridge
#      NEVER trusts the stub list it shipped (defense in depth). A write/gated
#      tool is rejected here even if a hand-edited script calls it directly.
#   2. STRIPS any script-supplied tenancy override (workspace_id / user_id /
#      pocket_id) and re-injects the runner-resolved values — a script can never
#      read another workspace's Fabric/KB by passing its own workspace_id.
#   3. Dispatches through ``ToolRegistry.execute`` so policy + audit + output
#      capping are all inherited (one chokepoint, no bypass).
#   4. REJECTS any result carrying the ``instinct_pending`` sentinel — a parked
#      write must never round-trip into the script's data flow.
#   5. ENFORCES a per-run cap on the number of tool calls.
#
# The bridge runs in the HOST process (it holds the live registry + event
# loop). Only the socket path crosses into the sandbox child.

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from pocketpaw.tools.code_mode.safety import (
    carries_instinct_pending,
    is_read_safe_name,
    is_read_safe_tool,
)
from pocketpaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Tenancy keys a script must never set — the bridge owns these and re-injects
# the runner-resolved values, ignoring whatever the script passed.
_TENANCY_OVERRIDE_KEYS = ("workspace_id", "user_id")

# A single RPC frame ceiling (matches the stub's client-side guard).
_MAX_FRAME = 8 * 1024 * 1024


@dataclass
class BridgeConfig:
    """Per-run bridge configuration resolved by the runner.

    ``workspace_id`` / ``user_id`` are the RESOLVED tenancy the bridge forces
    onto every call — the script's own values are discarded. ``max_calls`` caps
    the number of in-script tool calls per run.
    """

    workspace_id: str = ""
    user_id: str = ""
    max_calls: int = 50


@dataclass
class _RunState:
    """Mutable per-run counters (call budget)."""

    calls: int = 0
    rejected: list[str] = field(default_factory=list)


class CodeModeBridge:
    """A UDS server that brokers read-safe tool calls for one code-mode run.

    Lifecycle: ``async with CodeModeBridge(registry, config, socket_path) as
    bridge:`` starts the server; the runner spawns the sandbox child pointed at
    ``socket_path``; on exit the server is torn down. One bridge instance per
    run — never shared across runs.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        config: BridgeConfig,
        socket_path: str,
    ) -> None:
        self._registry = registry
        self._config = config
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._state = _RunState()

    @property
    def call_count(self) -> int:
        return self._state.calls

    @property
    def rejected_calls(self) -> list[str]:
        return list(self._state.rejected)

    async def __aenter__(self) -> CodeModeBridge:
        self._server = await asyncio.start_unix_server(self._handle_client, path=self._socket_path)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                logger.debug("code-mode bridge close raised", exc_info=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one request per connection (the stub opens a fresh socket per
        call). Reads one newline-delimited JSON frame, dispatches, replies."""
        try:
            raw = await reader.readline()
            if len(raw) > _MAX_FRAME:
                await self._reply(writer, ok=False, error="request too large")
                return
            reply = await self._dispatch(raw)
            await self._send(writer, reply)
        except Exception as exc:  # noqa: BLE001 — never let one bad call kill the server
            logger.debug("code-mode bridge request failed: %s", exc, exc_info=True)
            with _suppress():
                await self._reply(writer, ok=False, error=f"bridge error: {exc}")
        finally:
            with _suppress():
                writer.close()

    async def _dispatch(self, raw: bytes) -> dict:
        """Validate + execute one ``(name, args)`` request. Returns the reply
        dict. The security gates live here — every path returns through this
        function so none can be bypassed."""
        try:
            request = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": "malformed request JSON"}

        name = request.get("name")
        args = request.get("args") or {}
        if not isinstance(name, str) or not name:
            return {"ok": False, "error": "missing tool name"}
        if not isinstance(args, dict):
            return {"ok": False, "error": "args must be an object"}

        # GATE 1 — call budget. Count the attempt even if it's rejected so a
        # script can't burn the host with rejected calls either.
        self._state.calls += 1
        if self._state.calls > self._config.max_calls:
            return {
                "ok": False,
                "error": (
                    f"code-mode call budget exceeded "
                    f"(max {self._config.max_calls} tool calls per run)"
                ),
            }

        # GATE 2 — name allowlist (cheap first check).
        if not is_read_safe_name(name):
            self._state.rejected.append(name)
            return {"ok": False, "error": f"tool '{name}' is not read-safe (blocked in code mode)"}

        # GATE 3 — live-tool re-validation. Re-resolve the tool from the
        # registry and re-run the FULL predicate (trust ceiling included). The
        # name allowlist alone isn't enough: a tool's trust could have changed,
        # or a same-named non-read-safe tool could be registered.
        tool = self._registry.get(name)
        if tool is None:
            return {"ok": False, "error": f"tool '{name}' not found"}
        if not is_read_safe_tool(tool):
            self._state.rejected.append(name)
            return {"ok": False, "error": f"tool '{name}' is not read-safe (blocked in code mode)"}

        # GATE 4 — tenancy lock. Strip any script-supplied tenancy override and
        # force the runner-resolved values. A script can NEVER widen its own
        # tenancy by passing workspace_id/user_id.
        safe_args = {k: v for k, v in args.items() if k not in _TENANCY_OVERRIDE_KEYS}
        if self._config.workspace_id:
            safe_args["workspace_id"] = self._config.workspace_id
        if self._config.user_id:
            safe_args["user_id"] = self._config.user_id

        # Dispatch through the EXISTING registry chokepoint — policy + audit +
        # output capping are inherited; no separate execution path.
        try:
            result = await self._registry.execute(name, **safe_args)
        except Exception as exc:  # noqa: BLE001 — registry.execute is defensive, but be safe
            return {"ok": False, "error": f"tool '{name}' failed: {exc}"}

        # GATE 5 — reject a parked-write sentinel. A read-safe tool should never
        # return ``instinct_pending``, but if one ever does, refuse it rather
        # than leak a parked write into the script's data flow.
        if carries_instinct_pending(result):
            self._state.rejected.append(name)
            return {
                "ok": False,
                "error": (
                    f"tool '{name}' returned a pending-approval result — rejected in code mode"
                ),
            }

        return {"ok": True, "result": result}

    async def _reply(
        self, writer: asyncio.StreamWriter, *, ok: bool, error: str = "", result: str = ""
    ) -> None:
        reply = {"ok": ok}
        if error:
            reply["error"] = error
        if result:
            reply["result"] = result
        await self._send(writer, reply)

    async def _send(self, writer: asyncio.StreamWriter, reply: dict) -> None:
        writer.write((json.dumps(reply) + "\n").encode("utf-8"))
        await writer.drain()


class _suppress:
    """Swallow any exception in best-effort teardown / error-reply paths."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return True

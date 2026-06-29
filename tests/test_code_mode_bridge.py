# Tests for the Code Mode RPC bridge (re-validation, tenancy lock, sentinel,
# call budget) — exercised over a real Unix-domain socket.
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from pocketpaw.tools.code_mode.bridge import BridgeConfig, CodeModeBridge
from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


class _EchoArgsTool(BaseTool):
    """A read-safe (allowlisted) tool that echoes the args it received as JSON,
    so a test can assert what the bridge forwarded."""

    def __init__(self, name: str = "read_file", trust: str = "standard") -> None:
        self._name = name
        self._trust = trust

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "echo args"

    @property
    def trust_level(self) -> str:
        return self._trust

    @property
    def parameters(self) -> dict[str, Any]:
        # Declares the tenancy params so the bridge will inject the resolved
        # values — represents a tenancy-aware read tool (a future Fabric/KB read).
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "workspace_id": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": [],
        }

    async def execute(self, **params: Any) -> str:
        return json.dumps(params, sort_keys=True, default=str)


class _PendingTool(BaseTool):
    """A tool (allowlisted name) whose result carries the instinct_pending
    sentinel — the bridge must reject it."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "returns a pending sentinel"

    @property
    def trust_level(self) -> str:
        return "standard"

    async def execute(self, **params: Any) -> str:
        return "this action is instinct_pending awaiting approval"


def _sock_path() -> str:
    return str(Path(tempfile.gettempdir()) / f"pcm-test-{uuid.uuid4().hex[:8]}.sock")


async def _rpc(socket_path: str, name: str, args: dict) -> dict:
    """Open a fresh UDS connection, send one frame, read the reply."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write((json.dumps({"name": name, "args": args}) + "\n").encode("utf-8"))
    writer.write_eof()
    await writer.drain()
    raw = await reader.read()
    writer.close()
    return json.loads(raw.decode("utf-8").strip())


async def test_read_safe_tool_dispatches():
    reg = ToolRegistry()
    reg.register(_EchoArgsTool())
    cfg = BridgeConfig(workspace_id="ws-1", user_id="u-1", max_calls=10)
    sock = _sock_path()
    async with CodeModeBridge(reg, cfg, sock):
        reply = await _rpc(sock, "read_file", {"path": "/x"})
    assert reply["ok"] is True
    echoed = json.loads(reply["result"])
    assert echoed["path"] == "/x"


async def test_write_tool_rejected_even_when_invoked_directly():
    # The registry has a write tool registered, but the bridge must refuse it.
    reg = ToolRegistry()
    reg.register(_EchoArgsTool(name="write_file", trust="standard"))
    cfg = BridgeConfig(max_calls=10)
    sock = _sock_path()
    async with CodeModeBridge(reg, cfg, sock):
        reply = await _rpc(sock, "write_file", {"path": "/x", "content": "y"})
    assert reply["ok"] is False
    assert "read-safe" in reply["error"]


async def test_high_trust_tool_rejected_even_with_allowlisted_name():
    # A tool named 'read_file' but registered at trust 'high' must still be
    # rejected by the live-tool re-validation (GATE 3).
    reg = ToolRegistry()
    reg.register(_EchoArgsTool(name="read_file", trust="high"))
    cfg = BridgeConfig(max_calls=10)
    sock = _sock_path()
    async with CodeModeBridge(reg, cfg, sock):
        reply = await _rpc(sock, "read_file", {"path": "/x"})
    assert reply["ok"] is False
    assert "read-safe" in reply["error"]


async def test_tenancy_override_from_script_is_ignored():
    reg = ToolRegistry()
    reg.register(_EchoArgsTool())
    cfg = BridgeConfig(workspace_id="real-ws", user_id="real-user", max_calls=10)
    sock = _sock_path()
    async with CodeModeBridge(reg, cfg, sock):
        # Script tries to read another workspace by passing its own id.
        reply = await _rpc(sock, "read_file", {"path": "/x", "workspace_id": "EVIL-ws"})
    assert reply["ok"] is True
    echoed = json.loads(reply["result"])
    # The bridge forced the resolved tenancy; the script's value is discarded.
    assert echoed["workspace_id"] == "real-ws"
    assert echoed["user_id"] == "real-user"


class _TenancyBlindTool(BaseTool):
    """A read-safe tool that declares NO tenancy params (system_info-shaped)."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "blind"

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}

    async def execute(self, path: str = "") -> str:
        # No **kwargs — an injected workspace_id would raise TypeError here.
        return json.dumps({"path": path}, sort_keys=True)


async def test_tenancy_not_injected_into_blind_tool():
    # The bridge must NOT force-feed tenancy to a tool that doesn't declare it —
    # the strict-signature tool would otherwise raise on an unexpected kwarg.
    reg = ToolRegistry()
    reg.register(_TenancyBlindTool())
    cfg = BridgeConfig(workspace_id="real-ws", user_id="real-user", max_calls=10)
    sock = _sock_path()
    async with CodeModeBridge(reg, cfg, sock):
        # Even a script-supplied workspace_id is stripped (never reaches the tool).
        reply = await _rpc(sock, "read_file", {"path": "/x", "workspace_id": "EVIL"})
    assert reply["ok"] is True, reply.get("error")
    echoed = json.loads(reply["result"])
    assert echoed == {"path": "/x"}
    assert "workspace_id" not in echoed


async def test_instinct_pending_result_rejected():
    reg = ToolRegistry()
    reg.register(_PendingTool())
    cfg = BridgeConfig(max_calls=10)
    sock = _sock_path()
    async with CodeModeBridge(reg, cfg, sock):
        reply = await _rpc(sock, "read_file", {"path": "/x"})
    assert reply["ok"] is False
    assert "pending-approval" in reply["error"]


async def test_call_budget_enforced():
    reg = ToolRegistry()
    reg.register(_EchoArgsTool())
    cfg = BridgeConfig(max_calls=2)
    sock = _sock_path()
    async with CodeModeBridge(reg, cfg, sock):
        r1 = await _rpc(sock, "read_file", {"path": "/1"})
        r2 = await _rpc(sock, "read_file", {"path": "/2"})
        r3 = await _rpc(sock, "read_file", {"path": "/3"})
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r3["ok"] is False
    assert "budget" in r3["error"]


async def test_unknown_tool_rejected():
    reg = ToolRegistry()
    reg.register(_EchoArgsTool())
    cfg = BridgeConfig(max_calls=10)
    sock = _sock_path()
    async with CodeModeBridge(reg, cfg, sock):
        reply = await _rpc(sock, "totally_unknown", {})
    assert reply["ok"] is False

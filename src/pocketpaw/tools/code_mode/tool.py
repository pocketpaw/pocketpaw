# Code Mode builtin tool — exposes Programmatic Tool Calling to the agent.
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.
#
# The agent writes a Python script that imports ``paw_tools`` and chains N
# read-safe internal tool calls, printing only its final result. This tool
# runs that script in a sandbox (see runner.py) and returns ONLY the script's
# stdout — intermediate tool results are discarded, never entering the LLM's
# context.
#
# Read-only v1: the sandbox can only reach tools that pass the read-safe gate
# (code_mode.safety). Writes, gated tools, connector writes, instinct_*,
# terminal, and a NESTED code_mode are all absent from the stub surface AND
# rejected at the bridge. The tool builds a dedicated read-safe registry that
# can NEVER contain itself, so recursion is structurally impossible.

from __future__ import annotations

import logging
import os
from typing import Any

from pocketpaw.tools.code_mode.runner import (
    DEFAULT_MAX_CALLS,
    DEFAULT_STDOUT_CAP,
    DEFAULT_TIMEOUT_S,
    run_code_mode,
)
from pocketpaw.tools.code_mode.safety import is_read_safe_tool
from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Hard ceilings — a tool-call arg can lower these but never raise them.
_MAX_CALLS_CEILING = 50
_TIMEOUT_CEILING_S = 30


def _build_read_safe_registry() -> ToolRegistry:
    """Build a fresh registry containing ONLY read-safe builtin tools.

    Instantiates the builtin tools (same lazy map the tool bridge uses), filters
    through the read-safe gate, and registers the survivors. ``code_mode`` is
    NEVER in the builtin lazy map's read-safe survivors (it isn't on the
    allowlist), so a nested code_mode is structurally impossible — the stub for
    it is never generated and the bridge would reject it anyway.

    v1 SURFACE: this candidate pool is ``_LAZY_IMPORTS`` — the generic OSS
    builtins only. EE tools (fabric_query, instinct_*, …) live in a SEPARATE map
    (``_EE_NAMES``), which we deliberately do NOT iterate, so they never enter
    the pool. The read-safe gate's trust ceiling is the second layer that would
    reject them anyway (fabric_query is trust "high" by design — it emits bus
    trace events, a real side effect). Fabric/KB/connector reads in code mode are
    a v2 item: they need an action-level read classifier, not a tool-level trust.
    """
    from pocketpaw.tools.builtin import _LAZY_IMPORTS

    registry = ToolRegistry()
    import importlib

    for class_name, (module_path, attr_name) in _LAZY_IMPORTS.items():
        try:
            mod = importlib.import_module(module_path, "pocketpaw.tools.builtin")
            cls = getattr(mod, attr_name)
            tool = cls()
        except Exception as exc:  # noqa: BLE001 — one broken tool shouldn't kill the set
            logger.debug("code_mode: skipping builtin %s: %s", class_name, exc)
            continue
        if is_read_safe_tool(tool):
            registry.register(tool)
    return registry


class CodeModeTool(BaseTool):
    """Run a Python script that chains read-safe internal tool calls.

    Read-only v1. trust_level ``standard`` — the tool itself is a read-only
    orchestration surface; every tool it can reach is independently re-validated
    as read-safe by the bridge.
    """

    @property
    def name(self) -> str:
        return "code_mode"

    @property
    def description(self) -> str:
        return (
            "Run a Python script that chains several READ-ONLY internal tool calls and "
            "returns ONLY what the script prints to stdout. Import the generated 'paw_tools' "
            "module and call its functions (e.g. paw_tools.web_search(query='...'), "
            "paw_tools.read_file(path='...')). Each function returns the tool's string result. "
            "Use this when you need to call several read-safe tools and combine or filter their "
            "results IN CODE so only the final answer reaches you — intermediate tool outputs are "
            "discarded. Only read-safe tools are available; writes, sends, and approval-gated "
            "actions are blocked. End your script with print(<final result>)."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": (
                        "The Python script to run. Import 'paw_tools' and call its read-safe "
                        "stub functions. Print only the final result to stdout."
                    ),
                },
                "timeout_s": {
                    "type": "integer",
                    "description": f"Wall-clock timeout in seconds (default {DEFAULT_TIMEOUT_S}, "
                    f"max {_TIMEOUT_CEILING_S}).",
                    "default": DEFAULT_TIMEOUT_S,
                },
                "max_calls": {
                    "type": "integer",
                    "description": f"Max in-script tool calls (default {DEFAULT_MAX_CALLS}, "
                    f"max {_MAX_CALLS_CEILING}).",
                    "default": DEFAULT_MAX_CALLS,
                },
            },
            "required": ["script"],
        }

    async def execute(
        self,
        script: str,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        max_calls: int = DEFAULT_MAX_CALLS,
        workspace_id: str | None = None,
        user_id: str | None = None,
        **_extra: Any,
    ) -> str:
        if not script or not script.strip():
            return self._error("code_mode requires a non-empty 'script'.")

        # Clamp caps to the ceilings — a script arg can lower but never raise.
        timeout = min(int(timeout_s) if timeout_s else DEFAULT_TIMEOUT_S, _TIMEOUT_CEILING_S)
        calls = min(int(max_calls) if max_calls else DEFAULT_MAX_CALLS, _MAX_CALLS_CEILING)

        # Resolve tenancy: explicit caller-supplied values win (a web-request
        # context that has the workspace/user in hand passes them directly),
        # falling back to the runner-injected env (set when the agent runs as a
        # subprocess). The bridge forces whatever resolves here onto every tool
        # call; a script can never override it. These are NON-secret identifiers.
        workspace_id = workspace_id or os.environ.get("POCKETPAW_WORKSPACE_ID", "")
        user_id = user_id or os.environ.get("POCKETPAW_USER_ID", "")
        if not workspace_id and not user_id:
            # Blank tenancy is legitimate for local/OSS single-tenant runs, but
            # in a multi-tenant context it means the bridge will pass empty
            # scope — make that visible so a mis-wired call surfaces in logs.
            logger.debug(
                "code_mode: running with blank tenancy (no workspace_id/user_id "
                "from caller or env) — read-safe tools will scope to the default tenant"
            )

        registry = _build_read_safe_registry()

        try:
            result = await run_code_mode(
                registry=registry,
                script=script,
                workspace_id=workspace_id,
                user_id=user_id,
                max_calls=calls,
                timeout_s=timeout,
                stdout_cap=DEFAULT_STDOUT_CAP,
            )
        except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
            logger.error("code_mode run failed: %s", exc, exc_info=True)
            return self._error(f"code_mode run failed: {exc}")

        if result.timed_out:
            return self._error(
                f"code_mode script timed out after {timeout}s "
                f"({result.tool_calls} tool call(s) made before kill)."
            )

        # Compose the response: the script's stdout is the payload. Surface a
        # short diagnostic line for a non-zero exit so the agent can debug,
        # without dumping the whole traceback into context (stderr is capped).
        out = result.stdout.strip()
        if result.exit_code != 0:
            err_tail = (result.stderr or "").strip()
            if len(err_tail) > 800:
                err_tail = err_tail[-800:]
            note = f"[code_mode: script exited {result.exit_code}]"
            if err_tail:
                note += f"\n{err_tail}"
            return self._success(f"{out}\n{note}" if out else note)

        return self._success(out or "(script produced no stdout)")

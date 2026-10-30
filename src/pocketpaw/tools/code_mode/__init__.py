# Code Mode (Programmatic Tool Calling) package.
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.
#
# The agent writes a Python script that chains N read-safe internal tool calls
# through a generated stub library, runs it in a sandbox, and only the final
# stdout returns to the LLM. The Instinct gate survives: v1 exposes read-safe
# tools only; writes stay out.
#
# Modules:
#   safety   — the single read-safe allowlist predicate (stubgen + bridge share)
#   stubgen  — emits the ``paw_tools.py`` stub library from a registry
#   bridge   — UDS RPC server dispatching through ToolRegistry.execute
#   runner   — sandbox subprocess + bridge lifecycle + stdout capture
#   tool     — the ``code_mode`` builtin tool

from pocketpaw.tools.code_mode.tool import CodeModeTool

__all__ = ["CodeModeTool"]

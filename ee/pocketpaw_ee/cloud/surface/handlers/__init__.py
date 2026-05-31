# handlers/__init__.py — One module per SurfaceKind.
#
# Created: 2026-05-24 — Sub-package boundary marker. Every handler module
# exports ``async def build_preamble(workspace_id, user_id, meta) -> str``
# returning the XML-ish preamble block the agent reads. The service-layer
# registry maps SurfaceKind -> handler.build_preamble.
#
# Shared helper ``_helpers.truncate`` lives here so handlers don't have
# to re-implement the 1500-char preamble cap.

from __future__ import annotations

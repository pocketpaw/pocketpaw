# Browser automation module for PocketPaw
# Changes: 2026-09-06 (BR-1, feat/browser-surface-server) — dropped the
#   ``AccessibilityNode`` / ``SnapshotGenerator`` exports. Playwright removed
#   ``page.accessibility``, so both were dead code; snapshots now come from a
#   DOM walk (``SNAPSHOT_JS`` + ``render_snapshot``). ``RefMap`` is unchanged.
#
# This module provides Playwright-based browser automation with semantic
# DOM snapshots for AI agent control.
"""Browser automation module for PocketPaw."""

from .driver import BrowserDriver, NavigationResult
from .session import BrowserSession, BrowserSessionManager, get_browser_session_manager
from .snapshot import SNAPSHOT_JS, RefMap, render_snapshot

__all__ = [
    # Snapshot
    "RefMap",
    "SNAPSHOT_JS",
    "render_snapshot",
    # Driver
    "BrowserDriver",
    "NavigationResult",
    # Session
    "BrowserSession",
    "BrowserSessionManager",
    "get_browser_session_manager",
]

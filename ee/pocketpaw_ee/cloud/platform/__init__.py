"""Platform operator surface — cross-tenant routes for Paw Admin.

Created: 2026-09-14 (feat/platform-authority-axis) — chunk 1 of the Paw Admin PRD.

See ``router.py`` for why this is its own namespace and what that obliges.
"""

from __future__ import annotations

from pocketpaw_ee.cloud.platform import audit
from pocketpaw_ee.cloud.platform.router import router

__all__ = ["audit", "router"]

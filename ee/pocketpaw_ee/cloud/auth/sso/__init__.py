"""Workspace-scoped OIDC SSO (Wave 3 Task 10).

Public surface: the ``router`` from ``sso.router``. All other modules
are implementation details — services treat the SSO package as a unit.
"""

from __future__ import annotations

from pocketpaw_ee.cloud.auth.sso.router import router

__all__ = ["router"]

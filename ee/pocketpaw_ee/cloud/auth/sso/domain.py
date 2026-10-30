"""SSO domain — value objects for the service layer.

Re-uses the embedded ``SsoConfig`` pydantic from ``models.workspace`` —
it is plain pydantic, not a Beanie doc, so it doubles as both the
embedded document field and the service-layer value type.
"""

from __future__ import annotations

from pocketpaw_ee.cloud.models.workspace import SsoConfig

__all__ = ["SsoConfig"]

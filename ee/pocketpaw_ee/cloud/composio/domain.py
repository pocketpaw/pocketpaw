"""Composio domain — value objects with multi-tenancy baked in.

Per ``ee/pocketpaw_ee/cloud`` Rule 3: domain objects are frozen and enforce tenancy at
construction. A ``ComposioUserId`` cannot exist without both its namespace
prefix (``enterprise_id``) and the per-user ``user_id`` segment, so a
mistakenly-empty user_id won't silently address the wrong tenant downstream.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComposioUserId:
    """Namespaced Composio user_id: ``f"{enterprise_id}:{user_id}"``.

    The namespace prefix is required at construction. This prevents
    user_id collisions when a single Composio org serves multiple
    PocketPaw enterprise deployments (one prod tenant + one staging
    tenant on the same Composio account, for instance).

    Use ``ComposioUserId.from_ctx(ctx)`` from service code; do not
    construct directly with raw strings outside the service layer.
    """

    enterprise_id: str
    user_id: str

    def __post_init__(self) -> None:
        if not self.enterprise_id:
            raise ValueError("ComposioUserId.enterprise_id is required")
        if not self.user_id:
            raise ValueError("ComposioUserId.user_id is required")
        if ":" in self.enterprise_id:
            raise ValueError(
                "ComposioUserId.enterprise_id must not contain ':' (namespace separator)"
            )

    def __str__(self) -> str:
        return f"{self.enterprise_id}:{self.user_id}"

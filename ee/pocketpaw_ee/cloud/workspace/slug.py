"""Workspace slug rules — single source of truth.

New file. Centralizes the slug format regex and the reserved-handle set so
the create-request validator (``dto.py``), the availability service
(``service.py``), and the paw-enterprise client (which mirrors these rules
in ``src/lib/core/workspaces/schemas.ts``) all agree on what a legal,
claimable slug is. Pure module — no DB, no Beanie imports — so it stays
cheap to import anywhere.
"""

from __future__ import annotations

import re
from typing import Literal

# Lowercase alphanumeric with internal hyphens; no leading/trailing hyphen.
# A single char (``x``, ``7``) is valid. Length is bounded by the DTO Field
# constraint (1..50), not the regex.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")

# Handles that collide with reserved routes / hostnames, or would be
# confusing as a tenant slug. Lowercase only — slugs are already lowercased
# by the format rule before this set is consulted.
RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        "about",
        "account",
        "accounts",
        "admin",
        "api",
        "app",
        "apps",
        "assets",
        "auth",
        "billing",
        "blog",
        "cdn",
        "contact",
        "dashboard",
        "dns",
        "docs",
        "email",
        "ftp",
        "health",
        "help",
        "internal",
        "legal",
        "login",
        "logout",
        "mail",
        "new",
        "ns",
        "null",
        "oauth",
        "paw",
        "pocket",
        "pocketpaw",
        "pockets",
        "privacy",
        "public",
        "register",
        "root",
        "security",
        "settings",
        "signin",
        "signup",
        "sso",
        "static",
        "status",
        "support",
        "system",
        "terms",
        "test",
        "undefined",
        "workspace",
        "workspaces",
        "www",
    }
)

# Why a slug can't be claimed. ``None`` (returned by the service) means free.
SlugReason = Literal["invalid", "reserved", "taken"]


def static_slug_reason(slug: str) -> SlugReason | None:
    """Format + reserved-word check, without touching the DB.

    Returns ``"invalid"`` for a malformed slug, ``"reserved"`` for a taken
    handle, or ``None`` if it passes the static gates (uniqueness is a
    separate DB check, layered on top by the service).
    """
    if not SLUG_RE.match(slug):
        return "invalid"
    if slug in RESERVED_SLUGS:
        return "reserved"
    return None


__all__ = ["RESERVED_SLUGS", "SLUG_RE", "SlugReason", "static_slug_reason"]

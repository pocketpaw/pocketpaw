# Connectors — pure surface-profile derivation (M3 connector→skill authoring).
# Created: 2026-06-07 — Given the set of ENABLED pocket-scoped connectors for a
#   pocket (as cloud-domain ``AvailableConnector`` rows), fold each connector's
#   ``surface_profile`` contribution into ONE ``PocketSurfaceProfile``: skill_names
#   = union of each connector's ``skill``; allowed_sdk_tools = union of each
#   ``allow_tools``; deny_mcp_tool_ids = union of each ``deny_tools``. Connectors
#   with no ``surface_profile`` block contribute nothing.
#
#   PURE — no I/O, no Beanie. The caller (``connectors.service.enable_connector`` /
#   ``disable_connector``) loads the enabled set, calls this, then hands the result
#   to ``pockets.service.apply_derived_surface_profile`` for the Beanie write
#   (cloud Beanie-write boundary — connectors never touch the Pocket doc).
#
#   DETERMINISTIC + IDEMPOTENT: re-deriving from the full enabled set yields the
#   same profile (sorted lists, set-deduped). Owns ONLY the connector-contributed
#   dims; ``ripple_mode`` and ``system_message_override`` are left ``None`` here so
#   the caller can preserve any user-owned values already on the pocket.

from __future__ import annotations

from collections.abc import Iterable

from pocketpaw_ee.cloud.connectors.domain import AvailableConnector
from pocketpaw_ee.cloud.surface.domain import PocketSurfaceProfile


def derive_surface_profile(
    connectors: Iterable[AvailableConnector],
) -> PocketSurfaceProfile | None:
    """Union the surface-profile contributions of a pocket's enabled connectors.

    ``connectors`` is the set of connectors enabled at scope=pocket for ONE
    pocket (already tenant-filtered + merged with their registry defs, so each
    carries its ``surface_profile`` contribution). Order-independent.

    Returns ``None`` when NO connector contributes anything (empty set, or only
    connectors with no ``surface_profile`` block) — the "clear the connector-
    derived dims" signal. Otherwise returns a ``PocketSurfaceProfile`` carrying
    only the connector-owned dims (``skill_names`` / ``allowed_sdk_tools`` /
    ``deny_mcp_tool_ids``); ``ripple_mode`` and ``system_message_override`` stay
    ``None`` so the persistence layer can keep the user's values.
    """
    skills: set[str] = set()
    allow: set[str] = set()
    deny: set[str] = set()

    for c in connectors:
        sp = c.surface_profile
        if sp is None:
            continue
        if sp.skill:
            skills.add(sp.skill)
        allow.update(sp.allow_tools)
        deny.update(sp.deny_tools)

    if not skills and not allow and not deny:
        return None

    return PocketSurfaceProfile(
        skill_names=sorted(skills),
        # ``allowed_sdk_tools=None`` means "no SDK-tool restriction"; only emit a
        # concrete list when a connector actually contributed allow patterns, so
        # an empty list never accidentally narrows the allowlist.
        allowed_sdk_tools=sorted(allow) if allow else None,
        deny_mcp_tool_ids=sorted(deny),
    )


__all__ = ["derive_surface_profile"]

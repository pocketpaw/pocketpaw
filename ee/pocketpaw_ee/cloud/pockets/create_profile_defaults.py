# create_profile_defaults.py — pure create-time surface_profile derivation.
#
# Created: 2026-06-08 (M3 v2 — create-time derivation trigger) — adds the
#   SECOND surface_profile derivation trigger. M3 v1 derives a pocket's
#   ``surface_profile`` when a CONNECTOR is bound (see
#   ``connectors/derivation.py`` + ``pockets.service.apply_derived_surface_profile``).
#   v2 derives a sensible DEFAULT at pocket CREATE time from the pocket's
#   ``type`` / ``pattern`` — but ONLY when the caller didn't supply an explicit
#   profile (that check lives in ``pockets.service.create``, not here).
#
#   PURE — no I/O, no Beanie. ``pockets.service.create`` calls this with
#   ``body.type`` / ``body.pattern`` and, if it returns a profile, stamps it on
#   the new pocket doc. The connector-bind re-derivation (v1) composes ON TOP of
#   whatever is stamped here (it owns the connector dims and preserves the rest).
#
#   CONSERVATIVE BY DESIGN. The mapping table below is intentionally near-empty:
#   it only carries entries whose default ADDS something the surface-kind default
#   doesn't already give. Today no type/pattern qualifies (see the table comment
#   for the sites analysis), so the table is empty and every input returns
#   ``None`` (= "no entity override; inherit the surface-kind default"). The
#   MECHANISM is fully wired and tested so a future product policy can add a row
#   without re-plumbing.

from __future__ import annotations

from collections.abc import Callable

from pocketpaw_ee.cloud.surface.domain import PocketSurfaceProfile

# ---------------------------------------------------------------------------
# Create-time default mapping table.
#
# Each rule maps a (pocket_type, pattern) shape to a PocketSurfaceProfile the
# pocket should be BORN with. A value of ``None`` for either side of the key is
# a wildcard ("any"); the FIRST matching rule wins (most-specific first).
#
# WHY THE TABLE IS EMPTY TODAY — the sites analysis:
#   The obvious candidate is ``type="site"`` / ``pattern="landing"`` (marketing
#   landing pages). But the surface-profile a site gets is resolved from the
#   SURFACE the user is chatting on, not from the pocket's type:
#     * On the /sites surface, ``surface.service.resolve_profile`` already
#       returns ``_DEFAULT_PROFILE`` (``ripple_mode="on"``) for the ripple-create
#       and refine modes. Only the SVELTE-create mode turns ripple off, and that
#       is keyed on the per-turn ``meta.engine == "svelte"`` signal — NOT a
#       property of the pocket.
#     * On the /pockets/[id] surface a site pocket also gets ``_DEFAULT_PROFILE``
#       (ripple on).
#   ``surface.service.compose_entity_profile`` folds an entity override OVER that
#   base with ``ripple_mode`` entity-wins-WHEN-SET. So stamping
#   ``ripple_mode="on"`` here would be a redundant no-op ("on" over "on"), and
#   stamping ``ripple_mode="off"`` would be WRONG — ripple-track sites genuinely
#   author/edit a ripple spec. The surface default fully covers sites today.
#   => return None for sites; do NOT duplicate the surface default.
#
#   No other type/pattern has a clear, safe create-time default either, so the
#   table stays empty pending an explicit product policy. To add one later:
#   append a rule tuple below — the mechanism + tests already prove it's wired.
# ---------------------------------------------------------------------------

# A rule is ((type_or_None, pattern_or_None), PocketSurfaceProfile-factory).
# Kept as a list of (key, factory) so a profile is only constructed on a match.
# Intentionally empty today (see the sites analysis above).
_CREATE_TIME_RULES: list[
    tuple[tuple[str | None, str | None], Callable[[], PocketSurfaceProfile]]
] = []


def _matches(
    rule_key: tuple[str | None, str | None],
    pocket_type: str | None,
    pattern: str | None,
) -> bool:
    """A rule key matches when each non-``None`` side equals the input side.

    A ``None`` in the rule key is a wildcard. ``("site", None)`` matches any
    pattern for ``type="site"``; ``(None, "landing")`` matches any type with
    ``pattern="landing"``.
    """
    want_type, want_pattern = rule_key
    if want_type is not None and want_type != pocket_type:
        return False
    if want_pattern is not None and want_pattern != pattern:
        return False
    return True


def derive_create_time_profile(
    pocket_type: str | None, pattern: str | None
) -> PocketSurfaceProfile | None:
    """Derive a create-time default ``surface_profile`` from type / pattern.

    PURE — no I/O. Returns the profile for the FIRST matching rule in
    ``_CREATE_TIME_RULES`` (most-specific rules listed first), or ``None`` when
    nothing matches — which means "no entity override; inherit the surface-kind
    default." The table is intentionally conservative (empty today; see the
    module docstring's sites analysis), so this returns ``None`` for every input
    until a product policy adds a rule.

    The caller (``pockets.service.create``) only invokes this when the caller
    DIDN'T supply an explicit ``surface_profile`` — an explicit caller value is
    always respected and never overridden.
    """
    for rule_key, factory in _CREATE_TIME_RULES:
        if _matches(rule_key, pocket_type, pattern):
            return factory()  # pragma: no cover — no rules today.
    return None


__all__ = ["derive_create_time_profile"]

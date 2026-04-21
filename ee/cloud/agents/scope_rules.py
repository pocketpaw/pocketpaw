# ee/cloud/agents/scope_rules.py — Scope validation + assignment authorisation.
# Created: 2026-04-19 (feat/cluster-d-agent-scope-picker) — Shared helpers
# used by the agents schema + the PATCH /agents/{id}/scope endpoint. Mirrors
# the frontend ``src/lib/scope/normalise.ts`` rules so a malformed or
# escalation-shaped scope coming through REST is rejected on the server even
# when the ScopePicker was bypassed (direct API call, curl, another client).
#
# Scope grammar (same as frontend normalise.ts):
#   - lowercase, colon-separated segments matching ``[a-z0-9]+``
#   - ``*`` is only legal as the final segment (or the whole scope)
#   - empty segments (``::`` or leading ``:``) are rejected
#   - duplicates are collapsed
#
# The rule set is deliberately conservative: the endpoint refuses the
# universal wildcard ``*`` entirely (see ``FORBIDDEN_SCOPES``) so a fleet
# admin cannot accidentally grant an agent access to *all* workspaces.
# Wildcards with at least one namespace segment (``org:sales:*``) are fine.

from __future__ import annotations

import re

_SEGMENT_PATTERN = re.compile(r"^[a-z0-9]+$")

# Scopes explicitly disallowed on assignment. The universal wildcard would
# let an agent read every workspace's memories and fabric objects — we only
# allow that via intentional infra-level config, never from the admin UI.
FORBIDDEN_SCOPES = frozenset({"*"})


class ScopeValidationError(ValueError):
    """Raised when a scope tag violates the grammar or escalation rules.

    Pydantic surfaces this as a 422 with a readable ``detail`` — the REST
    router inherits FastAPI's default handler so we do not need to catch
    it explicitly at the endpoint level.
    """


def _validate_one(raw: str) -> str:
    """Normalise + validate a single scope tag. Returns the cleaned form
    (lowercase, stripped) or raises :class:`ScopeValidationError`.
    """
    cleaned = raw.strip().lower()
    if not cleaned:
        raise ScopeValidationError("Scope cannot be empty")
    if cleaned in FORBIDDEN_SCOPES:
        raise ScopeValidationError(
            f"Scope '{cleaned}' is not assignable from the admin UI — "
            "use a namespaced wildcard instead (e.g. org:sales:*)"
        )
    segments = cleaned.split(":")
    for idx, seg in enumerate(segments):
        if not seg:
            raise ScopeValidationError(f"Scope '{raw}' has an empty segment")
        if seg == "*":
            # ``*`` is only legal as the terminal segment.
            if idx != len(segments) - 1:
                raise ScopeValidationError(
                    f"Scope '{raw}' uses a mid-segment wildcard; '*' is only "
                    "legal as the final segment"
                )
            continue
        if not _SEGMENT_PATTERN.match(seg):
            raise ScopeValidationError(
                f"Scope '{raw}' has an invalid segment '{seg}' — "
                "segments must match [a-z0-9]+"
            )
    return cleaned


def normalise_and_validate_scopes(raw: list[str]) -> list[str]:
    """Validate + dedupe a list of scope tags, preserving order.

    Called from Pydantic validators on ``scopes`` fields. Raises
    :class:`ScopeValidationError` on the first malformed tag so the API
    response pinpoints the offender instead of silently dropping it.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw_scope in raw:
        if not isinstance(raw_scope, str):
            raise ScopeValidationError(f"Scope must be a string, got {type(raw_scope).__name__}")
        cleaned = _validate_one(raw_scope)
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def admin_can_assign_scopes(
    admin_scopes: list[str] | None, requested_scopes: list[str]
) -> bool:
    """Return True when ``admin_scopes`` covers every entry in
    ``requested_scopes`` by hierarchical containment.

    Containment follows the same rules as the soul-protocol scope matcher:
    ``org:sales:*`` contains ``org:sales`` and ``org:sales:leads``. An
    empty ``admin_scopes`` list is treated as "admin has no explicit
    scope narrowing configured, can assign anything non-forbidden" —
    that covers the current paw-enterprise deployment where workspace
    admins have an implicit workspace-wide scope until the scope-per-user
    model lands (tracked separately). The test suite pins this fallback
    so a future narrowing doesn't silently regress.
    """
    if not requested_scopes:
        return True
    if not admin_scopes:
        # No narrowing configured — admin sits at workspace root.
        return True
    for wanted in requested_scopes:
        if not any(_scope_covers(granted, wanted) for granted in admin_scopes):
            return False
    return True


def _scope_covers(granted: str, wanted: str) -> bool:
    """True when ``granted`` contains ``wanted`` by hierarchical glob.

    Duplicates the narrow semantic we need without pulling the full
    soul-protocol matcher — that one is bidirectional, which is wrong for
    "can this admin assign that scope to another actor". Here we want a
    strict "admin's grant includes the target" check.
    """
    if granted == wanted:
        return True
    if granted == "*":
        return True
    if granted.endswith(":*"):
        prefix = granted[:-2]
        return wanted == prefix or wanted.startswith(prefix + ":")
    return False

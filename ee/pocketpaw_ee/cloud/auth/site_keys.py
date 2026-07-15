# ee/pocketpaw_ee/cloud/auth/site_keys.py — resolve a public Paw Bar embed key
# (Site.signed_key) into a scoped RequestContext.
#
# Created 2026-07-14 (Paw Bar concierge seam, T1): the sibling of ``api_keys.py``
# for a DIFFERENT credential model. ``api_keys`` holds an argon2-HASHED SECRET
# bearer token (``paw_...``) — never world-visible. This module resolves the Site
# ``signed_key`` — a world-visible, origin-bound EMBED key baked into a foreign
# site's public HTML (the Stripe-publishable-key model). So it is stored + compared
# in PLAINTEXT and its real security controls are: the per-site origin allowlist,
# a revocation kill switch, and a narrow scope set — NOT secrecy. We deliberately
# do NOT fork the api_keys hashing path (wrong model for a public key) and do NOT
# mint a parallel key store: the Site's existing ``signed_key`` + ``allowed_origins``
# ARE the credential (RFC 12 capture reused them first; the concierge reuses them
# again).
#
# ``resolve_site_key`` generalizes the per-request check the leads capture path runs
# inline (``leads/router.py`` — origin_allowed → constant-time key compare), reusing
# the SAME primitives (``sites_capture.ingest.origin_allowed`` +
# ``secrets.compare_digest``). It differs from the leads path in ONE way: leads is
# handed the site id (``script_name``) in the URL and the key in the body, so it
# looks the Site up by id; the concierge is handed ONLY the embed key, so it looks
# the Site up BY THE KEY. That is why this works for a FOREIGN site whose
# ``script_name`` is "" (we never generated a Worker for it) — the lookup does not
# depend on ``script_name`` at all. The leads path is left untouched (its id-keyed
# lookup + inline checks still stand); this is the concierge-facing resolver.
#
# Updated 2026-07-14 (adversarial review follow-up): (FIX 2) guard the key with an
# explicit ``isinstance(str)`` before it reaches ``find_one`` — this is THE public
# credential value, so a smuggled ``{"$ne": ""}`` operator dict is rejected 401,
# not leaned on ``len`` to block; (FIX 3) copy the scope list null-safe
# (``site.scopes or []``) so a Mongo doc with ``scopes: null`` can't TypeError.
#
# Updated 2026-07-15 (Paw Bar glass frame, A1): the key→Site lookup + the context
# build were factored OUT of ``resolve_site_key`` so the new iframe FRAME endpoint
# (``paw_bar/router.py::frame``) can reuse the SAME credential path without forking
# it. (1) ``lookup_site_by_key`` runs the isinstance / min-len / find_one / revoked
# / constant-time-compare chain and returns the live Site — NO origin gate (the
# frame path gates the embedder with a CSP ``frame-ancestors`` header at render
# time, not a per-request Origin check). (2) ``_context_from_site`` builds the
# CONCIERGE ``RequestContext`` (the old tail of ``resolve_site_key``). (3)
# ``resolve_site_key`` now takes an optional ``frame_origin``: with it unset the
# behavior is byte-identical to before (inline widget → fail-closed
# ``origin_allowed`` gate); when the request ``Origin`` equals our configured frame
# origin, the embedder was ALREADY gated by the frame CSP, so the per-request
# origin gate degenerates to "is this our frame" and the ``allowed_origins`` check
# is skipped. This is the origin-model shift the glass bar rides on. The residual
# is unchanged: the key is world-visible, so a raw ``curl`` POST was always
# possible — CSP binds BROWSERS only; the real controls stay the rate-limit +
# injection screen + the zero-authority CONCIERGE scope.

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from pocketpaw.sites_capture.ingest import origin_allowed
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

# The lookup is ``find_one({"signed_key": key})``. Most Site docs carry the model
# default ``signed_key=""`` (never published, or published before a key was seeded),
# so a BLANK or absurdly short key MUST be rejected BEFORE the query — otherwise an
# empty key would match a real, unrelated Site row and (worse) ``compare_digest("",
# "")`` returns True. A real embed key is ``site_key_`` (9) + ``token_urlsafe(24)``
# (~32) ≈ 41 chars; 16 is comfortably above "" / short junk and below a real key, so
# it rejects the dangerous inputs without being brittle to a future token-length
# tweak. This is the single most important guard in the file.
_MIN_SITE_KEY_LEN = 16


def _normalize_origin(value: str | None) -> str:
    """Reduce a value to a comparable ``scheme://host[:port]`` origin.

    Lowercased, trailing slash + any path dropped. Used to compare a request's
    ``Origin`` header against the configured frame origin exactly (scheme + host +
    port), a STRICTER match than the host-only ``origin_allowed`` — "is this our
    frame" must not be satisfied by a mere host collision under a different scheme.
    """
    if not value:
        return ""
    v = value.strip().lower().rstrip("/")
    if "://" in v:
        scheme, rest = v.split("://", 1)
        host = rest.split("/", 1)[0]
        return f"{scheme}://{host}"
    return v


def _is_same_origin(a: str | None, b: str | None) -> bool:
    """True when both normalize to the same non-empty ``scheme://host[:port]``."""
    na, nb = _normalize_origin(a), _normalize_origin(b)
    return bool(na) and na == nb


async def lookup_site_by_key(key: str) -> _SiteDoc:
    """Authenticate a public embed key to its live Site — the SHARED key path.

    Runs the full fail-closed credential chain and returns the Site doc, WITHOUT
    any origin gate. Both callers reuse it: ``resolve_site_key`` (the concierge
    chat path, which adds the origin gate) and the FRAME endpoint (which gates the
    embedder with a CSP ``frame-ancestors`` header at render time instead of a
    per-request Origin check). Keeping the chain in one place means the
    isinstance / min-len / revoked / constant-time-compare guards can never drift
    between the two entry points.

    Order (every rejection is a 401 so an attacker can't enumerate which keys are
    live — bad / unknown / revoked / mismatched are indistinguishable):

      1. Non-str or too-short key → 401. ``isinstance`` FIRST so a smuggled Mongo
         operator dict (``{"$ne": ""}``) never reaches ``find_one``; the min-len
         guard then blocks the ``signed_key=""`` DB collision (see
         ``_MIN_SITE_KEY_LEN``).
      2. Unknown key → 401.
      3. Revoked key → 401 (the kill switch — a leaked key is cut off without
         deleting the Site).
      4. Constant-time mismatch → 401. Redundant against the exact-match query
         today, kept as defense-in-depth + shape-parity with the leads path.
    """
    if not isinstance(key, str) or len(key) < _MIN_SITE_KEY_LEN:
        raise HTTPException(status_code=401, detail="invalid_site_key")

    site = await _SiteDoc.find_one({"signed_key": key})
    if site is None:
        raise HTTPException(status_code=401, detail="invalid_site_key")

    if site.revoked:
        raise HTTPException(status_code=401, detail="invalid_site_key")

    # Constant-time compare so the key check can't be probed via timing, and so the
    # gate holds even if the lookup above is ever loosened. Redundant against the
    # exact-match query today, kept as defense-in-depth + shape-parity with leads.
    if not secrets.compare_digest(key, site.signed_key):
        raise HTTPException(status_code=401, detail="invalid_site_key")

    return site


def _context_from_site(site: _SiteDoc, customer_ref: str) -> RequestContext:
    """Build the CONCIERGE ``RequestContext`` a resolved embed key stands for.

    Bound to the Site's tenant + pocket, carrying a COPY of the Site's ``scopes``
    (null-safe) and stamping the anonymous ``customer_ref`` as ``user_id`` — the
    concierge is never a signed-in principal. ``scope=CONCIERGE`` so downstream
    gates treat it as the world-visible, non-session credential it is.
    """
    return RequestContext(
        user_id=customer_ref,
        workspace_id=site.workspace,
        request_id=uuid4().hex,
        scope=ScopeKind.CONCIERGE,
        started_at=datetime.now(UTC),
        # Copy the list so the frozen context never aliases the live model's
        # field; ``or []`` keeps it null-safe if a Mongo doc stored ``scopes: null``.
        scopes=list(site.scopes or []),
        pocket_id=site.pocket_id,
    )


async def resolve_site_key(
    key: str,
    origin: str | None,
    customer_ref: str,
    *,
    frame_origin: str | None = None,
) -> RequestContext:
    """Resolve a Paw Bar embed key into a CONCIERGE-scoped :class:`RequestContext`.

    Fail-closed, in order — every rejection is an :class:`HTTPException` (matching
    the ``request_context`` auth-resolver precedent in ``_core/context.py``), so a
    router can surface it directly:

      1-4. **Key auth** (``lookup_site_by_key``): empty/short → 401, unknown → 401,
         revoked → 401, constant-time mismatch → 401.
      5. **Origin gate — dual-mode.**
         * ``frame_origin`` unset (inline / legacy widget): the request ``Origin``
           IS the embedder, so ``origin_allowed`` must place its host in the Site's
           ``allowed_origins`` — fail-closed on an empty allowlist or missing
           ``Origin``. This is byte-identical to the pre-frame behavior.
         * ``frame_origin`` set AND ``origin`` equals it (iframe / glass bar): the
           embedder was ALREADY gated by the frame's CSP ``frame-ancestors`` header
           at render time, and every chat request from the iframe carries OUR frame
           origin (identical for all embedders), so the per-request origin gate
           degenerates to "is this our frame" — which the equality check confirms.
           The ``allowed_origins`` check is skipped (it would reject our own frame).
         * ``frame_origin`` set but ``origin`` does NOT equal it: fall back to the
           inline ``allowed_origins`` gate, so a request that merely CLAIMS to be
           the frame but isn't must still be an allowlisted embedder.

    On success the returned context is bound to the Site's tenant + pocket, carries
    the Site's ``scopes`` (what a concierge request may do), and stamps the caller's
    ``customer_ref`` as ``user_id`` — the concierge is NOT a signed-in user, so
    ``user_id`` is the anonymous customer handle the widget minted, never a real
    account id. ``scope`` is ``CONCIERGE`` so downstream gates treat it as the
    world-visible, non-session credential it is.

    Args:
        key: The public embed key presented by the widget (``Site.signed_key``).
        origin: The request's ``Origin`` header (host is matched against the
            Site's ``allowed_origins`` in inline mode).
        customer_ref: The anonymous, widget-minted customer handle to record as
            the request's ``user_id``.
        frame_origin: OUR configured iframe origin (``PAWBAR_FRAME_ORIGIN``, or the
            request's own origin by default). When the request ``Origin`` matches
            it, the embedder was gated by the frame CSP and the ``allowed_origins``
            check is skipped. ``None`` keeps the pure inline behavior.

    Returns:
        A ``RequestContext`` with ``scope=CONCIERGE``, ``workspace_id`` +
        ``pocket_id`` from the Site, ``scopes`` copied from the Site.

    Raises:
        HTTPException: 401 (bad/unknown/revoked/mismatched key) or 403 (origin not
            allowed). Deliberately no distinct code for "does not exist" vs "wrong
            key" beyond the status — both are 401 so an attacker can't enumerate
            which embed keys are live.
    """
    site = await lookup_site_by_key(key)

    # Dual-mode origin gate. Frame mode (request Origin == our frame origin) means
    # the embedder was already gated by the frame CSP, so we accept; otherwise the
    # request Origin is the embedder itself and must be on the Site's fail-closed
    # allowlist. ``origin_allowed`` rejects an empty allowlist / missing Origin.
    if frame_origin is not None and _is_same_origin(origin, frame_origin):
        pass
    elif not origin_allowed(site.allowed_origins, origin):
        raise HTTPException(status_code=403, detail="origin_not_allowed")

    return _context_from_site(site, customer_ref)


__all__ = ["lookup_site_by_key", "resolve_site_key"]

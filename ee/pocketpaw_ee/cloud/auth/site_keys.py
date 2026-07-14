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


async def resolve_site_key(
    key: str,
    origin: str | None,
    customer_ref: str,
) -> RequestContext:
    """Resolve a Paw Bar embed key into a CONCIERGE-scoped :class:`RequestContext`.

    Fail-closed, in order — every rejection is an :class:`HTTPException` (matching
    the ``request_context`` auth-resolver precedent in ``_core/context.py``), so a
    router can surface it directly:

      1. **Empty / too-short key → 401.** Guards the ``signed_key=""`` collision
         (see ``_MIN_SITE_KEY_LEN``) before any DB read.
      2. **Unknown key → 401.** ``find_one`` returns nothing.
      3. **Revoked key → 401.** The kill switch; a leaked key is cut off without
         deleting the Site.
      4. **Disallowed / missing origin → 403.** ``origin_allowed`` fails closed on
         an empty allowlist or a missing ``Origin`` header — the embed key is only
         valid from the origins the owner allowlisted.
      5. **Key mismatch → 401.** Constant-time ``secrets.compare_digest``. With the
         exact-match lookup above this is defense-in-depth (and keeps shape-parity
         with the leads path); it becomes the real gate if the lookup is ever
         changed to a prefix/range scan.

    On success the returned context is bound to the Site's tenant + pocket, carries
    the Site's ``scopes`` (what a concierge request may do), and stamps the caller's
    ``customer_ref`` as ``user_id`` — the concierge is NOT a signed-in user, so
    ``user_id`` is the anonymous customer handle the widget minted, never a real
    account id. ``scope`` is ``CONCIERGE`` so downstream gates treat it as the
    world-visible, non-session credential it is.

    Args:
        key: The public embed key presented by the widget (``Site.signed_key``).
        origin: The request's ``Origin`` header (host is matched against the
            Site's ``allowed_origins``).
        customer_ref: The anonymous, widget-minted customer handle to record as
            the request's ``user_id``.

    Returns:
        A ``RequestContext`` with ``scope=CONCIERGE``, ``workspace_id`` +
        ``pocket_id`` from the Site, ``scopes`` copied from the Site.

    Raises:
        HTTPException: 401 (bad/unknown/revoked/mismatched key) or 403 (origin not
            allowed). Deliberately no distinct code for "does not exist" vs "wrong
            key" beyond the status — both are 401 so an attacker can't enumerate
            which embed keys are live.
    """
    # ``isinstance`` FIRST: this value flows straight into a Mongo
    # ``find_one({"signed_key": key})``, so a non-str (e.g. a ``{"$ne": ""}``
    # operator dict smuggled through a JSON body) must be rejected outright rather
    # than reaching the query or the ``len`` check. Then the empty/short-key guard
    # (unchanged) blocks the ``signed_key=""`` DB collision.
    if not isinstance(key, str) or len(key) < _MIN_SITE_KEY_LEN:
        raise HTTPException(status_code=401, detail="invalid_site_key")

    site = await _SiteDoc.find_one({"signed_key": key})
    if site is None:
        raise HTTPException(status_code=401, detail="invalid_site_key")

    if site.revoked:
        raise HTTPException(status_code=401, detail="invalid_site_key")

    if not origin_allowed(site.allowed_origins, origin):
        raise HTTPException(status_code=403, detail="origin_not_allowed")

    # Constant-time compare so the key check can't be probed via timing, and so the
    # gate holds even if the lookup above is ever loosened. Redundant against the
    # exact-match query today, kept as defense-in-depth + shape-parity with leads.
    if not secrets.compare_digest(key, site.signed_key):
        raise HTTPException(status_code=401, detail="invalid_site_key")

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


__all__ = ["resolve_site_key"]

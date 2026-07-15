# tests/cloud/test_site_keys.py — Paw Bar concierge key resolver (T1).
# Created 2026-07-14: covers auth.site_keys.resolve_site_key end-to-end against a
# live Beanie Site (the mongo_db fixture), plus the sites.service.mint_foreign_site
# → resolve round-trip that proves the credential works for a FOREIGN site whose
# script_name is "". Fail-closed coverage: empty/short key (the signed_key="" DB
# collision guard), unknown key, revoked key, disallowed origin, missing origin,
# and a constant-time key mismatch. Also asserts scope propagation (default set +
# a narrowed override) and that user_id carries the caller's customer_ref.
# Updated 2026-07-14 (adversarial review follow-up): + a non-str key → 401 guard
# (FIX 2), and mint_foreign_site now mocks pockets_service.get with a cross-tenant
# → Forbidden + no-Site-written assertion (FIX 1).
# Updated 2026-07-15 (Paw Bar glass frame, A1): + coverage for the two extracted /
# added surfaces — ``lookup_site_by_key`` (the shared key-auth chain, no origin
# gate) and ``resolve_site_key``'s new ``frame_origin`` dual-mode gate (an Origin
# equal to our frame origin is accepted without an allowlist check; a mismatched
# Origin still falls back to the fail-closed ``allowed_origins`` gate).

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pocketpaw_ee.cloud._core.context import ScopeKind
from pocketpaw_ee.cloud.auth.site_keys import lookup_site_by_key, resolve_site_key
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service

# A well-formed embed key (>= the resolver's minimum length). The literal
# ``site_key_`` prefix matches the real minted format, but the resolver does not
# require it — length + exact match are the only rules.
_VALID_KEY = "site_key_" + "a" * 24
_ALLOWED_ORIGIN = "https://brewco.com"


async def _site(**overrides) -> Site:
    """Insert a concierge-shaped Site and return it. Defaults are a live,
    non-revoked site keyed on _VALID_KEY, allowlisting brewco.com."""
    defaults = dict(
        workspace="ws-1",
        pocket_id="pk-1",
        owner="user:maya",
        script_name="",  # foreign site — no generated Worker
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
    )
    defaults.update(overrides)
    site = Site(**defaults)
    await site.insert()
    return site


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_valid_key_builds_concierge_context(mongo_db):
    await _site()

    ctx = await resolve_site_key(_VALID_KEY, _ALLOWED_ORIGIN, "cust-42")

    assert ctx.scope is ScopeKind.CONCIERGE
    assert ctx.workspace_id == "ws-1"
    assert ctx.pocket_id == "pk-1"
    # The concierge is not a signed-in user: user_id is the widget-minted handle.
    assert ctx.user_id == "cust-42"
    # Default concierge scope set copied off the Site.
    assert ctx.scopes == ["chat", "kb.read", "event.ingest"]


@pytest.mark.asyncio
async def test_resolve_copies_narrowed_scopes(mongo_db):
    await _site(scopes=["chat"])

    ctx = await resolve_site_key(_VALID_KEY, _ALLOWED_ORIGIN, "cust-1")

    assert ctx.scopes == ["chat"]


@pytest.mark.asyncio
async def test_resolved_scopes_do_not_alias_the_model_list(mongo_db):
    """The frozen context must own a COPY of the scope list — mutating it must not
    reach back into any shared model state."""
    await _site()
    ctx = await resolve_site_key(_VALID_KEY, _ALLOWED_ORIGIN, "cust-1")
    ctx.scopes.append("tamper")  # a plain list; mutation is allowed on the copy
    # A fresh resolve still returns the untouched default set.
    ctx2 = await resolve_site_key(_VALID_KEY, _ALLOWED_ORIGIN, "cust-1")
    assert ctx2.scopes == ["chat", "kb.read", "event.ingest"]


# --------------------------------------------------------------------------- #
# Fail-closed paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rejects_disallowed_origin(mongo_db):
    await _site()
    with pytest.raises(HTTPException) as exc:
        await resolve_site_key(_VALID_KEY, "https://evil.example.com", "cust-1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_rejects_missing_origin(mongo_db):
    """A missing Origin header fails closed (origin_allowed rejects None)."""
    await _site()
    with pytest.raises(HTTPException) as exc:
        await resolve_site_key(_VALID_KEY, None, "cust-1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_rejects_revoked_key(mongo_db):
    await _site(revoked=True)
    with pytest.raises(HTTPException) as exc:
        await resolve_site_key(_VALID_KEY, _ALLOWED_ORIGIN, "cust-1")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_rejects_unknown_key(mongo_db):
    await _site()
    with pytest.raises(HTTPException) as exc:
        await resolve_site_key("site_key_" + "z" * 24, _ALLOWED_ORIGIN, "cust-1")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_rejects_empty_key(mongo_db):
    await _site()
    with pytest.raises(HTTPException) as exc:
        await resolve_site_key("", _ALLOWED_ORIGIN, "cust-1")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_empty_key_does_not_match_unpublished_signed_key(mongo_db):
    """The security landmine: an unpublished Site carries signed_key="" (the model
    default). An empty/blank key must NOT resolve against it — the min-length guard
    rejects before the query ever runs, and compare_digest("","") never gets a
    chance to return True."""
    # An unpublished site for a different pocket, default signed_key="".
    await _site(pocket_id="pk-unpublished", signed_key="", allowed_origins=["brewco.com"])
    for blank in ("", "   ", "short"):
        with pytest.raises(HTTPException) as exc:
            await resolve_site_key(blank, _ALLOWED_ORIGIN, "cust-1")
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_rejects_non_str_key(mongo_db):
    """Review FIX 2: a non-str key (e.g. a smuggled ``{"$ne": ""}`` Mongo operator
    dict, or a list) is rejected 401 by the isinstance guard — it must NEVER reach
    ``find_one`` (NoSQL injection) or raise a 500 from ``len``/``compare_digest``."""
    await _site()
    for bad in ({"$ne": ""}, {"$gt": ""}, ["site_key_aaaaaaaaaaaaaaaa"], 12345, None):
        with pytest.raises(HTTPException) as exc:
            await resolve_site_key(bad, _ALLOWED_ORIGIN, "cust-1")  # type: ignore[arg-type]
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_key_check_is_constant_time(mongo_db, monkeypatch):
    """The key comparison routes through secrets.compare_digest (constant-time),
    mirroring the leads path's H1 guarantee."""
    import pocketpaw_ee.cloud.auth.site_keys as site_keys_mod

    await _site()
    calls: list[tuple[str, str]] = []
    real = site_keys_mod.secrets.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(site_keys_mod.secrets, "compare_digest", _spy)
    ctx = await resolve_site_key(_VALID_KEY, _ALLOWED_ORIGIN, "cust-1")
    assert ctx.scope is ScopeKind.CONCIERGE
    assert (_VALID_KEY, _VALID_KEY) in calls


# --------------------------------------------------------------------------- #
# mint_foreign_site → resolve round-trip (T1.2 + T1.3 together)
#
# mint_foreign_site now runs the pockets-service ownership check first (review
# FIX 1), so these mock ``pockets_service.get`` — an OWNED pocket returns a wire
# dict; the cross-tenant test makes it raise Forbidden.
# --------------------------------------------------------------------------- #

_OWNED_POCKET = {"name": "Shop", "rippleSpec": {}}


@pytest.mark.asyncio
async def test_mint_foreign_site_then_resolve(mongo_db):
    """A foreign site (script_name="") minted by the service resolves by its
    signed_key — proving the lookup does NOT depend on script_name."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=_OWNED_POCKET),
    ) as mock_get:
        site = await sites_service.mint_foreign_site(
            workspace_id="ws-2",
            pocket_id="pk-2",
            owner="user:sam",
            # Passed with scheme+port to prove normalization to a bare host.
            allowed_origins=["https://shop.example.com:443"],
            name="Sam's Concierge",
        )
    # The ownership gate ran with (pocket_id, owner) before the insert.
    mock_get.assert_awaited_once_with("pk-2", "user:sam")
    assert site.script_name == ""
    assert site.deployed is False
    assert site.signed_key.startswith("site_key_")
    assert site.allowed_origins == ["shop.example.com"]

    ctx = await resolve_site_key(site.signed_key, "https://shop.example.com", "cust-9")
    assert ctx.scope is ScopeKind.CONCIERGE
    assert ctx.workspace_id == "ws-2"
    assert ctx.pocket_id == "pk-2"
    assert ctx.user_id == "cust-9"
    assert ctx.scopes == ["chat", "kb.read", "event.ingest"]


@pytest.mark.asyncio
async def test_mint_foreign_site_scope_override(mongo_db):
    from unittest.mock import AsyncMock, patch

    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=_OWNED_POCKET),
    ):
        site = await sites_service.mint_foreign_site(
            workspace_id="ws-3",
            pocket_id="pk-3",
            owner="user:sam",
            allowed_origins=["shop.example.com"],
            scopes=["chat"],
        )
    ctx = await resolve_site_key(site.signed_key, "https://shop.example.com", "cust-1")
    assert ctx.scopes == ["chat"]


@pytest.mark.asyncio
async def test_mint_foreign_site_denies_cross_tenant_pocket(mongo_db):
    """Review FIX 1 (HIGH): minting a concierge bound to a pocket the owner does
    NOT own is refused BY the pockets-service ownership check, and no Site is
    written — otherwise the resolved CONCIERGE context could read a victim
    pocket's KB (pocket:<victim>)."""
    from unittest.mock import AsyncMock, patch

    from pocketpaw_ee.cloud._core.errors import Forbidden

    denied = AsyncMock(side_effect=Forbidden("pocket.access_denied", "no access"))
    with patch("pocketpaw_ee.cloud.pockets.service.get", new=denied):
        with pytest.raises(Forbidden) as exc:
            await sites_service.mint_foreign_site(
                workspace_id="attacker-ws",
                pocket_id="pk-victim-xyz",
                owner="attacker",
                allowed_origins=["evil.example.com"],
            )
    assert exc.value.code == "pocket.access_denied"
    # Fail-before-write: no Site was inserted for the victim pocket.
    assert await Site.find_one({"pocket_id": "pk-victim-xyz"}) is None


# --------------------------------------------------------------------------- #
# lookup_site_by_key — the shared key-auth chain (no origin gate)  [A1]
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_lookup_site_by_key_returns_live_site(mongo_db):
    """The frame path authenticates the key WITHOUT an origin gate — a valid key
    returns the Site regardless of any Origin (the CSP gates the embedder instead)."""
    site = await _site()
    got = await lookup_site_by_key(_VALID_KEY)
    assert got.id == site.id
    assert got.signed_key == _VALID_KEY


@pytest.mark.asyncio
async def test_lookup_site_by_key_rejects_bad_keys(mongo_db):
    """Same fail-closed 401s as resolve_site_key's key chain: blank/short, unknown,
    revoked, and a non-str (NoSQL-operator) key."""
    await _site()
    for bad in ("", "   ", "short", "site_key_" + "z" * 24, {"$ne": ""}, None):
        with pytest.raises(HTTPException) as exc:
            await lookup_site_by_key(bad)  # type: ignore[arg-type]
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_lookup_site_by_key_rejects_revoked(mongo_db):
    await _site(revoked=True)
    with pytest.raises(HTTPException) as exc:
        await lookup_site_by_key(_VALID_KEY)
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------- #
# resolve_site_key frame_origin dual-mode  [A1]
# --------------------------------------------------------------------------- #

_FRAME_ORIGIN = "https://frame.pocketpaw.test"


@pytest.mark.asyncio
async def test_frame_origin_accepted_without_allowlist(mongo_db):
    """Iframe mode: a request whose Origin equals our frame origin is accepted even
    though that origin is NOT in the Site's allowed_origins — the embedder was gated
    by the frame CSP at render time."""
    await _site(allowed_origins=["brewco.com"])
    ctx = await resolve_site_key(_VALID_KEY, _FRAME_ORIGIN, "cust-1", frame_origin=_FRAME_ORIGIN)
    assert ctx.scope is ScopeKind.CONCIERGE
    assert ctx.workspace_id == "ws-1"


@pytest.mark.asyncio
async def test_frame_origin_mismatch_falls_back_to_allowlist(mongo_db):
    """A frame_origin is configured, but the request Origin is neither the frame nor
    an allowlisted embedder → the fail-closed allowlist gate still refuses (403)."""
    await _site(allowed_origins=["brewco.com"])
    with pytest.raises(HTTPException) as exc:
        await resolve_site_key(
            _VALID_KEY, "https://evil.example", "cust-1", frame_origin=_FRAME_ORIGIN
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_frame_origin_none_is_pure_inline_behavior(mongo_db):
    """Default (frame_origin=None): behavior is byte-identical to before A1 — the
    allowlisted embedder passes, a disallowed one is 403."""
    await _site(allowed_origins=["brewco.com"])
    ctx = await resolve_site_key(_VALID_KEY, "https://brewco.com", "cust-1")
    assert ctx.scope is ScopeKind.CONCIERGE
    with pytest.raises(HTTPException) as exc:
        await resolve_site_key(_VALID_KEY, "https://evil.example", "cust-1")
    assert exc.value.status_code == 403

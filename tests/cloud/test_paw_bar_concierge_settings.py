# tests/cloud/test_paw_bar_concierge_settings.py — Paw Bar concierge settings +
# kill switch (D1 / SS-6).
# Created 2026-07-16: covers the owner's on/off toggle + greeting. Layers:
#   * The shared resolver (resolve_site_key) — a disabled Site raises 403
#     ``concierge_disabled`` before the origin gate; an enabled Site resolves a ctx.
#   * The three public entry points fail closed on the kill switch: GET
#     /paw-bar/frame, POST /paw-bar/chat, POST /paw-bar/action each 403 when
#     ``concierge_enabled=False``, while a default (enabled=True) Site renders.
#   * The greeting rides into the frame's ``window.__PAWBAR__`` config payload.
#   * The admin settings surface: GET + PATCH /paw-bar/admin/site/{id}/settings
#     round-trips the two fields, rejects a cross-tenant id (404), and a PATCH that
#     flips the switch off silences the frame on the NEXT request (immediate effect).
# Updated 2026-07-26 (concierge transcripts): a fifth layer covers the
#   transcript-retention toggle — exposed on GET, writable via PATCH, defaults on,
#   and fully independent of the kill switch in both directions.
# Updated 2026-08-16 (fix/paw-bar-role-gates): the two admin settings routes moved
#   off ``require_scope("admin")`` onto the ``paw_bar.read`` / ``paw_bar.manage``
#   ROLE gate, so the app fixture now pins the caller's workspace ROLE (admin) and
#   a sixth layer covers the gate itself: admin/owner can PATCH the kill switch
#   (the reported bug — a cloud workspace admin used to get 403 "Missing required
#   scope: admin", because the scope gate only accepts a full-access dashboard
#   session / pp_ API key / ppat_ token and a workspace admin holds none of them),
#   a member is refused on both the PATCH and the GET with nothing written, and a
#   widget DELETE shows the same split even when the caller holds the per-widget
#   owner token.

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import PawBarBlock, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

_VALID_KEY = "site_key_" + "a" * 24
_ORIGIN = "https://brewco.com"
_CUST = "cust-0001"  # a valid customer_ref (>= 8 chars, allowed charset)


async def _site(**ov: Any):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        script_name="",
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


def _spec(pocket_id: str = "pocket-1") -> PawBarSpec:
    return PawBarSpec(
        widget_id="pp_seed",
        pocket_id=pocket_id,
        blocks=[PawBarBlock(type="text", content="Hi from Brew & Co")],
    )


def _widget(**ov: Any) -> PawBarWidget:
    d = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=_spec(),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
        rate_limit_per_min=60,
        per_customer_limit_per_min=10,
    )
    d.update(ov)
    return PawBarWidget(**d)


def _build_app(role: str = "admin") -> FastAPI:
    """Mount the paw_bar router with the caller's workspace ROLE pinned to ws-1.

    The two admin settings routes gate on ``paw_bar.read`` (GET) /
    ``paw_bar.manage`` (PATCH), so the role stand-in is what decides 200 vs 403;
    the public frame/chat/action routes on the same router ignore it entirely.
    """
    from pocketpaw_ee.paw_bar.router import router

    from tests.cloud.conftest import override_workspace_role

    app = FastAPI()
    app.include_router(router)
    override_workspace_role(app, role=role, workspace_id="ws-1")
    return app


@pytest_asyncio.fixture
async def client(tmp_path, mongo_db):
    """One public+admin app client for every D1 endpoint, backed by a tmp paw_bar
    store (widget) + Beanie (Site). The caller is a ws-1 ADMIN, so the admin
    settings routes clear their role gate; the public frame/chat/action routes
    ignore both the role and the workspace. Yields ``(client, store)``.
    """
    from unittest.mock import patch

    app = _build_app(role="admin")
    store = PawBarStore(tmp_path / "settings.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c, store


# --------------------------------------------------------------------------- #
# Layer 1 — the shared resolver kill switch (resolve_site_key)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_site_key_disabled_concierge_is_403(mongo_db):
    """A resolved Site with concierge_enabled=False refuses (403 concierge_disabled)
    at the shared resolver — the single gate chat/action/cart all pass through."""
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key

    await _site(concierge_enabled=False)
    with pytest.raises(HTTPException) as exc:
        await resolve_site_key(_VALID_KEY, _ORIGIN, _CUST)
    assert exc.value.status_code == 403
    assert exc.value.detail == "concierge_disabled"


@pytest.mark.asyncio
async def test_resolve_site_key_enabled_concierge_resolves(mongo_db):
    """The default (enabled=True) Site still resolves a CONCIERGE ctx — no regression."""
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key

    await _site()  # concierge_enabled defaults True
    ctx = await resolve_site_key(_VALID_KEY, _ORIGIN, _CUST)
    assert ctx.workspace_id == "ws-1"
    assert ctx.pocket_id == "pocket-1"
    assert ctx.user_id == _CUST


# --------------------------------------------------------------------------- #
# Layer 2 — the three public entry points fail closed on the kill switch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_frame_default_enabled_renders(client):
    """A default Site (concierge_enabled=True) still renders the frame — no regression."""
    c, _store = client
    await _site()
    res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200
    assert "window.__PAWBAR__" in res.text


@pytest.mark.asyncio
async def test_frame_disabled_concierge_is_403(client):
    c, _store = client
    await _site(concierge_enabled=False)
    res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 403
    # The refusal renders inside a VISIBLE iframe — it must be the blank
    # self-removing shell, never an error payload (2026-07-30 rig report).
    assert res.text.startswith("<!doctype html>")
    assert "concierge_disabled" not in res.text


@pytest.mark.asyncio
async def test_chat_disabled_concierge_is_403(client):
    """POST /paw-bar/chat refuses (403) before dispatching a run when the kill
    switch is off."""
    c, store = client
    await _site(concierge_enabled=False)
    widget = await store.create_widget(_widget())
    res = await c.post(
        "/paw-bar/chat",
        json={
            "widget_id": widget.id,
            "signed_key": _VALID_KEY,
            "customer_ref": _CUST,
            "message": "What time do you open?",
        },
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 403
    assert "concierge_disabled" in res.text


@pytest.mark.asyncio
async def test_action_disabled_concierge_is_403(client):
    """POST /paw-bar/action refuses (403) in the shared front gate when the kill
    switch is off — the executor is never reached."""
    c, store = client
    await _site(concierge_enabled=False)
    widget = await store.create_widget(_widget())
    res = await c.post(
        "/paw-bar/action",
        json={
            "key": _VALID_KEY,
            "w": widget.id,
            "customer_ref": _CUST,
            "verb": "add_to_cart",
            "args": {"product_id": "espresso"},
        },
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 403
    assert "concierge_disabled" in res.text


# --------------------------------------------------------------------------- #
# Layer 3 — the greeting rides into the frame config
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_frame_carries_greeting(client):
    c, _store = client
    await _site(concierge_greeting="Welcome to Brew & Co")
    res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200
    assert '"greeting": "Welcome to Brew & Co"' in res.text


@pytest.mark.asyncio
async def test_frame_empty_greeting_emits_empty_string(client):
    c, _store = client
    await _site()  # concierge_greeting defaults ""
    res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200
    assert '"greeting": ""' in res.text


# --------------------------------------------------------------------------- #
# Layer 4 — the admin settings surface (GET + PATCH, workspace-scoped)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settings_get_defaults(client):
    c, _store = client
    site = await _site()
    res = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["site_id"] == str(site.id)
    assert body["concierge_enabled"] is True
    assert body["concierge_greeting"] == ""


@pytest.mark.asyncio
async def test_settings_patch_round_trips(client):
    c, _store = client
    site = await _site()
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_enabled": False, "concierge_greeting": "Back at 9am"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["concierge_enabled"] is False
    assert body["concierge_greeting"] == "Back at 9am"

    # The read reflects the write.
    got = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert got.json()["concierge_enabled"] is False
    assert got.json()["concierge_greeting"] == "Back at 9am"


@pytest.mark.asyncio
async def test_settings_patch_is_partial(client):
    """A PATCH carrying only one field leaves the other untouched (model_fields_set)."""
    c, _store = client
    site = await _site(concierge_greeting="Original line")
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_enabled": False},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["concierge_enabled"] is False
    assert body["concierge_greeting"] == "Original line"  # untouched


@pytest.mark.asyncio
async def test_public_frame_is_not_marked_preview(client):
    """The public embed must not advertise itself as a preview.

    ``preview`` is what lets the glass app accept live token updates from its
    parent. A public bar's parent is the CUSTOMER's page, so the flag being wrong
    here would hand every embedder a restyling channel this feature never
    intended to open.
    """
    c, _store = client
    await _site()
    res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200
    assert '"preview": false' in res.text or '"preview":false' in res.text


@pytest.mark.asyncio
async def test_preview_tokens_renders_without_writing(client):
    """The editor's live preview needs tokens for a draft nobody has saved."""
    c, _store = client
    site = await _site()

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/appearance/preview-tokens",
        json={"concierge_appearance": {"accent": "#ff5a36", "radius": 8}},
    )

    assert res.status_code == 200, res.text
    tokens = res.json()["tokens"]
    assert tokens["--pawbar-accent"] == "#ff5a36"
    assert tokens["--pawbar-radius"] == "8px"

    # WRITES NOTHING. The whole point is that this renders a draft; if it
    # persisted, every keystroke in the editor would be a save and Revert would
    # have nothing to go back to.
    got = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert got.json()["concierge_appearance"]["accent"] != "#ff5a36"


@pytest.mark.asyncio
async def test_preview_tokens_returns_the_clamped_value_not_the_typed_one(client):
    """The preview must not promise a look a save would not store.

    Same model, same validators as the PATCH — so an out-of-range radius comes
    back clamped and an unsafe image URL comes back empty, and the owner sees
    that while editing instead of discovering it at save time.
    """
    c, _store = client
    site = await _site()

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/appearance/preview-tokens",
        json={
            "concierge_appearance": {
                "radius": 9999,
                "agent_avatar_url": "http://insecure.example/a.png",
            }
        },
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["concierge_appearance"]["radius"] == 32
    assert body["tokens"]["--pawbar-radius"] == "32px"
    # http:// is refused rather than upgraded — it would be a mixed-content block
    # on the owner's own https site.
    assert body["concierge_appearance"]["agent_avatar_url"] == ""


@pytest.mark.asyncio
async def test_preview_tokens_cross_tenant_is_404(client):
    """Not an open rendering oracle: another workspace's site id 404s.

    The render does not need the document — the appearance is in the body — so
    the scoped load exists precisely to keep this gate identical to the settings
    PATCH beside it.
    """
    c, _store = client
    site = await _site(workspace="ws-other")

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/appearance/preview-tokens",
        json={"concierge_appearance": {"accent": "#ff5a36"}},
    )

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_settings_cross_tenant_is_404(client):
    """A site owned by another workspace 404s for the ws-1 admin session."""
    c, _store = client
    site = await _site(workspace="ws-other")
    res = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert res.status_code == 404
    patched = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_enabled": False},
    )
    assert patched.status_code == 404


@pytest.mark.asyncio
async def test_settings_malformed_id_is_404(client):
    c, _store = client
    res = await c.get("/paw-bar/admin/site/not-an-objectid/settings")
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Layer 5 — end-to-end: toggling off takes effect on the next public request
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_toggle_off_silences_frame_immediately(client):
    """The frame renders while enabled, then 403s right after the owner PATCHes the
    switch off — the gate re-reads the Site every request (no warm-client caching)."""
    c, _store = client
    site = await _site()

    ok = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert ok.status_code == 200

    off = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_enabled": False},
    )
    assert off.status_code == 200

    after = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert after.status_code == 403
    # Blank shell, not an error payload — the body renders on the site.
    assert after.text.startswith("<!doctype html>")
    assert "concierge_disabled" not in after.text


# --------------------------------------------------------------------------- #
# Layer 5 — the transcript-retention toggle
#
# Retention is its own switch because it is the only concierge setting that
# governs whether NEW personal data is collected. These prove it is exposed,
# writable, independent of the kill switch, and defaults to on.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settings_get_exposes_transcript_retention_default_on(client):
    c, _store = client
    site = await _site()
    body = (await c.get(f"/paw-bar/admin/site/{site.id}/settings")).json()
    assert body["concierge_store_transcripts"] is True


@pytest.mark.asyncio
async def test_settings_patch_can_turn_transcript_retention_off(client):
    c, _store = client
    site = await _site()
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_store_transcripts": False},
    )
    assert res.status_code == 200, res.text
    assert res.json()["concierge_store_transcripts"] is False
    got = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert got.json()["concierge_store_transcripts"] is False


# --------------------------------------------------------------------------- #
# Layer 6 — the settings routes are ROLE-gated (fix/paw-bar-role-gates)
#
# The reported bug: PATCH /paw-bar/admin/site/{id}/settings answered a workspace
# ADMIN with 403 "Missing required scope: admin". The route gated on
# ``require_scope("admin")``, an OSS single-tenant primitive that only accepts
# request.state.full_access / a pp_ API key / a ppat_ OAuth token — a cloud
# workspace admin presents none of the three, so the site kill switch was
# unreachable for the person who owns it. These pin the fix from both sides:
# the intended caller gets in, and the caller the old gate wrongly admitted on
# self-hosted (any signed-in session, member included) does not.
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def member_client(tmp_path, mongo_db):
    """Same app, but the caller's ws-1 role is MEMBER (below the ADMIN the
    paw_bar actions require). Yields ``(client, store)``."""
    from unittest.mock import patch

    app = _build_app(role="member")
    store = PawBarStore(tmp_path / "settings_member.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c, store


@pytest.mark.enforce_scope
@pytest.mark.parametrize("role", ["admin", "owner"])
@pytest.mark.asyncio
async def test_admin_role_can_patch_the_kill_switch(role, tmp_path, mongo_db):
    """THE REPORTED BUG. A workspace admin PATCHes their own site's concierge
    settings and the write lands — no 403, no scope check.

    ``enforce_scope`` matters here even though this route no longer calls
    ``require_scope``: it turns OFF the root conftest's ``_TESTING_FULL_ACCESS``
    bypass, which makes ``require_scope`` a no-op that admits every caller. With
    the bypass on, this test would pass against the OLD gate too and prove
    nothing — which is exactly how the bug reached production with the settings
    surface fully covered. The role gate never reads that flag, so the marker is
    inert against the fixed code and lethal against a regression to the old one.
    """
    from unittest.mock import patch

    site = await _site()
    app = _build_app(role=role)
    store = PawBarStore(tmp_path / f"settings_{role}.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            res = await c.patch(
                f"/paw-bar/admin/site/{site.id}/settings",
                json={"concierge_enabled": False},
            )
            assert res.status_code == 200, f"{role}: {res.status_code} {res.text}"
            assert res.json()["concierge_enabled"] is False

    # The switch actually moved on the stored doc, not just in the response.
    await site.sync()
    assert site.concierge_enabled is False


@pytest.mark.asyncio
async def test_member_role_cannot_patch_the_kill_switch(member_client, mongo_db):
    """A MEMBER is refused (403) on the same PATCH, and the site is untouched.

    Under ``require_scope("admin")`` on a self-hosted deploy this succeeded: the
    session cookie sets ``full_access``, which the scope gate accepts outright.
    """
    c, _store = member_client
    site = await _site()
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_enabled": False},
    )
    assert res.status_code == 403, res.text
    assert "workspace.insufficient_role" in res.text

    await site.sync()
    assert site.concierge_enabled is True  # nothing was written


@pytest.mark.asyncio
async def test_admin_role_can_read_the_settings(client):
    """The GET takes ``paw_bar.read`` — an admin sees the toggle state."""
    c, _store = client
    site = await _site(concierge_greeting="Back at 9am")
    res = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert res.status_code == 200, res.text
    assert res.json()["concierge_greeting"] == "Back at 9am"


@pytest.mark.asyncio
async def test_member_role_cannot_read_the_settings(member_client, mongo_db):
    """A MEMBER is refused (403) on the GET too — the reads and writes on this
    surface are both ADMIN, so a member learns nothing about the site's config."""
    c, _store = member_client
    site = await _site(concierge_greeting="Back at 9am")
    res = await c.get(f"/paw-bar/admin/site/{site.id}/settings")
    assert res.status_code == 403, res.text
    assert "Back at 9am" not in res.text


@pytest.mark.asyncio
async def test_member_role_cannot_delete_a_widget(member_client, mongo_db):
    """A widget MUTATION refuses a member as well — the role gate covers the
    whole admin surface of this router, not just the site settings."""
    c, store = member_client
    widget = await store.create_widget(_widget())
    res = await c.delete(
        f"/paw-bar/widgets/{widget.id}",
        headers={"X-Paw-Bar-Token": widget.access_token},
    )
    assert res.status_code == 403, res.text
    # The role gate runs BEFORE the handler, so the row is still there even
    # though the caller held the correct per-widget owner token.
    assert await store.get_widget(widget.id) is not None


@pytest.mark.asyncio
async def test_admin_role_can_delete_a_widget(client):
    """The same DELETE succeeds for an admin holding the owner token (204)."""
    c, store = client
    widget = await store.create_widget(_widget())
    res = await c.delete(
        f"/paw-bar/widgets/{widget.id}",
        headers={"X-Paw-Bar-Token": widget.access_token},
    )
    assert res.status_code == 204, res.text
    assert await store.get_widget(widget.id) is None


@pytest.mark.asyncio
async def test_transcript_retention_is_independent_of_the_kill_switch(client):
    """Turning retention off must not silence the concierge, and turning the
    concierge off must not silently flip retention."""
    c, _store = client
    site = await _site(concierge_greeting="Hi there")
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_store_transcripts": False},
    )
    body = res.json()
    assert body["concierge_store_transcripts"] is False
    assert body["concierge_enabled"] is True  # concierge still live
    assert body["concierge_greeting"] == "Hi there"  # untouched

    res2 = await c.patch(
        f"/paw-bar/admin/site/{site.id}/settings",
        json={"concierge_enabled": False},
    )
    assert res2.json()["concierge_store_transcripts"] is False  # still off, not reset

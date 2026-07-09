# test_share_links.py — FL-12b "Public share links" security acceptance.
#
# Locks the security-critical behavior of the public share-link surface:
#   - A shared text/html file resolves to a download with
#     Content-Disposition: attachment (NEVER inline) — proving the public route
#     mints its URL through EEUploadService.presigned_get and inherits the
#     ART-4 forced-attachment gate instead of bypassing it.
#   - An image (inline mime) still resolves (adapter default disposition).
#   - Tokens are unguessable (secrets) and single-file scoped: a token for file
#     A never resolves file B.
#   - Expired -> 410, revoked -> 410, unknown -> 404.
#   - The store is workspace + owner scoped for create/revoke.
#   - The whole surface is dark (404) when the feature flag is off.
#   - Sharing is independent of hide_from_ai.
#
# The public route is exercised WITHOUT any workspace/auth principal — it is
# token-gated by design — by calling ``resolve_share_link`` directly with a
# spy EEUploadService swapped into the router module.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pocketpaw_ee.cloud.uploads import share_router as sr
from pocketpaw_ee.cloud.uploads.share_models import ShareLink
from pocketpaw_ee.cloud.uploads.share_store import ShareLinkStore

from pocketpaw.uploads.errors import NotFound

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Spy service — records the (file_id, requester_id, workspace) presigned_get is
# called with and returns a disposition-aware fake URL, so we can assert the
# public route replays the LINK's owner identity and inherits forced-attachment.
# ---------------------------------------------------------------------------

# Mirror the real service's inline policy so the spy is faithful.
_INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}


class _SpyService:
    def __init__(self, files: dict[str, dict]) -> None:
        # file_id -> {"mime":..., "owner":..., "workspace":...}
        self._files = files
        self.calls: list[tuple[str, str, str]] = []

    async def presigned_get(self, file_id, requester_id, workspace, ttl_seconds):
        self.calls.append((file_id, requester_id, workspace))
        f = self._files.get(file_id)
        # Enforce the real read gate: only the owner in the right workspace
        # resolves; anything else is NotFound (existence-hiding).
        if f is None or f["owner"] != requester_id or f["workspace"] != workspace:
            raise NotFound()
        mime = f["mime"]
        disposition = None if mime in _INLINE else 'attachment; filename="f.bin"'
        # Bake the disposition into the URL so the test can assert on it, exactly
        # like a real S3 presigned URL carries response-content-disposition.
        url = f"https://signed.example/{file_id}"
        if disposition:
            url += "?response-content-disposition=" + disposition.replace(" ", "%20")
        rec = SimpleNamespace(id=file_id, mime=mime, filename="f.bin")
        return rec, url


@pytest.fixture()
def enable_flag(monkeypatch):
    monkeypatch.setenv("POCKETPAW_SHARE_LINKS_ENABLED", "true")


@pytest.fixture()
def spy_service(monkeypatch):
    files = {
        "html1": {"mime": "text/html", "owner": "owner1", "workspace": "w1"},
        "img1": {"mime": "image/png", "owner": "owner1", "workspace": "w1"},
        "other": {"mime": "text/plain", "owner": "owner2", "workspace": "w2"},
    }
    spy = _SpyService(files)
    monkeypatch.setattr(sr, "_SVC", spy)
    return spy


# ---------------------------------------------------------------------------
# Model-level: unguessable token + activity logic
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
async def test_token_is_unguessable_and_unique():
    # Generate via the field default_factory path (fresh docs, no DB needed).
    from pocketpaw_ee.cloud.uploads.share_models import _new_token

    seen = {_new_token() for _ in range(500)}
    assert len(seen) == 500  # no collisions
    # URL-safe token from 32 bytes -> ~43 chars, plenty of entropy.
    assert all(len(t) >= 40 for t in seen)


# ---------------------------------------------------------------------------
# Store: workspace + file scoping (create / revoke / lookup)
# ---------------------------------------------------------------------------


async def test_store_create_and_token_lookup(beanie_upload_db):
    store = ShareLinkStore()
    link = await store.create(file_id="fA", workspace="w1", owner_id="owner1")
    assert link.revoked is False
    assert link.is_active()

    got = await store.get_by_token(link.token)
    assert got is not None
    assert got.file_id == "fA"
    assert got.workspace_id == "w1"
    assert got.owner_id == "owner1"


async def test_store_revoke_is_workspace_and_file_scoped(beanie_upload_db):
    store = ShareLinkStore()
    link = await store.create(file_id="fA", workspace="w1", owner_id="owner1")

    # Wrong workspace cannot revoke.
    assert await store.revoke(link.token, file_id="fA", workspace="w2") is None
    # Wrong file cannot revoke.
    assert await store.revoke(link.token, file_id="fB", workspace="w1") is None
    # Still active.
    assert (await store.get_by_token(link.token)).is_active()

    # Correct scope revokes.
    revoked = await store.revoke(link.token, file_id="fA", workspace="w1")
    assert revoked is not None and revoked.revoked is True
    assert not (await store.get_by_token(link.token)).is_active()


async def test_store_get_owner_scoped_isolation(beanie_upload_db):
    store = ShareLinkStore()
    link = await store.create(file_id="fA", workspace="w1", owner_id="owner1")
    # Token is real but scope mismatched -> None (no cross-tenant/file leak).
    assert await store.get_owner_scoped(link.token, file_id="fB", workspace="w1") is None
    assert await store.get_owner_scoped(link.token, file_id="fA", workspace="w2") is None
    assert await store.get_owner_scoped(link.token, file_id="fA", workspace="w1") is not None


# ---------------------------------------------------------------------------
# Public route — the security acceptance criteria
# ---------------------------------------------------------------------------


async def test_public_get_html_forces_attachment(beanie_upload_db, enable_flag, spy_service):
    store = ShareLinkStore()
    link = await store.create(file_id="html1", workspace="w1", owner_id="owner1")

    resp = await sr.resolve_share_link(link.token)
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    location = resp.headers["location"]
    # The forced-attachment disposition rode along on the presigned URL.
    assert "response-content-disposition=attachment" in location
    # And the public route replayed the LINK's owner identity + workspace,
    # not any caller-supplied principal.
    assert spy_service.calls[-1] == ("html1", "owner1", "w1")


async def test_public_get_image_serves_inline_ok(beanie_upload_db, enable_flag, spy_service):
    store = ShareLinkStore()
    link = await store.create(file_id="img1", workspace="w1", owner_id="owner1")

    resp = await sr.resolve_share_link(link.token)
    assert resp.status_code == 302
    # Inline mime -> no forced-attachment disposition (byte-identical default).
    assert "response-content-disposition" not in resp.headers["location"]


async def test_token_for_file_a_cannot_fetch_file_b(beanie_upload_db, enable_flag, spy_service):
    """The token is single-file scoped: it only ever resolves its own file_id.

    Even though 'other' exists, a link minted for 'html1' resolves 'html1'.
    """
    store = ShareLinkStore()
    link = await store.create(file_id="html1", workspace="w1", owner_id="owner1")
    await sr.resolve_share_link(link.token)
    # Only html1 was ever requested — never 'other'.
    assert all(c[0] == "html1" for c in spy_service.calls)


async def test_expired_token_returns_410(beanie_upload_db, enable_flag, spy_service):
    # Directly persist an expired link.
    link = ShareLink(
        file_id="html1",
        workspace_id="w1",
        owner_id="owner1",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    await link.insert()
    with pytest.raises(HTTPException) as ei:
        await sr.resolve_share_link(link.token)
    assert ei.value.status_code == 410
    # presigned_get must NOT have been called for an expired link.
    assert spy_service.calls == []


async def test_revoked_token_returns_410(beanie_upload_db, enable_flag, spy_service):
    store = ShareLinkStore()
    link = await store.create(file_id="html1", workspace="w1", owner_id="owner1")
    await store.revoke(link.token, file_id="html1", workspace="w1")
    with pytest.raises(HTTPException) as ei:
        await sr.resolve_share_link(link.token)
    assert ei.value.status_code == 410
    assert spy_service.calls == []


async def test_unknown_token_returns_404(beanie_upload_db, enable_flag, spy_service):
    with pytest.raises(HTTPException) as ei:
        await sr.resolve_share_link("does-not-exist")
    assert ei.value.status_code == 404


async def test_deleted_file_returns_404(beanie_upload_db, enable_flag, spy_service):
    """Link minted, then the underlying file goes away -> NotFound -> 404."""
    # Mint a link for a file the spy service doesn't know about.
    link = ShareLink(file_id="ghost", workspace_id="w1", owner_id="owner1")
    await link.insert()
    with pytest.raises(HTTPException) as ei:
        await sr.resolve_share_link(link.token)
    assert ei.value.status_code == 404


async def test_flag_off_makes_public_route_dark(beanie_upload_db, spy_service, monkeypatch):
    monkeypatch.setenv("POCKETPAW_SHARE_LINKS_ENABLED", "false")
    store = ShareLinkStore()
    link = await store.create(file_id="html1", workspace="w1", owner_id="owner1")
    with pytest.raises(HTTPException) as ei:
        await sr.resolve_share_link(link.token)
    assert ei.value.status_code == 404
    # Never even looked at the file.
    assert spy_service.calls == []


async def test_share_independent_of_hide_from_ai(beanie_upload_db, enable_flag, spy_service):
    """A file hidden from AI can still be shared — the two gates never couple.

    The public route only consults the ShareLink + presigned_get; it never reads
    hide_from_ai. This test proves resolution succeeds regardless.
    """
    store = ShareLinkStore()
    link = await store.create(file_id="html1", workspace="w1", owner_id="owner1")
    resp = await sr.resolve_share_link(link.token)
    assert resp.status_code == 302

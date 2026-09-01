# tests/ee/sites/test_public_assets.py — the PUBLIC asset rail for Paw Sites.
# Created 2026-08-31 (feat/sites-public-asset-uploads). New file.
#
# Under test: ``pocketpaw_ee.sites.public_assets`` plus the two seams it stands on,
# ``StorageAdapter.public_url`` and ``uploads.factory.build_public_adapter``.
#
# The adapter is FAKED (a dict) for the store's own contract; the real
# ``S3StorageAdapter`` public mode is exercised separately against moto, because the
# thing that would actually break in production — the object going up without
# ``ACL=public-read`` and therefore 403ing for every visitor — is invisible to a
# fake and is not something the store can assert about itself.
#
# What these prove:
#   * a file must PROVE it is an image by its magic bytes; a caller-supplied
#     Content-Type is never trusted, so HTML/SVG labelled ``image/png`` is refused
#     (the private rail's sniffer DOES fall back to that header — reusing it here
#     is the bug this rail exists to avoid);
#   * SVG is rejected even when well-formed, because it executes script on an
#     origin we serve;
#   * keys are content-addressed AND tenant-scoped, so the same bytes cost one
#     object and no workspace can name another's key;
#   * delete refuses any key outside the caller's own pocket prefix — without that
#     the endpoint is a cross-tenant arbitrary-object delete;
#   * the PRIVATE adapter mints no public URL (a regression here silently starts
#     handing out links that 403 for every visitor);
#   * the factory returns None rather than a half-configured adapter, so an
#     unconfigured deployment gets "not configured" instead of broken images.

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.sites.public_assets import (
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    PublicAssetError,
    PublicAssetStore,
    build_key,
    prefix_for,
    public_asset_store,
    sniff_media_mime,
)

from pocketpaw.uploads.adapter import StorageItem

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x20" + b"ftyp" + b"isom" + b"\x00" * 64
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
# A QuickTime .mov: the same ftyp box, with a brand browsers will not reliably
# play. Accepting it would publish a hero that is blank for some visitors, which
# is worse than refusing it — because nobody finds out.
MOV = b"\x00\x00\x00\x14" + b"ftyp" + b"qt  " + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!DOCTYPE html><html><script>alert(1)</script></html>"

BASE = "https://cdn.example.test/public-bucket"


class FakePublicAdapter:
    """Minimal StorageAdapter over a dict, with a working public_url."""

    def __init__(self, base: str | None = BASE) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self._base = base

    async def put(self, key, stream, mime):
        body = b""
        async for chunk in stream:
            body += chunk
        self.objects[key] = (body, mime)
        from pocketpaw.uploads.adapter import StoredObject

        return StoredObject(key=key, size=len(body), mime=mime)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def browse(self, prefix: str) -> list[StorageItem]:
        out = []
        for key, (body, _) in self.objects.items():
            if key.startswith(prefix) and "/" not in key[len(prefix) :]:
                out.append(StorageItem(name=key[len(prefix) :], is_dir=False, size=len(body)))
        return out

    def public_url(self, key: str) -> str | None:
        return f"{self._base}/{key}" if self._base else None


@pytest.fixture
def store() -> tuple[PublicAssetStore, FakePublicAdapter]:
    adapter = FakePublicAdapter()
    return PublicAssetStore(adapter), adapter


# ── The sniffer: bytes must prove the type ──────────────────────────────


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_sniff_accepts_real_images(data: bytes, expected: str) -> None:
    assert sniff_media_mime(data) == expected


@pytest.mark.parametrize("data", [SVG, HTML, b"plain text", b"", b"PK\x03\x04zipbytes"])
def test_sniff_rejects_everything_that_is_not_a_raster_image(data: bytes) -> None:
    assert sniff_media_mime(data) is None


# ── put(): the security gate ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stores_a_real_png_and_returns_a_durable_url(store) -> None:
    svc, adapter = store
    asset = await svc.put(PNG, filename="logo.png", workspace_id="ws1", pocket_id="pk1")

    assert asset.mime == "image/png"
    assert asset.size == len(PNG)
    assert asset.key in adapter.objects
    assert asset.url == f"{BASE}/{asset.key}"
    # No query string: a signature would mean an expiry, which is the whole
    # thing this rail exists to avoid.
    assert "?" not in asset.url


@pytest.mark.asyncio
async def test_html_claiming_to_be_a_png_is_refused(store) -> None:
    """The filename and any client Content-Type are irrelevant — bytes decide."""
    svc, adapter = store
    with pytest.raises(PublicAssetError, match="PNG, JPEG, GIF and WebP"):
        await svc.put(HTML, filename="innocent.png", workspace_id="ws1", pocket_id="pk1")
    assert adapter.objects == {}


@pytest.mark.asyncio
async def test_svg_is_refused_even_though_it_is_an_image(store) -> None:
    """SVG executes script; serving one from our origin is stored XSS."""
    svc, adapter = store
    with pytest.raises(PublicAssetError):
        await svc.put(SVG, filename="logo.svg", workspace_id="ws1", pocket_id="pk1")
    assert adapter.objects == {}


@pytest.mark.asyncio
async def test_empty_and_oversize_are_refused(store) -> None:
    svc, _ = store
    with pytest.raises(PublicAssetError, match="empty"):
        await svc.put(b"", filename="a.png", workspace_id="ws1", pocket_id="pk1")

    huge = PNG + b"\x00" * MAX_IMAGE_BYTES
    with pytest.raises(PublicAssetError, match="capped at"):
        await svc.put(huge, filename="a.png", workspace_id="ws1", pocket_id="pk1")


@pytest.mark.asyncio
async def test_an_adapter_with_no_public_url_never_stores_bytes() -> None:
    """Storing an object nobody can address is worse than failing."""
    adapter = FakePublicAdapter(base=None)
    svc = PublicAssetStore(adapter)
    with pytest.raises(PublicAssetError, match="not configured"):
        await svc.put(PNG, filename="logo.png", workspace_id="ws1", pocket_id="pk1")
    assert adapter.objects == {}


# ── Keys: content-addressed and tenant-scoped ───────────────────────────


@pytest.mark.asyncio
async def test_same_bytes_same_name_reuse_one_object(store) -> None:
    svc, adapter = store
    a = await svc.put(PNG, filename="logo.png", workspace_id="ws1", pocket_id="pk1")
    b = await svc.put(PNG, filename="logo.png", workspace_id="ws1", pocket_id="pk1")
    assert a.key == b.key
    assert len(adapter.objects) == 1


@pytest.mark.asyncio
async def test_different_bytes_get_different_keys(store) -> None:
    svc, adapter = store
    a = await svc.put(PNG, filename="logo.png", workspace_id="ws1", pocket_id="pk1")
    b = await svc.put(PNG + b"x", filename="logo.png", workspace_id="ws1", pocket_id="pk1")
    assert a.key != b.key
    assert len(adapter.objects) == 2


@pytest.mark.asyncio
async def test_the_same_file_in_two_workspaces_never_shares_a_key(store) -> None:
    svc, _ = store
    a = await svc.put(PNG, filename="logo.png", workspace_id="ws1", pocket_id="pk1")
    b = await svc.put(PNG, filename="logo.png", workspace_id="ws2", pocket_id="pk1")
    assert a.key != b.key
    assert a.key.startswith(prefix_for("ws1", "pk1"))
    assert b.key.startswith(prefix_for("ws2", "pk1"))


def test_a_hostile_filename_cannot_escape_the_prefix() -> None:
    key = build_key("ws1", "pk1", "a" * 64, "../../../../etc/passwd", "image/png")
    assert key.startswith(prefix_for("ws1", "pk1"))
    assert "/" not in key[len(prefix_for("ws1", "pk1")) :]
    assert ".." not in key


def test_a_windows_path_filename_is_reduced_to_its_basename() -> None:
    key = build_key("ws1", "pk1", "b" * 64, r"C:\Users\me\Desktop\hero shot.png", "image/png")
    assert key.endswith("-hero-shot.png")


# ── delete(): the cross-tenant guard ────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_refuses_a_key_belonging_to_another_site(store) -> None:
    svc, adapter = store
    victim = await svc.put(PNG, filename="logo.png", workspace_id="ws2", pocket_id="pk9")

    with pytest.raises(PublicAssetError, match="does not belong"):
        await svc.delete(workspace_id="ws1", pocket_id="pk1", key=victim.key)
    assert victim.key in adapter.objects


@pytest.mark.asyncio
async def test_delete_refuses_a_key_that_escapes_via_a_nested_path(store) -> None:
    svc, _ = store
    nested = prefix_for("ws1", "pk1") + "../pk2/stolen.png"
    with pytest.raises(PublicAssetError, match="does not belong"):
        await svc.delete(workspace_id="ws1", pocket_id="pk1", key=nested)


@pytest.mark.asyncio
async def test_delete_removes_the_sites_own_asset(store) -> None:
    svc, adapter = store
    asset = await svc.put(PNG, filename="logo.png", workspace_id="ws1", pocket_id="pk1")
    await svc.delete(workspace_id="ws1", pocket_id="pk1", key=asset.key)
    assert asset.key not in adapter.objects


# ── list(): the display name survives the round trip ────────────────────


@pytest.mark.asyncio
async def test_list_returns_only_this_sites_assets_with_readable_names(store) -> None:
    svc, _ = store
    await svc.put(PNG, filename="hero shot.png", workspace_id="ws1", pocket_id="pk1")
    await svc.put(JPEG, filename="team.jpg", workspace_id="ws1", pocket_id="pk1")
    await svc.put(GIF, filename="other.gif", workspace_id="ws2", pocket_id="pk1")

    listed = await svc.list(workspace_id="ws1", pocket_id="pk1")
    assert [a.filename for a in listed] == ["hero-shot.png", "team.jpg"]
    assert [a.mime for a in listed] == ["image/png", "image/jpeg"]
    assert all(a.url.startswith(BASE) for a in listed)
    # The 16-hex content hash is split back off, not left in the display name.
    assert all(len(a.sha256) == 16 for a in listed)


# ── The factory: no half-configured adapter ─────────────────────────────


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    # S3_PRIVATE_BUCKET is cleared too. Leaving the developer's real value in
    # place let a "no public bucket" test pass for the wrong reason: a mutation
    # that made the public rail fall back to the PRIVATE bucket still returned
    # None, because the endpoint happened to be missing as well. Every input the
    # factory reads is pinned per-test, so exactly one thing is under test.
    for var in (
        "POCKETPAW_UPLOAD_ADAPTER",
        "S3_PUBLIC_BUCKET",
        "S3_PUBLIC_BASE_URL",
        "S3_PRIVATE_BUCKET",
        "S3_BUCKET",
        "S3_ENDPOINT",
        "S3_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_local_mode_has_no_public_rail(clean_env: pytest.MonkeyPatch) -> None:
    """A dev box with local disk has nowhere world-readable — say so, don't fake it."""
    clean_env.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")
    assert public_asset_store() is None


def test_s3_mode_without_a_public_bucket_has_no_public_rail(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The private bucket must never be borrowed as the public one.

    Everything ELSE the factory needs is present — endpoint, region, credentials
    — so the missing ``S3_PUBLIC_BUCKET`` is the only reason this can return
    None. Without that setup a fallback-to-private bug passes this test.
    """
    clean_env.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    clean_env.setenv("S3_PRIVATE_BUCKET", "interacly-dev-private")
    clean_env.setenv("S3_ENDPOINT", "https://nbg1.your-objectstorage.com")
    clean_env.setenv("S3_REGION", "eu-central")
    clean_env.setenv("S3_ACCESS_KEY_ID", "k")
    clean_env.setenv("S3_SECRET_ACCESS_KEY", "s")

    assert public_asset_store() is None


def test_the_public_allowlist_contains_nothing_that_executes(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A direct assertion on the security boundary itself.

    The magic-byte sniffer happens to reject SVG today, which means widening
    ``PUBLIC_IMAGE_MIMES`` alone does not immediately open the hole — and that is
    exactly why it needs its own assertion. The two lists are separate, a later
    change could teach the sniffer a new signature, and the moment both agree on
    SVG we are serving script from an origin we own.
    """
    from pocketpaw_ee.sites.public_assets import PUBLIC_IMAGE_MIMES

    for mime in ("image/svg+xml", "text/html", "application/xhtml+xml", "text/xml"):
        assert mime not in PUBLIC_IMAGE_MIMES, f"{mime} can execute script — never public"
    # And the sniffer must not learn it either.
    assert sniff_media_mime(SVG) is None


def test_s3_public_bucket_without_an_endpoint_has_no_public_rail(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A guessed address 404s; refuse rather than mint one."""
    clean_env.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    clean_env.setenv("S3_PUBLIC_BUCKET", "public")
    assert public_asset_store() is None


def test_public_base_url_overrides_the_derived_path_style(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    clean_env.setenv("S3_PUBLIC_BUCKET", "public")
    clean_env.setenv("S3_ENDPOINT", "https://nbg1.example.test")
    clean_env.setenv("S3_PUBLIC_BASE_URL", "https://cdn.example.test/")
    clean_env.setenv("S3_ACCESS_KEY_ID", "k")
    clean_env.setenv("S3_SECRET_ACCESS_KEY", "s")

    from pocketpaw.uploads.factory import build_public_adapter

    adapter = build_public_adapter()
    assert adapter is not None
    # The trailing slash on the env value must not produce "//" in the URL.
    assert adapter.public_url("sites-assets/a/b/c.png") == (
        "https://cdn.example.test/sites-assets/a/b/c.png"
    )


def test_derived_public_url_is_path_style_over_the_endpoint(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    clean_env.setenv("S3_PUBLIC_BUCKET", "interacly-dev-public")
    clean_env.setenv("S3_ENDPOINT", "https://nbg1.your-objectstorage.com")
    clean_env.setenv("S3_ACCESS_KEY_ID", "k")
    clean_env.setenv("S3_SECRET_ACCESS_KEY", "s")

    from pocketpaw.uploads.factory import build_public_adapter

    adapter = build_public_adapter()
    assert adapter is not None
    assert adapter.public_url("x/y.png") == (
        "https://nbg1.your-objectstorage.com/interacly-dev-public/x/y.png"
    )


def test_the_private_adapter_mints_no_public_url(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE regression guard: the private bucket handing out links is a data leak."""
    clean_env.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    clean_env.setenv("S3_PRIVATE_BUCKET", "interacly-dev-private")
    clean_env.setenv("S3_ENDPOINT", "https://nbg1.your-objectstorage.com")
    clean_env.setenv("S3_ACCESS_KEY_ID", "k")
    clean_env.setenv("S3_SECRET_ACCESS_KEY", "s")

    from pocketpaw.uploads.factory import build_adapter

    adapter = build_adapter(local_root=tmp_path)
    assert adapter.public_url("anything") is None


# ── Video: scroll-driven background video is a mainstream site form ─────


@pytest.mark.parametrize(("data", "expected"), [(MP4, "video/mp4"), (WEBM, "video/webm")])
def test_sniff_accepts_web_playable_video(data: bytes, expected: str) -> None:
    assert sniff_media_mime(data) == expected


def test_quicktime_is_refused_even_though_it_is_video() -> None:
    """A .mov is real video that does not play everywhere.

    Publishing one produces a hero that is silently blank for some visitors —
    strictly worse than refusing the upload, because nobody finds out.
    """
    assert sniff_media_mime(MOV) is None


@pytest.mark.asyncio
async def test_a_video_stores_and_is_marked_as_video(store) -> None:
    svc, adapter = store
    asset = await svc.put(MP4, filename="hero loop.mp4", workspace_id="ws1", pocket_id="pk1")

    assert asset.mime == "video/mp4"
    assert asset.kind == "video", "the agent needs this to emit <video>, not <img>"
    # extension_for() is the private rail's map and has no video mimes; without
    # the local fallback this key would end ".bin".
    assert asset.key.endswith(".mp4")
    assert asset.key in adapter.objects


@pytest.mark.asyncio
async def test_an_image_is_marked_as_image(store) -> None:
    svc, _ = store
    asset = await svc.put(PNG, filename="logo.png", workspace_id="ws1", pocket_id="pk1")
    assert asset.kind == "image"


@pytest.mark.asyncio
async def test_video_gets_the_larger_cap_not_the_image_one(store) -> None:
    """A 12 MB video must not be judged against the 10 MB image ceiling."""
    svc, _ = store
    twelve_mb = MP4 + b"\x00" * (12 * 1024 * 1024)
    asset = await svc.put(twelve_mb, filename="loop.mp4", workspace_id="ws1", pocket_id="pk1")
    assert asset.kind == "video"


@pytest.mark.asyncio
async def test_an_oversized_video_is_refused_and_names_video(store) -> None:
    svc, _ = store
    huge = MP4 + b"\x00" * MAX_VIDEO_BYTES
    with pytest.raises(PublicAssetError, match="Video for a site are capped"):
        await svc.put(huge, filename="loop.mp4", workspace_id="ws1", pocket_id="pk1")


def test_the_video_cap_stays_under_the_adapters_in_memory_ceiling() -> None:
    """The S3 adapter buffers a whole body in memory before it PUTs.

    Raising MAX_VIDEO_BYTES past that ceiling does not need a bigger number, it
    needs multipart upload. This pins the dependency so whoever bumps it has to
    confront the comment rather than discover it as an OOM.
    """
    from pocketpaw.uploads.s3 import _MEM_BUFFER_WARN_BYTES

    assert MAX_VIDEO_BYTES < _MEM_BUFFER_WARN_BYTES

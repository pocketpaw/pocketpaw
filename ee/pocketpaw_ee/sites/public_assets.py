# ee/pocketpaw_ee/sites/public_assets.py — the PUBLIC asset rail for Paw Sites.
# Created 2026-08-31 (feat/sites-public-asset-uploads).
#
# WHY THIS EXISTS. A site we publish is read by anonymous visitors, so an image it
# displays needs an address with no credential and no expiry. Neither URL this
# codebase already mints qualifies:
#
#   * ``StorageAdapter.presigned_get`` expires (S3 caps a presign at 7 days) —
#     the site outlives the link and the image turns into a broken box.
#   * ``/api/v1/uploads/{id}`` (what screenshot.py and the deliver MCP tool hand
#     back) is auth-gated, so it 401s for exactly the visitor it is meant to serve.
#
# And the bytes cannot ride the build instead: ``generator_client`` sends a
# TEXT-ONLY ``source`` map, and its base64 ``assets`` sideband is rejected outright
# by the generator for every engine except html (paw-sites ``src/index.ts``, the
# ``assets is only supported with engine="html"`` throw). A URL is the only carrier
# that works on all four engines, and it keeps visitor-supplied bytes OUT of the
# build tree — which preserves the write-allowlist property that stops an edit
# reaching ``package.json`` and routing around the release-age floor.
#
# WHAT IT DELIBERATELY DOES NOT DO. It does not store documents. A PRD is agent
# INPUT, not site content; publishing a customer's requirements doc to an
# unauthenticated bucket is a data leak, so documents stay on the private rail
# (``EEUploadService``), where ``chat/agent_service._build_attachments_block``
# already extracts their text into the model's context. Only PUBLISHABLE MEDIA —
# the stills and video a page actually renders — lands here.
#
# Updated 2026-08-31 (video): the rail carries VIDEO as well as stills. A
# scroll-driven background video is a mainstream landing-page form now, and it has
# the same address problem a hero image does — worse, since a video element refetches
# by range and a signed URL dying mid-scroll is a visibly broken page. Video gets its
# own 50 MiB cap: the S3 adapter buffers a whole body in memory before it PUTs
# (``_MEM_BUFFER_WARN_BYTES`` is 64 MiB), so a larger ceiling here would trip that
# warning and eventually OOM a web worker. Raising it past 50 MiB means teaching the
# adapter multipart upload first — that is the honest prerequisite, not a config bump.
#
# KEY LAYOUT: ``sites-assets/{workspace_id}/{pocket_id}/{sha256[:16]}-{stem}{ext}``
# Tenant-scoped so one workspace can never enumerate or overwrite another's, and
# content-addressed so a key's bytes can never change — which is what makes the
# year-long immutable Cache-Control on the object safe, and makes re-uploading the
# same file free instead of duplicating it.

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.config import extension_for

logger = logging.getLogger(__name__)

# Raster images only. SVG is absent ON PURPOSE: it is an XML document that
# executes script, and serving one from a bucket origin we control turns an image
# upload into stored XSS / hosted phishing. HTML is absent for the same reason.
# Anything added here becomes world-readable at a stable URL forever — treat this
# set as a security boundary, not a convenience list.
PUBLIC_IMAGE_MIMES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)

# Web-playable containers only. A .mov/quicktime upload is common off a phone but
# does not play in every browser, so accepting it would publish a video that is
# silently blank for some visitors — worse than refusing it at the door.
PUBLIC_VIDEO_MIMES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
    }
)

PUBLIC_MEDIA_MIMES: frozenset[str] = PUBLIC_IMAGE_MIMES | PUBLIC_VIDEO_MIMES

# Magic-byte signatures for the allowlist above. The public rail must NOT reuse
# ``uploads.service._sniff_mime``: that helper FALLS BACK to the client's own
# Content-Type header when nothing matches, so a caller can label arbitrary bytes
# ``image/png`` and have them stored and served under that type. On a private,
# auth-gated rail that is survivable. On a public origin it is how you end up
# hosting somebody else's payload, so here a file must PROVE what it is.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    # Matroska/WebM: the EBML header is a plain prefix.
    (b"\x1a\x45\xdf\xa3", "video/webm"),
)

# ISO-BMFF brands that are actually web-playable MP4. `qt  ` (QuickTime) is
# deliberately absent — see PUBLIC_VIDEO_MIMES.
_MP4_BRANDS: frozenset[bytes] = frozenset(
    {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V "}
)

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MiB — a hero image.
# 50 MiB, NOT an arbitrary round number: the S3 adapter buffers the whole body in
# memory and warns past 64 MiB. Staying under that is the difference between a
# refusal the user can act on and a worker that dies mid-upload.
MAX_VIDEO_BYTES = 50 * 1024 * 1024
# Back-compat alias — the endpoint's read cap must admit the largest kind.
MAX_ASSET_BYTES = MAX_VIDEO_BYTES

_KEY_PREFIX = "sites-assets"
_HASH_LEN = 16
_STEM_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_MAX_STEM_LEN = 48
# Every stored name starts with the content hash and a dash, so a listing can
# split the display name back off the key.
_LISTED_RE = re.compile(r"^([0-9a-f]{16})-(.+)$")


class PublicAssetError(Exception):
    """Raised when an asset is rejected. The message is safe to show a user."""


@dataclass(frozen=True)
class PublicAsset:
    """One stored asset and the durable address a site can reference."""

    key: str
    url: str
    mime: str
    size: int
    filename: str
    sha256: str
    # "image" | "video" — the agent needs this to emit <img> vs <video>, and it
    # cannot reliably infer it from the extension alone in a listing.
    kind: str = "image"


def sniff_media_mime(head: bytes) -> str | None:
    """Return the media mime ``head`` actually proves, or ``None``.

    Unlike the private rail's sniffer there is no fallback to a caller-supplied
    Content-Type — unrecognised bytes are simply not publishable media.
    """
    for magic, mime in _SIGNATURES:
        if head.startswith(magic):
            return mime
    # WebP is the one image format whose marker is not a plain prefix: "RIFF",
    # then a 4-byte little-endian length, then "WEBP" at offset 8.
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    # MP4/ISO-BMFF: a 4-byte box length, then "ftyp", then the brand. Only the
    # brands a browser actually plays are accepted — an unrecognised brand (HEIF
    # off a phone, say) falls through to None rather than being published as
    # video/mp4 and rendering as a blank player on the live page.
    if head[4:8] == b"ftyp" and head[8:12] in _MP4_BRANDS:
        return "video/mp4"
    return None


def kind_for_mime(mime: str) -> str:
    """Return ``"image"`` or ``"video"`` — what the agent must emit for this asset."""
    return "video" if mime in PUBLIC_VIDEO_MIMES else "image"


def max_bytes_for_mime(mime: str) -> int:
    """The size ceiling that applies to this media kind."""
    return MAX_VIDEO_BYTES if mime in PUBLIC_VIDEO_MIMES else MAX_IMAGE_BYTES


def _safe_stem(filename: str) -> str:
    """Reduce a user filename to a short, URL-safe stem. Never empty."""
    # Basename only — a filename arrives from a browser and may carry path
    # separators from a hostile client.
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = _STEM_RE.sub("-", stem).strip("-.")[:_MAX_STEM_LEN]
    return stem or "asset"


def build_key(workspace_id: str, pocket_id: str, digest: str, filename: str, mime: str) -> str:
    """Return the tenant-scoped, content-addressed storage key for one asset."""
    stem = _safe_stem(filename)
    # ``extension_for`` is the shared private-rail map and carries no video
    # mimes, so fall back to the local one rather than emit ".bin" for an mp4 —
    # some CDNs and players still sniff the extension.
    ext = extension_for(mime) or _MEDIA_EXT.get(mime) or ".bin"
    return f"{_KEY_PREFIX}/{workspace_id}/{pocket_id}/{digest[:_HASH_LEN]}-{stem}{ext}"


def prefix_for(workspace_id: str, pocket_id: str) -> str:
    """Return the listing prefix owning every asset of one site."""
    return f"{_KEY_PREFIX}/{workspace_id}/{pocket_id}/"


class PublicAssetStore:
    """Store site assets on a world-readable bucket and hand back durable URLs.

    Construct via :func:`public_asset_store`, which returns ``None`` when the
    deployment has no public bucket — callers must surface that as "not
    configured" rather than silently falling back to the private adapter.
    """

    def __init__(self, adapter: StorageAdapter) -> None:
        self._adapter = adapter

    async def put(
        self,
        data: bytes,
        *,
        filename: str,
        workspace_id: str,
        pocket_id: str,
    ) -> PublicAsset:
        """Validate, store, and return the asset. Raises :class:`PublicAssetError`."""
        if not data:
            raise PublicAssetError("That file is empty.")

        # Sniff BEFORE the size check: the ceiling depends on the kind, and a
        # 30 MB video rejected against the 10 MB image cap would be a confusing
        # lie about why it failed.
        mime = sniff_media_mime(data[:512])
        if mime is None or mime not in PUBLIC_MEDIA_MIMES:
            raise PublicAssetError(
                "A site can publish PNG, JPEG, GIF and WebP images, or MP4 and WebM "
                "video. (SVG is not accepted — it can carry script. QuickTime .mov is "
                "not accepted — it does not play in every browser.) "
                "Documents like a PRD are read as context instead, not published."
            )

        cap = max_bytes_for_mime(mime)
        if len(data) > cap:
            noun = "Video" if kind_for_mime(mime) == "video" else "Images"
            raise PublicAssetError(
                f"That file is {len(data) // (1024 * 1024)} MB. "
                f"{noun} for a site are capped at {cap // (1024 * 1024)} MB."
            )

        digest = hashlib.sha256(data).hexdigest()
        key = build_key(workspace_id, pocket_id, digest, filename, mime)

        url = self._adapter.public_url(key)
        if not url:
            # Belt-and-braces: public_asset_store() already refuses to build a
            # store over an adapter with no public address, so reaching here
            # means the adapter changed under us. Fail rather than store bytes
            # nobody can ever reference.
            raise PublicAssetError("Public asset storage is not configured for this deployment.")

        async def _body() -> AsyncIterator[bytes]:
            yield data

        await self._adapter.put(key, _body(), mime)
        logger.info(
            "sites.public_assets: stored %d bytes for pocket %s at %s",
            len(data),
            pocket_id,
            key,
        )
        return PublicAsset(
            key=key,
            url=url,
            mime=mime,
            size=len(data),
            filename=filename,
            sha256=digest,
            kind=kind_for_mime(mime),
        )

    async def list(self, *, workspace_id: str, pocket_id: str) -> list[PublicAsset]:
        """Return every asset stored for one site. Empty on an unlistable adapter."""
        prefix = prefix_for(workspace_id, pocket_id)
        items = await self._adapter.browse(prefix)
        out: list[PublicAsset] = []
        for item in items:
            if item.is_dir:
                continue
            key = f"{prefix}{item.name}"
            url = self._adapter.public_url(key)
            if not url:
                continue
            match = _LISTED_RE.match(item.name)
            digest, display = (match.group(1), match.group(2)) if match else ("", item.name)
            out.append(
                PublicAsset(
                    key=key,
                    url=url,
                    mime=_mime_for_name(item.name),
                    size=item.size,
                    filename=display,
                    sha256=digest,
                    kind=kind_for_mime(_mime_for_name(item.name)),
                )
            )
        return sorted(out, key=lambda a: a.filename)

    async def delete(self, *, workspace_id: str, pocket_id: str, key: str) -> None:
        """Delete one asset, refusing any key outside this site's own prefix."""
        prefix = prefix_for(workspace_id, pocket_id)
        if not key.startswith(prefix) or "/" in key[len(prefix) :]:
            # The key arrives from the client. Without this the endpoint is an
            # arbitrary-object delete against the whole bucket, across tenants.
            raise PublicAssetError("That asset does not belong to this site.")
        await self._adapter.delete(key)


_MEDIA_EXT: dict[str, str] = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

_NAME_MIMES: tuple[tuple[str, str], ...] = (
    (".mp4", "video/mp4"),
    (".webm", "video/webm"),
    (".png", "image/png"),
    (".jpg", "image/jpeg"),
    (".jpeg", "image/jpeg"),
    (".gif", "image/gif"),
    (".webp", "image/webp"),
)


def _mime_for_name(name: str) -> str:
    lowered = name.lower()
    for ext, mime in _NAME_MIMES:
        if lowered.endswith(ext):
            return mime
    return "application/octet-stream"


def public_asset_store() -> PublicAssetStore | None:
    """Return the configured store, or ``None`` when no public bucket exists.

    ``None`` is a deployment fact, not an error: a local dev box with
    ``POCKETPAW_UPLOAD_ADAPTER=local`` has nowhere world-readable to put bytes.
    Callers turn it into a clear "not configured" response instead of pretending.
    """
    from pocketpaw.uploads.factory import build_public_adapter

    adapter = build_public_adapter()
    if adapter is None:
        return None
    # An adapter that cannot mint a public address is useless here even if it
    # exists, and the failure would otherwise surface as a broken image later.
    if adapter.public_url("probe") is None:
        logger.warning(
            "sites.public_assets: a public adapter was built but mints no public URL — "
            "set S3_PUBLIC_BASE_URL or S3_ENDPOINT"
        )
        return None
    return PublicAssetStore(adapter)


__all__ = [
    "MAX_ASSET_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_VIDEO_BYTES",
    "PUBLIC_IMAGE_MIMES",
    "PUBLIC_MEDIA_MIMES",
    "PUBLIC_VIDEO_MIMES",
    "PublicAsset",
    "PublicAssetError",
    "PublicAssetStore",
    "build_key",
    "prefix_for",
    "kind_for_mime",
    "max_bytes_for_mime",
    "public_asset_store",
    "sniff_media_mime",
]

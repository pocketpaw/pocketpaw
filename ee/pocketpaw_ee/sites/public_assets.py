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
# (``EEUploadService``) and reach the model as text. See ``router.py`` — the
# endpoint routes by kind and only images land here.
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
)

MAX_ASSET_BYTES = 10 * 1024 * 1024  # 10 MiB — a hero image, not a video.

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


def sniff_image_mime(head: bytes) -> str | None:
    """Return the image mime ``head`` actually proves, or ``None``.

    Unlike the private rail's sniffer there is no fallback to a caller-supplied
    Content-Type — unrecognised bytes are simply not an image.
    """
    for magic, mime in _SIGNATURES:
        if head.startswith(magic):
            return mime
    # WebP is the one format whose marker is not a plain prefix: "RIFF", then a
    # 4-byte little-endian length, then "WEBP" at offset 8.
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


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
    ext = extension_for(mime) or ".bin"
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
        if len(data) > MAX_ASSET_BYTES:
            raise PublicAssetError(
                f"That file is {len(data) // (1024 * 1024)} MB. "
                f"Site images are capped at {MAX_ASSET_BYTES // (1024 * 1024)} MB."
            )

        mime = sniff_image_mime(data[:512])
        if mime is None or mime not in PUBLIC_IMAGE_MIMES:
            raise PublicAssetError(
                "Only PNG, JPEG, GIF and WebP images can be published to a site. "
                "(SVG is not accepted — it can carry script.) "
                "Documents like a PRD go through the chat attachment instead."
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


_NAME_MIMES: tuple[tuple[str, str], ...] = (
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
    "PUBLIC_IMAGE_MIMES",
    "PublicAsset",
    "PublicAssetError",
    "PublicAssetStore",
    "build_key",
    "prefix_for",
    "public_asset_store",
    "sniff_image_mime",
]

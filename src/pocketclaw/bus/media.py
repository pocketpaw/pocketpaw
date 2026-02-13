"""Media download utility for channel adapters.

Downloads incoming media (images, documents, audio, video) to local disk
and returns file paths for populating InboundMessage.media.
"""

import hashlib
import logging
import mimetypes
import re
import time
from pathlib import Path

import httpx

from pocketclaw.config import get_config_dir, get_settings

logger = logging.getLogger(__name__)


def get_media_dir() -> Path:
    """Return the media storage directory, creating it if needed."""
    settings = get_settings()
    if settings.media_download_dir:
        media_dir = Path(settings.media_download_dir)
    else:
        media_dir = get_config_dir() / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def _sanitize_filename(name: str) -> str:
    """Remove unsafe characters from a filename, keeping extension."""
    # Keep only alphanumeric, dots, hyphens, underscores
    sanitized = re.sub(r"[^\w.\-]", "_", name)
    # Collapse multiple underscores
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "file"


def _unique_filename(name: str, mime: str | None = None) -> str:
    """Generate a collision-free filename: {timestamp_hex}_{hash8}_{sanitized_name}."""
    ts_hex = format(int(time.time() * 1000), "x")
    hash8 = hashlib.sha256(f"{time.time_ns()}{name}".encode()).hexdigest()[:8]

    sanitized = _sanitize_filename(name)

    # If no extension, try to guess from mime type
    if "." not in sanitized and mime:
        ext = mimetypes.guess_extension(mime) or ""
        sanitized += ext

    return f"{ts_hex}_{hash8}_{sanitized}"


def build_media_hint(filenames: list[str]) -> str:
    """Build a text hint for attached media files.

    Returns e.g. "\\n[Attached: photo.jpg, doc.pdf]"
    """
    if not filenames:
        return ""
    names = ", ".join(filenames)
    return f"\n[Attached: {names}]"


class MediaDownloader:
    """Downloads and saves media files from channel messages."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        return self._client

    def _check_size(self, data: bytes, name: str) -> None:
        """Raise ValueError if data exceeds configured max size."""
        settings = get_settings()
        max_mb = settings.media_max_file_size_mb
        if max_mb > 0 and len(data) > max_mb * 1024 * 1024:
            raise ValueError(
                f"File '{name}' ({len(data) / 1024 / 1024:.1f} MB) exceeds limit of {max_mb} MB"
            )

    async def save_from_bytes(self, data: bytes, name: str, mime: str | None = None) -> str:
        """Save raw bytes to disk and return the file path.

        Used by adapters that provide file content directly (Telegram, Neonize).
        """
        self._check_size(data, name)
        filename = _unique_filename(name, mime)
        dest = get_media_dir() / filename
        dest.write_bytes(data)
        logger.info("Saved media: %s (%d bytes)", dest, len(data))
        return str(dest)

    async def download_url(
        self,
        url: str,
        name: str | None = None,
        mime: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """Download a URL to disk and return the file path.

        Used by adapters that provide a direct download URL (Discord, Signal, Matrix).
        """
        client = await self._get_client()
        resp = await client.get(url, headers=headers or {})
        resp.raise_for_status()
        data = resp.content

        if name is None:
            # Try to extract filename from URL
            url_path = url.split("?")[0].rsplit("/", 1)[-1]
            name = url_path or "download"

        if mime is None:
            mime = resp.headers.get("content-type", "").split(";")[0].strip() or None

        self._check_size(data, name)
        filename = _unique_filename(name, mime)
        dest = get_media_dir() / filename
        dest.write_bytes(data)
        logger.info("Downloaded media: %s (%d bytes) from %s", dest, len(data), url[:80])
        return str(dest)

    async def download_url_with_auth(
        self,
        url: str,
        auth_header: str,
        name: str | None = None,
        mime: str | None = None,
    ) -> str:
        """Download a URL with an Authorization header.

        Used by adapters that require auth for file downloads (Slack, WhatsApp Business).
        """
        headers = {"Authorization": auth_header}
        return await self.download_url(url, name=name, mime=mime, headers=headers)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


_downloader: MediaDownloader | None = None


def get_media_downloader() -> MediaDownloader:
    """Return a singleton MediaDownloader instance."""
    global _downloader  # noqa: PLW0603
    if _downloader is None:
        _downloader = MediaDownloader()
    return _downloader

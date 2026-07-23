# registry.py — Fetch a starter from npm and turn it into a source map (CS-1b).
#
# Created 2026-07-22. REPLACES `engine.py`, which shelled a vendored TypeScript
# recipe engine through node. That whole apparatus is gone: no vendored template
# tree, no node subprocess, no `--experimental-strip-types`, no node 22.6 floor,
# no `.dockerignore` negation to get wrong. This module downloads a pinned
# tarball, verifies it, and extracts a directory out of it.
#
# ── Integrity is verified, not assumed ──────────────────────────────────────
# The extracted files are installed and executed inside a user's sandbox. A
# pinned VERSION alone still trusts whatever the network returns, so the
# registry's own SRI hash is checked against the pin in the catalog BEFORE
# anything is read out of the archive. A mismatch is a hard failure, never a
# warning — there is no safe way to continue past one.
#
# ── Cached on disk, keyed by what was verified ──────────────────────────────
# A starter is a pinned version, so the tarball is immutable and worth keeping.
# The cache key includes the catalog epoch, so changing the extraction rules
# cannot serve a tree extracted under the old ones.
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import posixpath
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen

from pocketpaw_ee.cloud._core.errors import CloudError, with_cause
from pocketpaw_ee.cloud.codescaffold.domain import CATALOG_EPOCH, Starter

logger = logging.getLogger(__name__)

REGISTRY_BASE = "https://registry.npmjs.org"

# npm publishes these as a few hundred KB at most; the cap is three orders of
# magnitude of headroom and exists to bound a hostile or corrupted response.
MAX_TARBALL_BYTES = 32 * 1024 * 1024

DOWNLOAD_TIMEOUT_SECONDS = 60

#: Files carried as base64 rather than text. A starter's PNG or .ico cannot ride
#: in a `dict[str, str]`, and DROPPING it silently is worse than carrying it —
#: a missing favicon is a mystery, a base64 blob is just a file.
_BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".avif", ".woff", ".woff2", ".ttf", ".otf"}
)


@dataclass
class Template:
    """An extracted starter, ready to materialize."""

    #: UTF-8 text files: {path: contents}.
    files: dict[str, str] = field(default_factory=dict)
    #: Binary files: {path: base64}. Usually one favicon.
    assets: dict[str, str] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files) + len(self.assets)


def cache_dir() -> Path:
    """Where verified tarballs live. Rides the prod data volume by default, the
    same convention `generator_client.artifact_home` uses."""
    raw = os.environ.get("PAW_CODESCAFFOLD_CACHE_DIR")
    base = Path(raw) if raw else Path.home() / ".pocketpaw" / "code-starters"
    base.mkdir(parents=True, exist_ok=True)
    return base


def tarball_url(starter: Starter) -> str:
    """The registry's tarball URL for a pinned version.

    Built rather than looked up: resolving `/latest` would defeat the pin, and
    the layout of this path is part of npm's public contract.
    """
    # A scoped package (@scope/name) puts the bare name in the tarball path.
    bare = starter.package.split("/")[-1]
    return f"{REGISTRY_BASE}/{starter.package}/-/{bare}-{starter.version}.tgz"


def _verify(data: bytes, integrity: str, starter: Starter) -> None:
    """Check the payload against the catalog's SRI hash. Raises on any mismatch.

    Only sha512 is accepted. Supporting a weaker algorithm because a registry
    entry happened to use one would make the check decorative.
    """
    algorithm, _, expected = integrity.partition("-")
    if algorithm != "sha512" or not expected:
        raise CloudError(
            500,
            "codescaffold.bad_integrity_pin",
            f"Starter {starter.id} has an unusable integrity pin",
        )
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if actual != expected:
        # Loud, and fatal. A tarball that does not match its pin is either a
        # corrupted download or a compromised one, and nothing downstream can
        # tell the difference.
        logger.error(
            "codescaffold: integrity MISMATCH for %s@%s (expected %s…, got %s…)",
            starter.package,
            starter.version,
            expected[:16],
            actual[:16],
        )
        raise CloudError(
            502,
            "codescaffold.integrity_mismatch",
            f"The {starter.label} starter failed its integrity check",
        )


def _download(url: str) -> bytes:
    """Blocking fetch. Called via a thread so the event loop is not held."""
    with urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 — https, fixed host
        return response.read(MAX_TARBALL_BYTES + 1)


async def _tarball(starter: Starter) -> bytes:
    """The verified tarball, from disk if we already have it.

    The cache is written only AFTER verification, so a cached file is by
    construction one that passed — and it is re-verified on read anyway, because
    a cache is a place bytes can rot.
    """
    cached = (
        cache_dir() / f"{CATALOG_EPOCH}-{starter.package.replace('/', '+')}-{starter.version}.tgz"
    )
    if cached.is_file():
        data = cached.read_bytes()
        _verify(data, starter.integrity, starter)
        return data

    url = tarball_url(starter)
    try:
        data = await asyncio.to_thread(_download, url)
    except Exception as exc:  # noqa: BLE001 — network, DNS, TLS, timeout all land here
        logger.warning("codescaffold: download failed for %s", url, exc_info=True)
        raise with_cause(
            CloudError(
                503,
                "codescaffold.registry_unreachable",
                f"Could not download the {starter.label} starter",
            ),
            exc,
        ) from exc

    if len(data) > MAX_TARBALL_BYTES:
        raise CloudError(
            502, "codescaffold.tarball_too_large", f"The {starter.label} starter is too large"
        )

    _verify(data, starter.integrity, starter)
    # Write via a temp file and replace, so a crash mid-write cannot leave a
    # truncated tarball that later reads as cached.
    tmp = cached.with_suffix(".tgz.part")
    tmp.write_bytes(data)
    tmp.replace(cached)
    logger.info(
        "codescaffold: cached %s@%s (%d bytes)", starter.package, starter.version, len(data)
    )
    return data


def _target_name(name: str, prefix: str) -> str:
    """Undo npm's dotfile smuggling.

    npm STRIPS a real `.gitignore` out of a published tarball, so every one of
    these projects ships it under an alias — `_gitignore` in create-vite, a bare
    `gitignore` in create-next-app. Restoring it is not cosmetic: without it the
    scaffolded project's first commit includes `node_modules`.
    """
    if prefix:
        return f".{name[len(prefix) :]}" if name.startswith(prefix) else name
    # An empty prefix means the package ships bare aliases. Only the known set is
    # rewritten — a blanket "add a dot to anything extensionless" would rename
    # LICENSE and README.
    return f".{name}" if name in _BARE_DOTFILES else name


_BARE_DOTFILES = frozenset({"gitignore", "npmrc", "env", "env.local", "eslintrc.json"})


def extract(data: bytes, starter: Starter) -> Template:
    """Pull one directory out of the tarball as a source map.

    npm tarballs are rooted at `package/`, so the directory of interest is at
    `package/<subdir>/`. Everything outside it — the CLI's own code, its
    package.json, the other fifteen templates — is skipped.
    """
    root = posixpath.join("package", starter.subdir).rstrip("/") + "/"
    template = Template()

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.replace("\\", "/")
            if not name.startswith(root):
                continue
            relative = name[len(root) :]
            if not relative or ".." in relative.split("/"):
                continue

            parts = relative.split("/")
            parts[-1] = _target_name(parts[-1], starter.dotfile_prefix)
            # A directory component can be smuggled too (create-vite ships
            # `_vscode/`, sv ships `DOT-vscode/`).
            parts[:-1] = [_target_name(p, starter.dotfile_prefix) for p in parts[:-1]]
            out_path = "/".join(parts)

            payload = tar.extractfile(member)
            if payload is None:
                continue
            raw = payload.read()

            if posixpath.splitext(out_path)[1].lower() in _BINARY_SUFFIXES:
                template.assets[out_path] = base64.b64encode(raw).decode("ascii")
                continue
            try:
                template.files[out_path] = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Not on the known-binary list but not text either. Carry it
                # rather than drop it — a silently missing file in a scaffold is
                # a bug report nobody can act on.
                template.assets[out_path] = base64.b64encode(raw).decode("ascii")

    # Merged AFTER extraction so a starter that later starts shipping its own
    # package.json is not silently overridden by ours — the catalog entry is
    # deleted at that point, and this stops being a special case.
    for path, contents in starter.extra_files:
        template.files.setdefault(path, contents)

    # Total count, not just text: a subdir that yielded only binaries is still a
    # subdir that yielded something. Checking `files` alone would report a real
    # extraction as "empty" and lose it.
    if not template.file_count:
        # The subdir moved between versions, or the pin is wrong. Either way this
        # is our bug, not the user's.
        raise CloudError(
            500,
            "codescaffold.template_empty",
            f"The {starter.label} starter contained no files at {starter.subdir}",
        )
    return template


async def fetch_template(starter: Starter) -> Template:
    """Download (or reuse), verify, and extract a starter."""
    data = await _tarball(starter)
    template = extract(data, starter)
    logger.debug(
        "codescaffold.fetch %s@%s -> %d files, %d assets",
        starter.package,
        starter.version,
        len(template.files),
        len(template.assets),
    )
    return template


__all__ = [
    "MAX_TARBALL_BYTES",
    "Template",
    "cache_dir",
    "extract",
    "fetch_template",
    "tarball_url",
]

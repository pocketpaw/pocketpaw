# archive.py — serve a repo's source as a zip, for seeding an in-tab pod (BP-3b).
# Created 2026-07-18 (feat/code-mode).
#
# WHY THIS EXISTS: the BrowserPod runtime originally seeded a project by running
# `git clone` INSIDE the pod. That puts the whole clone on BrowserPod's emulated
# TCP/TLS stack (its TLS-MITM relay), which is the least documented and least
# controllable part of that runtime — the vendor documents neither egress rules
# nor git auth. In practice clones of public repos that clone fine everywhere
# else stalled there, with nothing but kernel TODO noise to debug from.
#
# Fetching the source here instead removes that entire class of failure:
#   • no in-pod networking, so no relay, no egress allowlist, no git-in-Wasm
#   • no CORS problem — GitHub's archive endpoints send no CORS headers, so the
#     browser cannot fetch them directly (the vendor's own reference works around
#     this by fetching through the VM, which is what we are avoiding)
#   • a seam for PRIVATE repos: this runs server-side, where the GitHub App token
#     already lives and never has to reach the client (see repoauth/githubapp)
#
# SSRF: this endpoint takes a repo reference from the caller, so it must never be
# treated as a URL to fetch. `_parse_github_repo` extracts owner/name and we
# BUILD the GitHub URL ourselves; anything that is not a github.com owner/repo is
# refused. No caller-supplied string ever reaches httpx as a URL.
from __future__ import annotations

import logging
import re

import httpx

from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError

logger = logging.getLogger(__name__)

# GitHub's own limits are looser, but a pod is a browser tab — refuse anything
# that would blow the tab's memory rather than streaming it and failing later.
_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024

_FETCH_TIMEOUT_SECONDS = 60.0

# owner/name, each GitHub-legal. Anchored so nothing else can slip through.
_OWNER_RE = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_NAME_RE = r"[A-Za-z0-9_.-]{1,100}"
_SHORTHAND = re.compile(rf"^({_OWNER_RE})/({_NAME_RE})$")
_URL_FORM = re.compile(rf"^(?:https?://)?(?:www\.)?github\.com/({_OWNER_RE})/({_NAME_RE})$")


def _parse_github_repo(repo: str) -> tuple[str, str]:
    """Extract ``(owner, name)`` from a repo reference, or raise.

    Accepts ``owner/repo`` and the github.com URL forms the project registry
    stores. Everything else — another host, an IP, a path traversal, a URL with
    credentials or a port — is refused, because the result is used to build a URL
    this server will fetch.
    """
    candidate = (repo or "").strip()
    if not candidate:
        raise ValidationError("websandbox.invalid_repo", "A repository is required")

    candidate = candidate.removesuffix(".git").rstrip("/")

    match = _SHORTHAND.match(candidate)
    if not match:
        match = _URL_FORM.match(candidate)
    if not match:
        raise ValidationError(
            "websandbox.invalid_repo",
            "Only public github.com repositories can be opened in the in-browser runtime",
        )
    return match.group(1), match.group(2)


async def fetch_repo_archive(
    workspace_id: str,
    user_id: str,
    repo: str,
    ref: str | None = None,
) -> bytes:
    """Return the repo's source as a zip.

    Server-side so the client never has to reach GitHub itself (no CORS) and the
    pod never has to do networking at all. Unauthenticated today, which means
    PUBLIC repos only — the same scope the Daytona path clones. The token seam
    for private repos is deliberate but unbuilt (BP-6): it belongs here, where a
    GitHub App token stays server-side.
    """
    owner, name = _parse_github_repo(repo)
    # Built from validated parts — never from caller input directly.
    url = f"https://api.github.com/repos/{owner}/{name}/zipball"
    if ref:
        # A ref becomes a URL path segment, so it must not be able to climb out
        # of it. The character class alone is NOT enough: it permits '.' and '/',
        # so "../../etc" would traverse the path. Reject dot-dot and any leading
        # or trailing slash explicitly.
        if (
            not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", ref)
            or ".." in ref
            or ref.startswith("/")
            or ref.endswith("/")
        ):
            raise ValidationError("websandbox.invalid_ref", "Invalid git ref")
        url = f"{url}/{ref}"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pocketpaw-codemode",
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_FETCH_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("websandbox: repo archive fetch failed for %s/%s: %s", owner, name, exc)
        raise CloudError(
            502, "websandbox.archive_unreachable", "Could not reach GitHub to fetch the repository"
        ) from exc

    if response.status_code == 404:
        raise CloudError(
            404,
            "websandbox.repo_not_found",
            f"{owner}/{name} was not found. Private repositories are not supported "
            "in the in-browser runtime yet.",
        )
    if response.status_code != 200:
        logger.warning(
            "websandbox: repo archive fetch for %s/%s returned %s",
            owner,
            name,
            response.status_code,
        )
        raise CloudError(
            502,
            "websandbox.archive_failed",
            "GitHub refused to serve this repository's archive",
        )

    content = response.content
    if len(content) > _MAX_ARCHIVE_BYTES:
        raise CloudError(
            413,
            "websandbox.archive_too_large",
            "This repository is too large to open in the in-browser runtime",
        )

    logger.info(
        "websandbox: served repo archive %s/%s (%d bytes) to workspace=%s user=%s",
        owner,
        name,
        len(content),
        workspace_id,
        user_id,
    )
    return content


__all__ = ["fetch_repo_archive"]

# wire.py — point the VM's git remote at the Code Mode proxy (CM-3d).
# Created 2026-07-16 (feat/code-mode): after a repo is cloned into the sandbox,
# this repoints ``origin`` from the clean github.com URL to the broker, so an
# in-VM ``git push``/``fetch`` flows through the token-injecting proxy instead of
# failing (the VM has no GitHub credential — by design). The broker URL embeds a
# signed ticket (basic-auth password) that is scoped to this ``(sandbox, repo)``
# and is NOT the GitHub token.
#
# Gated on a PUBLIC backend URL: the Daytona VM lives in the cloud and cannot
# reach ``localhost``, so wiring is skipped (push simply stays unavailable, the
# clone is still usable) unless ``POCKETPAW_PUBLIC_BASE_URL`` names a non-local
# host. The embedded ticket is never logged.

from __future__ import annotations

import logging
import os
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from pocketpaw_ee.cloud.codegit.ticket import sign_ticket
from pocketpaw_ee.cloud.daytona.client import DaytonaClient

logger = logging.getLogger(__name__)

# The basic-auth username is cosmetic — only the ticket (password) is verified.
_PROXY_USER = "x-paw-git"
_REMOTE = "origin"
_SET_REMOTE_TIMEOUT_SECONDS = 30
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _public_base_url() -> str | None:
    """The public backend origin the VM's git can reach, or ``None`` when local.

    Reads ``POCKETPAW_PUBLIC_BASE_URL`` (the same var the SSO callback uses). A
    localhost / loopback host means we're in local dev where a cloud VM can't
    reach us — return ``None`` so push wiring is skipped rather than writing a
    dead remote.
    """
    raw = os.environ.get("POCKETPAW_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return None
    host = urlsplit(raw).hostname or ""
    if host in _LOCAL_HOSTS:
        return None
    return raw


def _proxy_remote_url(base_url: str, ticket: str, repo_full: str) -> str:
    """Build ``https://x-paw-git:<ticket>@<host>/api/v1/codegit/<owner>/<repo>``."""
    parts = urlsplit(base_url)
    netloc = f"{_PROXY_USER}:{ticket}@{parts.netloc}"
    path = f"{parts.path.rstrip('/')}/api/v1/codegit/{repo_full}"
    return urlunsplit((parts.scheme, netloc, path, "", ""))


async def wire_push_remote(
    daytona: DaytonaClient,
    sandbox_id: str,
    workspace_id: str,
    user_id: str,
    repo_full: str,
    project_dir: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Repoint the VM's ``origin`` at the git proxy so in-VM push/fetch works.

    Mints a ``(sandbox, repo)`` ticket, builds the broker remote URL, and runs
    ``git remote set-url origin`` in the VM. Returns ``True`` when wired, ``False``
    when skipped (no public URL — push stays unavailable, the clone is unaffected).
    The ticket-bearing URL is NEVER logged.
    """
    base_url = _public_base_url()
    if base_url is None:
        logger.info(
            "codegit.wire: POCKETPAW_PUBLIC_BASE_URL is local/unset — in-VM git push "
            "disabled for sandbox=%s (clone unaffected)",
            sandbox_id,
        )
        return False

    ticket = sign_ticket(workspace_id, user_id, sandbox_id, repo_full, now=now)
    remote_url = _proxy_remote_url(base_url, ticket, repo_full)
    # Single-quote the URL for the shell; the ticket is base64url + dots, so it
    # contains no quote to break out of. Never log ``remote_url`` (carries the ticket).
    await daytona.execute_command(
        sandbox_id,
        f"git remote set-url {_REMOTE} '{remote_url}'",
        cwd=project_dir,
        timeout=_SET_REMOTE_TIMEOUT_SECONDS,
    )
    logger.info(
        "codegit.wire: sandbox=%s origin repointed at the git proxy for repo=%s",
        sandbox_id,
        repo_full,
    )
    return True


__all__ = ["wire_push_remote"]

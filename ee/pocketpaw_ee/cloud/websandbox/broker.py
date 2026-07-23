# broker.py — Code Mode private-repo clone with token isolation (CM-3c).
# Created 2026-07-16 (feat/code-mode): the "clone a PRIVATE repo into the sandbox
# without the credential ever entering the VM" half of the WC-6 token-isolation
# guarantee. githubapp.py mints the short-lived, single-repo token; THIS module is
# the broker that uses it — server-side — to get the repo's files into the VM.
#
# THE HARD INVARIANT: the token NEVER enters the VM. git operations that carry the
# token run in the BACKEND process (the same trust boundary that already holds the
# App private key), not in the sandbox. Concretely, ``clone_into_vm``:
#   1. clones the tokenized upstream into a throwaway host temp dir (token only in
#      the backend subprocess argv, never on the wire to Daytona);
#   2. rewrites the clone's ``origin`` remote to the CLEAN (token-free) upstream
#      URL, so the ``.git/config`` that ships to the VM carries no credential;
#   3. tars the working tree (incl. the scrubbed ``.git``) and ships ONE tarball
#      into the VM via ``upload_bytes`` + ``tar -xzf`` — the exact vehicle the
#      durability layer already uses — then wipes the host temp dir.
# The VM receives only files + a token-free git remote. In-VM ``git checkout -b``
# (the WC-5a feature branch) works because ``.git`` is present and local; in-VM
# fetch/push-with-auth is a later slice (the smart-HTTP proxy that injects the
# token upstream on each request — see githubapp.py's header comment).
#
# ``resolve_repo_token`` is the routing oracle: given a repo URL and the caller's
# connections, it mints a repo-scoped token from the FIRST connection whose
# installation can actually reach the repo (a mint that GitHub declines means the
# installation can't see it — try the next). Returns ``None`` when no connection
# can auth the repo, so ``provision.open_sandbox`` cleanly falls back to the
# public, credential-free in-VM clone.
#
# Provider-agnostic by construction (see repoauth.py): the token minting and the
# clone-URL auth scheme both dispatch on ``ScopedRepoToken.provider``. GitHub is
# the only implemented provider today; a new one adds a branch, nothing else.
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse, urlunparse

from pocketpaw_ee.cloud._core.errors import CloudError, with_cause
from pocketpaw_ee.cloud.daytona.client import DaytonaClient
from pocketpaw_ee.cloud.websandbox.repoauth import (
    ProviderId,
    ScopedRepoToken,
    get_repo_auth_provider,
)

logger = logging.getLogger(__name__)

# The in-VM staging path for the shipped tarball (outside the workspace dir so it
# is never included in a later snapshot, and removed right after extraction).
_BROKER_TMP = "/tmp/ws-broker.tgz"  # noqa: S108 — a sandbox VM path, not host

# Server-side git/tar timeouts (seconds). A cold clone of a large private repo can
# take a while; the tar is local and fast.
_CLONE_TIMEOUT_SECONDS = 180
_SCRUB_TIMEOUT_SECONDS = 30
_TAR_TIMEOUT_SECONDS = 120

# Cap the packed tarball before shipping it into the VM, mirroring the durability
# snapshot cap — a runaway repo must not blow up memory or the VM disk.
_DEFAULT_BROKER_MAX_MB = 500.0

# A callable that clones + packs the repo server-side and returns the tar bytes.
# Injected in tests so no real ``git``/``tar`` subprocess runs.
PackRepo = Callable[[ScopedRepoToken, str, str | None], Awaitable[bytes]]


def _broker_max_bytes() -> int:
    """Packed-tarball size cap in bytes (``POCKETPAW_WEBSANDBOX_BROKER_MAX_MB``)."""
    raw = os.environ.get("POCKETPAW_WEBSANDBOX_BROKER_MAX_MB", "").strip()
    mb = _DEFAULT_BROKER_MAX_MB
    if raw:
        try:
            mb = float(raw)
        except ValueError:
            logger.warning(
                "ignoring non-numeric POCKETPAW_WEBSANDBOX_BROKER_MAX_MB=%r; using %s", raw, mb
            )
    return int(mb * 1024 * 1024)


# ---------------------------------------------------------------------------
# Routing: which connection (if any) can authenticate this repo?
# ---------------------------------------------------------------------------


def repo_full_name(repo_url: str) -> str | None:
    """Extract ``owner/repo`` from an https git URL, or ``None`` if it doesn't fit.

    Accepts ``https://github.com/owner/repo(.git)?`` with or without a trailing
    slash. A non-http(s) URL, a URL with fewer than two path segments, or one
    carrying embedded credentials returns ``None`` (the caller then treats the
    repo as un-brokered and clones it as public).
    """
    parsed = urlparse((repo_url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    repo = repo.removesuffix(".git")
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


async def resolve_repo_token(
    workspace_id: str,
    user_id: str,
    repo_url: str,
    *,
    provider_kind: ProviderId = ProviderId.GITHUB,
    now: object = None,
) -> ScopedRepoToken | None:
    """Mint a repo-scoped token for ``repo_url`` from the caller's connections.

    Iterates the caller's connections (tenant + owner scoped) for the given
    provider and mints a single-repo token from the FIRST one whose installation
    can reach the repo. A mint that GitHub declines (the installation can't see
    that repo) is expected routing signal, not an error — it is swallowed and the
    next connection is tried. Returns ``None`` when the repo isn't a recognizable
    provider repo, the provider isn't configured, the caller has no connections,
    or none of them can authenticate the repo — in every such case the caller
    falls back to the credential-free public clone.
    """
    repo_full = repo_full_name(repo_url)
    if repo_full is None:
        return None

    provider = get_repo_auth_provider(provider_kind)
    if provider is None:
        return None

    # Lazy import keeps the module load order clean (codeconnect.connect imports
    # websandbox.repoauth; importing codeconnect.service at call time avoids any
    # import-time coupling between the two entities).
    from pocketpaw_ee.cloud.codeconnect import service as codeconnect_service

    connections = await codeconnect_service.list_connections(workspace_id, user_id)
    for conn in connections:
        if conn.provider != provider_kind.value:
            continue
        try:
            return await provider.mint_repo_token(conn.installation_id, repo_full, now=now)
        except Exception:  # noqa: BLE001 — a failed mint just means "not this connection"
            logger.debug(
                "broker.resolve: installation %s can't auth %s — trying next",
                conn.installation_id,
                repo_full,
                exc_info=True,
            )
            continue
    return None


# ---------------------------------------------------------------------------
# Clone: server-side, token-isolated, into the VM.
# ---------------------------------------------------------------------------


async def clone_into_vm(
    daytona: DaytonaClient,
    sandbox_id: str,
    token: ScopedRepoToken,
    project_dir: str,
    *,
    clean_url: str,
    branch: str | None = None,
    pack: PackRepo | None = None,
) -> None:
    """Clone a private repo into the VM with the token NEVER entering the VM.

    Packs the repo server-side (``pack``, default :func:`_clone_and_pack_repo` —
    clone the tokenized upstream into a host temp dir, scrub ``origin`` to the
    clean URL, tar it), size-guards the tarball, then ships it into the VM via
    ``upload_bytes`` + ``tar -xzf``. ``pack`` is injectable so tests never spawn a
    real ``git``/``tar``.

    ``clean_url`` is the token-free upstream (e.g.
    ``https://github.com/owner/repo.git``) — it becomes the VM's ``origin`` and is
    also what the packer injects the token into for its own (server-side) clone.
    """
    packer = pack or _clone_and_pack_repo
    try:
        tar_bytes = await packer(token, clean_url, branch)
    except CloudError:
        raise
    except Exception as exc:  # noqa: BLE001 — uniform failure surface; never leak the token
        raise with_cause(
            CloudError(502, "websandbox.broker_clone_failed", "Failed to clone the repository"),
            exc,
        ) from exc

    cap = _broker_max_bytes()
    if len(tar_bytes) > cap:
        raise CloudError(
            413,
            "websandbox.broker_repo_too_large",
            f"Repository is {len(tar_bytes) / 1024 / 1024:.1f} MB packed, over the "
            f"{cap / 1024 / 1024:.0f} MB limit",
        )

    # Ship ONE tarball into the VM and extract it over the workspace dir — the same
    # vehicle durability.restore_workspace uses. The tarball carries a token-free
    # .git (origin already scrubbed server-side).
    untar = (
        f"mkdir -p {project_dir} && tar -xzf {_BROKER_TMP} -C {project_dir} && rm -f {_BROKER_TMP}"
    )
    await daytona.upload_bytes(sandbox_id, tar_bytes, _BROKER_TMP)
    await daytona.execute_command(sandbox_id, untar)
    logger.info(
        "broker.clone: sandbox=%s repo=%s cloned via broker (%d bytes, token isolated)",
        sandbox_id,
        token.repo,
        len(tar_bytes),
    )


def _authenticated_url(provider: ProviderId, clean_url: str, token: str) -> str:
    """Inject the provider's token auth into ``clean_url`` for a SERVER-SIDE clone.

    GitHub installation tokens authenticate as the ``x-access-token`` basic-auth
    user. The result is used only in the backend git subprocess and is scrubbed
    from the clone before anything ships to the VM.
    """
    if provider is not ProviderId.GITHUB:
        raise CloudError(
            501,
            "websandbox.broker_provider_unsupported",
            f"Broker clone is not implemented for provider {provider.value!r}",
        )
    parsed = urlparse(clean_url)
    host = parsed.hostname or ""
    netloc = f"x-access-token:{token}@{host}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _redact(text: str, secret: str) -> str:
    """Replace ``secret`` in ``text`` so a token can't reach a log or error body."""
    return text.replace(secret, "***") if secret else text


async def _clone_and_pack_repo(token: ScopedRepoToken, clean_url: str, branch: str | None) -> bytes:
    """Clone ``clean_url`` (with the token) into a host temp dir; return a tar.gz.

    Server-side only: ``git`` runs in the backend process, so the token lives in
    the backend subprocess argv and the throwaway temp dir — never on the wire to
    Daytona and never in the VM. Steps: clone the tokenized URL (with any host
    credential helper disabled so nothing caches the token) → ``remote set-url
    origin`` back to the clean URL (scrub the credential from ``.git/config``
    before it ships) → ``tar -czf -`` the tree (incl. the scrubbed ``.git``). The
    temp dir is always removed. Git/tar stderr is redacted of the token before it
    reaches any log or error.
    """
    git = shutil.which("git")
    if git is None:
        raise CloudError(
            503, "websandbox.broker_git_unavailable", "Server-side git is not available"
        )

    auth_url = _authenticated_url(token.provider, clean_url, token.token)
    tmp = tempfile.mkdtemp(prefix="paw-broker-")
    dest = os.path.join(tmp, "repo")
    try:
        clone_args = [git, "-c", "credential.helper=", "clone", "--origin", "origin"]
        if branch:
            clone_args += ["--branch", branch]
        clone_args += [auth_url, dest]
        await _run(clone_args, secret=token.token, timeout=_CLONE_TIMEOUT_SECONDS)

        # Scrub the token from the on-disk remote BEFORE packing, so the .git/config
        # that ships to the VM points at the clean, credential-free upstream.
        await _run(
            [git, "-C", dest, "remote", "set-url", "origin", clean_url],
            secret=token.token,
            timeout=_SCRUB_TIMEOUT_SECONDS,
        )

        return await _run_capture(
            ["tar", "-czf", "-", "-C", dest, "."],
            secret=token.token,
            timeout=_TAR_TIMEOUT_SECONDS,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _run(args: list[str], *, secret: str, timeout: int) -> None:
    """Run a subprocess to completion, raising a token-redacted CloudError on failure."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise CloudError(
            504, "websandbox.broker_clone_failed", "The repository clone timed out"
        ) from exc
    if proc.returncode != 0:
        detail = _redact((err or b"").decode(errors="replace").strip(), secret)
        logger.warning("broker: %s failed (rc=%s): %s", args[0], proc.returncode, detail)
        raise CloudError(502, "websandbox.broker_clone_failed", "Failed to clone the repository")


async def _run_capture(args: list[str], *, secret: str, timeout: int) -> bytes:
    """Run a subprocess and return its stdout bytes; token-redacted CloudError on failure."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise CloudError(
            504, "websandbox.broker_clone_failed", "Packing the repository timed out"
        ) from exc
    if proc.returncode != 0:
        detail = _redact((err or b"").decode(errors="replace").strip(), secret)
        logger.warning("broker: %s failed (rc=%s): %s", args[0], proc.returncode, detail)
        raise CloudError(502, "websandbox.broker_clone_failed", "Failed to pack the repository")
    return out


__all__ = [
    "clone_into_vm",
    "repo_full_name",
    "resolve_repo_token",
]

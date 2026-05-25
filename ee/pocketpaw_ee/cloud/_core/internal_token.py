# ee/pocketpaw_ee/cloud/_core/internal_token.py
# Created: 2026-05-25 (PR #1222 R1 follow-up) — process-local secret used
# to authenticate the loopback bypass on POST /pockets/{id}/spec/merge
# (and the matching read bypass on GET /pockets/{id}). The original MVP
# accepted any caller that sent ``X-PocketPaw-Internal: true`` + workspace
# / user headers from 127.0.0.1; a same-machine adversary could forge the
# tenancy. The token closes that gap: it is generated once per host,
# stored at ``~/.pocketpaw/internal-token`` with 0600 permissions, exported
# as ``POCKETPAW_INTERNAL_TOKEN`` so child subprocesses (the pocket
# specialist's Claude Code skill) inherit it, and compared via
# ``secrets.compare_digest`` on the endpoint.
#
# The bypass remains dev-grade. The follow-up PR (PR-2 in the captain's
# plan) replaces this with a short-lived JWT minted by the cloud auth
# stack. Until then, this module is the only thing standing between a
# local non-PocketPaw process and a tenancy forgery.
"""Process-local secret for the loopback internal bypass on the spec-merge endpoint.

Loaded lazily — the first call to ``ensure_internal_token`` generates +
writes the file (or reads it if it already exists) and sets the
``POCKETPAW_INTERNAL_TOKEN`` env var on the parent process so the
pocket-specialist subprocess inherits it. Subsequent calls are no-ops.

The token never appears in logs, error responses, or audit rows.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

INTERNAL_TOKEN_ENV_VAR = "POCKETPAW_INTERNAL_TOKEN"
INTERNAL_TOKEN_HEADER = "X-PocketPaw-Internal-Token"
_TOKEN_FILENAME = "internal-token"


def _default_token_path() -> Path:
    """Resolve the on-disk location of the internal token.

    ``~/.pocketpaw/internal-token`` matches every other PocketPaw secret
    (audit log, config, soul files) — one well-known directory per host.
    """
    return Path.home() / ".pocketpaw" / _TOKEN_FILENAME


def _read_existing_token(path: Path) -> str | None:
    """Return the on-disk token, or None if the file doesn't exist or is empty."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:  # noqa: BLE001
        logger.warning("internal-token: failed to read %s (%s) — will regenerate", path, exc)
        return None
    return raw or None


def _write_token_atomic(path: Path, token: str) -> None:
    """Write ``token`` to ``path`` with 0600 perms, atomically.

    We write to ``path.tmp`` first then rename so a crash mid-write
    cannot leave the dashboard with an empty token file (which would
    fall through to a fresh generate-and-write on the next boot — fine,
    but defensively avoid the case).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # ``opener`` lets us pin the file mode at create time so the secret
    # never lives at the default 0644 even briefly.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
            fh.write("\n")
    except Exception:
        # If the write failed, clean up the temp file so we don't leak
        # a half-written secret.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    # Restrict perms in case the umask widened them anyway (belt + braces).
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def ensure_internal_token(path: Path | None = None) -> str:
    """Return the host's internal token, generating it on first call.

    Idempotent: if the file already exists, reuse it (so a dashboard
    restart doesn't invalidate the token already shipped to running
    Claude Code subprocesses). Also exports the token to
    ``POCKETPAW_INTERNAL_TOKEN`` on the parent process so child
    subprocesses inherit it at spawn time.

    Setting an env var from this function is safe — it runs ONCE at
    dashboard boot, never from a request handler, so there is no race
    on concurrent requests.
    """
    target = path or _default_token_path()
    token = _read_existing_token(target)
    if token is None:
        token = secrets.token_urlsafe(32)
        _write_token_atomic(target, token)
        logger.info(
            "internal-token: generated new token at %s (perms 0600)",
            target,
        )
    else:
        logger.info("internal-token: loaded existing token from %s", target)

    # Export to env so the pocket-specialist subprocess inherits it
    # at spawn time (same mechanism used by ``subprocess_env`` in
    # ``ee/pocketpaw_ee/extensions.py``). Set unconditionally so a
    # stale env var from a previous host can't poison the lookup.
    os.environ[INTERNAL_TOKEN_ENV_VAR] = token
    return token


def get_internal_token() -> str | None:
    """Return the currently-loaded token, or None if boot hasn't run.

    Used by the endpoint bypass check and the specialist adapter's
    ``auth_headers`` builder. A return of None means either:

      1. The cloud app hasn't booted yet (unit test against the
         endpoint without calling ``mount_cloud``), or
      2. Someone called this from a worker process that didn't inherit
         the env var.

    Callers must treat None as "token not configured" — the endpoint
    rejects the bypass instead of falling through to a no-token compare.
    """
    return os.environ.get(INTERNAL_TOKEN_ENV_VAR) or None


__all__ = [
    "INTERNAL_TOKEN_ENV_VAR",
    "INTERNAL_TOKEN_HEADER",
    "ensure_internal_token",
    "get_internal_token",
]

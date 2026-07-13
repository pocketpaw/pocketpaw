# Token Store — file-based OAuth token persistence at ~/.pocketpaw/oauth/.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem
# 2026-06-08: re-keyed tokens by (service, user_id) so two users on the same
#   service no longer collide (VIP Onboarding Phase B foundation). Back-compat:
#   user_id=None keeps the legacy ``{service}.json`` path untouched; per-user
#   tokens land in ``{service}__{sanitized_user_id}.json``. user_id is sanitized
#   so emails / path chars can't escape the oauth dir. OAuthTokens carries an
#   optional user_id so ``save(tokens)`` knows which bucket to write.

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pocketpaw.config import get_config_dir

logger = logging.getLogger(__name__)

# Characters safe to use verbatim in the on-disk user segment. Anything else
# (e.g. ``@``, ``/``, ``..``) is replaced so a user_id can never path-traverse
# out of the oauth dir or collide with the service segment separator.
_SAFE_USER_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# Separator between the service segment and the user segment in a filename.
# Double underscore keeps a single underscore inside service/user names from
# being mistaken for the boundary.
_USER_SEP = "__"


@dataclass
class OAuthTokens:
    """OAuth 2.0 token set for a service.

    ``user_id`` scopes the token to a single user (e.g. an invited workspace
    member connecting their own Gmail). ``None`` is the shared / single-user
    default and maps to the legacy service-only storage path.
    """

    service: str
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float | None = None  # Unix timestamp
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None


def _get_oauth_dir() -> Path:
    """Get/create the OAuth token directory."""
    d = get_config_dir() / "oauth"
    d.mkdir(exist_ok=True)
    return d


def _user_segment(user_id: str) -> str:
    """Build a filesystem-safe, collision-resistant segment for a user_id.

    We keep a sanitized, human-readable prefix for debuggability, then append
    a short hash of the *raw* user_id so two distinct ids that sanitize to the
    same prefix (``a@x.com`` vs ``a/x.com``) never share a file.
    """
    safe = _SAFE_USER_CHARS.sub("-", user_id).strip("-") or "u"
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def _token_filename(service: str, user_id: str | None) -> str:
    """Map (service, user_id) to a token filename.

    ``user_id=None`` returns the legacy ``{service}.json`` path so existing
    single-user installs keep loading without migration.
    """
    if user_id is None:
        return f"{service}.json"
    return f"{service}{_USER_SEP}{_user_segment(user_id)}.json"


class TokenStore:
    """File-based token store at ~/.pocketpaw/oauth/.

    Tokens are keyed by (service, user_id):
    - ``user_id=None`` (default) → ``{service}.json`` (legacy, single-user).
    - ``user_id="alice"`` → ``{service}__alice-<hash>.json`` (per-user isolation).

    Files are chmod 0600 (owner-only read/write).
    """

    def save(self, tokens: OAuthTokens, user_id: str | None = None) -> None:
        """Save tokens for a (service, user_id).

        ``user_id`` precedence: an explicit argument wins; otherwise the
        ``tokens.user_id`` field is used. This lets callers either pass the
        scope alongside the data or stamp it onto the dataclass — both land
        in the same bucket.
        """
        effective_user = user_id if user_id is not None else tokens.user_id
        # Keep the persisted blob's user_id in sync with where it's stored.
        tokens.user_id = effective_user
        path = _get_oauth_dir() / _token_filename(tokens.service, effective_user)
        data = asdict(tokens)
        path.write_text(json.dumps(data, indent=2))
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        logger.info(
            "Saved OAuth tokens for %s%s",
            tokens.service,
            f" (user {effective_user})" if effective_user else "",
        )

    def load(self, service: str, user_id: str | None = None) -> OAuthTokens | None:
        """Load tokens for a (service, user_id). Returns None if not found."""
        path = _get_oauth_dir() / _token_filename(service, user_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
            # Legacy files (written before the per-user key) have no user_id
            # field; OAuthTokens defaults it to None, so old blobs still load.
            return OAuthTokens(**data)
        except Exception as e:
            logger.warning("Failed to load tokens for %s: %s", service, e)
            return None

    def delete(self, service: str, user_id: str | None = None) -> bool:
        """Delete tokens for a (service, user_id). Returns True if deleted."""
        path = _get_oauth_dir() / _token_filename(service, user_id)
        if path.exists():
            path.unlink()
            logger.info(
                "Deleted OAuth tokens for %s%s",
                service,
                f" (user {user_id})" if user_id else "",
            )
            return True
        return False

    def list_services(self) -> list[str]:
        """List all services that have stored tokens (deduped across users).

        A service connected by several users still appears once. The service
        segment is everything before the ``__`` user separator (legacy
        single-user files have no separator, so the whole stem is the service).
        """
        oauth_dir = _get_oauth_dir()
        services = {f.stem.split(_USER_SEP, 1)[0] for f in oauth_dir.glob("*.json")}
        return sorted(services)

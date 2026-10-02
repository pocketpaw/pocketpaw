# Public base URL — the one accessor for ``POCKETPAW_PUBLIC_BASE_URL``.
#
# Created 2026-10-02 (feat/studio-templates): the dup-ratchet "direct env read of
# base URL" rule asks for one settings accessor instead of copies of
# ``os.environ.get("POCKETPAW_PUBLIC_BASE_URL", ...)``. Studio templates and site
# template assets read it here; the remaining copies (auth/social, auth/sso,
# notifications/email, ...) move onto it at touch time.
#
# Read per call (tests monkeypatch the env). Unset → the local dev default; set
# to blank → "" (callers that treat blank as "not configured" keep working).

from __future__ import annotations

import os

DEFAULT_PUBLIC_BASE_URL = "http://localhost:8888"


def public_base_url() -> str:
    """The deployment's public origin, no trailing slash."""
    return os.environ.get("POCKETPAW_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip().rstrip("/")


__all__ = ["DEFAULT_PUBLIC_BASE_URL", "public_base_url"]

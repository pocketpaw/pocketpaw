"""Daytona configuration — read from environment.

Variables:
  DAYTONA_API_URL   — Base URL for the Daytona server API
                      (e.g. https://app.daytona.io/api)
  DAYTONA_API_KEY   — API key for Daytona authentication

Moved from OSS to EE: 2026-06-24
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def daytona_api_url() -> str:
    """Return the Daytona API base URL, or raise if unset."""
    url = os.environ.get("DAYTONA_API_URL", "").strip().rstrip("/")
    if not url:
        logger.warning("DAYTONA_API_URL is not set — Daytona integration disabled")
    return url


def daytona_api_key() -> str:
    """Return the Daytona API key, or raise if unset."""
    key = os.environ.get("DAYTONA_API_KEY", "").strip()
    if not key:
        logger.warning("DAYTONA_API_KEY is not set — Daytona integration disabled")
    return key


def daytona_enabled() -> bool:
    """Return True if Daytona is configured (both URL and API key are set)."""
    return bool(daytona_api_url() and daytona_api_key())

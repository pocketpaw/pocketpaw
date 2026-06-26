# ee/pocketpaw_ee/catalog/config.py — deployment-global config for the Model
# Catalog (MCG-1). The catalog's source of truth is a self-hosted LiteLLM proxy;
# its base URL + (optional) admin key come from the environment, NOT hardcoded,
# following the same env-helper pattern as the Recall provider client
# (ee/pocketpaw_ee/cloud/meetings/providers/recall/client.py).
#
#   LITELLM_PROXY_URL      — proxy base URL. Default http://localhost:4000.
#   LITELLM_PROXY_API_KEY  — proxy admin/virtual key (optional). When set it is
#                            sent as a Bearer token on every proxy call so a
#                            key-protected proxy authorizes the catalog reads.
#   CATALOG_CACHE_TTL_SECONDS  — assembled-catalog in-process TTL. Default 300.
#   CATALOG_MODELS_DEV_ENABLED — best-effort models.dev enrichment toggle.
#                                Default "true"; set "false"/"0" to run
#                                LiteLLM-only (no outbound models.dev fetch).
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1): new config module.

from __future__ import annotations

import os

# Defaults — referenced by tests and the example deploy config.
DEFAULT_PROXY_URL = "http://localhost:4000"
DEFAULT_CACHE_TTL_SECONDS = 300


def litellm_proxy_url() -> str:
    """Resolve the LiteLLM proxy base URL for this deployment.

    Trailing slashes are trimmed so callers can join paths with a leading
    ``/`` without producing a double slash. Falls back to ``http://localhost:4000``
    (a local proxy) when the env var is unset or blank.
    """
    raw = os.environ.get("LITELLM_PROXY_URL", "").strip() or DEFAULT_PROXY_URL
    return raw.rstrip("/")


def litellm_proxy_api_key() -> str | None:
    """Resolve the LiteLLM proxy admin/virtual key, or None when unset.

    Optional: a proxy with no key requirement (e.g. a local dev proxy) needs no
    value. When present it becomes the Bearer token on every proxy request.
    """
    key = os.environ.get("LITELLM_PROXY_API_KEY", "").strip()
    return key or None


def cache_ttl_seconds() -> int:
    """In-process TTL (seconds) for the assembled catalog. Default 300.

    A non-numeric or non-positive value falls back to the default so a bad env
    value can never disable caching or crash assembly.
    """
    raw = os.environ.get("CATALOG_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_CACHE_TTL_SECONDS


def models_dev_enabled() -> bool:
    """Whether to attempt best-effort models.dev enrichment. Default True.

    Enrichment is always non-blocking (a failed fetch falls back to LiteLLM-only
    data); this flag lets an operator skip the outbound call entirely.
    """
    raw = os.environ.get("CATALOG_MODELS_DEV_ENABLED", "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}

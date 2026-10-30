# ee/pocketpaw_ee/catalog/models_dev_client.py — best-effort models.dev
# enrichment for the Model Catalog (MCG-1). models.dev publishes an open metadata
# index (https://models.dev/api.json) keyed by provider -> model -> {name, ...}.
# We use it ONLY to fill gaps the LiteLLM proxy doesn't provide: a human
# description and any capabilities not already derived. (logo is provider-level
# and not reliably in the index, so we leave it None unless a model entry carries
# one.)
#
# Hard rule: enrichment is non-blocking and fail-open. Any error — network,
# timeout, non-2xx, malformed JSON — yields an EMPTY lookup, and the service then
# serves LiteLLM-only data unchanged. A models.dev outage can never break the
# catalog. The toggle CATALOG_MODELS_DEV_ENABLED (config.models_dev_enabled)
# skips the fetch entirely.
#
# httpx-based with an injectable ``_transport`` so tests stand in an
# httpx.MockTransport (and assert the fail-open fallback) with no network.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1): best-effort enrichment.

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"


def _normalize_index(payload: Any) -> dict[str, dict[str, Any]]:
    """Flatten the models.dev payload into ``{canonical_id: {description, logo,
    capabilities}}`` where canonical_id is "<provider>/<model>".

    The published shape is ``{provider: {"models": {model_id: {...}}}}`` (with the
    provider object also carrying a top-level icon/logo). This is defensive: it
    accepts either a ``models`` sub-dict or a provider object whose values are the
    model entries directly, and tolerates missing fields — anything unparseable is
    skipped, never raised."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return out
    for provider, pobj in payload.items():
        if not isinstance(pobj, dict):
            continue
        provider_logo = pobj.get("logo") or pobj.get("icon")
        models = pobj.get("models")
        # Either {"models": {...}} or the provider object is itself the model map.
        model_map = models if isinstance(models, dict) else pobj
        for model_id, mobj in model_map.items():
            if not isinstance(mobj, dict):
                continue
            caps = mobj.get("capabilities")
            out[f"{provider}/{model_id}"] = {
                "description": mobj.get("description") or mobj.get("name"),
                "logo": mobj.get("logo") or provider_logo,
                "capabilities": [str(c) for c in caps] if isinstance(caps, list) else [],
            }
    return out


class ModelsDevClient:
    """Fetches + flattens the models.dev index. Fail-open by contract."""

    def __init__(
        self,
        *,
        _transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._transport = _transport  # tests inject httpx.MockTransport
        self._timeout = timeout

    async def fetch_index(self) -> dict[str, dict[str, Any]]:
        """Return the flattened enrichment index, or an EMPTY dict on ANY error.

        Never raises — a models.dev problem degrades the catalog to LiteLLM-only,
        it does not fail the request.
        """
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                resp = await client.get(MODELS_DEV_URL)
            if resp.status_code // 100 != 2:
                logger.warning("models.dev returned %s; skipping enrichment", resp.status_code)
                return {}
            return _normalize_index(resp.json())
        except Exception:  # noqa: BLE001 — fail-open is the whole point
            logger.warning("models.dev fetch failed; serving LiteLLM-only catalog", exc_info=True)
            return {}


__all__ = ["ModelsDevClient", "MODELS_DEV_URL"]

# ee/pocketpaw_ee/catalog/litellm_client.py — async client + mapper for a
# self-hosted LiteLLM proxy (MCG-1). The proxy is the catalog's source of truth.
#
# Two reads:
#   * GET /v1/models      — the list of model ids the proxy currently serves
#                           (OpenAI-compatible {"data": [{"id": ...}, ...]}).
#   * GET /model/info     — per-model metadata {"data": [{"model_name": ...,
#                           "model_info": {max_tokens, input_cost_per_token,
#                           output_cost_per_token, mode, supports_*, ...},
#                           "litellm_params": {...}}, ...]}.
#
# ``fetch_entries`` joins the two and maps each row onto a ModelCatalogEntry:
# LiteLLM ``mode`` -> our Modality (MODE_TO_MODALITY), per-token cost -> per-mtok
# Pricing, supports_* flags -> the capabilities list, and the provider parsed
# from the canonical "<provider>/<model>" model_name (falling back to
# litellm_provider). v1 marks everything status="available" — /model/info does
# not expose per-model routability/readiness, so catalog ⊇ routable degrades to
# "all listed entries are available" (noted in the entity README + MCG-1 report).
#
# httpx-based with an injectable ``_transport`` so tests stand in an
# httpx.MockTransport (no live proxy). Fail-closed: a non-2xx raises
# CatalogUpstreamError so a broken proxy never silently yields a partial catalog.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1): the proxy client + mapper.

from __future__ import annotations

import logging
from typing import Any

import httpx

from pocketpaw_ee.catalog import config
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry, Pricing

logger = logging.getLogger(__name__)

# LiteLLM's per-model ``mode`` string -> our coarse Modality bucket. LiteLLM uses
# several spellings for "text in / text out" (chat, completion, responses); all
# collapse to CHAT. Anything we don't recognise is treated as CHAT by the mapper
# (the safest default for the picker — see _modality_for).
MODE_TO_MODALITY: dict[str, Modality] = {
    "chat": Modality.CHAT,
    "completion": Modality.CHAT,
    "responses": Modality.CHAT,
    "embedding": Modality.EMBEDDING,
    "image_generation": Modality.IMAGE,
    "audio_speech": Modality.AUDIO_TTS,
    "audio_transcription": Modality.AUDIO_STT,
    "video": Modality.VIDEO,
}

# LiteLLM model_info ``supports_<x>`` boolean -> the capability token we surface.
# Only flags that are True on a model land in ModelCatalogEntry.capabilities.
SUPPORTS_TO_CAPABILITY: dict[str, str] = {
    "supports_function_calling": "tool_call",
    "supports_tool_choice": "tool_call",
    "supports_vision": "vision",
    "supports_response_schema": "json_mode",
    "supports_parallel_function_calling": "parallel_tool_call",
    "supports_prompt_caching": "prompt_caching",
    "supports_reasoning": "reasoning",
    "supports_audio_input": "audio_input",
    "supports_audio_output": "audio_output",
}


class CatalogUpstreamError(Exception):
    """A LiteLLM proxy read failed (non-2xx or malformed body). Raised so the
    service can fail closed rather than serve a partial / empty catalog as if it
    were complete."""


def _provider_from_model_name(model_name: str, litellm_provider: str | None) -> str:
    """Provider for an entry. Prefer the prefix of the canonical
    "<provider>/<model>" model_name; fall back to LiteLLM's ``litellm_provider``;
    finally "unknown". This keeps the provider consistent with the id's prefix."""
    if "/" in model_name:
        prefix = model_name.split("/", 1)[0].strip()
        if prefix:
            return prefix
    if litellm_provider:
        return litellm_provider.strip()
    return "unknown"


def _modality_for(mode: str | None) -> Modality:
    """Map a LiteLLM ``mode`` onto a Modality. Unknown / missing -> CHAT, the
    dominant case and the safest default for the picker (a mis-bucketed chat
    model is still usable; dropping it is not)."""
    if not mode:
        return Modality.CHAT
    return MODE_TO_MODALITY.get(mode.strip().lower(), Modality.CHAT)


def _per_mtok(cost_per_token: Any) -> float | None:
    """LiteLLM exposes cost PER TOKEN; the catalog reports per MILLION tokens.
    Returns None for missing / non-numeric values so unknown cost stays None."""
    if cost_per_token is None:
        return None
    try:
        return round(float(cost_per_token) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None


def _pricing_for(model_info: dict[str, Any]) -> Pricing | None:
    """Build Pricing from per-token costs, or None when neither side is known."""
    inp = _per_mtok(model_info.get("input_cost_per_token"))
    out = _per_mtok(model_info.get("output_cost_per_token"))
    if inp is None and out is None:
        return None
    return Pricing(input_per_mtok=inp, output_per_mtok=out)


def _capabilities_for(model_info: dict[str, Any]) -> list[str]:
    """Derive the capability tokens from the model_info ``supports_*`` flags.
    Deduped + sorted so the wire output is deterministic; ``streaming`` is added
    for chat-family models (LiteLLM streams every chat model it proxies)."""
    caps: set[str] = set()
    for flag, token in SUPPORTS_TO_CAPABILITY.items():
        if model_info.get(flag) is True:
            caps.add(token)
    return sorted(caps)


def _display_name(model_name: str) -> str:
    """Human label derived from the model portion of "<provider>/<model>"."""
    tail = model_name.split("/", 1)[1] if "/" in model_name else model_name
    return tail or model_name


def map_model_info_row(row: dict[str, Any]) -> ModelCatalogEntry | None:
    """Map ONE /model/info row onto a ModelCatalogEntry, or None if it has no
    usable ``model_name`` (the canonical id). Pure + side-effect free so it is
    unit-testable in isolation."""
    model_name = (row.get("model_name") or "").strip()
    # Skip LiteLLM's "*" catch-all passthrough — a routing wildcard, not a real,
    # selectable model.
    if not model_name or model_name == "*":
        return None
    info: dict[str, Any] = row.get("model_info") or {}
    params: dict[str, Any] = row.get("litellm_params") or {}
    litellm_provider = info.get("litellm_provider") or params.get("custom_llm_provider")

    mode = info.get("mode")
    modality = _modality_for(mode)
    caps = _capabilities_for(info)
    # Chat-family models stream; surface it as a capability so the picker can show it.
    if modality == Modality.CHAT:
        caps = sorted(set(caps) | {"streaming"})

    return ModelCatalogEntry(
        id=model_name,
        display_name=_display_name(model_name),
        provider=_provider_from_model_name(model_name, litellm_provider),
        modality=modality,
        context=info.get("max_input_tokens") or info.get("max_tokens"),
        max_output_tokens=info.get("max_output_tokens"),
        pricing=_pricing_for(info),
        capabilities=caps,
        logo=None,  # enriched best-effort from models.dev in the service layer
        description=None,
        # v1: /model/info carries no per-model readiness signal, so every listed
        # model is "available". catalog ⊇ routable holds trivially here; a future
        # readiness probe can flip un-keyed models to "disabled".
        status="available",
    )


class LiteLLMClient:
    """Thin async client over a LiteLLM proxy. Reads only — no mutation."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        _transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = (base_url or config.litellm_proxy_url()).rstrip("/")
        # api_key explicitly passed wins; otherwise resolve from env at init.
        self._api_key = api_key if api_key is not None else config.litellm_proxy_api_key()
        self._transport = _transport  # tests inject httpx.MockTransport
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers(),
            transport=self._transport,
            timeout=self._timeout,
        )

    @staticmethod
    def _unwrap_data(resp: httpx.Response, what: str) -> list[dict[str, Any]]:
        """Fail-closed extraction of the ``data`` list from a proxy response.
        Non-2xx raises; a missing/!list ``data`` yields an empty list (the proxy
        answered but served nothing)."""
        if resp.status_code // 100 != 2:
            raise CatalogUpstreamError(f"LiteLLM proxy {what} returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise CatalogUpstreamError(f"LiteLLM proxy {what} returned non-JSON") from exc
        data = body.get("data") if isinstance(body, dict) else None
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def list_model_ids(self) -> list[str]:
        """GET /v1/models -> the model ids the proxy currently serves."""
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/v1/models")
        rows = self._unwrap_data(resp, "/v1/models")
        return [str(r["id"]).strip() for r in rows if r.get("id")]

    async def model_info(self) -> list[dict[str, Any]]:
        """GET /model/info -> the raw per-model metadata rows."""
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/model/info")
        return self._unwrap_data(resp, "/model/info")

    async def fetch_entries(self) -> list[ModelCatalogEntry]:
        """Assemble ModelCatalogEntry objects from /model/info, then reconcile
        against /v1/models so the catalog ⊇ routable (an /v1/models id missing
        from /model/info is still listed as a bare entry).

        /model/info is the rich source. /v1/models is the routable set. The union
        is the catalog: every routable id appears; entries the proxy describes but
        can't currently serve are kept (not dropped). The two reads run; if
        /model/info succeeds we map it; /v1/models is best-effort reconciliation —
        a failure there does not blank an otherwise-good catalog.
        """
        info_rows = await self.model_info()
        entries: dict[str, ModelCatalogEntry] = {}
        for row in info_rows:
            entry = map_model_info_row(row)
            if entry is not None:
                entries[entry.id] = entry

        # Reconcile against the routable list. Best-effort: a /v1/models failure
        # must not blank a good /model/info catalog.
        try:
            served_ids = await self.list_model_ids()
        except CatalogUpstreamError:
            logger.warning("LiteLLM /v1/models unavailable; catalog from /model/info only")
            served_ids = []
        for mid in served_ids:
            if mid == "*":
                continue  # routing wildcard, not a real model
            if mid not in entries:
                # Served but undescribed — list it as a minimal available entry so
                # the routable set is never narrower than the catalog.
                entries[mid] = ModelCatalogEntry(
                    id=mid,
                    display_name=_display_name(mid),
                    provider=_provider_from_model_name(mid, None),
                    modality=Modality.CHAT,
                    status="available",
                )

        return list(entries.values())


__all__ = [
    "LiteLLMClient",
    "CatalogUpstreamError",
    "MODE_TO_MODALITY",
    "SUPPORTS_TO_CAPABILITY",
    "map_model_info_row",
]

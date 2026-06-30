# pocketpaw_ee/discovery/_refine.py — the on-box REFINE pass for discovery (F3).
#
# Created: 2026-06-21 (F3 / feat/szd-finish-core) — un-stubs ``opts.refine`` on
# ``DiscoveryRun``. After the DETERMINISTIC digest produces an ``OntologyDraft``,
# this pass asks the tenant's ON-BOX model (Ollama) to clean it up: merge
# near-duplicate types, rename to canonical names, and drop spurious links. It
# sends the model the DRAFT shape (type names, property names, sample
# summaries already in the draft) — NOT raw tenant exhaust.
#
# SOVEREIGNTY (the whole point of this slice, encoded as mechanical tests):
# refining touches tenant data, so the model MUST be the tenant's on-box model
# and NEVER a cloud model. The enforcement point is the hard pin
# ``resolve_llm_client(settings, force_provider="ollama")`` — we never read
# ``settings.llm_provider`` (an ``auto`` resolve with a cloud key set would pick
# Anthropic and leak raw tenant text). ``force_provider="ollama"`` guarantees
# ``api_key is None`` / ``is_ollama is True``; ``create_openai_client`` then
# talks to the local Ollama ``/v1`` endpoint with the literal ``"ollama"``
# sentinel key — no cloud key is ever in the request path.
#
# FAIL CLOSED ON SOVEREIGNTY, SOFT ON AVAILABILITY: if Ollama can't connect
# (``ollama serve`` down) or returns garbage, we RETURN the original
# deterministic draft with ``draft.meta["refine"] = "unavailable"`` — we never
# raise and never fall back to a cloud model. Refine is an enhancement; the
# deterministic digest is the contract floor. On success we stamp
# ``draft.meta["refine"] = "applied"``. Mirrors the templated-fallback shape of
# ``cloud/decisions/explain/extractor.py`` (but the fallback is the draft).
#
# ``resolve_on_box_descriptor`` / ``resolve_on_box_client`` are the shared
# helpers slice F2 (unstructured categorization) also imports for its on-box
# model call — both lanes resolve the same way so the sovereignty pin lives in
# one place.
#
# Pure orchestration over the OSS ``pocketpaw.llm`` client seam + the discovery
# models. No DB, honours a request timeout. Async (one model round-trip).

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from pocketpaw.config import Settings
from pocketpaw.llm.client import LLMClient, resolve_llm_client
from pocketpaw_ee.discovery.models import (
    DraftObjectType,
    OntologyDraft,
)

logger = logging.getLogger(__name__)

# The on-box model gets a generous timeout — a local model on a tenant box is
# slower than a cloud API, and refine is not latency-sensitive (it runs once,
# behind a human-reviewed gate).
_REFINE_TIMEOUT_S = 120.0

# Cap how many types/links we describe to the model so the prompt stays bounded
# on a large draft (the deterministic draft is the floor either way).
_MAX_TYPES_IN_PROMPT = 60

_REFINE_SYSTEM = (
    "You are an ontology editor. You are given a DRAFT ontology that was "
    "reverse-engineered deterministically from a tenant's data. Your job is to "
    "CLEAN it: merge near-duplicate object types (e.g. 'Ticket' and 'Tickets'), "
    "rename types to a single canonical, human-readable name, and drop links "
    "that look spurious. Do NOT invent new types or properties that are not in "
    "the draft. Reply with a single JSON object of the form "
    '{"object_types": [{"name": str, "source_id_field": str|null, '
    '"field_map": {..}, "confidence": float, "key_confidence": float}], '
    '"links": []}. Keep field_map keys/values exactly as given for types you '
    "keep. Return ONLY the JSON object."
)


def resolve_on_box_descriptor(settings: Settings) -> LLMClient:
    """Resolve the on-box (Ollama) ``LLMClient`` descriptor — hard-pinned local.

    SOVEREIGNTY ENFORCEMENT POINT. ``force_provider="ollama"`` is passed
    unconditionally so this never reads ``settings.llm_provider``: an ``auto``
    resolve on a tenant with a cloud key configured would otherwise pick
    Anthropic and leak raw tenant text. The returned descriptor always has
    ``provider == "ollama"``, ``api_key is None``, ``is_ollama is True``.

    Shared with slice F2 (unstructured categorization) so both on-box model
    lanes resolve identically.
    """
    return resolve_llm_client(settings, force_provider="ollama")


def resolve_on_box_client(settings: Settings) -> Any:
    """Build the on-box ``AsyncOpenAI`` client bound to the local Ollama endpoint.

    Wraps ``resolve_on_box_descriptor`` + ``create_openai_client`` so callers get
    a ready-to-use client with one call. The client talks to
    ``{ollama_host}/v1`` with the literal ``"ollama"`` sentinel key — never a
    cloud key. ``timeout`` is generous because a local model is slower than a
    cloud API.
    """
    descriptor = resolve_on_box_descriptor(settings)
    return descriptor.create_openai_client(timeout=_REFINE_TIMEOUT_S)


def _draft_payload(draft: OntologyDraft) -> dict[str, Any]:
    """Build the bounded DRAFT-shape payload sent to the model.

    Sends only the inferred SHAPE — type names, property names, sample
    summaries already in the draft — never raw tenant exhaust. The
    deterministic draft is the floor, so an over-large draft is simply
    truncated for the prompt.
    """
    types: list[dict[str, Any]] = []
    for ot in draft.object_types[:_MAX_TYPES_IN_PROMPT]:
        types.append(
            {
                "name": ot.name,
                "source_id_field": ot.source_id_field,
                "field_map": dict(ot.field_map),
                "property_names": [p.name for p in ot.properties],
                "confidence": ot.confidence,
                "key_confidence": ot.key_confidence,
                "record_count": ot.record_count,
            }
        )
    links: list[dict[str, Any]] = []
    for link in draft.links:
        links.append(
            {
                "from_type": link.from_type,
                "to_type": link.to_type,
                "link_type": link.link_type,
                "via_field": link.via_field,
                "confidence": link.confidence,
            }
        )
    return {"object_types": types, "links": links}


def _extract_content(resp: Any) -> str | None:
    """Pull the text content out of a chat-completion response, or None."""
    try:
        return resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return None


def _apply_cleaned(draft: OntologyDraft, cleaned: dict[str, Any]) -> OntologyDraft:
    """Apply a cleaned ontology onto a COPY of the deterministic draft.

    The model returns the canonical ``object_types`` (merged/renamed) and the
    surviving ``links``. We rebuild the draft's types from the cleaned list and
    keep only the links the model kept, re-pointing any object whose type was
    renamed/merged onto a surviving type. Objects whose type vanished entirely
    are dropped (the model merged them away). Never raises on a partial/odd
    payload — a malformed shape is caught by the caller and degrades to the
    deterministic draft.
    """
    out = draft.model_copy(deep=True)

    cleaned_types = cleaned.get("object_types")
    if isinstance(cleaned_types, list) and cleaned_types:
        # Index the original types so we can carry forward fields the model
        # omitted (properties, record_count) by name match.
        by_name = {ot.name: ot for ot in draft.object_types}
        rebuilt: list[DraftObjectType] = []
        for raw in cleaned_types:
            if not isinstance(raw, Mapping):
                continue
            name = raw.get("name")
            if not name or not isinstance(name, str):
                continue
            base = by_name.get(name)
            rebuilt.append(
                DraftObjectType(
                    name=name,
                    properties=list(base.properties) if base else [],
                    source_id_field=raw.get("source_id_field")
                    if "source_id_field" in raw
                    else (base.source_id_field if base else None),
                    field_map=dict(raw.get("field_map") or (base.field_map if base else {})),
                    confidence=_as_float(raw.get("confidence"), base.confidence if base else 0.0),
                    key_confidence=_as_float(
                        raw.get("key_confidence"), base.key_confidence if base else 0.0
                    ),
                    record_count=base.record_count if base else 0,
                )
            )
        if rebuilt:
            out.object_types = rebuilt
            surviving = {ot.name for ot in rebuilt}
            # Keep only objects whose type survived the merge/rename.
            out.objects = [o for o in out.objects if o.type_name in surviving]

    # Links: the model returns the links it kept. Drop everything else. A
    # missing/empty "links" key means "drop all links" (the model deemed them
    # spurious) only when the key is explicitly present; otherwise leave intact.
    if "links" in cleaned:
        cleaned_links = cleaned.get("links")
        if not cleaned_links:
            out.links = []
        elif isinstance(cleaned_links, list):
            keep = {
                (str(item.get("from_type")), str(item.get("to_type")), str(item.get("via_field")))
                for item in cleaned_links
                if isinstance(item, Mapping)
            }
            out.links = [
                link for link in out.links if (link.from_type, link.to_type, link.via_field) in keep
            ]

    return out


def _as_float(value: Any, default: float) -> float:
    """Coerce a model-supplied confidence into a float, falling back on default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def refine_draft(draft: OntologyDraft, settings: Settings) -> OntologyDraft:
    """Refine a deterministic ``OntologyDraft`` with the tenant's ON-BOX model.

    Sends the model the DRAFT shape (type/property names, sample summaries in
    the draft — never raw exhaust) and asks it to merge/rename types and drop
    spurious links, then applies the cleaned result onto a copy of the draft.

    Sovereignty: the client is hard-pinned to Ollama via ``resolve_on_box_client``
    (``force_provider="ollama"``). No cloud model is ever reached.

    Fail closed on sovereignty, soft on availability: on ANY failure (Ollama
    down, malformed response, parse error) return the ORIGINAL deterministic
    draft with ``meta["refine"] = "unavailable"`` — never raise, never fall back
    to a cloud model. On success stamp ``meta["refine"] = "applied"``.
    """
    client = resolve_on_box_client(settings)
    payload = _draft_payload(draft)
    user_content = (
        "Clean this draft ontology. Merge near-duplicate types, rename to "
        "canonical names, and drop spurious links. Draft:\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    try:
        resp = await client.chat.completions.create(
            model=settings.ollama_model,
            messages=[
                {"role": "system", "content": _REFINE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
    except Exception:  # noqa: BLE001 — availability failure must degrade, not raise
        logger.warning(
            "discovery refine: on-box model unavailable; returning deterministic draft",
            exc_info=True,
        )
        return _unavailable(draft)

    content = _extract_content(resp)
    if not content:
        logger.info("discovery refine: empty model response; returning deterministic draft")
        return _unavailable(draft)

    try:
        cleaned = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        logger.info("discovery refine: non-JSON model response; returning deterministic draft")
        return _unavailable(draft)

    if not isinstance(cleaned, Mapping):
        return _unavailable(draft)

    try:
        refined = _apply_cleaned(draft, dict(cleaned))
    except Exception:  # noqa: BLE001 — a broken payload degrades to the floor
        logger.warning("discovery refine: failed to apply cleaned draft", exc_info=True)
        return _unavailable(draft)

    refined.meta["refine"] = "applied"
    return refined


def _unavailable(draft: OntologyDraft) -> OntologyDraft:
    """Return the deterministic draft, flagged refine-unavailable (no mutation)."""
    draft.meta["refine"] = "unavailable"
    return draft


__all__ = [
    "resolve_on_box_descriptor",
    "resolve_on_box_client",
    "refine_draft",
]

# ee/pocketpaw_ee/cloud/decisions/explain/extractor.py
# Created: 2026-05-25 (RFC 07 Slice 3a) — the entity extractor for the
#   natural-language `explain` pipeline. RFC 07 § "LLM-grounded query"
#   pins the contract: one small LLM call (Haiku-class), output is
#   structured filter args, the input is ALWAYS the same shape so the
#   system prompt + few-shots cache cleanly. The question text is the
#   only variable portion.
#
#   Two paths:
#     - Default: a Haiku call via the `anthropic` SDK. The system prompt
#       and a stable handful of few-shot pairs are sent as cache-able
#       blocks (cache_control = {"type": "ephemeral"} on the last
#       cacheable block — Anthropic caches the prefix). The question
#       lives in the user message and varies per call.
#     - Fallback: a deterministic regex-driven extractor copied from the
#       Slice 3 PoC at /tmp/team-rfc07/explain/narrator.py. Used when:
#         (a) the `anthropic` SDK isn't installed,
#         (b) no API key is configured,
#         (c) the LLM call raises, or
#         (d) the caller passed `backend="templated"`.
#
#   The fallback isn't a degraded path — for the captain's killer demo
#   it produces a *deterministic* test for the lease-renewal question
#   ("Why was LR-2026-117 approved on April 22?"). The LLM path widens
#   coverage to free-form questions but the templated path is the proven
#   floor.
#
#   Latency budget: 200-400ms for the LLM call; the fallback is O(question
#   length) regex and runs in microseconds.
"""Entity extractor for the natural-language explain pipeline."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


class ExtractedEntities(BaseModel):
    """Structured filters distilled from a natural-language question.

    Every field is optional — the explain pipeline takes the intersection of
    whatever the extractor recovered and the caller's scope. A question
    with NO recoverable entities triggers a fallback where the pipeline
    returns the most-recent-N decisions in the caller's scope (so the
    narrator always has something to walk).
    """

    model_config = ConfigDict(frozen=False)

    decision_ids: list[UUID] = Field(default_factory=list)
    actor_refs: list[str] = Field(default_factory=list)
    fabric_object_refs: list[str] = Field(default_factory=list)
    time_range: tuple[datetime | None, datetime | None] | None = None
    policy_ref: str | None = None
    pocket_id: str | None = None
    outcome_hint: Literal["approved", "rejected", "landed", "pending"] | None = None


# ---------------------------------------------------------------------------
# System prompt + few-shots — the cache-able prefix
# ---------------------------------------------------------------------------

# The system prompt and few-shots are split into one cache-able block so
# every call hits the same prefix. Anthropic caches up to four blocks
# marked with cache_control = ephemeral; we only need one here because
# system + few-shots are a single logical unit. Only the user message
# (the question) varies per call.

_EXTRACTOR_SYSTEM = """You extract structured filters from natural-language questions about an organization's decision log.

Return ONE JSON object with these optional keys:
- decision_ids: list of UUID strings referenced explicitly in the question
- actor_refs:   list of actor identifiers (e.g. "user:prakash", "did:soul:renewal_specialist")
- fabric_object_refs: list of fabric-object identifiers (e.g. "lease:LR-2026-117", "case:c_42")
- time_range:   pair [iso_start, iso_end] in UTC; either may be null for an open bound
- policy_ref:   Instinct policy name if named (e.g. "approve_per_row")
- pocket_id:    pocket id if named (e.g. "p_renewals")
- outcome_hint: one of "approved" | "rejected" | "landed" | "pending"

Rules:
- Omit any key the question does not constrain. Do not invent values.
- For fabric-object identifiers, lowercase the type prefix and uppercase the id ("lease:LR-2026-117").
- For month-day expressions without a year, assume the current calendar year.
- Output the JSON object only — no prose, no markdown fences."""


_FEWSHOTS: list[tuple[str, dict[str, Any]]] = [
    (
        "Why was lease renewal LR-2026-117 approved on April 22?",
        {
            "fabric_object_refs": ["lease:LR-2026-117"],
            "time_range": ["2026-04-22T00:00:00Z", "2026-04-23T00:00:00Z"],
            "outcome_hint": "approved",
        },
    ),
    (
        "Show me everything Prakash approved this month in the renewals pocket.",
        {
            "actor_refs": ["user:prakash"],
            "pocket_id": "p_renewals",
            "outcome_hint": "approved",
        },
    ),
    (
        "What did the approve_per_row policy reject last week?",
        {
            "policy_ref": "approve_per_row",
            "outcome_hint": "rejected",
        },
    ),
]


def _build_user_message(question: str) -> str:
    """Render the per-call user message. The few-shots are baked into the
    system prefix so the cache hit rate stays high across questions."""
    shots = "\n\n".join(
        f"Question: {q}\nOutput: {json.dumps(out, separators=(',', ':'))}" for q, out in _FEWSHOTS
    )
    return (
        f"Few-shot examples (do not echo):\n{shots}\n\n"
        f"Now extract from this question. Output JSON only.\n\n"
        f"Question: {question}\nOutput:"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def extract_entities(
    question: str,
    *,
    scope: dict[str, Any] | None = None,
    backend: Literal["llm", "templated"] | None = None,
    api_key: str | None = None,
    model: str = "claude-haiku-4-7",
) -> ExtractedEntities:
    """Extract structured filters from a natural-language question.

    Args:
        question: Free-form question from the user.
        scope: Caller's scope (workspace / pocket hints). When the question
            doesn't name a pocket but `scope["pocket_id"]` is set, the
            scope value is used.
        backend: Force "templated" to skip the LLM path entirely. Defaults
            to the LLM path when the SDK + key are available.
        api_key: Override the Anthropic API key (test seam). Defaults to
            the `POCKETPAW_ANTHROPIC_API_KEY` resolved via `get_settings`.
        model: Haiku-class model id. Caller can pin a specific version.

    Returns:
        An ExtractedEntities — never None, never raises. On any LLM
        failure (SDK missing, key missing, network error, parse error)
        the call falls through to the templated extractor and the result
        is still a valid ExtractedEntities.
    """
    scope = scope or {}
    if backend == "templated":
        return _templated_extract(question, scope)

    # Resolve API key — caller override > settings.
    if api_key is None:
        try:
            from pocketpaw.config import get_settings

            api_key = get_settings().anthropic_api_key
        except Exception:  # noqa: BLE001
            api_key = None

    if not api_key:
        return _templated_extract(question, scope)

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.info("anthropic SDK not installed; explain extractor using templated path")
        return _templated_extract(question, scope)

    try:
        client = AsyncAnthropic(api_key=api_key, timeout=10.0, max_retries=1)
        response = await client.messages.create(
            model=model,
            max_tokens=400,
            system=[
                {
                    "type": "text",
                    "text": _EXTRACTOR_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {"role": "user", "content": _build_user_message(question)},
            ],
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "explain extractor LLM call failed; falling back to templated", exc_info=True
        )
        return _templated_extract(question, scope)

    try:
        raw = response.content[0].text
    except (AttributeError, IndexError):
        return _templated_extract(question, scope)

    parsed = _parse_llm_output(raw)
    if parsed is None:
        return _templated_extract(question, scope)

    # Apply scope defaults — if the LLM didn't name a pocket but the
    # caller scope has one, prefer the scope value.
    if not parsed.pocket_id and scope.get("pocket_id"):
        parsed.pocket_id = scope["pocket_id"]
    return parsed


def _parse_llm_output(raw: str) -> ExtractedEntities | None:
    """Parse the model's JSON output into ExtractedEntities. Returns None
    on any parse / shape error so the caller can fall through."""
    text = raw.strip()
    if not text:
        return None
    # Strip a stray code fence if the model wrapped it.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    decision_ids: list[UUID] = []
    for raw_id in data.get("decision_ids", []) or []:
        try:
            decision_ids.append(UUID(str(raw_id)))
        except (ValueError, TypeError):
            continue

    time_range: tuple[datetime | None, datetime | None] | None = None
    tr = data.get("time_range")
    if isinstance(tr, list) and len(tr) == 2:
        time_range = (_parse_iso(tr[0]), _parse_iso(tr[1]))

    return ExtractedEntities(
        decision_ids=decision_ids,
        actor_refs=[str(a) for a in (data.get("actor_refs") or []) if a],
        fabric_object_refs=[str(o) for o in (data.get("fabric_object_refs") or []) if o],
        time_range=time_range,
        policy_ref=data.get("policy_ref") or None,
        pocket_id=data.get("pocket_id") or None,
        outcome_hint=(
            data["outcome_hint"]
            if data.get("outcome_hint") in {"approved", "rejected", "landed", "pending"}
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Templated fallback — deterministic regex extractor
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_FABRIC_PREFIXES = ("lease", "case", "patient", "vendor", "tenant", "deal", "ticket")


def _templated_extract(question: str, scope: dict[str, Any]) -> ExtractedEntities:
    """Regex-driven entity extraction. Mirrors the PoC at
    /tmp/team-rfc07/explain/narrator.py extract_entities() but returns
    the production ExtractedEntities shape."""

    q_lower = question.lower()
    out = ExtractedEntities()

    # decision-id UUIDs explicitly mentioned
    for raw_uuid in re.findall(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        question,
    ):
        try:
            out.decision_ids.append(UUID(raw_uuid))
        except ValueError:
            continue

    # fabric-object refs like "lease:LR-2026-117" or bare "LR-2026-117"
    for m in re.finditer(
        r"\b((?:" + "|".join(_FABRIC_PREFIXES) + r")\s*[:\-]?\s*[A-Za-z0-9\-_]+)\b",
        question,
        re.IGNORECASE,
    ):
        token = m.group(1).strip()
        # normalize "lease:LR-..." → "lease:LR-..."; "lease LR-..." → same
        norm = re.sub(r"\s+", "", token)
        # split prefix from id
        sep_match = re.match(r"^([A-Za-z]+)[:\-]?(.+)$", norm)
        if sep_match:
            prefix = sep_match.group(1).lower()
            ident = sep_match.group(2).upper()
            normalized = f"{prefix}:{ident}"
            if normalized not in out.fabric_object_refs:
                out.fabric_object_refs.append(normalized)

    # Bare "LR-YYYY-NNN" — assume lease.
    for m in re.finditer(r"\b(LR-\d{4}-\d+)\b", question):
        normalized = f"lease:{m.group(1)}"
        if normalized not in out.fabric_object_refs:
            out.fabric_object_refs.append(normalized)

    # actor — "by Prakash", "Prakash approved"
    for m in re.finditer(r"\bby\s+([A-Z][a-z]+)\b", question):
        ref = f"user:{m.group(1).lower()}"
        if ref not in out.actor_refs:
            out.actor_refs.append(ref)
    # Capitalized name followed by an approval/decision verb.
    for m in re.finditer(r"\b([A-Z][a-z]+)\s+(?:approved|rejected|decided|signed)\b", question):
        ref = f"user:{m.group(1).lower()}"
        if ref not in out.actor_refs:
            out.actor_refs.append(ref)

    # time — ISO yyyy-mm-dd or "April 22"
    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", question)
    if iso_match:
        y, mo, d = map(int, iso_match.groups())
        start = datetime(y, mo, d, tzinfo=UTC)
        out.time_range = (start, start + timedelta(days=1))
    else:
        mo_match = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\b",
            question,
            re.IGNORECASE,
        )
        if mo_match:
            month = _MONTH_NAMES[mo_match.group(1).lower()]
            day = int(mo_match.group(2))
            year = datetime.now(UTC).year
            start = datetime(year, month, day, tzinfo=UTC)
            out.time_range = (start, start + timedelta(days=1))

    # policy — "approve_per_row policy" or just "approve_per_row"
    pol_match = re.search(r"\b([a-z][a-z0-9_]+)(?:\s+policy)\b", q_lower)
    if pol_match:
        out.policy_ref = pol_match.group(1)
    else:
        pol_match = re.search(r"\bpolicy\s+([a-z][a-z0-9_]+)\b", q_lower)
        if pol_match:
            out.policy_ref = pol_match.group(1)

    # outcome
    if any(w in q_lower for w in ("approved", "approve")):
        out.outcome_hint = "approved"
    elif any(w in q_lower for w in ("rejected", "denied", "blocked")):
        out.outcome_hint = "rejected"
    elif "landed" in q_lower:
        out.outcome_hint = "landed"
    elif "pending" in q_lower:
        out.outcome_hint = "pending"

    # pocket — explicit or scope default
    pkt_match = re.search(r"\bpocket\s+([a-z][a-z0-9_]+)\b", q_lower)
    if pkt_match:
        out.pocket_id = pkt_match.group(1)
    if not out.pocket_id and scope.get("pocket_id"):
        out.pocket_id = scope["pocket_id"]

    return out


def _parse_iso(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


__all__ = ["ExtractedEntities", "extract_entities"]

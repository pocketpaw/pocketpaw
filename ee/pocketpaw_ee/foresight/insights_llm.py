# ee/pocketpaw_ee/foresight/insights_llm.py
# Created: 2026-05-26 (feat/foresight-v10-insights-llm) — RFC 08 v1.0
# Insights synthesizer alternative: LLM-driven pattern discovery on top
# of the same SynthesizerInput-style structured summary the v0.5 pattern
# rules consume.
#
# Stays inside the engine namespace (``pocketpaw_ee.foresight``) — same
# as the v0.5 pattern synthesizer in ``insights.py``. No I/O against
# Beanie / FastAPI / Pydantic; the cloud service composes the structured
# summary and hands it in. The module owns:
#
#   - ``LLMInsightsInput`` — the structured summary shape (rolling
#     accuracy + drift + modal outcomes + recent prediction records +
#     last backtest decision) handed to the prompt.
#   - ``LLMBackendProtocol`` — the minimal model-backend surface; matches
#     ``ee.foresight.llm.adapter.ClaudeCodeBackend.complete`` so the
#     existing CC-SDK adapter from PR #1227/#1232 plugs in unchanged.
#   - ``synthesize_insights_llm(...)`` — async pure function: builds a
#     prompt, calls the backend, parses + validates the JSON output into
#     ``Insight`` records (reusing the dataclass from the v0.5
#     synthesizer so the cloud-side InsightView mapper handles both paths
#     uniformly).
#   - In-memory LRU cache keyed on
#     ``(workspace_id, aggregator_state_hash)`` with a configurable TTL
#     (default 5 minutes). Stable-id discipline (SHA-1 of
#     ``(workspace_id, period_key, prompt_hash, kind, salt)``) means the
#     UI sees consistent ids across reloads while the input data is
#     unchanged.
#   - Hard fallback: timeouts, malformed JSON, schema validation errors,
#     rate-limit / connection failures ALL collapse to ``[]`` so the
#     cloud-side ``get_insights`` can fall back to the pattern rules
#     instead of 5xx-ing the panel.
#
# Cost discipline: LLM mode is opt-in via the per-workspace
# ``insights_synthesizer`` config field. The pattern path is unchanged
# (deterministic, free) and stays the default. The LLM path runs only
# when a workspace admin flips the toggle.

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

from pocketpaw_ee.foresight.insights import (
    _SEVERITY_RANK,
    ConfidenceDriftInput,
    Insight,
    InsightSeverity,
    LatestBacktestGate,
    PerPersonaCalibration,
    RollingAccuracyPoint,
    TierDistributionDelta,
    _iso,
    _safe_id_segment,
)

logger = logging.getLogger(__name__)


# Severity values the LLM is allowed to emit. The cloud-side wire DTO
# (``InsightResponse.severity``) is a plain ``str`` but the synthesizer
# input ``Insight.severity`` is the v0.5 ``InsightSeverity`` Literal —
# any value outside this set is silently coerced to ``"info"`` so the
# UI's severity → colour mapping stays defined.
_ALLOWED_SEVERITIES: frozenset[str] = frozenset({"info", "warning", "critical"})

# The opaque kind values the LLM is encouraged to emit, beyond the
# v0.5 pattern set. Documented to the model so its output stays in a
# bounded enum the UI can later partition by; values outside this list
# pass through (the cloud-side ``InsightView.kind`` is ``str``).
LLM_KIND_VOCABULARY: tuple[str, ...] = (
    "trend_explainer",
    "outlier_cluster",
    "persona_pattern",
    "tier_pattern",
    "backtest_observation",
    "cross_signal_correlation",
    "regression_risk",
    "data_quality",
)

# Cap on insights the LLM is asked to produce. The cloud-side
# ``get_insights`` already caps the wire response at
# ``INSIGHTS_DEFAULT_CAP=20``; the per-call ceiling here is lower so
# the prompt stays focused on the most actionable items.
LLM_PROMPT_INSIGHT_CAP: int = 8

# Default TTL for the in-memory cache. Keeps the LLM cost per workspace
# bounded — if the UI polls /insights every minute, only the first call
# of a 5-minute window pays the LLM round-trip; the next four hit cache.
DEFAULT_CACHE_TTL_SECONDS: int = 300

# Default cache capacity. LRU eviction is keyed by
# ``(workspace_id, aggregator_state_hash)`` so a workspace with rapidly
# changing data won't displace another workspace's cached entry.
DEFAULT_CACHE_CAPACITY: int = 64


class LLMBackendProtocol(Protocol):
    """Minimal model-backend surface ``synthesize_insights_llm`` requires.

    Matches the surface :class:`ee.foresight.llm.adapter.ClaudeCodeBackend`
    exposes (``async def complete(prompt: str) -> str``) so the existing
    CC-SDK adapter from PR #1227/#1232 plugs in unchanged. Tests inject
    a stub that returns canned JSON.
    """

    async def complete(self, prompt: str) -> str:  # pragma: no cover — protocol
        ...


@dataclass(frozen=True)
class RecentPredictionRecordSummary:
    """One row of recent prediction-record data fed into the LLM prompt.

    Compact shape — the LLM only needs anchor / persona / outcome /
    confidence / paired status. Full PredictionRecord docs would blow
    the prompt budget for any workspace with a non-trivial history.
    """

    anchor_id: str
    persona_id: str
    modal_outcome: str
    confidence: float
    paired: bool
    observed_outcome: str | None = None
    captured_at: datetime | None = None


@dataclass(frozen=True)
class LLMInsightsInput:
    """Structured summary the LLM synthesizer reads.

    Same data shape the v0.5 pattern synthesizer consumes, plus a
    compact list of recent prediction records. Each field is optional
    so partial data (empty workspace, no backtests yet) collapses to an
    empty prompt section rather than raising.

    ``workspace_id`` and ``period_key`` are required positionally per
    the cloud rule #3 multi-tenancy invariant — the cache key and the
    stable-id hash both consume these so a leak across tenants would
    surface as a stable-id collision rather than a silent miss.

    ``now`` anchors every ``generated_at`` so tests stay deterministic.
    """

    workspace_id: str
    period_key: str
    now: datetime
    rolling_accuracy: Sequence[RollingAccuracyPoint] = ()
    confidence_drift: ConfidenceDriftInput | None = None
    per_persona_calibration: Sequence[PerPersonaCalibration] = ()
    tier_distribution_deltas: Sequence[TierDistributionDelta] = ()
    latest_backtest: LatestBacktestGate | None = None
    recent_records: Sequence[RecentPredictionRecordSummary] = ()

    # Tunables (kept on the input bundle so callers don't have to pass
    # a separate config). Defaults match the cost-discipline targets
    # documented in the module header.
    max_insights: int = LLM_PROMPT_INSIGHT_CAP
    recent_records_cap: int = 25


@dataclass(frozen=True)
class _CachedInsights:
    """One LRU cache entry — the synthesized insight list plus the
    Unix timestamp at insertion so TTL expiry is O(1) on lookup.
    """

    insights: tuple[Insight, ...]
    inserted_at: float


@dataclass
class _LRUCache:
    """Tiny LRU + TTL cache. Kept module-private so callers can't
    sidestep the eviction policy.

    Key: ``(workspace_id, aggregator_state_hash)``.
    Value: ``_CachedInsights``.

    Capacity bound + TTL together keep memory usage bounded even when
    every workspace polls hot. The OrderedDict supplies the LRU
    ordering; ``move_to_end`` on lookup keeps the touched key at the
    "most recent" tail.
    """

    capacity: int = DEFAULT_CACHE_CAPACITY
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    _store: OrderedDict[tuple[str, str], _CachedInsights] = field(default_factory=OrderedDict)

    def get(self, key: tuple[str, str]) -> tuple[Insight, ...] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if (time.monotonic() - entry.inserted_at) > self.ttl_seconds:
            # Stale — evict and miss.
            del self._store[key]
            return None
        # Touch recency.
        self._store.move_to_end(key)
        return entry.insights

    def put(self, key: tuple[str, str], insights: tuple[Insight, ...]) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = _CachedInsights(
            insights=insights,
            inserted_at=time.monotonic(),
        )
        # Evict from the head (LRU) until under capacity.
        while len(self._store) > self.capacity:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()


# Module-level singleton — one cache per process. Tests can clear it
# via ``reset_cache`` between cases without monkey-patching.
_cache: _LRUCache = _LRUCache()


def reset_cache() -> None:
    """Clear the module-level LRU. Test-only convenience."""
    _cache.clear()


def configure_cache(*, ttl_seconds: int | None = None, capacity: int | None = None) -> None:
    """Reconfigure the module-level cache. Used by the cloud-side config
    endpoint when an admin tunes ``llm_cache_ttl_seconds`` (forward-
    compat: v1.0 ships a fixed TTL; v1.1 will expose it). Reset is
    implicit — clearing the store on reconfigure avoids stale-TTL races.
    """
    if ttl_seconds is not None:
        if ttl_seconds < 1:
            raise ValueError(f"ttl_seconds must be >= 1, got {ttl_seconds}")
        _cache.ttl_seconds = ttl_seconds
    if capacity is not None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        _cache.capacity = capacity
    _cache.clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def synthesize_insights_llm(
    bundle: LLMInsightsInput,
    backend: LLMBackendProtocol,
    *,
    cap: int = 20,
    timeout_seconds: float = 30.0,
) -> list[Insight]:
    """Run the LLM-driven synthesizer over ``bundle`` and return a sorted
    list of insights.

    The function is failure-resilient by construction: any exception
    (timeout, network error, malformed JSON, missing fields) is caught
    and surfaced as an empty list so the cloud-side ``get_insights``
    can fall back to the pattern rules instead of 5xx-ing.

    Cache strategy:
      1. Compose a stable hash of the input data.
      2. Look up ``(workspace_id, hash)`` in the LRU.
      3. On hit, return the cached insights (recency-touched).
      4. On miss, call the backend, parse + validate, store and return.

    Stable-id discipline:
      - Each parsed insight's id is SHA-1 of
        ``(workspace_id, period_key, prompt_hash, kind, salt)`` so the
        same input always yields the same id. The salt is the insight's
        ordinal in the LLM output to keep duplicates within a single
        response disambiguated.

    Sorting + capping matches the v0.5 pattern synthesizer exactly:
    severity descending (critical > warning > info), then
    ``generated_at`` descending, then id (lexicographic). Capped at
    ``cap`` items so the wire-response cap downstream is preserved.
    """
    if cap < 1:
        raise ValueError(f"cap must be >= 1, got {cap}")

    if not bundle.workspace_id:
        # Cloud rule #3 — workspace_id is required. Don't raise; just
        # collapse to empty so the cloud-side fallback path takes over.
        logger.warning(
            "insights_llm.empty_workspace_id",
            extra={"period_key": bundle.period_key},
        )
        return []

    state_hash = _hash_bundle(bundle)
    cache_key = (bundle.workspace_id, state_hash)
    cached = _cache.get(cache_key)
    if cached is not None:
        # Return a list copy — callers may sort/modify in place.
        return list(cached)[:cap]

    prompt = _build_prompt(bundle)
    prompt_hash = hashlib.sha1(prompt.encode("utf-8")).hexdigest()

    try:
        raw_output = await asyncio.wait_for(
            backend.complete(prompt),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "insights_llm.timeout",
            extra={
                "workspace_id": bundle.workspace_id,
                "period_key": bundle.period_key,
                "timeout_seconds": timeout_seconds,
            },
        )
        return []
    except Exception as exc:  # noqa: BLE001 — backend errors are opaque
        logger.warning(
            "insights_llm.backend_error",
            extra={
                "workspace_id": bundle.workspace_id,
                "period_key": bundle.period_key,
                "error": repr(exc),
            },
        )
        return []

    parsed = _parse_llm_output(
        raw_output,
        workspace_id=bundle.workspace_id,
        period_key=bundle.period_key,
        prompt_hash=prompt_hash,
        now=bundle.now,
    )

    # Sort: severity desc, generated_at desc, then id lex — mirrors the
    # v0.5 synthesizer exactly so the LLM and pattern paths render in
    # consistent order on the UI.
    parsed.sort(
        key=lambda item: (
            -_SEVERITY_RANK.get(item.severity, 0),
            -item.generated_at.timestamp(),
            item.id,
        )
    )
    capped = parsed[:cap]
    _cache.put(cache_key, tuple(capped))
    return capped


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_PROMPT_SYSTEM = (
    "You are a foresight analyst. Given a structured summary of one "
    "workspace's recent prediction performance, identify up to {max_insights} "
    "actionable patterns that an operator should know about. Focus on "
    "explainable, evidence-based observations — name the persona, tier, "
    "or anchor that drives each pattern.\n\n"
    "Return a JSON object with a single top-level field 'insights' whose "
    "value is an array of objects. Each insight object MUST have:\n"
    "  - 'kind' (string): short snake_case label; prefer one of "
    "{kind_vocab} but a new label is acceptable when none fit.\n"
    "  - 'title' (string, <= 80 chars): one-line summary.\n"
    "  - 'body' (string, <= 400 chars): why this matters and what to "
    "look at next. Reference concrete IDs from the data.\n"
    "  - 'severity' (string): one of 'info', 'warning', 'critical'.\n"
    "  - 'anchor_refs' (array of strings, optional): IDs the UI can "
    "deep-link to (e.g. 'persona:abc', 'tier:premium', "
    "'backtest:5f5...'). Empty array when no refs.\n\n"
    "Return ONLY the JSON object, no prose. If the data does not "
    'support any insights, return {{"insights": []}}.'
)


def _build_prompt(bundle: LLMInsightsInput) -> str:
    """Compose the prompt sent to the model backend.

    The prompt has three sections:
      1. System guidance — what to emit, severity vocabulary, kind
         vocabulary, output schema.
      2. Structured data summary — JSON-formatted view of the rolling
         accuracy series, drift, per-persona calibration, tier deltas,
         latest backtest, and a capped tail of recent prediction
         records.
      3. The closing instruction — emit JSON only.
    """
    system = _PROMPT_SYSTEM.format(
        max_insights=bundle.max_insights,
        kind_vocab=", ".join(LLM_KIND_VOCABULARY),
    )

    data: dict[str, Any] = {
        "workspace_id": bundle.workspace_id,
        "period_key": bundle.period_key,
        "now": _iso(bundle.now),
        "rolling_accuracy": [
            {
                "ts": _iso(p.ts),
                "accuracy": round(float(p.accuracy), 4),
                "sample_count": int(p.sample_count),
            }
            for p in bundle.rolling_accuracy
        ],
        "confidence_drift": (
            None
            if bundle.confidence_drift is None
            else {
                "trend": bundle.confidence_drift.trend,
                "magnitude": round(float(bundle.confidence_drift.magnitude), 4),
            }
        ),
        "per_persona_calibration": [
            {
                "persona_id": entry.persona_id,
                "calibration": round(float(entry.calibration), 4),
                "sample_count": int(entry.sample_count),
            }
            for entry in bundle.per_persona_calibration
        ],
        "tier_distribution_deltas": [
            {
                "tier": entry.tier,
                "configured": round(float(entry.configured), 4),
                "actual": round(float(entry.actual), 4),
                "delta": round(float(entry.delta), 4),
            }
            for entry in bundle.tier_distribution_deltas
        ],
        "latest_backtest": (
            None
            if bundle.latest_backtest is None
            else {
                "backtest_id": bundle.latest_backtest.backtest_id,
                "passed": bool(bundle.latest_backtest.passed),
                "observed": round(float(bundle.latest_backtest.observed), 4),
                "threshold": round(float(bundle.latest_backtest.threshold), 4),
                "completed_at": _iso(bundle.latest_backtest.completed_at),
            }
        ),
        "recent_records": [
            {
                "anchor_id": rec.anchor_id,
                "persona_id": rec.persona_id,
                "modal_outcome": rec.modal_outcome,
                "confidence": round(float(rec.confidence), 4),
                "paired": bool(rec.paired),
                "observed_outcome": rec.observed_outcome,
                "captured_at": _iso(rec.captured_at) if rec.captured_at else None,
            }
            for rec in list(bundle.recent_records)[: bundle.recent_records_cap]
        ],
    }
    return f"{system}\n\nDATA:\n{json.dumps(data, sort_keys=True)}\n"


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _parse_llm_output(
    raw_output: str,
    *,
    workspace_id: str,
    period_key: str,
    prompt_hash: str,
    now: datetime,
) -> list[Insight]:
    """Parse the model's JSON response into a list of ``Insight`` records.

    Robustness rules:
      - The body may be wrapped in a fenced code block; strip ``` fences
        and any leading prose.
      - The top-level shape MUST be an object with an ``insights`` array.
        Anything else collapses to an empty list with a debug log.
      - Per-item validation: required keys (kind/title/body/severity)
        must all be present and the right type. Invalid items are
        skipped (warn) while valid siblings are kept.
      - Severity must be one of ``info|warning|critical``; out-of-vocab
        values coerce to ``info`` rather than dropping the row.
      - Each surviving item gets a stable id computed by hashing
        ``(workspace_id, period_key, prompt_hash, kind, ordinal)``.
    """
    cleaned = _strip_json_fences(raw_output).strip()
    if not cleaned:
        return []
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "insights_llm.json_decode_error",
            extra={
                "workspace_id": workspace_id,
                "period_key": period_key,
                "error": str(exc),
                "raw_head": cleaned[:120],
            },
        )
        return []

    if not isinstance(payload, dict):
        logger.warning(
            "insights_llm.payload_not_object",
            extra={"workspace_id": workspace_id, "period_key": period_key},
        )
        return []

    items_raw = payload.get("insights")
    if not isinstance(items_raw, list):
        logger.warning(
            "insights_llm.insights_field_missing_or_not_list",
            extra={"workspace_id": workspace_id, "period_key": period_key},
        )
        return []

    parsed: list[Insight] = []
    skipped = 0
    for ordinal, raw_item in enumerate(items_raw):
        if not isinstance(raw_item, dict):
            skipped += 1
            continue
        kind = raw_item.get("kind")
        title = raw_item.get("title")
        body = raw_item.get("body")
        severity_raw = raw_item.get("severity")
        anchor_refs_raw = raw_item.get("anchor_refs", [])
        if not (
            isinstance(kind, str)
            and kind.strip()
            and isinstance(title, str)
            and title.strip()
            and isinstance(body, str)
            and body.strip()
            and isinstance(severity_raw, str)
        ):
            skipped += 1
            continue
        # Coerce out-of-vocab severity to "info" so the UI's severity
        # → colour mapping stays defined. cast() keeps the Literal type
        # narrow for mypy without an opaque ignore.
        severity_value = severity_raw if severity_raw in _ALLOWED_SEVERITIES else "info"
        severity_norm = cast(InsightSeverity, severity_value)
        anchor_refs: tuple[str, ...]
        if isinstance(anchor_refs_raw, list):
            anchor_refs = tuple(
                str(ref) for ref in anchor_refs_raw if isinstance(ref, str) and ref.strip()
            )
        else:
            anchor_refs = ()
        # Trim oversized fields so the wire response stays bounded.
        # The DTO doesn't enforce a length but the UI's card layout
        # breaks past these bounds.
        safe_title = title.strip()[:160]
        safe_body = body.strip()[:800]
        safe_kind = kind.strip()[:64]
        insight_id = _stable_insight_id(
            workspace_id=workspace_id,
            period_key=period_key,
            prompt_hash=prompt_hash,
            kind=safe_kind,
            ordinal=ordinal,
        )
        # The opaque kind extension means we feed strings outside the
        # ``InsightKind`` Literal into the dataclass — Python is
        # permissive at runtime; cast() keeps mypy quiet without an
        # unscoped ignore.
        parsed.append(
            Insight(
                id=insight_id,
                kind=cast(Any, safe_kind),
                title=safe_title,
                body=safe_body,
                severity=severity_norm,
                generated_at=now,
                anchor_refs=anchor_refs,
            )
        )

    if skipped:
        logger.warning(
            "insights_llm.partial_parse",
            extra={
                "workspace_id": workspace_id,
                "period_key": period_key,
                "skipped": skipped,
                "kept": len(parsed),
            },
        )
    return parsed


def _strip_json_fences(text: str) -> str:
    """Strip ``` fences and a leading ``json`` language tag if present.

    LLMs commonly wrap JSON in a fenced block even when the prompt asks
    for raw JSON. Be tolerant rather than fail closed.
    """
    s = text.strip()
    if s.startswith("```"):
        # Strip the opening fence (with or without a language tag).
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1 :]
        else:
            s = s[3:]
        # Strip the trailing fence if present.
        if s.rstrip().endswith("```"):
            last_fence = s.rfind("```")
            s = s[:last_fence]
    return s.strip()


def _stable_insight_id(
    *,
    workspace_id: str,
    period_key: str,
    prompt_hash: str,
    kind: str,
    ordinal: int,
) -> str:
    """Compute a deterministic insight id from the surrounding context.

    The id has the prefix ``llm_`` so debugging logs make the synthesizer
    source obvious at a glance, and a 12-hex-char SHA-1 tail so the
    namespace is bounded but collision-resistant for a single workspace's
    poll cadence.
    """
    safe_kind = _safe_id_segment(kind) or "insight"
    payload = "|".join(
        [
            workspace_id,
            period_key or "",
            prompt_hash,
            safe_kind,
            str(ordinal),
        ]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"llm_{safe_kind}_{digest}"


# ---------------------------------------------------------------------------
# Bundle hashing — cache key
# ---------------------------------------------------------------------------


def _hash_bundle(bundle: LLMInsightsInput) -> str:
    """Compute a stable hash of the data portion of the bundle.

    The hash drives the LRU cache key — two calls with the same workspace
    + same data hash return the cached output. ``now`` and tunables are
    EXCLUDED so a polling UI doesn't blow the cache every second; the
    period_key + the data series implicitly drift over time as new
    records land.
    """
    items: dict[str, Any] = {
        "period_key": bundle.period_key,
        "rolling_accuracy": [
            (round(float(p.accuracy), 6), int(p.sample_count), _iso(p.ts))
            for p in bundle.rolling_accuracy
        ],
        "drift": (
            None
            if bundle.confidence_drift is None
            else (bundle.confidence_drift.trend, round(float(bundle.confidence_drift.magnitude), 6))
        ),
        "per_persona": sorted(
            [
                (e.persona_id, round(float(e.calibration), 6), int(e.sample_count))
                for e in bundle.per_persona_calibration
            ]
        ),
        "tier_deltas": sorted(
            [
                (
                    e.tier,
                    round(float(e.configured), 6),
                    round(float(e.actual), 6),
                )
                for e in bundle.tier_distribution_deltas
            ]
        ),
        "backtest": (
            None
            if bundle.latest_backtest is None
            else (
                bundle.latest_backtest.backtest_id,
                bool(bundle.latest_backtest.passed),
                round(float(bundle.latest_backtest.observed), 6),
                round(float(bundle.latest_backtest.threshold), 6),
            )
        ),
        "recent": [
            (
                r.anchor_id,
                r.persona_id,
                r.modal_outcome,
                round(float(r.confidence), 6),
                bool(r.paired),
                r.observed_outcome,
            )
            for r in bundle.recent_records
        ],
    }
    serialized = json.dumps(items, sort_keys=True, default=str)
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_CACHE_CAPACITY",
    "DEFAULT_CACHE_TTL_SECONDS",
    "LLM_KIND_VOCABULARY",
    "LLM_PROMPT_INSIGHT_CAP",
    "LLMBackendProtocol",
    "LLMInsightsInput",
    "RecentPredictionRecordSummary",
    "configure_cache",
    "reset_cache",
    "synthesize_insights_llm",
]

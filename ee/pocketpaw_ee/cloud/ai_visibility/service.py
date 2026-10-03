# AI visibility — service: run a check and store it.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3).
#
# ``run_check`` asks every engine every question ``runs`` times (concurrently,
# at most ``concurrency`` calls in flight), judges each answer, picks one fix and
# stores an AiVisibilityCheck. Answers vary run to run, so we sample and report
# a frequency per engine ("named X of N"), never a rank. N is the number of
# answers actually received; failed calls are stored with ``ok=False`` and
# counted under ``failed`` so a UI can say "1 of 3 calls failed".
#
# Judging per answer: presence by string match (``judge.mentioned``); a fuzzy
# near miss goes to the optional ``confirm`` hook, else counts as not named
# (``near_miss`` is kept on the row). Only a named answer goes to the decision
# model for position + sentiment. Every judge failure is logged and the row keeps
# ``judgement=None``; it never fails the check.
#
# Tenancy: ``workspace_id`` / ``site_id`` are None for an anonymous check (the
# free public check AV-4 adds). Nothing reads checks back yet (AV-4 / AV-6).

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.ai_visibility.domain import Business, EngineAnswer, Location
from pocketpaw_ee.cloud.ai_visibility.dto import CheckResponse
from pocketpaw_ee.cloud.ai_visibility.engines import Engine, EngineError
from pocketpaw_ee.cloud.ai_visibility.fixes import LISTING_TYPES, Signals, pick_fix
from pocketpaw_ee.cloud.ai_visibility.judge import (
    DecisionModel,
    competitors_mentioned,
    judge_mention,
    mentioned,
    passage_for,
    source_type,
    url_names_business,
)
from pocketpaw_ee.cloud.models.ai_visibility_check import AiVisibilityCheck

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 6
MAX_RUNS = 10
MAX_QUESTIONS = 10

QUESTION_TEMPLATES = (
    "best {type} in {city}",
    "{type} near {area} recommendations",
    "which {type} in {city} do locals recommend?",
    "who is the most trusted {type} in {city}?",
    "top rated {type} in {area}, {city}",
    "affordable {type} in {city}",
    "where should I go for a good {type} in {city}?",
    "{type} in {city} with great reviews",
)

Confirm = Callable[[str, Business], Awaitable[bool]]


def generate_questions(
    business_type: str, city: str, n: int = 3, area: str | None = None
) -> list[str]:
    """The first ``n`` local questions from fixed templates (deterministic)."""
    values = {"type": business_type.strip(), "city": city.strip(), "area": (area or city).strip()}
    return [t.format(**values) for t in QUESTION_TEMPLATES[: max(0, n)]]


async def _judge_answer(
    answer: EngineAnswer,
    business: Business,
    decision_model: DecisionModel | None,
    fallback_model: DecisionModel | None,
    confirm: Confirm | None,
) -> dict[str, Any]:
    passage = passage_for(answer.text, business)
    named = mentioned(answer.text, business, answer.cited_urls)
    near_miss = named is None
    if named is None and confirm is not None:
        try:
            named = await confirm(passage, business)
        except Exception:
            logger.warning("ai_visibility: near-miss confirm failed", exc_info=True)
    judgement = None
    primary = decision_model or fallback_model
    if named and primary is not None:
        try:
            judgement = await judge_mention(
                passage, business, primary, fallback_model if decision_model else None
            )
        except Exception:
            logger.warning("ai_visibility: mention judgement failed", exc_info=True)
    return {
        "mentioned": bool(named),
        "near_miss": near_miss,
        "competitors": competitors_mentioned(answer.text, business.competitors),
        "judgement": asdict(judgement) if judgement else None,
        "sources": [
            {"url": u, "type": source_type(u, business.domain)} for u in answer.consulted_urls
        ],
        "judge_cost_usd": judgement.cost_usd if judgement else 0.0,
    }


def _summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in runs:
        s = out.setdefault(
            row["engine"],
            {"named": 0, "of": 0, "failed": 0, "near_miss": 0, "competitors": Counter()},
        )
        if not row["ok"]:
            s["failed"] += 1
            continue
        s["of"] += 1
        s["named"] += row["mentioned"]
        s["near_miss"] += row["near_miss"]
        s["competitors"].update(row["competitors"])
    for s in out.values():
        s["competitors"] = dict(s["competitors"])
    return out


def _signals(runs: list[dict[str, Any]], business: Business, site_blocks_ai_bots: bool) -> Signals:
    ok = [r for r in runs if r["ok"]]
    judged = [r["judgement"] for r in ok if r["judgement"]]
    sources = [s for r in ok for s in r["sources"]]
    seen = {s["type"] for s in sources if s["type"] in LISTING_TYPES}
    own_listing = {
        s["type"]
        for s in sources
        if s["type"] in LISTING_TYPES and url_names_business(s["url"], business)
    }
    return Signals(
        mentioned_any=any(r["mentioned"] for r in ok),
        recommended_any=any(j["position"] == "recommended" for j in judged),
        negative_mentions=sum(j["position"] == "negative" for j in judged),
        avg_sentiment=(sum(j["sentiment"] for j in judged) / len(judged)) if judged else None,
        has_site=bool(business.domain),
        own_site_consulted=any(s["type"] == "own_site" for s in sources),
        listing_types_seen=frozenset(seen),
        business_listing_types=frozenset(own_listing),
        site_blocks_ai_bots=site_blocks_ai_bots,
    )


async def run_check(
    business: Business,
    location: Location,
    questions: Sequence[str],
    engines: Sequence[Engine],
    runs: int = 3,
    *,
    workspace_id: str | None = None,
    site_id: str | None = None,
    decision_model: DecisionModel | None = None,
    fallback_model: DecisionModel | None = None,
    confirm: Confirm | None = None,
    site_blocks_ai_bots: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> CheckResponse:
    """Ask ``engines`` x ``questions`` x ``runs``, judge, pick a fix, store and
    return the check. Engine and judge failures are recorded, never raised."""
    questions = [q.strip() for q in questions if q and q.strip()]
    if not business.name.strip():
        raise ValidationError("ai_visibility.business_name", "Business name is required")
    if not engines:
        raise ValidationError("ai_visibility.no_engines", "No AI engine is configured")
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValidationError(
            "ai_visibility.questions", f"Ask between 1 and {MAX_QUESTIONS} questions"
        )
    if not 1 <= runs <= MAX_RUNS:
        raise ValidationError("ai_visibility.runs", f"Runs must be between 1 and {MAX_RUNS}")

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(engine: Engine, question: str, run: int) -> dict[str, Any]:
        base = {"engine": engine.name, "question": question, "run": run}
        async with sem:
            try:
                answer = await engine.ask(question, location)
            except Exception as exc:
                # EngineError text is key-scrubbed; anything else keeps its type only.
                error = str(exc) if isinstance(exc, EngineError) else type(exc).__name__
                logger.warning("ai_visibility: %s failed: %s", engine.name, error)
                return {**base, "ok": False, "error": error[:500], "cost_usd": 0.0}
            judged = await _judge_answer(answer, business, decision_model, fallback_model, confirm)
        judge_cost = judged.pop("judge_cost_usd")
        return {
            **base,
            "ok": True,
            "model": answer.model,
            "text": answer.text,
            "cited_urls": list(answer.cited_urls),
            "consulted_urls": list(answer.consulted_urls),
            "usage": answer.raw_usage,
            "engine_cost_usd": answer.cost_usd,
            "judge_cost_usd": judge_cost,
            "cost_usd": answer.cost_usd + judge_cost,
            **judged,
        }

    rows = await asyncio.gather(
        *(one(e, q, i) for e in engines for q in questions for i in range(runs))
    )
    doc = AiVisibilityCheck(
        workspace=workspace_id,
        site_id=site_id,
        business=asdict(business),
        location=asdict(location),
        questions=questions,
        runs=list(rows),
        summary=_summary(list(rows)),
        fix=pick_fix(_signals(list(rows), business, site_blocks_ai_bots)),
        total_cost_usd=round(sum(r["cost_usd"] for r in rows), 6),
    )
    await doc.insert()
    # no-event: nothing consumes checks until the AV-4 / AV-6 routes and views land.
    return _to_response(doc)


def _to_response(doc: AiVisibilityCheck) -> CheckResponse:
    return CheckResponse(
        id=str(doc.id),
        workspace_id=doc.workspace,
        site_id=doc.site_id,
        business=doc.business,
        location=doc.location,
        questions=doc.questions,
        runs=doc.runs,
        summary=doc.summary,
        fix=doc.fix,
        total_cost_usd=doc.total_cost_usd,
        created_at=doc.createdAt,
    )


__all__ = ["QUESTION_TEMPLATES", "generate_questions", "run_check"]

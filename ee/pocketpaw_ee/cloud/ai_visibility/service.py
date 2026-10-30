# AI visibility — service: run checks, store them, and serve a site's card.
#
# ``run_check`` asks every engine every question ``runs`` times (concurrently,
# at most ``concurrency`` calls in flight), judges each answer, picks one fix and
# stores an AiVisibilityCheck. Answers vary run to run, so we sample and report
# a frequency per engine ("named X of N"), never a rank. N is the number of
# answers actually received; failed calls are stored with ``ok=False`` and
# counted under ``failed``. Judge failures are logged and never fail a check.
# ``signals`` turns stored rows into ``fixes.pick_fix`` inputs so a caller can
# pick a fix over a subset of rows.
#
# Anonymous checks (the free public check, ``public_check.py``) have no
# workspace or site; ``anonymous_spend_today`` sums them (one Mongo $group) for
# its spend cap.
#
# Site checks (the Staff card): an AiVisibilitySite row holds a site's questions
# and its latest check state. ``request_check`` gates on the site's plan (the
# tier that sells the concierge, i.e. Staff, via ``resolve_site_entitlements``)
# and a 24-hour limit, marks the row pending and queues ``run_site_check`` on the
# site lane; ``service_admin.sweep_due_checks`` queues the monthly ones (only with
# ``scheduler_enabled()``, so the card shows ``next_run_at`` only then). A job that
# fails, or is cancelled by arq's timeout, leaves the row "failed", never
# "running". ``apply_fix`` needs the Staff plan and the fix id of the site's latest
# check, then starts the site's normal republish (``sites.service.publish_pocket``
# with no plan key, so the plan never changes; note it also pushes any unpublished
# draft edits live). Every read is workspace-scoped; ``run_site_check`` reads by
# the site id it queued.

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import (
    BadRequest,
    Forbidden,
    NotFound,
    RateLimited,
    ValidationError,
)
from pocketpaw_ee.cloud.ai_visibility.domain import Business, EngineAnswer, Location
from pocketpaw_ee.cloud.ai_visibility.dto import (
    ApplyFixRequest,
    CheckResponse,
    SetQuestionsRequest,
    SiteVisibilityResponse,
)
from pocketpaw_ee.cloud.ai_visibility.engines import Engine, EngineError, default_engines
from pocketpaw_ee.cloud.ai_visibility.fixes import FIXES, LISTING_TYPES, Signals, pick_fix
from pocketpaw_ee.cloud.ai_visibility.judge import (
    DecisionModel,
    competitors_mentioned,
    default_decision_models,
    judge_mention,
    mentioned,
    passage_for,
    source_type,
    url_names_business,
)
from pocketpaw_ee.cloud.models.ai_visibility_check import AiVisibilityCheck
from pocketpaw_ee.cloud.models.ai_visibility_site import AiVisibilitySite
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

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

ENGINE_LABELS = {"openai": "ChatGPT", "perplexity": "Perplexity (search)", "claude": "Claude"}
APPLICABLE_FIXES = frozenset(fid for fid, (_, ours) in FIXES.items() if ours)
CHECK_COOLDOWN = timedelta(hours=24)
CHECK_INTERVAL = timedelta(days=30)
SITE_CHECK_RUNS = 3
SITE_CHECK_FUNCTION_NAME = "ai_visibility_site_check"

# A word in the business name that tells us what to ask about. Anything else is
# asked about as a plain "business".
_TYPE_WORDS = {
    "pizza": "pizza restaurant",
    "pizzeria": "pizza restaurant",
    "cafe": "cafe",
    "coffee": "coffee shop",
    "bakery": "bakery",
    "restaurant": "restaurant",
    "bistro": "restaurant",
    "grill": "restaurant",
    "sushi": "sushi restaurant",
    "bar": "bar",
    "pub": "pub",
    "brewery": "brewery",
    "dentist": "dentist",
    "dental": "dentist",
    "clinic": "clinic",
    "salon": "hair salon",
    "barber": "barber",
    "barbershop": "barber",
    "spa": "spa",
    "gym": "gym",
    "fitness": "gym",
    "yoga": "yoga studio",
    "hotel": "hotel",
    "plumber": "plumber",
    "plumbing": "plumber",
    "electrician": "electrician",
    "florist": "florist",
    "bookstore": "bookstore",
    "books": "bookstore",
    "pharmacy": "pharmacy",
    "vet": "vet",
    "veterinary": "vet",
    "law": "law firm",
    "realty": "real estate agent",
}


def guess_business_type(name: str) -> str:
    """The kind of business a word in ``name`` names, else "business"."""
    for word in re.findall(r"[a-z]+", name.lower()):
        if word in _TYPE_WORDS:
            return _TYPE_WORDS[word]
    return "business"


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


def signals(runs: list[dict[str, Any]], business: Business, site_blocks_ai_bots: bool) -> Signals:
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
        fix=pick_fix(signals(list(rows), business, site_blocks_ai_bots)),
        total_cost_usd=round(sum(r["cost_usd"] for r in rows), 6),
    )
    await doc.insert()
    # no-event: checks are read back by request (the routes), nothing subscribes.
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


async def anonymous_spend_today(now: datetime | None = None) -> float:
    """USD spent today (UTC) on anonymous checks (no workspace, no site)."""
    now = now or datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor = AiVisibilityCheck.get_pymongo_collection().aggregate(
        [
            {"$match": {"workspace": None, "site_id": None, "createdAt": {"$gte": start}}},
            {"$group": {"_id": None, "total": {"$sum": "$total_cost_usd"}}},
        ]
    )
    if inspect.isawaitable(cursor):
        cursor = await cursor
    async for row in cursor:
        return round(float(row.get("total") or 0), 6)
    return 0.0


# --------------------------------------------------------------------------- #
# Site checks (the Staff card)
# --------------------------------------------------------------------------- #


def scheduler_enabled() -> bool:
    """The opt-in for scheduled work (the monthly sweep), same flag as the web
    process's scheduled loops."""
    return os.environ.get("POCKETPAW_CLOUD_SCHEDULER_ENABLED", "").lower() == "true"


def _utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _oid(value: str) -> PydanticObjectId | None:
    try:
        return PydanticObjectId(value)
    except Exception:
        return None


async def _site(workspace_id: str, site_id: str) -> _SiteDoc:
    oid = _oid(site_id)
    doc = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id}) if oid else None
    if doc is None:
        raise NotFound("site", site_id)
    return doc


async def _state(workspace_id: str, site_id: str) -> AiVisibilitySite | None:
    return await AiVisibilitySite.find_one({"workspace": workspace_id, "site_id": site_id})


def site_plan_allows_check(site: Any) -> bool:
    """True when the site's own plan sells the concierge (Staff) and is active.
    The pure resolver, not ``concierge_plan_entitled``, which answers True for
    every site when site billing is not enforced: checks spend platform money."""
    from pocketpaw_ee.cloud.entitlements.service import resolve_site_entitlements

    return resolve_site_entitlements(
        site_id=str(site.id),
        workspace_id=site.workspace,
        plan_tier=site.plan_tier,
        subscription_status=site.subscription_status,
        concierge_enabled=True,
    ).concierge_entitled


def _business_for(site: Any) -> Business:
    name = (site.name or "").strip() or "this business"
    host = urlsplit(site.url).hostname if site.url else None
    return Business(name=name, business_type=guess_business_type(name), domain=host)


def _site_response(
    site: Any, state: AiVisibilitySite | None, last: AiVisibilityCheck | None
) -> dict:
    allows = site_plan_allows_check(site)
    check = None
    if state is not None and state.status != "none":
        summary = last.summary if last else {}
        competitors: Counter[str] = Counter()
        for row in summary.values():
            competitors.update(row.get("competitors") or {})
        urls_by_type: dict[str, set[str]] = defaultdict(set)
        for run in last.runs if last else []:
            if not run.get("ok"):
                continue
            for src in run.get("sources") or []:
                urls_by_type[src["type"]].add(src["url"])
        ran_at = _utc(state.ran_at)
        check = {
            "status": state.status,
            "ran_at": ran_at,
            "next_run_at": (
                ran_at + CHECK_INTERVAL if allows and ran_at and scheduler_enabled() else None
            ),
            "questions": last.questions if last else state.questions,
            "engines": [
                {
                    "label": ENGINE_LABELS.get(name, name),
                    "named": row["named"],
                    "of": row["of"],
                    "failed": row["failed"],
                }
                for name, row in summary.items()
            ],
            "competitors": [{"name": n, "count": c} for n, c in competitors.most_common()],
            "sources": sorted(
                ({"type": t, "count": len(u)} for t, u in urls_by_type.items()),
                key=lambda x: -x["count"],
            ),
            "fix": last.fix if last else None,
        }
    return SiteVisibilityResponse(
        ai_training_allowed=bool(getattr(site, "ai_training_allowed", False)),
        plan_allows_check=allows,
        questions=state.questions if state else [],
        check=check,
    ).model_dump(mode="json")


async def _last_check(
    workspace_id: str, state: AiVisibilitySite | None
) -> AiVisibilityCheck | None:
    if state is not None and state.last_check_id and (oid := _oid(state.last_check_id)):
        return await AiVisibilityCheck.find_one({"_id": oid, "workspace": workspace_id})
    return None


async def get_site_visibility(workspace_id: str, site_id: str) -> dict:
    site = await _site(workspace_id, site_id)
    state = await _state(workspace_id, site_id)
    return _site_response(site, state, await _last_check(workspace_id, state))


async def set_questions(workspace_id: str, site_id: str, body: SetQuestionsRequest) -> dict:
    body = SetQuestionsRequest.model_validate(body)
    await _site(workspace_id, site_id)
    state = await _state(workspace_id, site_id) or AiVisibilitySite(
        workspace=workspace_id, site_id=site_id
    )
    state.questions = body.questions
    await state.save()
    # no-event: the card reads this back by request; nothing subscribes.
    return await get_site_visibility(workspace_id, site_id)


async def _enqueue(site_id: str) -> None:
    from pocketpaw_ee.cloud._core.redis_client import get_arq_pool
    from pocketpaw_ee.sites.build_job import SITE_BUILD_QUEUE_NAME

    pool = await get_arq_pool()
    await pool.enqueue_job(SITE_CHECK_FUNCTION_NAME, site_id, _queue_name=SITE_BUILD_QUEUE_NAME)


async def queue_check(state: AiVisibilitySite, now: datetime) -> None:
    """Mark ``state`` pending at ``now`` and queue its check. Stamped before the
    enqueue so neither the 24-hour limit nor the monthly sweep queues it twice."""
    state.status, state.requested_at, state.error = "pending", now, ""
    await state.save()
    try:
        await _enqueue(state.site_id)
    except Exception:
        state.status, state.error = "failed", "could not queue the check"
        await state.save()
        raise
    # no-event: the card polls GET; the job writes the result.


async def request_check(workspace_id: str, site_id: str, now: datetime | None = None) -> dict:
    """Queue an owner-requested check: Staff only, at most once per 24 hours."""
    site = await _site(workspace_id, site_id)
    if not site_plan_allows_check(site):
        raise Forbidden(
            "ai_visibility.plan_required", "AI visibility checks come with the Staff plan."
        )
    state = await _state(workspace_id, site_id)
    if state is None or not state.questions:
        raise ValidationError("ai_visibility.questions", "Add at least one question first.")
    now = now or datetime.now(UTC)
    requested = _utc(state.requested_at)
    if requested is not None and requested > now - CHECK_COOLDOWN:
        raise RateLimited("ai_visibility.too_soon", "You can run a check once a day.")
    await queue_check(state, now)
    return {"status": "pending"}


async def run_site_check(site_id: str) -> None:
    """The queued job: ask every configured engine the site's questions
    ``SITE_CHECK_RUNS`` times and record the result on its state row."""
    # global-read: the job carries only the site id it queued; the row names the
    # workspace and every read below is scoped to it.
    state = await AiVisibilitySite.find_one({"site_id": site_id})
    if state is None:
        return
    oid = _oid(site_id)
    site = await _SiteDoc.find_one({"_id": oid, "workspace": state.workspace}) if oid else None
    try:
        if site is None:
            raise RuntimeError("site not found")
        engines = default_engines()
        if not engines:
            raise RuntimeError("no engine configured")
        await state.set({"status": "running"})
        decision, fallback = default_decision_models()
        check = await run_check(
            _business_for(site),
            Location(city="", country=""),
            state.questions,
            engines,
            runs=SITE_CHECK_RUNS,
            workspace_id=state.workspace,
            site_id=site_id,
            decision_model=decision,
            fallback_model=fallback,
        )
        await state.set(
            {"status": "done", "ran_at": datetime.now(UTC), "last_check_id": check.id, "error": ""}
        )
    except asyncio.CancelledError:
        # arq's job timeout cancels the task; record it or the card polls forever.
        logger.warning("ai_visibility: site check %s timed out", site_id)
        await state.set({"status": "failed", "error": "the check timed out"})
        raise
    except Exception as exc:
        error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        logger.warning("ai_visibility: site check %s failed: %s", site_id, error)
        await state.set({"status": "failed", "error": error[:200]})
    # no-event: the card polls GET for the result.


_republishes: set[asyncio.Task] = set()


def _republish_done(task: asyncio.Task) -> None:
    _republishes.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("ai_visibility: apply-fix republish failed: %r", task.exception())


async def apply_fix(workspace_id: str, user_id: str, site_id: str, body: ApplyFixRequest) -> dict:
    """Start the site's normal republish for the fix its latest check picked, when
    Paw Sites applies that fix itself (``ai_access``: the republish writes the
    AI-ready robots.txt). Staff only; any other fix id is a 400."""
    body = ApplyFixRequest.model_validate(body)
    if body.fix_id not in APPLICABLE_FIXES:
        raise BadRequest(
            "ai_visibility.fix_not_applicable", "This fix is a step you take on another site."
        )
    site = await _site(workspace_id, site_id)
    if not site_plan_allows_check(site):
        raise Forbidden(
            "ai_visibility.plan_required", "AI visibility fixes come with the Staff plan."
        )
    last = await _last_check(workspace_id, await _state(workspace_id, site_id))
    if last is None or (last.fix or {}).get("id") != body.fix_id:
        raise BadRequest(
            "ai_visibility.fix_not_applicable", "This isn't the fix your latest check suggested."
        )
    from pocketpaw_ee.sites import service as sites_service

    # No plan key: a keyless republish keeps the site's plan (a content edit).
    task = asyncio.create_task(
        sites_service.publish_pocket(
            workspace_id=workspace_id, user_id=user_id, pocket_id=site.pocket_id
        )
    )
    _republishes.add(task)
    task.add_done_callback(_republish_done)
    # no-event: the republish emits SitePublished itself.
    return {"republish": "started"}


__all__ = [
    "APPLICABLE_FIXES",
    "CHECK_COOLDOWN",
    "CHECK_INTERVAL",
    "ENGINE_LABELS",
    "QUESTION_TEMPLATES",
    "SITE_CHECK_FUNCTION_NAME",
    "anonymous_spend_today",
    "apply_fix",
    "generate_questions",
    "get_site_visibility",
    "guess_business_type",
    "queue_check",
    "request_check",
    "run_check",
    "run_site_check",
    "scheduler_enabled",
    "set_questions",
    "signals",
    "site_plan_allows_check",
]

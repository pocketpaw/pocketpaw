# AI visibility — the free public check behind ``POST /tools/ai-check``.
#
# Anyone can ask "does ChatGPT name my business?" without signing in, so every
# call is guarded in cost order: Cloudflare Turnstile first (a bot never costs a
# DB read or an engine call), then "is an engine configured", then the daily
# spend cap over today's anonymous checks (``POCKETPAW_AI_CHECK_DAILY_USD``), and
# only then the paid run: OpenAI only, 3 questions x 2 runs.
#
# One question names the business ("Is X in Y a good choice?") so we can see what
# the engine reads about it. An answer to that question nearly always repeats the
# name, so "mentioned" and the fix are decided ONLY from the two questions that do
# not name it; the named one contributes sources. Competitors are always [] here:
# an anonymous check has no competitor list and AV-3 does no competitor discovery.
#
# The check is stored like any other (workspace and site None); the caller's IP
# is never stored (the shared ``_core.turnstile`` verifier). With
# ``POCKETPAW_TURNSTILE_SECRET`` unset (dev) Turnstile is skipped with a warning.
# Turnstile network errors fail closed.

from __future__ import annotations

import logging

import httpx

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud._core.turnstile import verify_turnstile as _verify_turnstile
from pocketpaw_ee.cloud.ai_visibility import service
from pocketpaw_ee.cloud.ai_visibility.domain import Business, Location
from pocketpaw_ee.cloud.ai_visibility.dto import AiCheckRequest, AiCheckResponse
from pocketpaw_ee.cloud.ai_visibility.engines import Engine, default_engines
from pocketpaw_ee.cloud.ai_visibility.fixes import pick_fix
from pocketpaw_ee.cloud.ai_visibility.judge import default_decision_models

logger = logging.getLogger(__name__)

RUNS = 2
MAX_SOURCES = 10


class AiCheckDailyLimit(CloudError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "tools.ai_check.daily_limit",
            "The free check has hit its limit for today. Try again tomorrow.",
        )


class AiCheckEngineFailed(CloudError):
    def __init__(self) -> None:
        super().__init__(
            502,
            "tools.ai_check.engine_failed",
            "We couldn't reach the AI assistant just now. Try again in a few minutes.",
        )


def questions_for(name: str, city: str) -> tuple[str, list[str]]:
    """``(named_question, all_questions)``: one that names the business, two that don't."""
    named = f"Is {name} in {city} a good choice?"
    kind = service.guess_business_type(name)
    return named, [named, *service.generate_questions(kind, city, n=2)]


async def verify_turnstile(
    token: str,
    remote_ip: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Raise 400 ``tools.ai_check.turnstile_failed`` unless Cloudflare accepts ``token``
    (the shared ``_core.turnstile`` verifier with this route's error code)."""
    await _verify_turnstile(
        token, remote_ip, code="tools.ai_check.turnstile_failed", transport=transport
    )


async def run_public_check(
    body: AiCheckRequest,
    remote_ip: str | None,
    *,
    engines: list[Engine] | None = None,
    turnstile_transport: httpx.AsyncBaseTransport | None = None,
) -> AiCheckResponse:
    from pocketpaw.config import get_settings

    await verify_turnstile(body.turnstile_token, remote_ip, transport=turnstile_transport)

    if engines is None:
        engines = [e for e in default_engines() if e.name == "openai"]
    if not engines:
        logger.error("ai_check: no engine configured (POCKETPAW_AI_VISIBILITY_OPENAI_API_KEY)")
        raise AiCheckEngineFailed()

    # ponytail: checked before the run, so concurrent checks can overshoot the cap
    # by the checks already in flight (~$0.15 each); a reservation row fixes that.
    cap = float(get_settings().ai_check_daily_usd)
    if await service.anonymous_spend_today() >= cap:
        raise AiCheckDailyLimit()

    business = Business(
        name=body.name, business_type=service.guess_business_type(body.name), domain=body.website
    )
    named_q, questions = questions_for(body.name, body.city)
    decision, fallback = default_decision_models()
    check = await service.run_check(
        business,
        Location(city=body.city, country=""),
        questions,
        engines,
        runs=RUNS,
        decision_model=decision,
        fallback_model=fallback,
    )

    ok = [r for r in check.runs if r["ok"]]
    unprompted = [r for r in ok if r["question"] != named_q]
    if not unprompted:
        raise AiCheckEngineFailed()

    sources: dict[str, str] = {}
    for row in ok:
        for src in row["sources"]:
            if src["url"].startswith("https://") and len(sources) < MAX_SOURCES:
                sources.setdefault(src["url"], src["type"])

    fix = pick_fix(service.signals(unprompted, business, site_blocks_ai_bots=False))
    return AiCheckResponse(
        mentioned=any(r["mentioned"] for r in unprompted),
        answers_checked=len(unprompted),
        competitors=[],
        sources=[{"type": t, "url": u} for u, t in sources.items()],
        fix={"id": fix["id"], "text": fix["text"]},
    )


__all__ = [
    "AiCheckDailyLimit",
    "AiCheckEngineFailed",
    "questions_for",
    "run_public_check",
    "verify_turnstile",
]

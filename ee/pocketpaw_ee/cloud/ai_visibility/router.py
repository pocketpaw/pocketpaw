# AI visibility — HTTP routes. Mounted under /api/v1 from ``cloud/__init__.py``.
#
#   POST /tools/ai-check — PUBLIC free check: does ChatGPT name this business?
#                          Per-IP 5/hour, Turnstile, daily spend cap
#                          (``public_check.py``). No user dependency: the dashboard
#                          auth middleware lets /api/v1/* through, so no exemption
#                          entry is needed (same as the public Discover reads).
#
# Thin: no Beanie doc import here (import-linter "AiVisibility" contract); errors
# are CloudError subclasses mapped by the global handler.

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from pocketpaw_ee.cloud._core.rate_limit import client_ip, rate_limit_ai_check_public
from pocketpaw_ee.cloud.ai_visibility.dto import AiCheckRequest, AiCheckResponse
from pocketpaw_ee.cloud.ai_visibility.public_check import run_public_check
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(tags=["ai-visibility"], dependencies=[Depends(require_license)])


@router.post(
    "/tools/ai-check",
    response_model=AiCheckResponse,
    dependencies=[Depends(rate_limit_ai_check_public)],
)
async def free_ai_check(body: AiCheckRequest, request: Request) -> AiCheckResponse:
    """PUBLIC — no sign-in. Ask ChatGPT (3 questions x 2 runs) whether it names the
    business. 400 ``tools.ai_check.turnstile_failed``, 429 ``tools.ai_check.rate_limited``,
    503 ``tools.ai_check.daily_limit``, 502 ``tools.ai_check.engine_failed``."""
    return await run_public_check(body, client_ip(request))

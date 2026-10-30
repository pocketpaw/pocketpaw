"""Platform health — is this deployment's LiteLLM proxy actually working.

Created: 2026-09-16 (feat/platform-settings-health) — chunk 11 of the Paw Admin
PRD. Design: docs/design/drafts/2026-09-15-paw-admin-screen-settings-health.md
§3.3 and §4.3 (the binding spec for the three probes) and
docs/design/drafts/2026-09-15-paw-admin-prd-corrections.md (V15, the same bug
fixed at the source in ``src/pocketpaw/health/checks/connectivity.py``).

WHY THIS IS ITS OWN ENDPOINT AT SUPPORT, NOT PART OF ``/settings`` (OPERATOR).
``platform.health.read`` is SUPPORT because support staff triage outages —
"is chat down for everyone" is a five-second question a support operator must
be able to answer without an operator escalation. If the probes only lived
inside the settings payload (OPERATOR), the SUPPORT grant on
``platform.health.read`` would name a capability no surface could reach — a
registry entry that is decorative. See design doc §3.3.

WHY THIS DOES NOT REUSE ``_check_litellm_reachable``. That function (in
``src/pocketpaw/health/checks/connectivity.py``) is the OSS local health
engine's check, built for a different contract (``HealthCheckResult``, a
single ok/warning/critical verdict against ``/health``). This route needs its
own request/response shape, its own endpoint choice (``/health/readiness``,
not ``/health`` — see probe 1 below), and independent probe 2. It also used to
carry the exact bug (V15/L1) this route exists partly to give an operator
visibility into; that bug is now fixed at the source, but the two call sites
stay independent on purpose so a future regression in one cannot silently
mask as "fixed" via the other still working.

ONLY PROBES 1 AND 2. Probe 3 (BYOK header-forwarding) sends a real, billed
completion through the proxy — a write in every sense that matters, per the
design doc's own §3.3 — and needs a new registry action
(``platform.health.probe``, OPERATOR) that does not exist today. Adding a new
action key is explicitly out of scope for this chunk, so probe 3 is not
implemented here. The response still carries a ``probe3`` block so the
frontend contract has a stable shape to build against, but it always reports
``never_run`` — there is no storage for a probe result that nothing ever
writes. See the PR description for this as an explicit, named gap.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from pocketpaw.config import get_settings
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.platform import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["platform"])

_TIMEOUT_SECONDS = 5.0


class ProbeOut(BaseModel):
    status: str  # success | warning | error | neutral
    word: str
    detail: str
    latency_ms: int | None = None


class Probe3Out(BaseModel):
    """Always ``never_run``: no write endpoint exists yet to populate this. See module docstring."""

    status: str = "never_run"
    word: str = "Never run"
    detail: str = (
        "This deployment has never been checked. Running this probe needs a new "
        "platform.health.probe (operator) action that does not exist yet; until "
        "it ships, nobody knows who is paying for BYOK turns here."
    )
    checked_at: str | None = None
    checked_by: str | None = None


class PlatformHealthOut(BaseModel):
    proxy_reachable: ProbeOut
    master_key_authenticates: ProbeOut
    byok_forwarding: Probe3Out


async def _probe_proxy_reachable(base_url: str) -> ProbeOut:
    """Probe 1: ``GET {litellm_api_base}/health/readiness``, 5s timeout.

    Deliberately not ``/health`` — that endpoint authenticates and actively
    pings every configured model group on the proxy, turning a liveness check
    into upstream traffic. ``/health/readiness`` answers "is the process up"
    without side effects (design doc §4.3, open question on confirming this
    against the running proxy is noted in the PR description).
    """
    url = f"{base_url.rstrip('/')}/health/readiness"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
    except Exception as exc:
        return ProbeOut(
            status="error",
            word="No answer",
            detail=(
                f"Nothing answered at {base_url}: {exc}. "
                "Every chat turn on this deployment is failing right now."
            ),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code == 200:
        return ProbeOut(
            status="success",
            word="Reachable",
            detail=f"Answered from {base_url} in {latency_ms} ms.",
            latency_ms=latency_ms,
        )
    return ProbeOut(
        status="warning",
        word="Answering, not serving",
        detail=(
            f"{base_url} answered with HTTP {resp.status_code}. "
            "The address resolves but the proxy is not ready. Chat turns are failing."
        ),
        latency_ms=latency_ms,
    )


async def _probe_master_key_authenticates(
    base_url: str, api_key: str | None, *, proxy_reached: bool
) -> ProbeOut:
    """Probe 2: ``GET {base}/model/info`` with the master key.

    This is the exact call the model catalog makes (see
    ``ee/pocketpaw_ee/catalog/config.py``), so a green result here means the
    catalog actually works, not just that the network path is open.
    """
    if not proxy_reached:
        return ProbeOut(
            status="neutral", word="Not run", detail="Not run: the proxy did not answer probe 1."
        )

    if not api_key:
        return ProbeOut(
            status="warning",
            word="No key set",
            detail=(
                "No POCKETPAW_LITELLM_API_KEY is set. That is correct only if this "
                "proxy requires no key. On a key-protected proxy, every catalog "
                "read and every key-provisioning call is failing."
            ),
        )

    url = f"{base_url.rstrip('/')}/model/info"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        return ProbeOut(status="error", word="Rejected", detail=f"Request to {url} failed: {exc}")

    if resp.status_code == 200:
        try:
            payload = resp.json()
            count = len(payload.get("data", [])) if isinstance(payload, dict) else 0
        except Exception:
            count = 0
        return ProbeOut(
            status="success",
            word="Authenticates",
            detail=(
                f"The proxy accepted POCKETPAW_LITELLM_API_KEY and returned {count} model groups."
            ),
        )
    return ProbeOut(
        status="error",
        word="Rejected",
        detail=(
            f"The proxy rejected POCKETPAW_LITELLM_API_KEY with HTTP {resp.status_code}. "
            "The model catalog is empty for every operator and every tenant, and no "
            "new tenant key can be minted until this is fixed."
        ),
    )


@router.get("", response_model=PlatformHealthOut)
async def get_platform_health(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.health.read"))],
) -> PlatformHealthOut:
    """Live probes 1 and 2 against the LiteLLM proxy, plus the (never-populated) probe 3 slot."""
    settings = get_settings()
    base_url = settings.litellm_api_base
    api_key = settings.litellm_api_key or None

    proxy = await _probe_proxy_reachable(base_url)
    master_key = await _probe_master_key_authenticates(
        base_url, api_key, proxy_reached=proxy.status == "success"
    )

    payload = PlatformHealthOut(
        proxy_reachable=proxy,
        master_key_authenticates=master_key,
        byok_forwarding=Probe3Out(),
    )

    await audit.record_read(
        operator=operator,
        action="platform.health.read",
        query=f"proxy={proxy.status} master_key={master_key.status}",
        target_type="platform_health",
        request=request,
    )

    return payload


__all__ = ["router"]

# AI visibility — arq glue for the site lane (``sites/build_worker.py``).
#
# ``site_check_fn`` runs one queued site check (``service.run_site_check``);
# ``sweep_cron`` ticks daily at 07:00 UTC and queues every Staff site due its
# monthly check (``service_admin.sweep_due_checks``). The tick only runs with
# ``POCKETPAW_CLOUD_SCHEDULER_ENABLED=true`` on the worker, the same opt-in flag
# the web process's scheduled loops use, because each check spends platform money
# on AI engines. ``unique=True`` keeps a scaled worker fleet to one tick.

from __future__ import annotations

import logging
import os
from typing import Any

from arq import cron
from arq.worker import func

from pocketpaw_ee.cloud.ai_visibility import service, service_admin

logger = logging.getLogger(__name__)

# 27 engine calls (3 engines x up to 10 questions x 3 runs, 6 in flight) with
# provider retries; well past arq's 300s default, far under a build's budget.
_SITE_CHECK_TIMEOUT_SECONDS = 900


async def site_check_job(ctx: dict[str, Any], site_id: str) -> None:
    await service.run_site_check(site_id)


async def monthly_sweep(ctx: dict[str, Any]) -> int:
    if os.environ.get("POCKETPAW_CLOUD_SCHEDULER_ENABLED", "").lower() != "true":
        logger.info("ai_visibility: monthly sweep off (POCKETPAW_CLOUD_SCHEDULER_ENABLED)")
        return 0
    return await service_admin.sweep_due_checks()


site_check_fn = func(
    site_check_job,
    name=service.SITE_CHECK_FUNCTION_NAME,
    timeout=_SITE_CHECK_TIMEOUT_SECONDS,
    max_tries=1,
)

sweep_cron = cron(
    monthly_sweep,
    name="ai_visibility_monthly_sweep",
    hour=7,
    minute=0,
    unique=True,
    run_at_startup=False,
)

__all__ = ["monthly_sweep", "site_check_fn", "site_check_job", "sweep_cron"]

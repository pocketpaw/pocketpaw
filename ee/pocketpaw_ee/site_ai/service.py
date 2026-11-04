# ee/pocketpaw_ee/site_ai/service.py — owner-side reads and writes for a site's AI API.
#
# Settings, presets, budget and usage for one site, addressed by its pocket. Every
# function takes the caller's workspace and resolves the pocket inside it first
# (``bindings.pocket_in_workspace``: another workspace's pocket is a 404), so no
# read or write can cross workspaces. The router gates mutations on
# ``sites.ai_manage`` (ADMIN); this module writes the audit row for each one.
#
# The site's plan (``Site.plan_tier``, free floor when there is no Site yet) decides
# ``plan_models`` and ``allowance_month_micro``. Stored ``allowed_models`` are kept
# as the owner set them; the call path intersects them with the plan at call time,
# so a downgrade narrows access without rewriting the owner's choice.
#
# Spend figures read the atomic counters ``budget.py`` maintains: ``spent`` is
# settled spend only (holds in flight are not spend yet).

from __future__ import annotations

import inspect
import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.models.site_ai import (
    SiteAiBudgetDoc,
    SiteAiCallDoc,
    SiteAiCounterDoc,
    SiteAiPresetDoc,
    SiteAiSettingsDoc,
)
from pocketpaw_ee.site_ai.dto import (
    SiteAiBudget,
    SiteAiBudgetPut,
    SiteAiPreset,
    SiteAiPresetBody,
    SiteAiSettings,
    SiteAiSettingsPut,
    SiteAiUsage,
    UsageCaller,
    UsageDay,
    UsageModel,
)

logger = logging.getLogger(__name__)

MAX_PRESETS = 50
MAX_USAGE_DAYS = 92


# ------------------------------------------------------------------ periods


def day_period(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")


def month_period(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")


def counter_key(workspace_id: str, pocket_id: str, scope: str, period: str) -> str:
    return f"{workspace_id}:{pocket_id}:{scope}:{period}"


# ------------------------------------------------------------------ access


async def _pocket(workspace_id: str, pocket_id: str) -> Any:
    from pocketpaw_ee.integrations.bindings import pocket_in_workspace

    return await pocket_in_workspace(workspace_id, pocket_id)


async def site_for_pocket(workspace_id: str, pocket_id: str) -> Any | None:
    from pocketpaw_ee.cloud.models.site import Site

    return await Site.find_one({"workspace": workspace_id, "pocket_id": pocket_id})


def plan_key(site: Any | None) -> str | None:
    return getattr(site, "plan_tier", None) if site is not None else None


def _audit(user_id: str, workspace_id: str, pocket_id: str, action: str, **extra: Any) -> None:
    try:
        from pocketpaw_ee.guards.audit import log_privileged_action

        log_privileged_action(
            actor=user_id,
            action=action,
            resource_id=pocket_id,
            workspace_id=workspace_id,
            **extra,
        )
    except Exception:  # noqa: BLE001 - auditing never changes the answer
        logger.debug("site_ai: audit failed", exc_info=True)


# ------------------------------------------------------------------ settings


async def load_settings(workspace_id: str, pocket_id: str) -> SiteAiSettingsDoc:
    doc = await SiteAiSettingsDoc.find_one(
        SiteAiSettingsDoc.workspace == workspace_id, SiteAiSettingsDoc.pocket_id == pocket_id
    )
    return doc or SiteAiSettingsDoc(workspace=workspace_id, pocket_id=pocket_id)


def _settings_view(doc: SiteAiSettingsDoc, site: Any | None) -> SiteAiSettings:
    allowed = [m for m in doc.allowed_models if m in site_plans.SITE_AI_CHAT_ALIASES]
    return SiteAiSettings(
        enabled=doc.enabled,
        browser_access=doc.browser_access,
        require_preset_for_browser=doc.require_preset_for_browser,
        allowed_models=allowed,  # type: ignore[arg-type]
        plan_models=list(site_plans.site_ai_models(plan_key(site))),
        keep_transcripts=doc.keep_transcripts,
    )


async def get_settings(workspace_id: str, pocket_id: str) -> SiteAiSettings:
    await _pocket(workspace_id, pocket_id)
    doc = await load_settings(workspace_id, pocket_id)
    return _settings_view(doc, await site_for_pocket(workspace_id, pocket_id))


async def put_settings(
    workspace_id: str, user_id: str, pocket_id: str, body: SiteAiSettingsPut
) -> SiteAiSettings:
    await _pocket(workspace_id, pocket_id)
    site = await site_for_pocket(workspace_id, pocket_id)
    doc = await load_settings(workspace_id, pocket_id)
    changes = body.model_dump(exclude_none=True)
    if "allowed_models" in changes:
        plan = set(site_plans.site_ai_models(plan_key(site)))
        outside = [m for m in changes["allowed_models"] if m not in plan]
        if outside:
            raise ValidationError(
                "site_ai.model_not_in_plan",
                f"This site's plan doesn't include: {', '.join(outside)}.",
            )
        changes["allowed_models"] = list(dict.fromkeys(changes["allowed_models"]))
    for key, value in changes.items():
        setattr(doc, key, value)
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    _audit(user_id, workspace_id, pocket_id, "sites.ai.settings.update", fields=sorted(changes))
    return _settings_view(doc, site)


# ------------------------------------------------------------------ presets


def _preset_view(doc: SiteAiPresetDoc) -> SiteAiPreset:
    return SiteAiPreset(
        id=doc.preset_id,
        name=doc.name,
        system_prompt=doc.system_prompt,
        model=doc.model,  # type: ignore[arg-type]
        max_output_tokens=doc.max_output_tokens,
        temperature=doc.temperature,
        version=doc.version,
        updated_at=doc.updated_at.astimezone(UTC).isoformat(),
    )


async def find_preset(workspace_id: str, pocket_id: str, preset_id: str) -> SiteAiPresetDoc | None:
    if not isinstance(preset_id, str) or not preset_id or len(preset_id) > 64:
        return None
    return await SiteAiPresetDoc.find_one(
        SiteAiPresetDoc.workspace == workspace_id,
        SiteAiPresetDoc.pocket_id == pocket_id,
        SiteAiPresetDoc.preset_id == preset_id,
    )


async def list_presets(workspace_id: str, pocket_id: str) -> list[SiteAiPreset]:
    await _pocket(workspace_id, pocket_id)
    rows = (
        await SiteAiPresetDoc.find(
            SiteAiPresetDoc.workspace == workspace_id, SiteAiPresetDoc.pocket_id == pocket_id
        )
        .sort("+name")
        .to_list()
    )
    return [_preset_view(r) for r in rows]


async def get_preset(workspace_id: str, pocket_id: str, preset_id: str) -> SiteAiPreset:
    await _pocket(workspace_id, pocket_id)
    doc = await find_preset(workspace_id, pocket_id, preset_id)
    if doc is None:
        raise NotFound("site_ai_preset", preset_id)
    return _preset_view(doc)


async def create_preset(
    workspace_id: str, user_id: str, pocket_id: str, body: SiteAiPresetBody
) -> SiteAiPreset:
    await _pocket(workspace_id, pocket_id)
    count = await SiteAiPresetDoc.find(
        SiteAiPresetDoc.workspace == workspace_id, SiteAiPresetDoc.pocket_id == pocket_id
    ).count()
    if count >= MAX_PRESETS:
        raise ValidationError(
            "site_ai.too_many_presets", f"A site can have up to {MAX_PRESETS} presets."
        )
    doc = SiteAiPresetDoc(
        workspace=workspace_id,
        pocket_id=pocket_id,
        preset_id=f"pre_{secrets.token_hex(6)}",
        **body.model_dump(),
    )
    await doc.insert()
    _audit(user_id, workspace_id, pocket_id, "sites.ai.preset.create", preset_id=doc.preset_id)
    return _preset_view(doc)


async def update_preset(
    workspace_id: str, user_id: str, pocket_id: str, preset_id: str, body: SiteAiPresetBody
) -> SiteAiPreset:
    await _pocket(workspace_id, pocket_id)
    doc = await find_preset(workspace_id, pocket_id, preset_id)
    if doc is None:
        raise NotFound("site_ai_preset", preset_id)
    for key, value in body.model_dump().items():
        setattr(doc, key, value)
    # Calls pin the version they ran with; the next message uses the new one.
    doc.version += 1
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    _audit(user_id, workspace_id, pocket_id, "sites.ai.preset.update", preset_id=preset_id)
    return _preset_view(doc)


async def delete_preset(workspace_id: str, user_id: str, pocket_id: str, preset_id: str) -> None:
    await _pocket(workspace_id, pocket_id)
    doc = await find_preset(workspace_id, pocket_id, preset_id)
    if doc is None:
        raise NotFound("site_ai_preset", preset_id)
    await doc.delete()
    _audit(user_id, workspace_id, pocket_id, "sites.ai.preset.delete", preset_id=preset_id)


# ------------------------------------------------------------------ budget


async def load_budget(workspace_id: str, pocket_id: str) -> SiteAiBudgetDoc:
    doc = await SiteAiBudgetDoc.find_one(
        SiteAiBudgetDoc.workspace == workspace_id, SiteAiBudgetDoc.pocket_id == pocket_id
    )
    return doc or SiteAiBudgetDoc(workspace=workspace_id, pocket_id=pocket_id)


async def spent_micro(workspace_id: str, pocket_id: str, scope: str, period: str) -> int:
    doc = await SiteAiCounterDoc.find_one(
        SiteAiCounterDoc.key == counter_key(workspace_id, pocket_id, scope, period)
    )
    return int(doc.spent_micro) if doc is not None else 0


async def _budget_view(doc: SiteAiBudgetDoc, site: Any | None) -> SiteAiBudget:
    now = datetime.now(UTC)
    return SiteAiBudget(
        monthly_cap_micro=doc.monthly_cap_micro,
        daily_cap_micro=doc.daily_cap_micro,
        per_visitor_daily_micro=doc.per_visitor_daily_micro,
        preview_daily_cap_micro=doc.preview_daily_cap_micro,
        alert_thresholds=list(doc.alert_thresholds),
        paused=doc.paused,
        spent_today_micro=await spent_micro(doc.workspace, doc.pocket_id, "day", day_period(now)),
        spent_month_micro=await spent_micro(
            doc.workspace, doc.pocket_id, "month", month_period(now)
        ),
        allowance_month_micro=site_plans.site_ai_allowance_micro(plan_key(site)),
    )


async def get_budget(workspace_id: str, pocket_id: str) -> SiteAiBudget:
    await _pocket(workspace_id, pocket_id)
    doc = await load_budget(workspace_id, pocket_id)
    return await _budget_view(doc, await site_for_pocket(workspace_id, pocket_id))


async def put_budget(
    workspace_id: str, user_id: str, pocket_id: str, body: SiteAiBudgetPut
) -> SiteAiBudget:
    await _pocket(workspace_id, pocket_id)
    doc = await load_budget(workspace_id, pocket_id)
    changes = body.model_dump(exclude_none=True)
    if "alert_thresholds" in changes:
        values = changes["alert_thresholds"]
        if any(not 1 <= v <= 100 for v in values):
            raise ValidationError(
                "site_ai.invalid_thresholds", "Alert thresholds are percentages from 1 to 100."
            )
        changes["alert_thresholds"] = sorted(set(values))
    for key, value in changes.items():
        setattr(doc, key, value)
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    _audit(user_id, workspace_id, pocket_id, "sites.ai.budget.update", fields=sorted(changes))
    return await _budget_view(doc, await site_for_pocket(workspace_id, pocket_id))


# ------------------------------------------------------------------ usage


def _parse_day(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise ValidationError("site_ai.invalid_range", f"`{field}` must be a date.") from None


async def usage(
    workspace_id: str, pocket_id: str, from_: str | None = None, to: str | None = None
) -> SiteAiUsage:
    await _pocket(workspace_id, pocket_id)
    today = datetime.now(UTC).date()
    end = _parse_day(to, "to") or today
    start = _parse_day(from_, "from") or end - timedelta(days=29)
    if start > end or (end - start).days >= MAX_USAGE_DAYS:
        raise ValidationError(
            "site_ai.invalid_range", f"Pick a range of 1 to {MAX_USAGE_DAYS} days."
        )
    pipeline = [
        {
            "$match": {
                "workspace": workspace_id,
                "pocket_id": pocket_id,
                "state": "settled",
                "day": {"$gte": start.isoformat(), "$lte": end.isoformat()},
            }
        },
        {
            "$group": {
                "_id": {"day": "$day", "model": "$model", "caller": "$caller"},
                "micro": {"$sum": "$actual_micro"},
                "calls": {"$sum": 1},
            }
        },
    ]
    cursor = SiteAiCallDoc.get_pymongo_collection().aggregate(pipeline)
    if inspect.isawaitable(cursor):  # pymongo-async in prod, mongomock in tests
        cursor = await cursor
    rows = [row async for row in cursor]
    by_day: dict[str, list[int]] = {}
    by_model: dict[str, list[int]] = {}
    by_caller: dict[str, list[int]] = {}
    for row in rows:
        key, micro, calls = row["_id"], int(row["micro"]), int(row["calls"])
        pairs = ((by_day, key["day"]), (by_model, key["model"]), (by_caller, key["caller"]))
        for bucket, name in pairs:
            acc = bucket.setdefault(name, [0, 0])
            acc[0] += micro
            acc[1] += calls
    days = []
    cursor = start
    while cursor <= end:
        micro, calls = by_day.get(cursor.isoformat(), [0, 0])
        days.append(UsageDay(day=cursor.isoformat(), micro=micro, calls=calls))
        cursor += timedelta(days=1)
    return SiteAiUsage(
        from_=start.isoformat(),
        to=end.isoformat(),
        total_micro=sum(v[0] for v in by_day.values()),
        calls=sum(v[1] for v in by_day.values()),
        by_day=days,
        by_model=[UsageModel(model=k, micro=v[0], calls=v[1]) for k, v in sorted(by_model.items())],
        by_caller=[
            UsageCaller(caller=k, micro=v[0], calls=v[1])  # type: ignore[arg-type]
            for k, v in sorted(by_caller.items())
        ],
    )

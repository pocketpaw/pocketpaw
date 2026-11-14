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
# ``alias_models`` (which agent and model serve each alias) is checked on write: the
# agent must be one the owner may use in this workspace, a model one the gateway
# serves on a platform-paid agent. ``list_models`` is the picker's source: the
# workspace's agents plus the chat catalog the per-send picker reads.
#
# Spend figures read the atomic counters ``budget.py`` maintains: ``spent`` is
# settled spend only (holds in flight are not spend yet).
#
# Transcripts (opt-in, ``keep_transcripts``) are written by ``write_transcript``
# capped and image-free, readable by ``sites.ai_manage`` only, and deleted when the
# owner turns them off. ``move_for_pocket`` (site transfer) re-keys settings,
# presets and budget to the new workspace and deletes transcripts; usage rows stay
# with the old workspace, and spend counters (keyed by pocket) keep counting, so a
# transfer never resets the caps. ``purge_for_pocket`` (site delete) removes every
# row of the pocket in any workspace.

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
    SiteAiTranscriptDoc,
)
from pocketpaw_ee.site_ai.dto import (
    AliasModel,
    SiteAiAgentOption,
    SiteAiBudget,
    SiteAiBudgetPut,
    SiteAiModels,
    SiteAiPreset,
    SiteAiPresetBody,
    SiteAiSettings,
    SiteAiSettingsPut,
    SiteAiTranscript,
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
    """A site's spend counter id. Keyed by the pocket only (pocket ids are unique),
    so a transfer to another workspace keeps the site's spend and its caps;
    ``workspace_id`` is accepted for call-site symmetry and not part of the key."""
    del workspace_id
    return f"site:{pocket_id}:{scope}:{period}"


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
    from pocketpaw_ee.site_ai import aliases

    allowed = [m for m in doc.allowed_models if m in site_plans.SITE_AI_CHAT_ALIASES]
    plan = [m for m in site_plans.site_ai_models(plan_key(site)) if m in aliases.CHAT_ALIASES]
    mapping = {
        alias: AliasModel.model_validate(value)
        for alias, value in (doc.alias_models or {}).items()
        if alias in aliases.CHAT_ALIASES and isinstance(value, dict)
    }
    return SiteAiSettings(
        enabled=doc.enabled,
        browser_access=doc.browser_access,
        require_preset_for_browser=doc.require_preset_for_browser,
        allowed_models=allowed,  # type: ignore[arg-type]
        plan_models=plan,
        online_models=[m for m in plan if not aliases.is_offline(doc.workspace, doc.pocket_id, m)],
        alias_models=mapping,  # type: ignore[arg-type]
        keep_transcripts=doc.keep_transcripts,
    )


async def get_settings(workspace_id: str, pocket_id: str) -> SiteAiSettings:
    await _pocket(workspace_id, pocket_id)
    doc = await load_settings(workspace_id, pocket_id)
    return _settings_view(doc, await site_for_pocket(workspace_id, pocket_id))


async def _check_alias_models(
    workspace_id: str, user_id: str, mapping: dict[str, Any]
) -> dict[str, dict[str, str | None]]:
    """Each entry's agent must be one the owner may use in this workspace, and a
    model must be one the gateway serves (on a platform-paid agent; a BYOK agent's
    own provider judges its model at call time). An empty entry is dropped."""
    from pocketpaw_ee.cloud.chat import side_completion
    from pocketpaw_ee.cloud.chat.runs import run_core

    out: dict[str, dict[str, str | None]] = {}
    for alias, entry in mapping.items():
        agent_id = (entry.get("agent_id") or "").strip() or None
        model = (entry.get("model") or "").strip() or None
        if agent_id is None and model is None:
            continue
        target = await side_completion.resolve_target(workspace_id, user_id, agent_id)
        if target is None:
            raise ValidationError(
                "site_ai.agent_not_found", f"The agent for {alias} isn't in this workspace."
            )
        if model and target.payer != "byok" and await run_core._model_is_unknown_to_gateway(model):
            raise ValidationError(
                "site_ai.model_not_available", f"The model for {alias} isn't available."
            )
        out[alias] = {"agent_id": agent_id, "model": model}
    return out


async def put_settings(
    workspace_id: str, user_id: str, pocket_id: str, body: SiteAiSettingsPut
) -> SiteAiSettings:
    from pocketpaw_ee.site_ai import aliases

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
    if "alias_models" in changes:
        changes["alias_models"] = await _check_alias_models(
            workspace_id, user_id, changes["alias_models"]
        )
        aliases.clear_offline(workspace_id, pocket_id)
    turning_off_transcripts = doc.keep_transcripts and changes.get("keep_transcripts") is False
    for key, value in changes.items():
        setattr(doc, key, value)
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    if turning_off_transcripts:
        await SiteAiTranscriptDoc.find(
            SiteAiTranscriptDoc.workspace == workspace_id,
            SiteAiTranscriptDoc.pocket_id == pocket_id,
        ).delete()
    _audit(user_id, workspace_id, pocket_id, "sites.ai.settings.update", fields=sorted(changes))
    return _settings_view(doc, site)


async def list_models(workspace_id: str, user_id: str, pocket_id: str) -> SiteAiModels:
    """What the owner can map an alias to: the workspace agents they may use (from
    the agents service, concierges left out) and the chat catalog the per-send
    picker reads, with this workspace's availability."""
    from pocketpaw_ee.catalog import availability
    from pocketpaw_ee.catalog import service as catalog_service
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.chat import agent_service

    await _pocket(workspace_id, pocket_id)
    default_id = await agent_service._get_default_workspace_agent_id(workspace_id)
    agents = [
        SiteAiAgentOption(
            id=a.id,
            name=a.name,
            backend=a.config.backend,
            model=a.config.model or "",
            default=a.id == default_id,
        )
        for a in await agents_service.list_agents(workspace_id, viewer_user_id=user_id)
        if not a.disabled
    ]
    models: list[dict[str, Any]] = []
    catalog_error = False
    try:
        entries = await catalog_service.list_models(modality="chat")
        entries = await availability.overlay(workspace_id, entries)
        models = [e.model_dump(mode="json") for e in entries]
    except Exception:  # noqa: BLE001 - the agents still answer without the catalog
        logger.warning("site_ai: model catalog unavailable for the models list", exc_info=True)
        catalog_error = True
    return SiteAiModels(
        agents=agents, models=models, default_agent_id=default_id, catalog_error=catalog_error
    )


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


# ------------------------------------------------------------------ transcripts

TRANSCRIPT_MESSAGES = 20
TRANSCRIPT_TEXT_CHARS = 4_000
TRANSCRIPT_OUTPUT_CHARS = 16_000


def _clip(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else ""
    return text if len(text) <= limit else text[:limit] + "…"


def _transcript_message(msg: dict[str, Any]) -> dict[str, Any]:
    content = msg.get("content")
    if isinstance(content, list):
        parts = [
            _clip(p.get("text"), TRANSCRIPT_TEXT_CHARS) if p.get("type") == "text" else "[image]"
            for p in content
        ]
        text = "\n".join(parts)  # each part is clipped already
    else:
        text = _clip(content, TRANSCRIPT_TEXT_CHARS)
    out: dict[str, Any] = {"role": msg.get("role", ""), "content": text}
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {
                "name": c["function"]["name"],
                "arguments": _clip(c["function"]["arguments"], 2_000),
            }
            for c in msg["tool_calls"]
        ]
    return out


async def write_transcript(
    *,
    workspace_id: str,
    pocket_id: str,
    run_id: str,
    caller: str,
    model: str,
    messages: list[dict[str, Any]],
    output: str,
    tool_calls: list[dict[str, Any]],
) -> None:
    """Keep one call's conversation for the owner (only when they opted in).
    System messages are skipped: a preset's prompt is already the owner's."""
    kept = [m for m in messages if m.get("role") != "system"][-TRANSCRIPT_MESSAGES:]
    await SiteAiTranscriptDoc(
        workspace=workspace_id,
        pocket_id=pocket_id,
        run_id=run_id,
        caller=caller,
        model=model,
        messages=[_transcript_message(m) for m in kept],
        output=_clip(output, TRANSCRIPT_OUTPUT_CHARS),
        tool_calls=[
            {"name": c.get("name", ""), "arguments": _clip(c.get("arguments", ""), 2_000)}
            for c in tool_calls[:16]
        ],
    ).insert()


async def list_transcripts(
    workspace_id: str, pocket_id: str, limit: int = 20, before: str | None = None
) -> list[SiteAiTranscript]:
    await _pocket(workspace_id, pocket_id)
    query: dict[str, Any] = {"workspace": workspace_id, "pocket_id": pocket_id}
    if before:
        try:
            query["created_at"] = {"$lt": datetime.fromisoformat(before)}
        except ValueError:
            raise ValidationError(
                "site_ai.invalid_cursor", "`before` must be a timestamp."
            ) from None
    rows = await SiteAiTranscriptDoc.find(query).sort("-created_at").limit(limit).to_list()
    return [
        SiteAiTranscript(
            run_id=r.run_id,
            caller=r.caller,
            model=r.model,
            created_at=r.created_at.astimezone(UTC).isoformat(),
            messages=r.messages,
            output=r.output,
            tool_calls=r.tool_calls,
        )
        for r in rows
    ]


# ------------------------------------------------------------------ lifecycle


async def move_for_pocket(*, pocket_id: str, destination_workspace_id: str) -> int:
    """A site transfer: settings, presets and budget follow the pocket to the new
    workspace (new calls bill the new owner). Held calls are released, transcripts
    (the old owner's visitors) are deleted, settled usage rows stay where they were
    billed. Matched by pocket in any other workspace, so a resume is idempotent."""
    destination = (destination_workspace_id or "").strip()
    if not pocket_id or not destination:
        return 0
    from pocketpaw_ee.site_ai import budget

    moved = 0
    for doc in (SiteAiSettingsDoc, SiteAiPresetDoc, SiteAiBudgetDoc):
        stale = {"pocket_id": pocket_id, "workspace": {"$ne": destination}}
        if doc is not SiteAiPresetDoc and await doc.find_one(
            {"pocket_id": pocket_id, "workspace": destination}
        ):
            # The destination already configured this site: its row wins.
            await doc.find(stale).delete()
            continue
        result = await doc.get_pymongo_collection().update_many(
            stale, {"$set": {"workspace": destination}}
        )
        moved += int(getattr(result, "modified_count", 0) or 0)
    held = await SiteAiCallDoc.find(
        {"pocket_id": pocket_id, "workspace": {"$ne": destination}, "state": "held"}
    ).to_list()
    for row in held:
        await budget.release(
            budget.Hold(row.run_id, row.workspace, row.pocket_id, row.reserved_micro, row.counters)
        )
    await SiteAiTranscriptDoc.find(
        {"pocket_id": pocket_id, "workspace": {"$ne": destination}}
    ).delete()
    # Counters are keyed by the pocket, so today's and this month's spend (and
    # therefore the caps) carry over; only their owner label moves.
    await SiteAiCounterDoc.get_pymongo_collection().update_many(
        {"pocket_id": pocket_id, "workspace": {"$ne": destination}},
        {"$set": {"workspace": destination}},
    )
    return moved


async def purge_for_pocket(*, workspace_id: str, pocket_id: str) -> int:
    """A site delete: every site AI row of the pocket. The credit ledger keeps its
    debits (they are the bill), nothing else about the site's AI survives."""
    if not pocket_id:
        return 0
    removed = 0
    for doc in (
        SiteAiSettingsDoc,
        SiteAiPresetDoc,
        SiteAiBudgetDoc,
        SiteAiCallDoc,
        SiteAiCounterDoc,
        SiteAiTranscriptDoc,
    ):
        # By pocket in any workspace: a transferred site's history rows stay keyed
        # to the workspace that paid, and they go with the site too.
        result = await doc.find({"pocket_id": pocket_id}).delete()
        removed += int(getattr(result, "deleted_count", 0) or 0)
    return removed

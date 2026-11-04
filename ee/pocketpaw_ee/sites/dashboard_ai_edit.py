# ee/pocketpaw_ee/sites/dashboard_ai_edit.py — "Ask AI to change": turns an owner's
# request into proposed widget ops for one dashboard. Nothing here saves: the client
# previews the ops and applies them through PATCH (with If-Match), which re-gates them.
#
# ``ai_edit`` (manage only, one of the workspace's 50 AI edits a day, refunded when the
# model doesn't answer, 503 ``unavailable`` / ``model_unavailable``, or the site's data
# is too busy to summarize, 429)
# shows the model the schema summary (same rules as generation: no sign-in tables,
# masked / PII columns or values), the dashboard's widgets and the request (neutralised,
# 2,000 chars), and takes ONE forced ``propose_ops`` call. With ``widget_id`` it may only
# update, move or remove that widget, and the widget's current problem (from the drift
# check) rides along, which is how "Fix with AI" works.
#
# Every op is checked before it is returned: ``remove`` / ``move`` / ``update`` must name
# a widget of this dashboard, a layout must fit the 12-column grid, and an ``add`` or an
# ``update`` that changes the query must pass the SQL gate and a live test run (one D1
# batch) and draw with its mapping; failures get one repair call, then are dropped and
# reported in ``dropped``. An ``add`` without a valid layout is placed under the
# existing widgets.
from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
from pocketpaw_ee.sites import dashboard_generate as gen
from pocketpaw_ee.sites import dashboard_service as svc
from pocketpaw_ee.sites.site_data import budget, d1
from pocketpaw_ee.sites.site_data import dashboard_prompt as prompt
from pocketpaw_ee.sites.site_data.dashboard_dto import (
    AiEditOut,
    DroppedWidget,
    WidgetIn,
    WidgetLayout,
)

_FIELDS = ("title", "viz", "chart_type", "sql", "mapping")
_PATCHABLE = (*_FIELDS, "layout", "refresh", "interval_s")


def _layout(raw: Any) -> dict[str, Any] | None:
    try:
        lay = WidgetLayout.model_validate(raw).model_dump()
    except Exception:  # noqa: BLE001 — a bad layout is simply not used
        return None
    return lay if lay["x"] + lay["w"] <= 12 else None


def _stored_fields(w: dict[str, Any]) -> dict[str, Any]:
    return {k: w.get(k) for k in _FIELDS if w.get(k) is not None}


def _patch_of(before: dict[str, Any], after: WidgetIn, extra: dict[str, Any]) -> dict[str, Any]:
    """The fields of ``after`` that differ from the stored widget, plus layout /
    refresh / interval from the model's patch."""
    final = gen.op_widget(after)
    was = {**before, "mapping": before.get("mapping") or {}}
    patch = {k: final[k] for k in _FIELDS if final.get(k) != was.get(k)}
    patch.update(extra)
    return patch


def model_unavailable() -> CloudError:
    return CloudError(
        503,
        "unavailable",
        "The AI couldn't answer right now. Try again in a minute.",
        {"reason": "model_unavailable"},
    )


@svc.busy_as_rate_limited
async def ai_edit(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    instruction: str,
    widget_id: str | None = None,
    _cloudflare: Any | None = None,
) -> AiEditOut:
    target = await svc.manage_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    doc = await svc.find(target, dashboard_id)
    if target.reason:
        raise svc.unavailable(target.reason)
    by_id = {w["id"]: w for w in doc.widgets}
    focus_widget = None
    if widget_id is not None:
        focus_widget = by_id.get(widget_id)
        if focus_widget is None:
            raise NotFound("site_dashboard_widget", widget_id)
    cf = svc.client(target, _cloudflare)
    schema = await d1.introspect(cf, target.d1_id)
    await gen.take_daily(workspace_id, "ai_edit")
    model = prompt.model_name()
    try:
        summary = await gen.summary_for(
            cf, target.d1_id, schema, include_stats=False, focus=instruction, model=model
        )
    except budget.DataBusy:  # nothing was asked of the model: give the edit back
        await gen.refund_daily(workspace_id, "ai_edit")
        raise
    problem = None
    if focus_widget is not None:
        broken = {b.widget_id: b.reason for b in svc.broken_widgets(doc, schema)}
        problem = broken.get(widget_id or "") or focus_widget.get("status_detail")
    args = None
    if summary is not None:
        args = await prompt.call_tool(
            system=prompt.SYSTEM,
            user=prompt.edit_message(
                summary, doc.widgets, instruction, target=focus_widget, problem=problem
            ),
            tool=prompt.OPS_TOOL,
            model=model,
        )
    if args is None:
        await gen.refund_daily(workspace_id, "ai_edit")
        raise model_unavailable()

    ops: list[tuple[int, dict[str, Any]]] = []  # (position, op) of ops needing no test
    tests: list[tuple[int, Any]] = []  # (position, widget dict) to gate + run
    pending: dict[int, dict[str, Any]] = {}  # position -> what to build once tested
    dropped: list[DroppedWidget] = []
    for pos, op in enumerate((args.get("ops") or [])[: prompt.MAX_OPS]):
        if not isinstance(op, dict):
            continue
        kind, wid = op.get("op"), op.get("widget_id")
        title = str((by_id.get(wid) or {}).get("title") or "Widget")
        if widget_id is not None and (kind == "add" or wid != widget_id):
            continue  # asked about one widget: leave the rest alone
        if kind in ("remove", "move", "update") and wid not in by_id:
            dropped.append(DroppedWidget(title=title, reason="That widget isn't on the dashboard."))
            continue
        if kind == "remove":
            ops.append((pos, {"op": "remove", "widget_id": wid}))
        elif kind == "move":
            lay = _layout(op.get("layout"))
            if lay is None:
                dropped.append(DroppedWidget(title=title, reason="The new position didn't fit."))
            else:
                ops.append((pos, {"op": "move", "widget_id": wid, "layout": lay}))
        elif kind == "add":
            raw = op.get("widget") if isinstance(op.get("widget"), dict) else {}
            tests.append((pos, raw))
            pending[pos] = {"op": "add", "layout": _layout(raw.get("layout"))}
        elif kind == "update":
            base = by_id[wid]
            patch = op.get("patch") if isinstance(op.get("patch"), dict) else {}
            patch = {k: v for k, v in patch.items() if k in _PATCHABLE}
            extra: dict[str, Any] = {}
            if "layout" in patch:
                lay = _layout(patch["layout"])
                if lay is not None:
                    extra["layout"] = lay
            if patch.get("refresh") in ("open", "interval"):
                extra["refresh"] = patch["refresh"]
            if isinstance(patch.get("interval_s"), int) and 60 <= patch["interval_s"] <= 86_400:
                extra["interval_s"] = patch["interval_s"]
            merged = {**_stored_fields(base), **{k: patch[k] for k in _FIELDS if k in patch}}
            if all(merged.get(k) == base.get(k) for k in _FIELDS):
                if extra:
                    ops.append((pos, {"op": "update", "widget_id": wid, "patch": extra}))
                continue
            tests.append((pos, merged))
            pending[pos] = {"op": "update", "widget_id": wid, "extra": extra}

    if tests:
        working, failed, bad = await gen.validate(cf, target.d1_id, schema, tests)
        dropped += bad
        if failed and summary is not None:
            fixed = await prompt.call_tool(
                system=prompt.SYSTEM,
                user=prompt.repair_message(summary, failed),
                tool=prompt.WIDGETS_TOOL,
                model=model,
            )
            by_index = {
                item["index"]: item
                for item in (fixed or {}).get("widgets") or []
                if isinstance(item, dict) and isinstance(item.get("index"), int)
            }
            retry = [(f["index"], by_index[f["index"]]) for f in failed if f["index"] in by_index]
            again, still, more = await gen.validate(cf, target.d1_id, schema, retry)
            working += again
            dropped += more
            failed = still + [f for f in failed if f["index"] not in by_index]
        dropped += [DroppedWidget(title=f["widget"]["title"], reason=f["error"]) for f in failed]
        adds: list[tuple[int, WidgetIn]] = []
        for pos, w in working:
            meta = pending[pos]
            if meta["op"] == "update":
                base = by_id[meta["widget_id"]]
                patch = _patch_of(base, w, meta["extra"])
                if patch:
                    ops.append(
                        (pos, {"op": "update", "widget_id": meta["widget_id"], "patch": patch})
                    )
            elif meta["layout"] is not None:
                laid = WidgetIn.model_validate({**w.model_dump(), "layout": meta["layout"]})
                ops.append((pos, {"op": "add", "widget": gen.op_widget(laid)}))
            else:
                adds.append((pos, w))
        if adds:
            placed = gen.place_below(doc.widgets, [w for _p, w in adds])
            slots = {(w.title, w.sql): w.layout for w in placed}
            for pos, w in adds:
                laid = w.model_copy(update={"layout": slots[(w.title, w.sql)]})
                ops.append((pos, {"op": "add", "widget": gen.op_widget(laid)}))

    ops.sort(key=lambda p: p[0])
    await svc._audit(
        target,
        user_id,
        "site_dashboard.ai_edit",
        str(doc.id),
        widget_id=widget_id,
        ops=len(ops),
        dropped=len(dropped),
    )
    return AiEditOut(ops=[op for _p, op in ops], dropped=dropped)

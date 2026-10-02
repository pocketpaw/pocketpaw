"""Move existing legacy concierges to the v2 runtime once the eval gate is open.

New concierges take their runtime from ``paw_bar.concierge_gate`` at create time.
Sites created before that keep ``Site.concierge_runtime == "legacy"`` forever unless
something moves them; this does, for the sites where nothing a visitor relies on
changes.

THE GATE DECIDES. Each run asks ``concierge_gate.default_concierge_runtime()``
first. Anything but "v2" (the deployment asked for legacy, or there is no passing
real-model report for the configured model) is a no-op that writes nothing.

WHAT IT MOVES. Site rows that have a concierge (``concierge_created_at`` set) on
the legacy runtime, whose bar is answered by the site's own dedicated agent
(``concierge-<site_id>``) in the site's workspace, with that agent untouched since
it was generated and no declared action beyond ``add_to_cart`` / ``checkout``. The
write sets ``concierge_runtime = "v2"`` and nothing else: the agent binding stays,
so the frame's starters and the agent-scoped knowledge v2 reads keep working, and
an owner can switch back from settings.

WHAT IT SKIPS (logged with the reason, re-checked every run, never moved):

  * a declared verb other than add_to_cart / checkout. Legacy exposes each one to
    the agent as a ``pawbar_<verb>`` tool that raises an Instinct proposal on the
    agent's say-so. v2 has no tools: such a verb can only appear as a form the
    visitor fills in, and one with no args cannot appear at all;
  * a bar bound to an agent that is not the site's dedicated one (an owner picked
    it by hand), or a dedicated agent the owner changed: system prompt, model,
    tools, scopes, skills, plugins, a rewritten persona, or disabled. v2 answers
    with the deployment's model and a fixed frame, so all of that would be lost;
  * no bar, no bound agent, or a bound agent that no longer exists. Those bars
    do not answer on legacy today, and moving one would switch it on unasked;
  * any error reading the site, which is left for the next run.

Connectors are not a reason to skip: a legacy concierge refuses every turn while
its pocket has one (``concierge_pocket_has_connectors``), so no live bar uses them.

IDEMPOTENT. Rows are selected on ``concierge_runtime`` still being legacy, and each
write repeats that guard, so a moved row never matches again and an owner's own
switch racing this run wins.

HOW IT RUNS. At boot, from ``shared.db.init_cloud_db`` (``migrate_on_boot``), after
the concierge-marker backfill, so a deployment that promotes a passing gate report
moves its sites on the next start. And by hand:

    python -m pocketpaw_ee.sites.migrate_concierge_v2 --dry-run
    python -m pocketpaw_ee.sites.migrate_concierge_v2

It lives in the package because the deployed image carries no ``scripts/``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Verbs v2 can still serve: a product card's buttons call the public action
# endpoint for them (``card_spec.HOST_EVENTS``).
_V2_VERBS = frozenset({"add_to_cart", "checkout"})

# Has a concierge, still on legacy (a row that never stored the field reads the
# model default, legacy; ``$in`` with None matches the missing field).
_LEGACY_CONCIERGE: dict[str, Any] = {
    "concierge_runtime": {"$in": ["legacy", None]},
    "concierge_created_at": {"$ne": None},
}


@dataclass
class Skipped:
    site_id: str
    reason: str


@dataclass
class MoveStats:
    """What one run did. ``gate_shut`` means it looked at nothing."""

    examined: int = 0
    moved: int = 0
    skipped: list[Skipped] = field(default_factory=list)
    gate_shut: bool = False


def _gate_runtime() -> str:
    from pocketpaw_ee.paw_bar.concierge_gate import default_concierge_runtime

    return default_concierge_runtime()


def _customised(agent: Any, site: dict[str, Any]) -> list[str]:
    """What the owner changed on a dedicated agent that v2 would not use."""
    from pocketpaw_ee.paw_bar.agent_provisioning import concierge_persona

    cfg = agent.config
    changed = [
        name
        for name in ("system_prompt", "model", "tools", "scopes", "skill_refs", "plugins")
        if getattr(cfg, name, None)
    ]
    site_name = str(site.get("name") or "")
    generated = {
        "",
        concierge_persona(site_name),
        concierge_persona(site_name, str(site.get("concierge_name") or "")),
    }
    if (getattr(cfg, "soul_persona", "") or "") not in generated:
        changed.append("persona")
    if getattr(agent, "disabled", False):
        changed.append("disabled")
    return changed


async def legacy_only_reason(site: dict[str, Any], store: Any) -> str | None:
    """Why this legacy concierge must stay on legacy, or None when v2 serves it."""
    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.paw_bar.agent_provisioning import concierge_slug

    pocket_id = str(site.get("pocket_id") or "")
    workspace_id = str(site.get("workspace") or "")
    if not pocket_id or not workspace_id:
        return "it has no bar"
    widgets = await store.list_widgets(pocket_id=pocket_id, workspace_id=workspace_id, limit=1)
    if not widgets:
        return "it has no bar"
    widget = widgets[0]

    actions = getattr(getattr(widget, "spec", None), "actions", None) or []
    server_verbs = [a.verb for a in actions if a.verb not in _V2_VERBS]
    if server_verbs:
        return "declares server-run actions: " + ", ".join(server_verbs)

    agent_id = str(getattr(widget, "agent_id", "") or "")
    if not agent_id:
        return "its bar has no agent"
    try:
        agent = await agents_service.get(agent_id)
    except NotFound:
        return "its bar's agent no longer exists"
    if agent.workspace_id != workspace_id or agent.slug != concierge_slug(str(site["_id"])):
        return f"bound to a hand-picked agent ({agent.slug})"
    changed = _customised(agent, site)
    if changed:
        return "its agent is customised: " + ", ".join(changed)
    return None


async def migrate_concierges_to_v2(*, store: Any, dry_run: bool = False) -> MoveStats:
    """Move every legacy concierge v2 can serve, when the gate is open.

    ``store`` is the paw-bar store to read bars from; production passes
    ``get_paw_bar_store()``. Requires Beanie to be initialised (Site, Agent).
    """
    from pocketpaw_ee.cloud.models.site import Site

    stats = MoveStats()
    if _gate_runtime() != "v2":
        stats.gate_shut = True
        return stats

    coll = Site.get_pymongo_collection()
    rows = await coll.find(
        _LEGACY_CONCIERGE,
        projection={"_id": 1, "workspace": 1, "pocket_id": 1, "name": 1, "concierge_name": 1},
    ).to_list(None)
    for site in rows:
        stats.examined += 1
        site_id = str(site["_id"])
        try:
            reason = await legacy_only_reason(site, store)
        except Exception:  # noqa: BLE001 — one unreadable row must not stop the rest
            logger.warning("concierge v2 move: could not read site %s", site_id, exc_info=True)
            reason = "could not be read; retried next run"
        if reason:
            stats.skipped.append(Skipped(site_id, reason))
            continue
        stats.moved += 1
        if not dry_run:
            await coll.update_one(
                {"_id": site["_id"], "concierge_runtime": {"$in": ["legacy", None]}},
                {"$set": {"concierge_runtime": "v2"}},
            )
    return stats


def _log(stats: MoveStats, dry_run: bool) -> None:
    if stats.gate_shut:
        logger.info("concierge v2 move: the v2 gate is shut; nothing to do")
        return
    logger.info(
        "concierge v2 move %s: %d legacy concierge(s) examined, %d %s, %d kept on legacy",
        "dry run" if dry_run else "complete",
        stats.examined,
        stats.moved,
        "would move" if dry_run else "moved to v2",
        len(stats.skipped),
    )
    for s in stats.skipped:
        logger.info("concierge v2 move: kept site %s on legacy: %s", s.site_id, s.reason)


async def migrate_on_boot() -> None:
    """Run the move at cloud startup. Best-effort: logs, never blocks boot.

    A failure leaves every site on the runtime it already had, which answers."""
    try:
        from pocketpaw_ee.api import get_paw_bar_store

        stats = await migrate_concierges_to_v2(store=get_paw_bar_store())
    except Exception:  # noqa: BLE001 — a migration hiccup must never block boot
        logger.error(
            "concierge v2 move failed at boot; sites keep their runtime. "
            "Run: python -m pocketpaw_ee.sites.migrate_concierge_v2 --dry-run",
            exc_info=True,
        )
        return
    if stats.examined:
        _log(stats, dry_run=False)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from pocketpaw_ee.cloud.credits.migrate_micro_credits import resolve_mongo_target

    try:
        uri, db_name = resolve_mongo_target(os.environ)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    from beanie import init_beanie
    from pymongo import AsyncMongoClient

    from pocketpaw_ee.api import get_paw_bar_store
    from pocketpaw_ee.cloud.models.agent import Agent
    from pocketpaw_ee.cloud.models.site import Site

    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        await init_beanie(database=client[db_name], document_models=[Site, Agent])
        stats = await migrate_concierges_to_v2(store=get_paw_bar_store(), dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — any driver error is the same answer here
        logger.error("concierge v2 move failed: %s", exc)
        return 2
    finally:
        client.close()

    _log(stats, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

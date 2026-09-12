# ee/pocketpaw_ee/paw_bar/purge.py — the Paw Bar surface's OWN entry point for
# destroying one site's concierge transcripts.
#
# Created 2026-09-12 (sites lifecycle wave 4, feat/sites-pause).
#
# WHY THIS FILE EXISTS AT ALL, rather than four lines inside the sites delete
# cascade. ``service.purge_site_records`` deliberately refused to purge these rows
# and wrote its reason down: they are ``ChatRunDoc`` rows keyed on the POCKET plus
# two SQLite tables, all of them shaped, written and read by this surface, and a
# second writer reaching in from another package is a contract nobody declared.
# That refusal was right about the seam and wrong about the outcome — the rows
# survived the site, so a visitor's free text outlived the tenant's reason to hold
# it. This module is the seam the refusal was waiting for: the sites package asks
# Paw Bar to forget a site, and Paw Bar decides what forgetting means.
#
# A TRANSCRIPT IS THREE STORES, and any purge that knows about fewer than all
# three leaves a readable remnant. ``router._load_transcript`` is the definition,
# and it merges:
#
#   1. ``ChatRunDoc`` (Mongo) — the visitor's line in ``user_text`` and the agent's
#      in ``partial_text``. Both are text a person can read back.
#   2. ``paw_bar_owner_messages`` (SQLite) — the lines with no run behind them: a
#      human on the team typing, the product explaining itself, and any VISITOR
#      message that arrived while the bot was muted. That last kind is the one
#      that makes skipping this table a data bug rather than an untidiness: it is
#      visitor free text, and it is the only copy.
#   3. ``paw_bar_conversations`` (SQLite) — the thread index, which carries the
#      visitor's contact email and the owner's notes and tags about them.
#
# WHAT IS DELIBERATELY LEFT ALONE:
#
#   * THE WIDGET. It survives a paused site and the next publish re-binds it.
#     ``store.delete_widget`` is the caller that removes one.
#   * THE CONCIERGE AGENT and its knowledge. Those belong to the workspace, not to
#     the transcript, and a site can be deleted while the agent answers elsewhere.
#   * ``paw_bar_events`` / ``paw_bar_decisions`` / ``paw_bar_carts``. None of them
#     hold conversation text. They are a real follow-up for a full widget teardown
#     and they are not what a transcript purge is for; widening this function to
#     mean "everything Paw Bar knows" would make its name a lie at exactly the
#     moment the name is the only thing the caller reads.
#
# FAILURE-SOFT IS NOT AN OPTION HERE. Every sibling read in this surface degrades
# to an empty list when a store hiccups, because a missing line renders as a
# shorter thread. A purge that degrades quietly reports a destroyed transcript
# over live personal data, so this raises and lets the cascade's ledger record the
# step as unfinished — which is what makes the retry resume it.

"""Purge one site's concierge transcripts, through the surface that owns them."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _concierge_context_type() -> str:
    """``"concierge"``, taken from the enum that DEFINES it rather than re-spelt.

    ``ScopeKind.CONCIERGE`` is the one authority for this string; the two other
    places that carry a literal copy (``router._CONCIERGE_CONTEXT_TYPE``, the
    metering sweeper's) are exactly the drift this avoids. Imported lazily because
    ``agent_service`` pulls in the whole chat stack, and the sites delete lane must
    not carry that import cost to purge two tables.
    """
    from pocketpaw_ee.cloud.chat.agent_service import ScopeKind

    return str(ScopeKind.CONCIERGE.value)


def _store() -> Any:
    from pocketpaw_ee.api import get_paw_bar_store

    return get_paw_bar_store()


async def purge_site_transcripts(*, workspace_id: str, pocket_id: str) -> dict[str, int]:
    """Forget every concierge conversation held for the site published at ``pocket_id``.

    Returns a per-store count so a caller can record "there was nothing here"
    separately from "I removed it" — the distinction the delete cascade's ledger is
    built on.

    THE THREE PREDICATES ON THE RUN QUERY ARE ALL LOAD-BEARING, and they are the
    same ones ``router._concierge_runs_for_visitor`` isolates a visitor with, minus
    ``user_id`` because this purge spans every visitor of one site:

      * ``workspace``    — tenant isolation. Another tenant's runs never match, and
                           the filter is IN the query so their rows are never read.
      * ``context_type`` — concierge runs only. An AUTHED pocket or session run on
                           the same pocket is a workspace member's own conversation
                           with their agent, not a visitor transcript, and deleting
                           it would destroy the owner's chat history because they
                           deleted a website.
      * ``scope_id``     — this site's own pocket. A sibling site never matches.

    ``pocket_id`` and not ``site_id``: these rows have never carried a site id. The
    concierge is bound to the POCKET, which is why the sites package could not write
    this query itself without guessing at a key it does not own.

    Raises whatever the stores raise. See the module header on why this one does not
    degrade to a shrug.
    """
    if not workspace_id or not pocket_id:
        # Not defensive padding: an empty workspace would make the run query
        # tenant-wide, and an empty pocket would make it match every concierge run
        # in that tenant. The two arguments ARE the scope.
        raise ValueError("purge_site_transcripts needs both a workspace and a pocket")

    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    result = await ChatRunDoc.find(
        {
            "workspace": workspace_id,
            "context_type": _concierge_context_type(),
            "scope_id": pocket_id,
        }
    ).delete()
    counts = {
        "runs": int(getattr(result, "deleted_count", 0) or 0),
        "owner_messages": 0,
        "conversations": 0,
    }

    store = _store()
    # Widgets are resolved workspace-scoped, and the per-widget purge is scoped
    # AGAIN on the same workspace. Two independent guards on the same fact, which
    # is the posture the rest of this surface takes on every owner-side write —
    # here it matters more, because the widget table's ``workspace_id`` is the
    # widget OWNER's while the conversation table's is the real tenant's.
    widgets = await store.list_widgets(pocket_id=pocket_id, workspace_id=workspace_id)
    for widget in widgets:
        removed = await store.purge_widget_threads(widget.id, workspace_id=workspace_id)
        counts["owner_messages"] += int(removed.get("owner_messages", 0) or 0)
        counts["conversations"] += int(removed.get("conversations", 0) or 0)

    logger.info(
        "paw_bar.purge: forgot %d runs, %d owner messages and %d conversations for pocket %s",
        counts["runs"],
        counts["owner_messages"],
        counts["conversations"],
        pocket_id,
    )
    return counts


__all__ = ["purge_site_transcripts"]

# purge.py — Delete ALL of a member's Phase B per-user data (the purge path).
# Created: 2026-06-08 — VIP Onboarding Phase B, chunk 7.
#
# Why this exists
# ---------------
# Phase B ingests a consented member's PERSONAL Gmail/calendar into their
# strictly-private ``user:{member_id}`` KB scope (member_ingest.service). When
# the member leaves — they disconnect their accounts, or an admin offboards
# them from the workspace — that personal data MUST be deleted. It's their
# mail; leaving must purge it. This module is the exact inverse of
# ``ingest_member``: same opaque member id, same derived scope, four deletes.
#
# What gets purged (all keyed on the opaque member id)
# ----------------------------------------------------
#   1. the ``user:{member_id}`` kb-go scope — their ingested mail/cal text,
#      wiped via the KEYLESS ``kb clear --scope user:{member_id}`` subprocess
#      (no LLM call, no API key — same keyless posture as the ingest accept).
#   2. their per-user OAuth tokens — ``token_store.delete`` for BOTH the
#      Gmail (``google_gmail``) and Calendar (``google_calendar``) services
#      under this member's bucket (chunk 1's per-user keying).
#   3. their per-user WorkspaceConnector rows — scope="user", pinned on BOTH
#      workspace AND user_id so a workspace-scoped row of the same connector
#      never gets swept.
#   4. their MemberIngestState sync-state doc — pinned on workspace + member_id.
#
# Per-member isolation (the load-bearing invariant)
# -------------------------------------------------
# The scope is DERIVED as ``user:{member_id}`` inside this function — there is
# no caller-supplied scope surface. The token deletes pass member_id as the
# bucket; the Beanie deletes pin workspace + member/user id. Purging member A
# therefore can NEVER touch member B's KB, tokens, connectors, or state — every
# delete target is a pure function of A's opaque id.
#
# Idempotent + best-effort
# ------------------------
# Safe to call twice and safe when nothing exists: token delete returns False
# when absent, ``kb clear`` on an empty scope is a no-op, and the Beanie
# deletes match zero rows. Each store is wrapped in its own try/except so one
# failing step (e.g. a missing kb binary) never blocks the others — the same
# best-effort cascade shape as workspace.service.remove_member. The returned
# summary records what was deleted and any per-store errors; status is
# ``error`` if any step failed, else ``ok``.

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import MemberDataPurged
from pocketpaw_ee.cloud.member_ingest.dto import PurgeMemberRequest

logger = logging.getLogger(__name__)

# Token store service names the per-user clients load under. GmailClient(
# user_id) -> service="google_gmail"; CalendarClient(user_id) ->
# service="google_calendar". These are the SERVICE keys in token_store, NOT
# the connector registry names ("gmail"/"gcalendar"); the purge must delete
# both buckets for the member.
_TOKEN_SERVICES = ("google_gmail", "google_calendar")

# kb_clear(scope) -> result dict. Async by contract. Injected in tests so the
# purge runs without a kb binary; defaults to the keyless ``kb clear`` wrapper.
KbClearFn = Callable[[str], Awaitable[dict[str, Any]]]


async def purge_member_data(
    workspace_id: str,
    member_id: str,
    *,
    kb_clear: KbClearFn | None = None,
    token_store: Any | None = None,
) -> dict[str, Any]:
    """Delete every Phase B per-user store for ``member_id`` in ``workspace_id``.

    Triggered on (a) a member disconnecting their accounts and (b) a member
    being offboarded from the workspace. Deletes the four stores listed in the
    module docstring, idempotently and best-effort.

    The KB scope is derived internally as ``user:{member_id}`` — the only scope
    this function ever clears, which is the per-member isolation guarantee.

    ``kb_clear`` defaults to the keyless ``kb clear`` subprocess wrapper;
    ``token_store`` defaults to a fresh ``TokenStore`` (the on-disk OAuth
    store). Both are injectable so the unit tests run with no kb binary and a
    tmp-rooted store.

    Returns a summary dict: ``status`` (``ok``/``error``), ``scope``,
    ``kb_cleared`` (bool), ``tokens_deleted`` (int), ``connectors_deleted``
    (int), ``ingest_state_deleted`` (bool), and any per-store ``errors``.
    """
    # Validate at entry (cloud rule §6) — internal callers (the offboard
    # cascade, the disconnect endpoint) get the same blank-id guard an HTTP
    # body would, so a blank member_id can never collapse the scope to a bare
    # ``user:`` and wipe the wrong member.
    body = PurgeMemberRequest.model_validate({"workspace_id": workspace_id, "member_id": member_id})
    workspace_id, member_id = body.workspace_id, body.member_id

    # The scope is a pure function of the opaque member id. Nothing else.
    scope = f"user:{member_id}"
    clear = kb_clear or _default_kb_clear
    if token_store is None:
        # Import here so a pure-unit test that injects a store doesn't pull the
        # OSS client stack in, and so the module stays import-light.
        from pocketpaw.clients.token_store import TokenStore

        token_store = TokenStore()

    errors: list[str] = []

    # --- 1. KB scope: clear the member's private mail/calendar articles. ---
    kb_cleared = False
    try:
        await clear(scope)
        kb_cleared = True
    except Exception as exc:  # noqa: BLE001 — isolate per store so one failing
        # step (e.g. a missing kb binary) never blocks the other three deletes.
        logger.warning("purge: kb clear failed for scope=%s ws=%s: %s", scope, workspace_id, exc)
        errors.append(f"kb: {exc}")

    # --- 2. Per-user OAuth tokens: Gmail + Calendar buckets for this member. ---
    tokens_deleted = 0
    for service in _TOKEN_SERVICES:
        try:
            # token_store.delete returns True only when a file was removed, so
            # the count is the number of real tokens this member had — and a
            # second purge naturally counts zero (idempotent).
            if token_store.delete(service, member_id):
                tokens_deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "purge: token delete failed for service=%s member=%s: %s",
                service,
                member_id,
                exc,
            )
            errors.append(f"token[{service}]: {exc}")

    # --- 3. Per-user WorkspaceConnector rows for this member. ---
    connectors_deleted = 0
    try:
        connectors_deleted = await _delete_user_connectors(workspace_id, member_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "purge: connector-row delete failed for member=%s ws=%s: %s",
            member_id,
            workspace_id,
            exc,
        )
        errors.append(f"connectors: {exc}")

    # --- 4. MemberIngestState sync-state doc for this member. ---
    ingest_state_deleted = False
    try:
        ingest_state_deleted = await _delete_ingest_state(workspace_id, member_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "purge: ingest-state delete failed for member=%s ws=%s: %s",
            member_id,
            workspace_id,
            exc,
        )
        errors.append(f"ingest_state: {exc}")

    status = "error" if errors else "ok"

    # Emit on write (cloud rule §9) — downstream consumers (soul memory, the
    # member's home surface, search index) drop anything keyed on this scope.
    await emit(
        MemberDataPurged(
            data={
                "workspace_id": workspace_id,
                "member_id": member_id,
                "scope": scope,
                "status": status,
                "kb_cleared": kb_cleared,
                "tokens_deleted": tokens_deleted,
                "connectors_deleted": connectors_deleted,
                "ingest_state_deleted": ingest_state_deleted,
            }
        )
    )

    logger.info(
        "purge: member=%s ws=%s status=%s kb=%s tokens=%d connectors=%d state=%s",
        member_id,
        workspace_id,
        status,
        kb_cleared,
        tokens_deleted,
        connectors_deleted,
        ingest_state_deleted,
    )

    return {
        "workspace_id": workspace_id,
        "member_id": member_id,
        "scope": scope,
        "status": status,
        "kb_cleared": kb_cleared,
        "tokens_deleted": tokens_deleted,
        "connectors_deleted": connectors_deleted,
        "ingest_state_deleted": ingest_state_deleted,
        "errors": errors,
    }


# --------------------------------------------------------------------------
# Beanie deletes — both pinned on the tenant + the opaque member id.
# --------------------------------------------------------------------------


async def _delete_user_connectors(workspace_id: str, member_id: str) -> int:
    """Delete the member's per-user connector rows, returning the count.

    Pins workspace AND user_id AND scope="user" (cloud rule §7 tenant filter):
    a workspace-scoped row of the same connector name (``user_id=None``) is
    never swept, and another member's rows are never touched. WorkspaceConnector
    is owned by connectors/service.py; we read its doc directly here for the
    same reason member_ingest.service.list_connected_members does — the
    connector service exposes no user-scoped delete helper yet (flagged for a
    future ``connectors.service.delete_user_connectors`` extraction).
    """
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

    rows = await WorkspaceConnector.find(
        WorkspaceConnector.workspace == workspace_id,  # tenant filter
        WorkspaceConnector.scope == "user",
        WorkspaceConnector.user_id == member_id,
    ).to_list()
    count = 0
    for row in rows:
        await row.delete()
        count += 1
    return count


async def _delete_ingest_state(workspace_id: str, member_id: str) -> bool:
    """Delete the member's ingest-state doc; True when a row was removed.

    Pinned on workspace + member_id (cloud rule §7) — the same composite key
    member_ingest.service._load_or_create_state writes, so two members never
    share state and no cross-tenant row is touched.
    """
    from pocketpaw_ee.cloud.models.member_ingest_state import MemberIngestState

    state = await MemberIngestState.find_one(
        MemberIngestState.workspace == workspace_id,
        MemberIngestState.member_id == member_id,
    )
    if state is None:
        return False
    await state.delete()
    return True


# --------------------------------------------------------------------------
# The keyless ``kb clear`` subprocess wrapper (mirrors _default_kb_accept).
# --------------------------------------------------------------------------


async def _default_kb_clear(scope: str) -> dict[str, Any]:
    """Wipe a kb-go scope via ``kb clear --scope <scope>`` (no LLM, no key).

    kb-go's ``clear`` removes every article in a scope and rebuilds the index
    with no Anthropic call — usable on this keyless cloud backend, the same
    posture as the ingest accept path. ``KnowledgeService.clear`` hard-codes
    ``agent:{id}``; this wrapper passes the member's ``user:`` scope verbatim.
    """
    import subprocess

    from pocketpaw_ee.cloud.agents.knowledge import KB_BIN

    def _run() -> dict[str, Any]:
        try:
            result = subprocess.run(
                [KB_BIN, "clear", "--scope", scope, "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"kb binary not found at {KB_BIN!r}. Install kb-go or set POCKETPAW_KB_BIN."
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(f"kb clear failed: {result.stderr[:200]}")
        try:
            import json

            return json.loads(result.stdout)
        except Exception:  # noqa: BLE001
            return {"raw": result.stdout.strip()}

    return await asyncio.to_thread(_run)


__all__ = ["purge_member_data"]

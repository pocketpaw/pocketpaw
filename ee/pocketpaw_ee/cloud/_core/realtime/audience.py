# audience.py — resolves an Event into the user_ids that should receive it.
# Updated: 2026-09-04 — the member cache is now an LRU with single-flight.
#   It was a plain dict whose 2-second TTL was only a freshness check on read,
#   so nothing was ever removed and one entry per group/workspace/user ever
#   seen was retained for the process lifetime. Concurrent misses for the same
#   key also each issued their own query; since this resolver runs on every
#   workspace-scoped event, that fan-out is the normal case, not the rare one.
# Updated: 2026-08-15 (HTN-5) — agent.plan_updated joins the agent runtime
#   stream branch, scoped to the group's members like agent.tool_use.
# Updated: 2026-08-11 — call.participant_joined / call.participant_left now
#   resolve to the call's group members (they previously fell through to [],
#   so InProcessBus skipped fan-out and the frontend's pre-join "who's in the
#   call" chip went stale). Removed an unreachable duplicate call.started /
#   call.ended branch that sat below the live one.
"""Resolves an Event into the list of user_ids that should receive it."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from pocketpaw_ee.cloud._core.realtime.events import Event

MemberFetcher = Callable[[str], Awaitable[list[str]]]

#: Cache ceiling. Sized so a large deployment's active groups and workspaces
#: fit comfortably — eviction is a backstop against unbounded growth, not a
#: working constraint. Entries cost a short list of ids.
_DEFAULT_CACHE_MAX = 10_000


class AudienceResolver:
    """One branch per event type. Caches group/workspace member lookups briefly."""

    def __init__(
        self,
        *,
        group_members: MemberFetcher | None = None,
        workspace_members: MemberFetcher | None = None,
        workspace_admins: MemberFetcher | None = None,
        workspace_peers: MemberFetcher | None = None,
        cache_ttl_seconds: float = 2.0,
        cache_max_entries: int = _DEFAULT_CACHE_MAX,
    ) -> None:
        self._group_members = group_members
        self._workspace_members = workspace_members
        self._workspace_admins = workspace_admins
        self._workspace_peers = workspace_peers
        self._ttl = cache_ttl_seconds
        self._max = cache_max_entries
        # LRU, not a plain dict. The TTL is only a freshness check on read —
        # it never removed anything, so one entry per group/workspace/user
        # ever seen was retained for the life of the process.
        self._cache: OrderedDict[tuple[str, str], tuple[float, list[str]]] = OrderedDict()
        # Single-flight. Without it, N concurrent events for the same group
        # all miss together and issue N identical queries; this resolver runs
        # on every workspace-scoped event, so that fan-out is the common case
        # rather than the rare one.
        self._inflight: dict[tuple[str, str], asyncio.Future[list[str]]] = {}

    def invalidate_group(self, group_id: str) -> None:
        self._cache.pop(("group", group_id), None)

    def invalidate_workspace(self, workspace_id: str) -> None:
        # Peer caches are user-scoped (keyed by user_id, not workspace_id) and are
        # handled by invalidate_user_peers or the short TTL — do not try to pop
        # them here.
        self._cache.pop(("workspace", workspace_id), None)
        self._cache.pop(("workspace_admins", workspace_id), None)

    def invalidate_user_peers(self, user_id: str) -> None:
        self._cache.pop(("workspace_peers", user_id), None)

    async def _cached(self, kind: str, key: str, fn: MemberFetcher | None) -> list[str]:
        if fn is None:
            return []
        ck = (kind, key)
        now = time.monotonic()

        entry = self._cache.get(ck)
        if entry and now - entry[0] < self._ttl:
            self._cache.move_to_end(ck)
            return list(entry[1])

        # Single-flight: the first caller to miss owns the fetch and everyone
        # else awaits the same task. Nothing is awaited between the lookup and
        # the insert, so no second caller can interleave and start a duplicate.
        # A task rather than a bare future because a failure then propagates to
        # every waiter by itself, and the exception is retrieved by whoever
        # awaits — no "exception was never retrieved" warning to hand-manage.
        pending = self._inflight.get(ck)
        if pending is not None:
            return list(await pending)

        # ensure_future, not create_task: MemberFetcher is typed as returning
        # an Awaitable, and a caller is free to hand back a future rather than
        # a coroutine. create_task rejects the former.
        task = asyncio.ensure_future(fn(key))
        self._inflight[ck] = task
        try:
            value = await task
        finally:
            # Popped on failure too, so one transient error does not pin a
            # permanently-failing task in front of every later caller.
            self._inflight.pop(ck, None)

        self._cache[ck] = (now, value)
        self._cache.move_to_end(ck)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return list(value)

    async def _group(self, gid: str) -> list[str]:
        return await self._cached("group", gid, self._group_members)

    async def _workspace(self, wid: str) -> list[str]:
        return await self._cached("workspace", wid, self._workspace_members)

    async def _admins(self, wid: str) -> list[str]:
        return await self._cached("workspace_admins", wid, self._workspace_admins)

    async def _peers(self, uid: str) -> list[str]:
        return await self._cached("workspace_peers", uid, self._workspace_peers)

    async def audience(self, event: Event) -> list[str]:  # noqa: C901
        t = event.type
        d = event.data

        # --- Groups -------------------------------------------------------------
        if t == "group.created":
            # Public channels and public groups should be visible to every
            # workspace member so the new channel appears in their channel
            # browser / sidebar without a manual refresh. Private channels,
            # private groups, and DMs are restricted to the explicit member
            # list (the creator + any invited members).
            gtype = d.get("type", "")
            vis = d.get("visibility", "public")
            is_public = gtype == "public" or (gtype == "channel" and vis != "private")
            if is_public and (wid := d.get("workspace")):
                return await self._workspace(wid)
            return list(d.get("member_ids", []))
        if t in {
            "group.updated",
            "group.deleted",
            "group.member_added",
            "group.member_role",
            "group.agent_added",
            "group.agent_removed",
            "group.agent_updated",
            "group.pinned",
            "group.unpinned",
        }:
            members = await self._group(d["group_id"])
            # member_added: include the new user if present
            if t == "group.member_added" and (uid := d.get("user_id")):
                return list({*members, uid})
            return members
        if t == "group.member_removed":
            members = await self._group(d["group_id"])
            return list({*members, d["user_id"]})
        if t == "group.joined":
            # Scoped hydration event: audience is exactly the new user(s)
            # carried in ``member_ids``. Existing members already have the
            # room and receive ``group.member_added`` instead.
            return list(d.get("member_ids", []))
        if t == "group.unread_delta":
            return [d["user_id"]]

        # --- Messages -----------------------------------------------------------
        if t == "message.new":
            # Fan out to EVERY group member, sender included. The originating
            # socket already rendered its optimistic row and dedups this against
            # it (the frontend swaps the local-{ts} id for the persisted id and
            # drops the redundant echo). Excluding the sender by user_id was a
            # cross-device sync bug: the same account on phone + desktop shares
            # one user_id, so the second device was also treated as "the sender"
            # and never received the persisted message — its timeline went stale
            # until a manual reload.
            return await self._group(d["group_id"])
        if t in {
            "message.edited",
            "message.deleted",
            "message.reaction.added",
            "message.reaction.removed",
            "message.reaction",
            "message.read",
        }:
            return await self._group(d["group_id"])
        if t == "message.sent":
            return [d["sender_id"]]
        if t in {"thread.reply", "thread.created", "thread.closed"}:
            return await self._group(d.get("group_id", ""))
        if t == "message.ui_state.updated":
            # Group-context messages fan out to every group member so a peer
            # viewing the same room sees the kanban update live. Pocket /
            # session-context messages are single-owner — the event carries
            # ``user_id`` and routes only to that user's other tabs.
            if gid := d.get("group_id"):
                return await self._group(gid)
            if uid := d.get("user_id"):
                return [uid]
            return []

        # --- Workspace ----------------------------------------------------------
        if t in {"workspace.updated", "workspace.deleted", "workspace.member_role"}:
            return await self._workspace(d["workspace_id"])
        if t == "workspace.member_added":
            members = await self._workspace(d["workspace_id"])
            if uid := d.get("user_id"):
                return list({*members, uid})
            return members
        if t == "workspace.member_removed":
            members = await self._workspace(d["workspace_id"])
            return list({*members, d["user_id"]})
        if t in {
            "workspace.invite.created",
            "workspace.invite.accepted",
            "workspace.invite.revoked",
        }:
            admins = await self._admins(d["workspace_id"])
            if uid := d.get("user_id"):
                return list({*admins, uid})
            return admins

        # --- Sessions -----------------------------------------------------------
        if t in {"session.created", "session.updated", "session.deleted"}:
            user_id = d.get("user_id")
            peer_id = d.get("peer_id")
            if user_id and peer_id:
                return list({user_id, peer_id})
            if user_id:
                return [user_id]
            return []

        # --- Files --------------------------------------------------------------
        if t in {"file.ready", "file.deleted"}:
            # Chat-scoped uploads broadcast to the chat group's members so the
            # timeline updates live. Workspace-only uploads (no chat_id) don't
            # have a chat audience — local subscribers (e.g. the KB indexer)
            # still fire via the bus's in-process handlers.
            gid = d.get("group_id")
            if not gid:
                return []
            return await self._group(gid)

        # --- Agent --------------------------------------------------------------
        if t in {
            "agent.thinking",
            "agent.tool_start",
            "agent.tool_result",
            "agent.error",
            "agent.stream_chunk",
            "agent.stream_end",
            "agent.stream_start",
            "agent.tool_use",
            # HTN-5: the plan panel is chat furniture, so it is scoped exactly
            # like the tool chips it replaces — group members, never a broadcast.
            "agent.plan_updated",
        }:
            return await self._group(d["group_id"])

        # --- Agent CRUD (workspace-scoped configs) ------------------------------
        # An Agent doc lives in a workspace; visibility is private/workspace/public
        # but the sidebar is per-workspace, so fan out to every workspace member.
        # Public agents would ideally cross workspaces, but the realtime fan-out
        # is bounded by workspace anyway (cross-tenant sockets aren't joined).
        if t in {"agent.created", "agent.updated", "agent.deleted", "agent.scope_updated"}:
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Pockets ------------------------------------------------------------
        # Audience is computed by the service (it's the only layer that knows
        # the pocket's visibility + shared_with) and shipped on the event:
        #   - ``recipient_ids``: explicit list, used for private pockets
        #   - ``workspace_id``: present for workspace-visible pockets;
        #     fanned out to every workspace member
        # ``pocket.deleted`` always carries ``recipient_ids`` (the service
        # captures it before the doc is dropped).
        if t in {"pocket.created", "pocket.updated", "pocket.deleted"}:
            recipients = list(d.get("recipient_ids") or [])
            if wid := d.get("workspace_id"):
                recipients.extend(await self._workspace(wid))
            return list(set(recipients))

        # --- Calls (LiveKit group call lifecycle) -------------------------------
        # The room is per-group, so audience is every group member: the call
        # panel needs to light up for the receiver and clear for everyone on
        # end. Notes posting also fans out so peer tabs can scroll to the
        # newly-created meeting-notes message without a manual refetch.
        # participant_joined / participant_left keep the pre-join roster chip
        # ("who's already in the call") honest for members who haven't joined
        # yet. The joiner is NOT excluded: their own tab needs the roster too,
        # and ``identity`` may be a guest id minted by livekit/invites.py
        # rather than a workspace user_id, so it is not an audience key.
        if t in {
            "call.started",
            "call.ended",
            "call.notes_posted",
            "call.participant_joined",
            "call.participant_left",
        }:
            if gid := d.get("group_id"):
                return await self._group(gid)
            return []

        # --- Connectors (workspace-scoped adapter rows) -------------------------
        if t in {
            "connector.enabled",
            "connector.disabled",
            "connector.config_updated",
            "connector.sync_recorded",
        }:
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Notifications ------------------------------------------------------
        if t in {"notification.new", "notification.read", "notification.cleared"}:
            return [d["user_id"]]

        # --- Unread (per-user counter delta) ------------------------------------
        if t == "unread.update":
            return [d["user_id"]]

        # --- Tasks (Mission Control work items) ---------------------------------
        # Payload carries explicit ``recipient_ids`` (creator + human assignee);
        # fan out to those plus every workspace member so any operator with the
        # Mission Control board open sees the row update live.
        if t in {
            "task.proposed",
            "task.updated",
            "task.claimed",
            "task.resolved",
            "task.blocked",
        }:
            recipients = list(d.get("recipient_ids") or [])
            if wid := d.get("workspace_id"):
                recipients.extend(await self._workspace(wid))
            return list(set(recipients))

        # --- Cycles (Mission Control time-boxed windows) ------------------------
        if t in {"cycle.created", "cycle.updated", "cycle.closed", "cycle.snapshotted"}:
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Projects (Linear-style scoping primitive) --------------------------
        if t in {
            "project.created",
            "project.updated",
            "project.archived",
            "project.deleted",
        }:
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Planner (PRD materialization + gap resolution) ---------------------
        if t in {"plan.generated", "plan.gap_resolved"}:
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Pocket outcomes (named business events from write actions) ---------
        # Workspace-scoped: the outcomes ledger dashboard watches every write.
        if t == "pocket.outcome":
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Belt & Pulley station runs (develop-station lifecycle) -------------
        # Workspace-scoped: the /belt console is a per-workspace view, and a run
        # status change (propose / approve / reject / landed / failed) fires
        # asynchronously relative to the chat turn, so it must fan out to every
        # workspace member with the page open — not just the proposing session.
        if t == "belt_run_updated":
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Belt mandates (shift plan proposals) --------------------------------
        # Workspace-scoped, same rationale as belt_run_updated: a plan lands at
        # the gate asynchronously and must reach every member with the mandates
        # page open. The UI subscribes to the ``belt_plan`` topic and reads
        # {mandate_id, proposal} off the payload.
        if t == "belt_plan":
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Composio (per-user OAuth identity probes) --------------------------
        if t in {"composio.connection.verified", "composio.connection.mismatch"}:
            if uid := d.get("user_id"):
                return [uid]
            return []

        # --- Presence -----------------------------------------------------------
        if t in {"presence.online", "presence.offline"}:
            return await self._peers(d["user_id"])

        # --- Meetings ----------------------------------------------------------
        if t in {"meeting.scheduled", "meeting.updated", "meeting.cancelled", "meeting.started"}:
            # Fan out to all group members so they see meeting schedule/updates
            gid = d.get("group_id")
            if not gid:
                return []
            return await self._group(gid)

        # --- Sites (site lifecycle: draft created, then published) ---------------
        # Workspace-scoped: the /sites gallery is a per-workspace view, and BOTH
        # halves of the lifecycle land asynchronously relative to the chat turn that
        # started them (the agent creates, then publishes, server-side). Fan out to
        # every workspace member so an open gallery gains the new card
        # (``site.created``) and flips it to Live (``site.published``) on its own —
        # no poll, no manual refresh. The per-run ``pocket_created`` SSE cannot do
        # this job: it reaches only the tab that owns that chat stream, so a create
        # from a second tab / a teammate / an import is invisible without the bus.
        # Payloads carry {workspace_id, site_id, pocket_id, owner, ...}; the client
        # keys the gallery row off site_id / pocket_id.
        if t in {"site.created", "site.published"}:
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # --- Workspace jobs (durable ARQ job lifecycle) -------------------------
        # Workspace-scoped: a dynamic site's provisioning runs as a durable job
        # that completes well after the publish, in the ARQ worker. Fan out the
        # queued/terminal update to every workspace member so the gallery/builder
        # can advance provisioning -> live without polling job status. Payload
        # carries {job_id, workspace_id, pocket_id, action, job_name, status}.
        if t in {"workspace_job.queued", "workspace_job.updated"}:
            if wid := d.get("workspace_id"):
                return await self._workspace(wid)
            return []

        # Room-scoped events (typing.*) are routed by the ConnectionManager directly,
        # not via this resolver. Falling through returns [] and the bus will no-op.
        return []

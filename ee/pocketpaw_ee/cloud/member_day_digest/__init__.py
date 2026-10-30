# member_day_digest — The structured "your day" digest service (Phase B chunk 5).
# Created: 2026-06-08 — VIP Onboarding Phase B (the synthesized "your day").
"""A reusable, per-member structured digest of the member's day.

Pieces
------
* ``service.member_day_digest(workspace_id, member_id)`` — read one member's
  upcoming calendar (next ~7 days) + recent/unread mail (count + top items)
  LIVE with THEIR per-user Gmail/Calendar token (chunk 1), and return a
  structured ``MemberDayDigest``. Per-member isolation is structural: the
  per-user clients are keyed on ``member_id`` (a different token bucket per
  member), and the returned ``member_id`` is the argument echoed back — there
  is no caller-supplied member-id surface.
* ``dto.MemberDayDigest`` (+ ``DigestEvent`` / ``DigestMail``) — the reusable
  structured shape. The agent briefing renders it to a capped string; chunk
  6's intent board reads the same shape.

Why LIVE (not the chunk-4 KB ingest)
------------------------------------
The chunk-4 ingest feeds the agent's DEEP retrieval over the private
``user:{id}`` KB scope. This digest is the COMPLEMENTARY structured "what's on
today" — pulled live so the briefing is current to the minute, not as of the
last ingest tick. They are two faces of the same per-user data.

Consumers
---------
* The agent "your day" briefing — ``chat.agent_service._member_briefing_block``
  renders this digest down to a concise capped block, GATED to the member's
  OWN solo session (the same ``members == [user_id]`` rule as the private
  ``user:`` KB scope).
* (next) chunk 6's intent board reads the structured shape directly.

Follow-ups (v1 ships a solid, bounded slice)
--------------------------------------------
* A status/read router (``GET /api/v1/member-day-digest``) over the
  structured shape for operator visibility / a UI card.
* Per-session caching so a chatty session doesn't re-pull every turn (today
  the briefing path pulls once per system-message build; the gate already
  short-circuits in shared rooms so no wasted I/O there).
"""

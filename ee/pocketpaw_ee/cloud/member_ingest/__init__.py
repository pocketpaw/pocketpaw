# member_ingest — Per-user Gmail/Calendar → private-KB ingest worker.
# Created: 2026-06-08 — VIP Onboarding Phase B (per-user ingest worker).
"""Per-member ingest of recent mail + upcoming calendar into the member's
strictly-private ``user:{member_id}`` KB scope.

Pieces
------
* ``service.ingest_member`` — read one member's Gmail/Calendar with THEIR
  per-user token (chunk 1) and write the text into ``user:{member_id}``
  (chunk 2) via the KEYLESS ``kb accept`` path (chunk 0). Backfill on first
  run, incremental thereafter. Per-member isolation is structural: the scope
  is a pure function of ``member_id``.
* ``service.list_connected_members`` — enumerate (workspace, member) pairs
  with a per-user Gmail/Calendar connector enabled.
* ``service.run_ingest_sweep`` — fan out ``ingest_member`` across all
  connected members under a bounded concurrency cap; per-member failures are
  isolated.
* ``scheduler.MemberIngestScheduler`` — the periodic background sweep (every
  5 min, same shape as the ChatRunDoc sweeper), gated on
  ``POCKETPAW_CLOUD_SCHEDULER_ENABLED`` and wired into ``mount_cloud``.
* ``models.member_ingest_state.MemberIngestState`` — per-member sync status
  (last_sync_at, status, backfill_done, per-source cursors).

Follow-ups (v1 deliberately ships a solid-but-bounded slice — see the chunk
report's CONCERNS):
* True Gmail incremental via ``historyId`` cursors + per-message dedup
  (today: a ``newer_than:Nd`` window + accept's title-slug upsert; safe but
  re-reads the overlap window each tick).
* Full message-body extraction (today: metadata + snippet for mail; the
  GmailClient.read full-body path is available but not yet wired into the
  bulk loop to keep the backfill cheap).
* A first-connect trigger that kicks an immediate backfill on the
  ``connector.enabled`` event instead of waiting for the next sweep tick.
* A status/read router (``GET /api/v1/member-ingest/status``) over
  ``MemberIngestStatusResponse`` for operator visibility.
"""

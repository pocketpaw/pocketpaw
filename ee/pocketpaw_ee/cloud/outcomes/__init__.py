# __init__.py — Pocket outcomes entity package marker.
# Created: 2026-05-22 (RFC 05 M2b.2) — the minimal outcome meter. A pocket
#   write action whose binding declares a named `outcome` emits a
#   `pocket.outcome` event after the write succeeds. This entity's bus
#   subscriber appends each event to a workspace-scoped JSONL ledger;
#   `GET /api/v1/outcomes` reads the ledger back as a grouped count.
#
#   Layer 4 (billing — assigning a monetary `outcome_value`/`outcome_unit`)
#   is reserved: the event carries both fields as `null` and this entity
#   never sets them. The meter exists so an operator can SEE how many
#   business outcomes a pocket produced before any pricing is wired.

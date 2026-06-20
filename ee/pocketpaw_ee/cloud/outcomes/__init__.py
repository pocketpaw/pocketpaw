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
#
# Updated: 2026-06-11 (gap-3 outcome VALUE metering) — Layer 4 is now
#   partially wired: `outcome_value` / `outcome_unit` carry the author-time
#   billable pair declared on the `ActionBinding`, persisted on the ledger
#   row, and `GET /api/v1/outcomes/meter` aggregates them into a billable
#   figure per workspace per period (count + total value by unit). This is
#   the "pay for governed outcomes" READ primitive. Still DEFERRED:
#   invoicing, payment, currency, a pricing-rules engine, and
#   disputes/clawback — turning the queryable figure into money.

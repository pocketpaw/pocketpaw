# __init__.py — Decision-graph entity package marker.
# Created: 2026-05-25 (RFC 07 Slice 1) — the substrate that folds an
#   N-event subgraph of the org journal (`agent.proposed`, `human.corrected`,
#   `policy.evaluated`, `decision.completed`, `decision.outcome_attached`)
#   into one queryable Decision row + edge rows. Materialized in
#   `~/.soul/decisions.db` (SQLite, WAL); queried via the in-process
#   `DecisionGraph` Python API. Slice 1 ships the substrate (projection +
#   store + Python API + smoke router). REST endpoints + narrator land in
#   Slice 2 / Slice 3. See RFC 07 for the load-bearing contract.

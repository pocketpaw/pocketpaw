# herdr_cockpit — read-only cockpit telemetry over the flagged HerdrRuntime.
#
# Created: 2026-07-24 (feat/herdr-cockpit-sse, HR-10a) — the contract-defining
# backend half of HR-10: a live SSE stream of herdr pane status ("dots") plus an
# on-demand pane-preview endpoint. Both routes are ADMIN-gated and fail open when
# herdr is disabled or absent (see router.py for the tenancy rationale). No pane
# mutation lives here — this module is read-only by design.

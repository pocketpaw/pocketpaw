# pocketpaw_ee/paw_bar/ — enterprise HTTP surface for Paw Bar ingest.
# Updated: 2026-07-08 — Renamed widget "Paw Print" → "Paw Bar" (dir paw_print→paw_bar).
#   The separate one-word audit feed (past-tense record) is a DIFFERENT feature, untouched.
#
# The Paw Bar logic (store, models) moved to pocketpaw.paw_bar in the
# OSS-EE split (Phase 2). What remains here is the FastAPI router, which
# depends on the pocketpaw_ee.api store factories and is mounted by the
# cloud app. Import the logic from pocketpaw.paw_bar; import the router
# from pocketpaw_ee.paw_bar.router.

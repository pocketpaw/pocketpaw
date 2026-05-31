# pocketpaw_ee/paw_print/ — enterprise HTTP surface for Paw Print ingest.
#
# The Paw Print logic (store, models) moved to pocketpaw.paw_print in the
# OSS-EE split (Phase 2). What remains here is the FastAPI router, which
# depends on the pocketpaw_ee.api store factories and is mounted by the
# cloud app. Import the logic from pocketpaw.paw_print; import the router
# from pocketpaw_ee.paw_print.router.

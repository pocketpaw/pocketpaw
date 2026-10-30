# pocketpaw_ee/instinct/ — enterprise HTTP surface for the Instinct pipeline.
#
# The Instinct logic (store, models, correction, trace, trace_collector)
# moved to pocketpaw.instinct in the OSS-EE split (Phase 2). What remains
# here is the FastAPI router, which gates access behind enterprise license
# / plan / RBAC checks (pocketpaw_ee.cloud.*) and the pocketpaw_ee.api store
# factories. Import the logic from pocketpaw.instinct; import the router
# from pocketpaw_ee.instinct.router.

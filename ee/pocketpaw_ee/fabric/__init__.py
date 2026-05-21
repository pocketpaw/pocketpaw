# pocketpaw_ee/fabric/ — enterprise HTTP surface for the Fabric ontology.
#
# The Fabric logic (store, models, policy, projection, events) moved to
# pocketpaw.fabric in the OSS-EE split (Phase 2). What remains here is the
# FastAPI router, which gates access behind enterprise license / plan /
# RBAC checks (pocketpaw_ee.cloud.*). Import the logic from pocketpaw.fabric;
# import the router from pocketpaw_ee.fabric.router.

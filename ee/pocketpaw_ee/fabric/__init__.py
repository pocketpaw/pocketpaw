# pocketpaw_ee/fabric/ — enterprise HTTP surface for the Fabric ontology.
#
# Created: 2026-03-28 — first introduced as the FastAPI router gating
# Fabric API access behind enterprise license / plan / RBAC checks.
# Updated: 2026-05-28 (feat/wave-4c-fabric-registry) — adds the
# concrete ``WorkspaceFabricRegistry`` (RFC 03 v2 PR 2g promised seam)
# and its workspace-scoped SQLite write-side
# (``WorkspaceFabricStore``). The pair satisfies the
# ``pocketpaw.bundled_templates.FabricRegistry`` Protocol so EE callers
# can lint / resolve ``tier: registered`` templates against real
# workspace data instead of the Wave 4b JSON mock.
#
# The Fabric *object* layer (live entity instances, links, projection,
# events) still lives in ``pocketpaw.fabric`` from the Phase 2 OSS-EE
# split. What lives here is intentionally smaller: the FastAPI router
# (HTTP gating) and the workspace-scoped *registry* contract (entity
# type / property / link declarations consumed by the lint + runtime
# resolver paths). The two layers are independent on purpose — Wave 4b
# lint must not require booting the full async object store.

from pocketpaw_ee.fabric.registry import WorkspaceFabricRegistry
from pocketpaw_ee.fabric.storage import DEFAULT_DB_PATH, WorkspaceFabricStore

__all__ = [
    "DEFAULT_DB_PATH",
    "WorkspaceFabricRegistry",
    "WorkspaceFabricStore",
]

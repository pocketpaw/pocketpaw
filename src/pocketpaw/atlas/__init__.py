# atlas/__init__.py — package exports for the atlas primitive (AT-1).
# Updated: 2026-07-02 (feat/atlas-fabric, AT-7) — export the live Fabric
# introspection seam (FabricIntrospector protocol, the EE-wired
# build_workspace_fabric_introspector hook, and RegistryFabricIntrospector)
# so EE wiring and tests can bind a workspace's live ontology to the atlas
# tools; OSS installs get None from the builder (fail-closed).
# Updated: 2026-07-02 (feat/atlas-overlay, AT-5) — export the live overlay
# (AtlasOverlay / OverlaidEntry), the EntitlementProvider protocol, and the
# OSS DefaultEntitlementProvider next to the store, so consumers can render
# context-aware (workspace-scoped, fail-closed) atlas answers.
# Updated: 2026-07-02 (feat/atlas-compiler, AT-4) — export the compiler
# (compile_atlas / write_artifact / check_artifact) and the startup
# connector-drift check next to the store.
# Created: 2026-07-02 (feat/atlas-core) — atlas is the runtime OS
# self-model: a hand-authored capability map the product's runtime agents
# (chat agent, pocket specialist) query to learn what the OS itself is
# and can do, instead of guessing from LLM priors.

from pocketpaw.atlas.compile import check_artifact, compile_atlas, write_artifact
from pocketpaw.atlas.fabric import (
    FabricIntrospector,
    RegistryFabricIntrospector,
    build_workspace_fabric_introspector,
)
from pocketpaw.atlas.model import AtlasEntry, AtlasModel
from pocketpaw.atlas.overlay import (
    AtlasOverlay,
    DefaultEntitlementProvider,
    EntitlementProvider,
    OverlaidEntry,
)
from pocketpaw.atlas.store import AtlasStore, check_connector_drift, get_atlas_store

__all__ = [
    "AtlasEntry",
    "AtlasModel",
    "AtlasOverlay",
    "AtlasStore",
    "DefaultEntitlementProvider",
    "EntitlementProvider",
    "FabricIntrospector",
    "OverlaidEntry",
    "RegistryFabricIntrospector",
    "build_workspace_fabric_introspector",
    "check_artifact",
    "check_connector_drift",
    "compile_atlas",
    "get_atlas_store",
    "write_artifact",
]

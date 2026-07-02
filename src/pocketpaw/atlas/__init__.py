# atlas/__init__.py — package exports for the atlas primitive (AT-1).
# Updated: 2026-07-02 (feat/atlas-compiler, AT-4) — export the compiler
# (compile_atlas / write_artifact / check_artifact) and the startup
# connector-drift check next to the store.
# Created: 2026-07-02 (feat/atlas-core) — atlas is the runtime OS
# self-model: a hand-authored capability map the product's runtime agents
# (chat agent, pocket specialist) query to learn what the OS itself is
# and can do, instead of guessing from LLM priors.

from pocketpaw.atlas.compile import check_artifact, compile_atlas, write_artifact
from pocketpaw.atlas.model import AtlasEntry, AtlasModel
from pocketpaw.atlas.store import AtlasStore, check_connector_drift, get_atlas_store

__all__ = [
    "AtlasEntry",
    "AtlasModel",
    "AtlasStore",
    "check_artifact",
    "check_connector_drift",
    "compile_atlas",
    "get_atlas_store",
    "write_artifact",
]

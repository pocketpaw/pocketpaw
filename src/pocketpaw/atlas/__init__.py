# atlas/__init__.py — package exports for the atlas primitive (AT-1).
# Created: 2026-07-02 (feat/atlas-core) — atlas is the runtime OS
# self-model: a hand-authored capability map the product's runtime agents
# (chat agent, pocket specialist) query to learn what the OS itself is
# and can do, instead of guessing from LLM priors.

from pocketpaw.atlas.model import AtlasEntry, AtlasModel
from pocketpaw.atlas.store import AtlasStore, get_atlas_store

__all__ = [
    "AtlasEntry",
    "AtlasModel",
    "AtlasStore",
    "get_atlas_store",
]

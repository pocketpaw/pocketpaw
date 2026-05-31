"""Soul Protocol integration for PocketPaw.

Public interface for soul-protocol consumers. Implementation lives in
private modules (`_manager`, `_cognitive`, `_bridge`); callers should
only import from `pocketpaw.soul` so internal restructuring stays free.

See `soul/README.md` for an overview of the module's role and lifecycle.

2026-05-08: Public surface introduced as part of #1073 (deep-module
consolidation). Previously, callers reached into `soul.manager`,
`soul.cognitive`, and `paw.soul_bridge` directly.
"""

from pocketpaw.soul._bridge import SoulBootstrapProvider, SoulBridge
from pocketpaw.soul._cognitive import PocketPawCognitiveEngine
from pocketpaw.soul._manager import (
    SoulManager,
    get_soul_manager,
    set_soul_manager,
)

__all__ = [
    "PocketPawCognitiveEngine",
    "SoulBootstrapProvider",
    "SoulBridge",
    "SoulManager",
    "get_soul_manager",
    "set_soul_manager",
]

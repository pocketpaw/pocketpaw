#!/usr/bin/env python3
"""OSS-EE boundary runtime check (OSS-EE split, Phase 3b).

Simulates an OSS-only install by making ``pocketpaw_ee`` unimportable, then
asserts that every core ``pocketpaw`` module touched by the split imports
cleanly and the extension registry degrades to an empty result.

This complements the ``OSS core may not import from EE`` import-linter
contract: the linter catches *static* ``from pocketpaw_ee import ...`` lines,
this catches anything that would only blow up at import time (and proves the
registry's per-provider error handling actually holds when EE is absent).

Exits non-zero on failure so CI fails loudly.
"""

from __future__ import annotations

import importlib
import sys

# Block pocketpaw_ee before importing anything else — any `import pocketpaw_ee`
# (or submodule import) now raises ImportError, exactly as on an OSS install
# with no EE package on disk.
sys.modules["pocketpaw_ee"] = None  # type: ignore[assignment]

# Core modules reworked by the OSS-EE split. None may import pocketpaw_ee at
# module load time — they reach EE only through the entry-point registry.
CORE_MODULES = [
    "pocketpaw",
    "pocketpaw.extensions",
    "pocketpaw._registry",
    "pocketpaw.dashboard_state",
    "pocketpaw.agents.errors",
    "pocketpaw.agents.pool",
    "pocketpaw.agents.loop",
    "pocketpaw.agents.claude_sdk",
    "pocketpaw.agents.codex_cli",
    "pocketpaw.agents.tool_bridge",
    "pocketpaw.agents.sdk_mcp_widgets",
]

# Extension-point groups whose only implementations live in pocketpaw_ee.
EE_ONLY_GROUPS = [
    "pocketpaw.mcp_servers",
    "pocketpaw.models",
    "pocketpaw.pockets",
    "pocketpaw.agent_extensions",
]


def main() -> int:
    for name in CORE_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {name} does not import without pocketpaw_ee: {exc!r}")
            return 1
    print(f"OK: {len(CORE_MODULES)} core modules import with pocketpaw_ee blocked")

    from pocketpaw._registry import providers

    for group in EE_ONLY_GROUPS:
        resolved = providers(group)
        if resolved != ():
            print(f"FAIL: registry group {group!r} resolved {resolved!r} with EE blocked")
            return 1
    print(f"OK: {len(EE_ONLY_GROUPS)} EE-only registry groups degrade to empty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

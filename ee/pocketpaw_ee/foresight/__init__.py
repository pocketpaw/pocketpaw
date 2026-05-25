# ee/pocketpaw_ee/foresight/__init__.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
# Foresight module — the "rehearse the future" engine for the Paw IS.
#
# This v0.1 PR establishes the module skeleton + a minimal end-to-end
# loop (Decision Forecast sub-type, 5 personas, 1 tick). It is the
# first of several PRs that will land the full engine described in
# RFC 08; see docs/internal/2026-05-foresight.md for the cut.
#
# Public surface (v0.1):
#   - ForesightWorld   — Fabric-backed world stub (world.py)
#   - SoulSeededPersona — soul-seeded persona stub (persona.py)
#   - ClaudeCodeBackend — CC SDK ↔ CAMEL BaseModelBackend adapter (llm/adapter.py)
#   - run_scenario     — single-scenario smoke entrypoint (scenarios/runner.py)
#
# All four are intentionally minimal at v0.1 — protocol-shaped, not
# subclass-shaped, so they run without the OASIS src-copy on disk.
# v1.0 wires the vendored substrate; v2.0 scales to 100K personas.

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "ForesightWorld",
    "SoulSeededPersona",
    "ClaudeCodeBackend",
    "run_scenario",
    "ScenarioConfig",
    "RunResult",
    "__version__",
]


def __getattr__(name: str):  # pragma: no cover — lazy import shim
    """Lazy re-export so ``import pocketpaw_ee.foresight`` doesn't pay
    the full import cost when callers only need the version. CAMEL is a
    heavy import (50+ backend adapters); we don't want a top-level
    foresight import to drag it in if a caller just wants ``__version__``.
    """
    if name == "ForesightWorld":
        from pocketpaw_ee.foresight.world import ForesightWorld

        return ForesightWorld
    if name == "SoulSeededPersona":
        from pocketpaw_ee.foresight.persona import SoulSeededPersona

        return SoulSeededPersona
    if name == "ClaudeCodeBackend":
        from pocketpaw_ee.foresight.llm.adapter import ClaudeCodeBackend

        return ClaudeCodeBackend
    if name in {"run_scenario", "ScenarioConfig", "RunResult"}:
        from pocketpaw_ee.foresight.scenarios.runner import (
            RunResult,
            ScenarioConfig,
            run_scenario,
        )

        return {
            "run_scenario": run_scenario,
            "ScenarioConfig": ScenarioConfig,
            "RunResult": RunResult,
        }[name]
    raise AttributeError(f"module 'pocketpaw_ee.foresight' has no attribute {name!r}")

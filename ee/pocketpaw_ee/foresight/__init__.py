# ee/pocketpaw_ee/foresight/__init__.py
# Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — PR 2:
#   - Bumped __version__ to 0.2.0 to reflect the OASIS substrate
#     vendoring + adapter expansion + PawAgent wrapping.
#   - Added LiteLLMFallbackBackend to the lazy-export surface (stub at
#     v0.2; PR 3 wires the real proxy).
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
# Foresight module — the "rehearse the future" engine for the Paw IS.
#
# PR 1 (v0.1) shipped the module skeleton + minimal end-to-end loop
# (Decision Forecast sub-type, 5 personas, 1 tick).
# PR 2 (v0.2 — this commit) lands:
#   - The vendored OASIS fork at substrate/oasis/ (upstream SHA 46cdc8d).
#   - ClaudeCodeBackend.run(messages, ...) — CAMEL BaseModelBackend surface.
#   - SoulSeededPersona(paw_agent=...) — RFC §7.2 fidelity floor.
#   - LiteLLMFallbackBackend stub.
# See docs/internal/2026-05-foresight.md for the full cut.
#
# Public surface (v0.2):
#   - ForesightWorld   — Fabric-backed world stub (world.py)
#   - SoulSeededPersona — soul-seeded persona (now PawAgent-aware) (persona.py)
#   - ClaudeCodeBackend — CC SDK ↔ CAMEL BaseModelBackend adapter (llm/adapter.py)
#   - LiteLLMFallbackBackend — fallback proxy stub (llm/adapter.py)
#   - run_scenario     — single-scenario smoke entrypoint (scenarios/runner.py)
#
# The engine surfaces remain protocol-shaped at v0.2 — the OASIS
# substrate is vendored but PR 3 is the one that wires it into the
# tick loop. v1.0 fully wires the vendored substrate; v2.0 scales to
# 100K personas.

from __future__ import annotations

__version__ = "0.2.0"

__all__ = [
    "ForesightWorld",
    "SoulSeededPersona",
    "ClaudeCodeBackend",
    "LiteLLMFallbackBackend",
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
    if name in {"ClaudeCodeBackend", "LiteLLMFallbackBackend"}:
        from pocketpaw_ee.foresight.llm.adapter import (
            ClaudeCodeBackend,
            LiteLLMFallbackBackend,
        )

        return {
            "ClaudeCodeBackend": ClaudeCodeBackend,
            "LiteLLMFallbackBackend": LiteLLMFallbackBackend,
        }[name]
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

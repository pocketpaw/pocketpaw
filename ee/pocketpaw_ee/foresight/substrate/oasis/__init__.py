# ee/pocketpaw_ee/foresight/substrate/oasis/__init__.py
# Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — vendored fork
# of camel-ai/oasis at upstream SHA 46cdc8d.
#
# This module wraps the vendored OASIS package with a safe top-level
# init so ``import pocketpaw_ee.foresight.substrate.oasis`` always
# succeeds — even on machines without camel-ai installed. The upstream
# top-level re-exports live in ``_upstream_init.py`` (preserved verbatim
# except for absolute-import path rewrites from ``oasis.*`` to
# ``pocketpaw_ee.foresight.substrate.oasis.*``); we attempt them on
# import and fall through to a namespace-only package when CAMEL is
# missing. PR 3 wires the substrate into ForesightWorld; until then,
# v0.1's engine surfaces (World, Persona, Backend) are protocol-shaped
# and do NOT import from this package, so missing-CAMEL machines remain
# fully operational.

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Mirror the upstream OASIS package version so downstream code that
# checks ``oasis.__version__`` (or our own audit tooling) finds it.
__version__ = "0.2.5"

# Track what we tried to load. ``__all__`` and the bound names are only
# populated if the upstream re-exports succeed. PR 3's wiring code
# should branch on ``OASIS_AVAILABLE`` rather than catching ImportError
# at every call site.
OASIS_AVAILABLE: bool = False
OASIS_LOAD_ERROR: Exception | None = None

try:
    # The upstream init pulls in oasis.environment, oasis.social_agent,
    # oasis.social_platform — each of which transitively imports CAMEL.
    # On an OSS-only install (``uv sync --dev`` without ``--group ee``),
    # camel-ai is not installed and this import will fail. That is OK —
    # the foresight engine's v0.1 protocol surfaces don't depend on
    # this package being loaded.
    from pocketpaw_ee.foresight.substrate.oasis._upstream_init import (  # noqa: F401
        ActionType,
        AgentGraph,
        DefaultPlatformType,
        LLMAction,
        ManualAction,
        Platform,
        SocialAgent,
        UserInfo,
        generate_reddit_agent_graph,
        generate_twitter_agent_graph,
        make,
        print_db_contents,
    )

    OASIS_AVAILABLE = True
    __all__ = [
        "ActionType",
        "AgentGraph",
        "DefaultPlatformType",
        "LLMAction",
        "ManualAction",
        "OASIS_AVAILABLE",
        "Platform",
        "SocialAgent",
        "UserInfo",
        "__version__",
        "generate_reddit_agent_graph",
        "generate_twitter_agent_graph",
        "make",
        "print_db_contents",
    ]
except Exception as exc:  # noqa: BLE001 — broad on purpose; any import failure means no symbols
    OASIS_LOAD_ERROR = exc
    logger.debug(
        "OASIS substrate is vendored but not loaded (likely missing camel-ai dep). "
        "Module is importable as a namespace package; symbols unavailable. "
        "Underlying error: %s: %s",
        type(exc).__name__,
        exc,
    )
    __all__ = ["OASIS_AVAILABLE", "OASIS_LOAD_ERROR", "__version__"]

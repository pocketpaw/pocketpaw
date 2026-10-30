# ee/pocketpaw_ee/foresight/substrate/oasis/__init__.py
# Updated: 2026-05-25 (feat/foresight-v03-calibration) — PR 3:
#   - Switched to a tiered import strategy so OASIS's torch-dependent
#     recsys (Twitter Platform / TWHIN-BERT / process_recsys_posts)
#     stays lazy. The bits PR 3+ actually uses — SocialAgent, AgentGraph,
#     UserInfo, ActionType, Channel — DO NOT need torch and load
#     cleanly. RFC 08 §6.2 explicitly drops the SQLite/recsys-backed
#     Platform; setting up the OASIS_AVAILABLE branch on the lightweight
#     core lets ForesightWorld wire through SocialAgent without
#     dragging torch into every dev shell. The heavy upstream surface
#     (Platform, make, print_db_contents, generate_*_agent_graph,
#     LLMAction, ManualAction) is still importable on machines that
#     happen to have torch — guarded by OASIS_RECSYS_AVAILABLE.
# Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — vendored fork
# of camel-ai/oasis at upstream SHA 46cdc8d.
#
# This module wraps the vendored OASIS package with a safe top-level
# init so ``import pocketpaw_ee.foresight.substrate.oasis`` always
# succeeds — even on machines without camel-ai installed. The upstream
# top-level re-exports live in ``_upstream_init.py`` (preserved verbatim
# except for absolute-import path rewrites). PR 3 splits the surface
# in two:
#   - the *core* (SocialAgent / AgentGraph / UserInfo / ActionType /
#     Channel) — needs CAMEL but NOT torch; this is what the Foresight
#     engine actually wires through.
#   - the *recsys* tier (Platform / make / generate_*_agent_graph /
#     LLMAction / ManualAction / print_db_contents) — needs torch
#     because oasis.social_platform.platform pulls recsys.py.

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
OASIS_RECSYS_AVAILABLE: bool = False
OASIS_RECSYS_LOAD_ERROR: Exception | None = None

# Names that get bound at module level when their tier loads cleanly.
# Declared up-front for type checkers; reassigned in the try blocks.
ActionType = None  # type: ignore[assignment]
AgentGraph = None  # type: ignore[assignment]
Channel = None  # type: ignore[assignment]
SocialAgent = None  # type: ignore[assignment]
UserInfo = None  # type: ignore[assignment]

# Recsys-tier symbols (only bound when torch is installed).
DefaultPlatformType = None  # type: ignore[assignment]
LLMAction = None  # type: ignore[assignment]
ManualAction = None  # type: ignore[assignment]
Platform = None  # type: ignore[assignment]
generate_reddit_agent_graph = None  # type: ignore[assignment]
generate_twitter_agent_graph = None  # type: ignore[assignment]
make = None  # type: ignore[assignment]
print_db_contents = None  # type: ignore[assignment]

try:
    # The CORE OASIS surface — SocialAgent / AgentGraph / Channel /
    # UserInfo / ActionType. These do NOT touch torch; they only
    # require camel-ai (the BaseModelBackend protocol + Channel
    # primitives). This is the surface RFC 08 §6.2 says we KEEP.
    from pocketpaw_ee.foresight.substrate.oasis.social_agent.agent import SocialAgent  # noqa: F811
    from pocketpaw_ee.foresight.substrate.oasis.social_agent.agent_graph import (  # noqa: F811
        AgentGraph,
    )
    from pocketpaw_ee.foresight.substrate.oasis.social_platform.channel import Channel  # noqa: F811
    from pocketpaw_ee.foresight.substrate.oasis.social_platform.config import UserInfo  # noqa: F811
    from pocketpaw_ee.foresight.substrate.oasis.social_platform.typing import (  # noqa: F811
        ActionType,
    )

    OASIS_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 — broad on purpose
    OASIS_LOAD_ERROR = exc
    logger.debug(
        "OASIS core substrate not loaded (likely missing camel-ai dep). "
        "Module is importable as a namespace package; symbols unavailable. "
        "Underlying error: %s: %s",
        type(exc).__name__,
        exc,
    )

# The RECSYS-tier OASIS surface — Platform / make / generate_*_agent_graph
# / LLMAction / ManualAction / print_db_contents. These all transitively
# import ``oasis.social_platform.recsys`` which depends on torch. Per
# RFC 08 §6.2 we explicitly DROP Platform (replace with Fabric-backed
# ForesightWorld), so the recsys tier is only useful for upstream-
# compat smoke tests and the v2.0 Market Sim sub-type (which may
# revisit recsys). On machines without torch this branch is skipped.
try:
    from pocketpaw_ee.foresight.substrate.oasis._upstream_init import (  # noqa: F401, F811
        DefaultPlatformType,
        LLMAction,
        ManualAction,
        Platform,
        generate_reddit_agent_graph,
        generate_twitter_agent_graph,
        make,
        print_db_contents,
    )

    OASIS_RECSYS_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 — broad on purpose
    OASIS_RECSYS_LOAD_ERROR = exc
    logger.debug(
        "OASIS recsys tier not loaded (likely missing torch). "
        "Foresight only needs the recsys tier for Market Sim sub-type "
        "(v2.0). Core OASIS symbols (SocialAgent, AgentGraph, Channel) "
        "remain available. Underlying error: %s: %s",
        type(exc).__name__,
        exc,
    )

__all__ = [
    "ActionType",
    "AgentGraph",
    "Channel",
    "DefaultPlatformType",
    "LLMAction",
    "ManualAction",
    "OASIS_AVAILABLE",
    "OASIS_LOAD_ERROR",
    "OASIS_RECSYS_AVAILABLE",
    "OASIS_RECSYS_LOAD_ERROR",
    "Platform",
    "SocialAgent",
    "UserInfo",
    "__version__",
    "generate_reddit_agent_graph",
    "generate_twitter_agent_graph",
    "make",
    "print_db_contents",
]

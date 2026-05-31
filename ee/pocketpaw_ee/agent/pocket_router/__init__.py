# __init__.py — Pocket execution router package.
# Created: 2026-05-22 (Increment 3) — the classified router that front-runs
#   ``pocket_specialist__edit`` and routes a request to the cheapest
#   capable tier (Tier 0 declarative / Tier 1 deterministic op / Tier 2
#   specialist) instead of always invoking the LLM specialist.
#
#   * classifier.py — pure, rule-based ``classify(intent, ripple_spec)``
#   * router.py     — ``classify_and_route`` dispatch + observability
#   * events.py     — the ``pocket_execution`` SSE frame schema
"""Pocket execution router — classify an edit, route to the cheapest tier."""

from __future__ import annotations

from pocketpaw_ee.agent.pocket_router.classifier import Classification, classify
from pocketpaw_ee.agent.pocket_router.events import PocketExecutionFrame
from pocketpaw_ee.agent.pocket_router.router import classify_and_route

__all__ = [
    "Classification",
    "PocketExecutionFrame",
    "classify",
    "classify_and_route",
]

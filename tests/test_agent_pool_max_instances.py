# tests/test_agent_pool_max_instances.py
# Created: 2026-09-01 (feat/scale-concurrency-knobs) — pins that the AgentPool
# instance ceiling is reachable from config.
#
# Why this file exists: ``max_instances`` was a hardcoded constructor default
# (20) and ``get_agent_pool()`` built the singleton with NO arguments, so the
# only way to raise it on a deploy serving many users was to edit pool.py. The
# risk in fixing that is the opposite mistake — resolving config inside
# ``AgentPool.__init__`` would drag ambient settings into the ~8 places that
# construct ``AgentPool()`` directly in tests. So the contract is split, and
# both halves are pinned here:
#   * ``get_agent_pool()`` — the ONE instance the app runs on — reads settings.
#   * ``AgentPool(...)`` direct construction does NOT; explicit args still win.

from __future__ import annotations

from pocketpaw.agents import pool as pool_mod
from pocketpaw.agents.pool import AgentPool


def test_get_agent_pool_reads_max_instances_from_settings(monkeypatch):
    monkeypatch.setenv("POCKETPAW_AGENT_POOL_MAX_INSTANCES", "50")
    monkeypatch.setattr(pool_mod, "_pool", None)

    pool = pool_mod.get_agent_pool()
    try:
        assert pool._max_instances == 50
    finally:
        pool_mod._pool = None


def test_get_agent_pool_default_matches_the_constructor(monkeypatch):
    """Shipping the knob must not move any existing deploy: with no env set the
    singleton lands on the same 20 the constructor has always used."""
    monkeypatch.delenv("POCKETPAW_AGENT_POOL_MAX_INSTANCES", raising=False)
    monkeypatch.setattr(pool_mod, "_pool", None)

    pool = pool_mod.get_agent_pool()
    try:
        assert pool._max_instances == AgentPool()._max_instances == 20
    finally:
        pool_mod._pool = None


def test_direct_construction_ignores_settings(monkeypatch):
    """Explicit args beat the environment — this is what keeps the direct
    ``AgentPool()`` construction used across the pool test suite independent of
    ambient config."""
    monkeypatch.setenv("POCKETPAW_AGENT_POOL_MAX_INSTANCES", "99")
    assert AgentPool(max_instances=4)._max_instances == 4


def test_singleton_is_still_cached(monkeypatch):
    """The settings read must happen once, on first build — not per call."""
    monkeypatch.setattr(pool_mod, "_pool", None)
    try:
        assert pool_mod.get_agent_pool() is pool_mod.get_agent_pool()
    finally:
        pool_mod._pool = None

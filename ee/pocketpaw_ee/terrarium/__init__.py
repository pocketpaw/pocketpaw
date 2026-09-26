# ee/pocketpaw_ee/terrarium/__init__.py
#
# TERRARIUM — a watchable agent civilization. A UNIVERSE is a world with a
# PHYSICS FILE (its genome), a Journal of Events (the truth), and CITIZENS that
# are Souls with a credit balance. Citizens tick: they sense, recall, make ONE
# LLM call, act through a fixed verb set, and pay for every act. Viewers watch,
# speak (as an outside voice, never as fact), and pledge tokens for WEATHER.
#
# Package layout (4-file entity rule + the mandates module's extras):
#   physics.py  — the PhysicsFile schema + YAML loader + hard validation
#   domain.py   — Beanie docs (service.py is the SOLE importer) + frozen views
#   world.py    — the PURE engine: state + decision in, events out. No DB, no
#                 soul, no bus — so most of the rules are testable without Mongo.
#   weather.py  — PURE world events. Structurally cannot touch a soul.
#   llm.py      — the citizen judgment seat (claude CLI | deterministic mock)
#   soul_link.py— best-effort Soul bridge; a soul failure never wedges a tick
#   service.py  — the DB / soul / bus glue and the only Beanie importer
#   executor.py — the Instinct approve-side hook for ``world_spawn``
#   router.py   — private (RBAC) + public (anonymous, fail-closed) routers
#
# This module is deliberately import-light at package level: importing the
# package must not pull the router (→ auth → deps), the same discipline the
# mandates package follows for its lazy Beanie registration.

"""Terrarium — the agent-civilization runtime."""

from __future__ import annotations

__all__: list[str] = []

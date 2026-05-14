# ee/cloud/mission_control/__init__.py
# Created: 2026-05-13 (feat/mission-control-facade) — workspace-aware façade
# entity that composes Instinct pending actions / Pawprints + the in-process
# activity buffer into the unified WorkItem shape the paw-enterprise Mission
# Control UI consumes. PR 1 of the three-PR Mission Control series — Tasks
# (PR 2) and Cycles (PR 3) plug into the same façade in follow-ups.
"""Mission Control façade package.

Re-exports the router so ``mount_cloud`` can import without reaching into
``ee.cloud.mission_control.router`` explicitly. The 4-file shape inside
mirrors ``ee/cloud/pockets/`` (the canonical reference per CLAUDE.md).
"""

from ee.cloud.mission_control.router import router

__all__ = ["router"]

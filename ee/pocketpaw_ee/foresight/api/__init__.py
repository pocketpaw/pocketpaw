# ee/pocketpaw_ee/foresight/api/__init__.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
# Foresight API surface — v0.1 ships a minimal in-memory router so the
# REST contract is fixed before Mongo wiring lands. The router itself
# is mounted from ee/pocketpaw_ee/cloud/foresight/router.py (cloud's
# mount_cloud picks it up). This module just exposes the runtime
# in-memory run store so tests can introspect it without touching the
# cloud package.

from __future__ import annotations

from pocketpaw_ee.foresight.api.run_store import RunStore, get_run_store

__all__ = ["RunStore", "get_run_store"]

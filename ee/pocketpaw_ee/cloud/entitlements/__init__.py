# ee/pocketpaw_ee/cloud/entitlements/__init__.py — Entitlements entity package
# marker (BC-6, the Entitlement primitive).
#
# The entitlements RESOLVER maps a workspace -> what it's entitled to (its plan,
# the plan's feature set, and its monthly credit allotment) by reading the
# workspace's CURRENT ``Workspace.plan`` and looking it up in the billing plan
# catalog (``ee.cloud.billing.plans``). It is the read/declarative layer BC-7
# (subscriptions) and BC-9 (per-site) build on. No event projection here —
# entitlements derive from the existing ``Workspace.plan`` field.
#
# Plain marker — the router is imported explicitly by
# ``ee.cloud.__init__:mount_cloud``, not eagerly here, so importing this package
# never pulls FastAPI / Beanie.
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.

"""Entitlements — resolve a workspace to its plan, features, and allotment."""

from __future__ import annotations

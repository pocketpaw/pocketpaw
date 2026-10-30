# ee/pocketpaw_ee/cloud/metering/__init__.py — Metering entity package marker.
# Created 2026-06-24 (integration/billing-credits, BC-3): the compute-cost meter
# + rate card (the Meter + Price primitives). Every completed / terminal chat run
# is billed by its real compute cost times a flat markup, converted to integer
# credits and debited to the workspace wallet EXACTLY ONCE via a durable sweeper.
# The 4-file entity shape (domain / dto / service / sweeper) lives here. Plain
# marker — nothing is imported eagerly so importing this package never pulls
# FastAPI / Beanie.

"""Compute-cost metering — bills each terminal chat run's compute cost to the
workspace wallet exactly once, surviving crashes via a durable sweeper."""

from __future__ import annotations

# ee/pocketpaw_ee/cloud/credits/__init__.py — Credits entity package marker.
# Created 2026-06-23 (integration/billing-credits, BC-1): the credit ledger +
# atomic balance (the Ledger primitive). The 4-file entity shape (domain / dto /
# service / router) lives here. Plain marker — the router is imported explicitly
# by ``ee.cloud.__init__:mount_cloud``, not eagerly here, so importing this
# package never pulls FastAPI / Beanie.

"""Credit ledger — workspace-scoped wallet with grant / debit / query."""

from __future__ import annotations

# ee/pocketpaw_ee/cloud/billing/__init__.py — Billing entity package marker
# (BC-2, the Gateway primitive). Plain marker — the routers are imported
# explicitly by ``ee.cloud.__init__:mount_cloud``, not eagerly here, so importing
# this package never pulls FastAPI / Beanie / the dodopayments SDK.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): a user buys credits.
# ``service.create_topup`` returns a Dodo HOSTED-CHECKOUT url; a verified
# ``payment.succeeded`` webhook grants credits EXACTLY ONCE via BC-1's
# idempotent ``credits.grant``. Dodo is the only gateway in v1, behind the
# ``providers`` abstraction (Razorpay et al. land later).

"""Billing — Dodo one-time top-up + verified idempotent webhook (Gateway primitive)."""

from __future__ import annotations

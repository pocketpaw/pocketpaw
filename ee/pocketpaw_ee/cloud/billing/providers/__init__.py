# ee/pocketpaw_ee/cloud/billing/providers/__init__.py — payment-provider
# abstraction package marker (BC-2, the Gateway primitive).
#
# ``base`` defines the ``IPaymentsProvider`` ABC — the ONLY surface the billing
# service depends on. ``dodo`` is the single v1 implementation. A later gateway
# (Razorpay) is a new module here plus a one-line factory swap; nothing in the
# service / webhook / router layer changes.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new package.

from __future__ import annotations

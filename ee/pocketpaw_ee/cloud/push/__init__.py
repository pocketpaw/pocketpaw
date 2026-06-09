# Web Push entity (pocketpaw#1391) — stores browser Web Push subscriptions
# and serves the per-workspace VAPID public key. Storage + key only; sending
# notifications (pywebpush dispatch + 410 pruning) is #1392, wiring real
# events is #1393.
#
# Follows the ee/cloud 4-file shape: domain.py (pure value objects),
# dto.py (request/response wire models), service.py (sole Beanie writer),
# router.py (thin HTTP boundary).

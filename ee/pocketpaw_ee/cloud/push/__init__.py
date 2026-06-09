# Web Push entity (pocketpaw#1391) — stores browser Web Push subscriptions
# and serves the per-workspace VAPID public key. Storage + key (#1391),
# pywebpush dispatch + 410 pruning (#1392), and product-event wiring with
# WS-vs-Web-Push dedupe (#1393) all live here now.
#
# Follows the ee/cloud 4-file shape: domain.py (pure value objects),
# dto.py (request/response wire models), service.py (sole Beanie writer),
# router.py (thin HTTP boundary). Two extra modules carry the #1393 wiring:
# dispatch.py (``notify`` — the WS/Web-Push transport fork that never
# double-notifies a user with both the desktop app and a browser tab open)
# and listeners.py (the v1 product events — agent.stream_end /
# instinct.approval.created / meeting.started — subscribed to the realtime
# bus and routed through ``notify``). Neither writes Beanie directly, so the
# "Push — Beanie writes only from service.py" contract still holds.

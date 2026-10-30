# Cloud discover entity — the public Discover index.
#
# Created 2026-10-01 (feat/discover-index). One thin catalogue any product can
# publish into: ``sources.py`` is the registry of products (site templates first),
# ``listeners.py`` keeps listings in sync with source events, ``service.py`` holds
# the signed-in actions (use, report) and ``service_admin.py`` the public,
# cross-tenant reads plus sync and moderation writes.

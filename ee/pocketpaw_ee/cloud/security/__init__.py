# ee/pocketpaw_ee/cloud/security/__init__.py — the shield control-plane proxy.
# Created: 2026-07-01 (feat/sec-5-security-proxy, SEC-5).
#
# Home of the cloud-side proxy to shield — the same-box Go security daemon that
# serves a control API on a UNIX socket. The router (``router.py``) maps 1:1 to
# shield's endpoints (decisions / stats / config read + PATCH, decision
# resolve), OWNER-gates every route via ``require_action_any_workspace(
# "security.manage")``, forwards shield's status + JSON through, and degrades
# cleanly when shield is absent. The transport / config lives in ``config.py``.

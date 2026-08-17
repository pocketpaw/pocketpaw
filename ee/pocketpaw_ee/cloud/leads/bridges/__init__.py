# ee/pocketpaw_ee/cloud/leads/bridges/__init__.py — cross-domain wiring that
# keeps the rest of the platform lead-aware.
#
# Created 2026-08-06 (feat/coupling-lead-captured, T-6): mirrors
# ``meetings/bridges`` — the leads domain emits ``lead.captured`` and knows
# nothing about who listens; each bridge here subscribes and translates.
#
# * ``notifications.py`` — ``lead.captured`` → an in-app notification for the
#   workspace's owner/admins, deep-linked to the site's Leads view.

"""Bridges that turn external event streams into in-app notifications.

Created 2026-08-06 (feat/coupling-alerts-to-bell) — houses the OSS
operational-alert bridge (``alerts.py``). Mirrors the house bridge shape
used by ``meetings/bridges/``; this package lives under ``notifications/``
because the SOURCE here (the OSS ``pocketpaw.alert_manager``) has no cloud
entity of its own, so the bridge hangs off the entity it produces into —
the same placement logic that puts ``sessions/title_listener.py`` (the
other OSS-bus subscriber) under its target entity.
"""

# pocketpaw runtime — local-process side of the connector bus.
# Created: 2026-05-03 — Phase 1 PR-8.
# Hosts the connector bus listener that picks up
# `connector.exec.requested` events and runs CLI adapters
# (firebase, gcp, future kubectl/gh/aws/...) on the user's machine,
# then publishes `connector.exec.completed` back.

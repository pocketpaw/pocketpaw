# Native connector adapters — Python implementations of the Connector
# protocol that go beyond the YAML/REST default. Used when the upstream
# API needs auth flows, response shaping, or per-action logic the YAML
# format can't express (Gmail's MIME message construction, Calendar's
# RFC 5545 recurrence, etc.).
#
# Created: 2026-05-03 — Phase 1 PR-3 lands GmailConnector.
# Calendar / Docs / Drive / Reddit / Spotify follow in PR-4 through PR-8.

# Discovery — cloud entity package (workspace-discovery TRIGGER).
# Created: 2026-06-21 (SZD finish slice F1 / feat/szd-finish-core) — the
#   4-file entity (domain/dto/service/router) that exposes the
#   workspace-discovery run over HTTP (``POST /cloud/discovery/run``). Before
#   this slice a discovery run could only be started from a script; the router
#   makes it reachable so the UI can fire it.

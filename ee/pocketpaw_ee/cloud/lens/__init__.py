# ee/pocketpaw_ee/cloud/lens — the workspace-scoped proxy to paw-lens.
#
# paw-lens is an internal Go service that stores agent traces and serves a read
# API guarded by a shared-secret ``X-Lens-Token`` header. The browser never
# calls it: ``router.py`` exposes ``/api/v1/lens/*`` 1:1 with paw-lens's
# ``/v1/*`` routes, ``service.py`` injects the caller's workspace_id, and
# ``client.py`` makes the upstream call.
#
# There is no ``domain.py``: lens data is pass-through JSON owned by the
# paw-lens contract, so the proxy adds no fields and models nothing.
#
# Invariants: workspace_id always comes from ``current_workspace_id``, never the
# client; an unset POCKETPAW_LENS_API_URL answers ``{"enabled": false}`` with no
# network call; a paw-lens outage is a 503 CloudError, never a 500; the token is
# never logged.

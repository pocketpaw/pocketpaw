"""The opt-in gate for the PTY terminal router.

Created: 2026-09-07 — extracted so the gate can be imported and tested on any
platform. ``terminal.py`` itself imports ``fcntl``, ``pty`` and ``termios``, so
it cannot be imported at all on Windows; a gate defined inside it would only
ever be exercised on Linux CI, which is the wrong place for the one check
standing between the public internet and a login shell.

WHAT THIS GUARDS

``POST /api/v1/terminal/input`` writes to the stdin of a real bash process
started with ``cwd=~`` and the server's own environment. It had no auth
dependency of any kind and sat under ``/api/v1/``, where the global
AuthMiddleware deliberately skips its 401 on the understanding that every
router beneath it authenticates for itself. This one did not, so a single
unauthenticated POST ran a command on the host and ``GET /terminal/sse``
streamed the output back.

WHY IT IS OFF BY DEFAULT

Nothing calls these routes. paw-enterprise's /code surface uses the Daytona VM's
own web terminal (``getWorkspaceVmTerminal``), the OSS dashboard and the Tauri
client never reference them, and no test did either. So the shell was reachable
without being wanted. Default-off costs nobody a feature and means a deployment
has to say out loud that it wants a remote shell.

Turning it on is deliberate: set ``POCKETPAW_TERMINAL_ENABLED=true``. The
setting is in ``_IMMUTABLE_FIELDS`` in ``api/v1/settings.py``, so the REST
settings surface cannot flip it — otherwise any admin-scoped API key could
re-enable a host shell over HTTP, which is the same hole one level up.

WHY 404 AND NOT 403

A disabled deployment should not advertise that the surface exists.

ORDER MATTERS. This dependency is declared BEFORE ``require_scope("admin")`` on
the router so a disabled deployment answers 404 to everyone, authenticated or
not. It also means the two guards are separable in tests: enable the terminal
and an anonymous caller must still get 403 from the scope check alone. Testing
only the anonymous-on-a-disabled-deployment case would pass with the scope
dependency deleted.
"""

from __future__ import annotations

from fastapi import HTTPException


def terminal_enabled() -> bool:
    """True when this deployment has opted into the PTY terminal routes."""
    from pocketpaw.config import get_settings

    return bool(getattr(get_settings(), "terminal_enabled", False))


async def require_terminal_enabled() -> None:
    """FastAPI dependency: 404 the terminal routes unless opted in."""
    if not terminal_enabled():
        raise HTTPException(status_code=404, detail="Not Found")

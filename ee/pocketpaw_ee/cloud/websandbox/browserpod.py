# browserpod.py — BrowserPod (in-tab WASM runtime) credential broker (BP-1b).
# Created 2026-07-18 (feat/code-mode): Code Mode runs Node-shaped projects in a
# BrowserPod pod — an x86 Linux VM in WebAssembly that executes INSIDE THE USER'S
# BROWSER TAB — instead of a Daytona cloud VM. ``BrowserPod.boot({apiKey})`` runs
# client-side, so the key has to reach the browser for a pod to exist at all.
#
# WHY THIS EXISTS: the key must NOT be a frontend build-time constant. Baked into
# the bundle it lands in every build artifact, CDN copy and source map, is readable
# by anyone who can load the app (including logged-out visitors), and can only be
# rotated by a rebuild + redeploy. Brokering it here means the key lives ONLY in
# server config and is handed out per-request to an authenticated, workspace-scoped
# caller — so it can be rotated centrally, gated, rate-limited, audited and revoked.
#
# WHAT THIS DOES *NOT* DO — be honest about the boundary: this is containment and
# control, NOT secrecy. An authenticated user can still read the key out of the
# network response, because ``boot()`` needs it in the tab. Closing that last gap
# requires BrowserPod to support short-lived or origin-scoped tokens (a vendor /
# commercial-terms question). Until it does, treat this key as "exposed to every
# authenticated, entitled user" and scope its blast radius accordingly:
# BrowserPod's own domain allowlist governs what a pod may reach OUTBOUND (npm,
# GitHub) — it is NOT an origin-lock on the key, so it cannot be leaned on as a
# publishable key the way a Stripe/Maps key can.
#
# Contrast with ``codegit/ticket.py``: there the VM gets a short-lived JWT we mint
# ourselves and the real GitHub token never leaves the server. We cannot do that
# here — the credential the client needs IS the vendor key, and the vendor has no
# exchange endpoint. That asymmetry is the whole reason this file documents its
# own limits instead of implying parity.
#
# An unconfigured deploy is NOT an error: it returns ``available: false`` so the
# frontend router cleanly falls back to the Daytona runtime instead of failing.
#
# Modified 2026-07-21 (RR-4, feat/webcontainer-credentials): the key RESOLUTION
# (env, then ``.env``, whitespace counts as unset) moved to ``vendor_keys.py``
# when WebContainers needed exactly the same behaviour. Nothing about the
# precedence, the test seam or this module's answers changed — the second caller
# is the reason it is shared rather than copied.
from __future__ import annotations

import logging

from pocketpaw_ee.cloud.websandbox.dto import BrowserPodCredentialsResponse
from pocketpaw_ee.cloud.websandbox.vendor_keys import dotenv_value, read_vendor_key

logger = logging.getLogger(__name__)

# Server-side only. Deliberately NOT prefixed for the frontend bundle — nothing
# in the built client may ever read this.
_ENV_VAR = "BROWSERPOD_API_KEY"


def _dotenv_key() -> str:
    """``.env`` fallback for the key.

    Kept as a module-level function rather than inlined because it is the seam
    tests patch to mean "this host has no .env" — otherwise deleting the
    environment variable in a test would still find the developer's real key on
    disk and the "unconfigured" path could never be exercised.
    """
    return dotenv_value(_ENV_VAR)


def browserpod_api_key() -> str:
    """Return the configured BrowserPod embedding key, or ``""`` when unset.

    ``.env`` is consulted defensively for the same reason ``uploads/factory.py``
    does it: this name has NO ``POCKETPAW_`` prefix, so pydantic-settings never
    reads it, and it only reaches ``os.environ`` if something called
    ``load_dotenv`` first — which depends on the entrypoint (the dashboard
    lifecycle does; a bare uvicorn/cloud boot may not). Getting that wrong is
    invisible: the broker answers ``available: false``, the frontend routes to
    Daytona, and nothing anywhere reports an error.

    Modified 2026-07-21 (RR-4): the resolution itself moved to
    ``vendor_keys.read_vendor_key`` when WebContainers needed the identical
    env-then-.env, whitespace-is-unset behaviour. Same precedence, same seam,
    one implementation.
    """
    return read_vendor_key(_ENV_VAR, _dotenv_key)


def browserpod_enabled() -> bool:
    """True when this deploy has a BrowserPod key configured."""
    return bool(browserpod_api_key())


async def get_credentials(workspace_id: str, user_id: str) -> BrowserPodCredentialsResponse:
    """Hand the calling user the credential needed to boot an in-tab pod.

    The router has already enforced license + an authenticated, workspace-scoped
    RequestContext, so reaching here means the caller is entitled. An unconfigured
    deploy returns ``available: false`` with a null key (the frontend then routes
    the project to Daytona) rather than raising — a missing optional runtime is a
    fallback condition, not a failure.

    The issue is logged (without the key) because BrowserPod bills on usage, so
    "who booted a pod" is an operational and cost-attribution signal.
    """
    key = browserpod_api_key()
    if not key:
        logger.debug(
            "browserpod: no %s configured — reporting unavailable (workspace=%s)",
            _ENV_VAR,
            workspace_id,
        )
        return BrowserPodCredentialsResponse(available=False, apiKey=None)

    logger.info(
        "browserpod: issued boot credential (workspace=%s, user=%s)",
        workspace_id,
        user_id,
    )
    return BrowserPodCredentialsResponse(available=True, apiKey=key)


__all__ = ["browserpod_api_key", "browserpod_enabled", "get_credentials"]

# webcontainer.py — WebContainers (in-tab WASM runtime) credential broker (RR-4).
# Created 2026-07-21 (feat/webcontainer-credentials): Code Mode can run a project
# inside StackBlitz's WebContainer — a Node.js runtime compiled to WebAssembly
# that executes INSIDE THE USER'S BROWSER TAB — instead of a Daytona cloud VM.
# ``configureAPIKey(key)`` runs client-side and must be called before
# ``WebContainer.boot()``, so the key has to reach the browser for a container to
# exist at all on a licensed origin.
#
# WHY THIS EXISTS: same reason as ``browserpod.py`` — the key must NOT be a
# frontend build-time constant. Baked into the bundle it lands in every build
# artifact, CDN copy and source map, is readable by anyone who can load the app,
# and can only be rotated by a rebuild + redeploy. Brokered here, it lives ONLY
# in server config and is handed out per-request to an authenticated,
# workspace-scoped caller, so it can be rotated, gated, audited and revoked
# centrally. Registered by runtime id in ``runtimes.py``; there is no
# WebContainers-specific endpoint.
#
# WHAT THIS DOES *NOT* DO: this is containment and control, NOT secrecy. An
# authenticated user can still read the key out of the network response, because
# the SDK needs it in the tab. Do not document it as a secret.
#
# ONE REAL DIFFERENCE FROM BROWSERPOD, and it decides how the client uses this:
# WebContainer boots WITHOUT a key on origins StackBlitz permits keyless
# (localhost and their own domains), and requires a licensed key on any other
# origin. So ``available: false`` here does NOT mean "this runtime cannot run" —
# it means "this deploy has no licensed key", and the CLIENT decides whether its
# own origin can proceed keyless. That is why this module reports configuration
# and takes no view on the caller's origin, which it cannot see anyway.
#
# An unconfigured deploy is NOT an error: it returns ``available: false`` so the
# frontend either boots keyless (localhost) or falls back to Daytona.
from __future__ import annotations

import logging

from pocketpaw_ee.cloud.websandbox.dto import RuntimeCredentialsResponse
from pocketpaw_ee.cloud.websandbox.vendor_keys import dotenv_value, read_vendor_key

logger = logging.getLogger(__name__)

# Server-side only. Deliberately NOT ``VITE_``-prefixed — nothing in the built
# client may ever read this. The name matches StackBlitz's own docs for the
# WebContainer API key so an operator can map it without a translation table.
_ENV_VAR = "WEBCONTAINER_API_KEY"


def _dotenv_key() -> str:
    """``.env`` fallback for the key. Patched by tests to mean "no .env here"."""
    return dotenv_value(_ENV_VAR)


def webcontainer_api_key() -> str:
    """Return the configured WebContainer API key, or ``""`` when unset."""
    return read_vendor_key(_ENV_VAR, _dotenv_key)


def webcontainer_enabled() -> bool:
    """True when this deploy has a licensed WebContainer key configured.

    Note the narrow claim: this is about the KEY, not about whether the runtime
    is usable. A localhost deploy with no key still boots containers; a deploy
    whose frontend has cross-origin isolation switched off cannot boot one even
    with a key. Both of those live on the client, which is the only place that
    knows its own origin and its own headers.
    """
    return bool(webcontainer_api_key())


async def get_credentials(workspace_id: str, user_id: str) -> RuntimeCredentialsResponse:
    """Hand the calling user the key needed to boot an in-tab WebContainer.

    The router has already enforced license + an authenticated, workspace-scoped
    RequestContext, so reaching here means the caller is entitled. An
    unconfigured deploy returns ``available: false`` with a null key rather than
    raising — see the module header for why that is a routing signal and not a
    failure.

    The issue is logged (without the key) because StackBlitz licenses this per
    deployment, so "who booted a container" is an operational signal.
    """
    key = webcontainer_api_key()
    if not key:
        logger.debug(
            "webcontainer: no %s configured — reporting unavailable (workspace=%s)",
            _ENV_VAR,
            workspace_id,
        )
        return RuntimeCredentialsResponse(available=False, apiKey=None)

    logger.info(
        "webcontainer: issued boot credential (workspace=%s, user=%s)",
        workspace_id,
        user_id,
    )
    return RuntimeCredentialsResponse(available=True, apiKey=key)


__all__ = ["get_credentials", "webcontainer_api_key", "webcontainer_enabled"]

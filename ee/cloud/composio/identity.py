"""Composio per-toolkit identity probes.

Composio's connected-accounts API returns OAuth tokens but does NOT
expose the underlying account identity (the GitHub login, Gmail
address, Slack user) in a uniform way. The documented pattern is
toolkit-specific: after a Connect Link auth flow completes, call the
toolkit's own "who am I" action to learn whose account was actually
authorized.

This matters for our tenancy model: a user can click Connect, then
authorize as ANY account they have access to — their personal GitHub
instead of their work one, a shared mailbox instead of their own. The
agent has no way to detect that without an explicit probe. We surface
the probed identity back to the chat ("Connected as @octocat —
continue?") so wrong-account binds are caught the first time.

The registry below maps toolkit slug → ``IdentityProbe``. To add a
toolkit:
    1. Find the toolkit's "get current user" action (Composio docs).
    2. Add a row; ``field_path`` is dot-separated for nested responses
       (e.g. ``"user.emailAddress"``).
    3. Add a unit test against a recorded fixture if possible.

Unknown toolkit → ``probe_identity_sync`` returns ``None`` and logs
``info``. The caller treats that as "verification unavailable" and
proceeds (better than blocking when an obscure toolkit lacks a probe).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IdentityProbe:
    """How to ask a Composio toolkit "who is the connected account?"

    ``action`` is the Composio action slug (e.g.
    ``"GITHUB_GET_AUTHENTICATED_USER"``). ``field_path`` is the
    dot-separated path into the action's response payload that holds
    the human-readable identity (a login, an email, a user id).
    """

    action: str
    field_path: str


# Per-toolkit probe registry. Extend per toolkit added to
# ``POCKETPAW_COMPOSIO_TOOLKITS``. Composio's response shapes vary by
# toolkit — verify the ``field_path`` against a real call when adding
# a new entry.
IDENTITY_PROBES: dict[str, IdentityProbe] = {
    "github": IdentityProbe(action="GITHUB_GET_THE_AUTHENTICATED_USER", field_path="login"),
    "gmail": IdentityProbe(action="GMAIL_GET_PROFILE", field_path="emailAddress"),
    "slack": IdentityProbe(action="SLACK_AUTH_TEST", field_path="user"),
    "googlecalendar": IdentityProbe(
        action="GOOGLECALENDAR_GET_CURRENT_DATE_TIME", field_path="user.email"
    ),
    "googledrive": IdentityProbe(action="GOOGLEDRIVE_GET_ABOUT", field_path="user.emailAddress"),
    "linear": IdentityProbe(action="LINEAR_VIEWER", field_path="email"),
}


def probe_identity_sync(client: Any, *, user_id: str, toolkit: str) -> str | None:
    """Probe the connected account's external identity for ``toolkit``.

    Returns the identity string (login / email / user id), or ``None``
    when:
        * The toolkit has no entry in ``IDENTITY_PROBES``.
        * The Composio call fails (network, action not enabled, etc.).
        * The expected ``field_path`` is missing from the response.

    All ``None`` returns are logged so an admin can extend the registry
    or fix the field path. The caller treats ``None`` as "verification
    unavailable" and proceeds without storing an identity for tripwire
    comparison — better than blocking the user.
    """
    probe = IDENTITY_PROBES.get(toolkit.lower())
    if probe is None:
        logger.info(
            "composio.identity: no probe registered for toolkit=%r — skipping verification",
            toolkit,
        )
        return None

    try:
        result = client.tools.execute(probe.action, user_id=user_id, arguments={})
    except Exception:  # noqa: BLE001
        logger.exception(
            "composio.identity: probe %s for toolkit=%s user=%s failed",
            probe.action,
            toolkit,
            user_id,
        )
        return None

    payload = _extract_payload(result)
    if payload is None:
        logger.warning(
            "composio.identity: probe %s returned an unrecognized envelope: %r",
            probe.action,
            type(result).__name__,
        )
        return None

    value = _walk_field_path(payload, probe.field_path)
    if value is None:
        logger.warning(
            "composio.identity: probe %s response missing field_path=%r",
            probe.action,
            probe.field_path,
        )
        return None
    return str(value)


def _extract_payload(result: Any) -> dict[str, Any] | None:
    """Unwrap Composio's typical response envelope.

    Composio's ``tools.execute`` returns either a dict like
    ``{"data": {...}, "successful": True}`` or a pydantic model with
    those same attrs depending on minor SDK version. We extract the
    ``data`` payload defensively without assuming a specific class.
    """
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            return data
        if data is None and result.get("successful") is not None:
            # Some calls return {"successful": True, ...} flat.
            return {k: v for k, v in result.items() if k != "successful"}
        return None
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    if hasattr(result, "model_dump"):
        try:
            dumped = result.model_dump()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(dumped, dict):
            inner = dumped.get("data")
            return inner if isinstance(inner, dict) else dumped
    return None


def _walk_field_path(payload: dict[str, Any], path: str) -> Any | None:
    """Walk a dot-separated path through a nested dict.

    Returns ``None`` on any miss. Does not interpret list indices —
    every level must be a dict. Composio response shapes for the
    probes registered above all match this expectation; if a future
    toolkit needs list indexing, extend here.
    """
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
        if current is None:
            return None
    return current


__all__ = ["IDENTITY_PROBES", "IdentityProbe", "probe_identity_sync"]

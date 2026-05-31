"""Composio Connect Link → inline Ripple spec.

When a Composio tool call returns a "needs authorization" response, we
extract the Connect Link URL and emit it to the active chat stream as
an inline Ripple spec — a single button the user clicks to authorize
the integration in a new browser tab. Once authorized, the user sends
the same prompt again and the Composio call succeeds.

Why inline Ripple instead of a markdown link: the cloud chat already
supports interactive Ripple via the chat.send round-trip (see
``feedback_inline_ripple_not_pockets`` memory). A clickable button is
the consistent surface for chat-side interactions; a raw URL would mix
two formats in the transcript and degrade UX.

Detection (heuristic — refine when we have real Composio responses to
calibrate against):
    * dict with a ``connect_url`` / ``connect_link`` / ``auth_url`` key
    * dict with ``needs_connection: true`` / ``requires_auth: true``
      AND a URL key nested anywhere obvious

The detection is feature-flag gated via
``settings.composio_connect_link_inline``; set False to disable inline
rendering (URL falls back to a text response).
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.config import Settings

logger = logging.getLogger(__name__)

_URL_KEYS: tuple[str, ...] = ("connect_url", "connect_link", "auth_url", "authorization_url")
_AUTH_FLAGS: tuple[str, ...] = (
    "needs_connection",
    "requires_auth",
    "needs_auth",
    "auth_required",
)


def as_inline_ripple(
    url: str,
    toolkit: str,
    action_label: str | None = None,
) -> dict[str, Any]:
    """Return a canonical inline Ripple ``{version, ui}`` spec.

    The UI is a single button that opens ``url`` in a new browser tab.
    The frontend's Ripple renderer maps ``actions=[{type:"open_url"}]``
    to ``window.open(url, "_blank")``.

    ``action_label`` defaults to ``Connect {Toolkit}`` (title-cased).
    """
    if not url:
        raise ValueError("as_inline_ripple: url is required")
    if not toolkit:
        raise ValueError("as_inline_ripple: toolkit is required")

    label = action_label or f"Connect {toolkit.title()}"

    return {
        "version": "1.0",
        "ui": {
            "type": "flex",
            "props": {"direction": "column", "gap": 8, "align": "start"},
            "children": [
                {
                    "type": "text",
                    "props": {
                        "value": (
                            f"{toolkit.title()} isn't connected yet. Click below to "
                            "authorize in a new tab, then send your message again."
                        ),
                    },
                },
                {
                    "type": "button",
                    "props": {
                        "label": label,
                        "variant": "primary",
                    },
                    "actions": [
                        {
                            "type": "open_url",
                            "url": url,
                            "target": "_blank",
                        }
                    ],
                },
            ],
        },
    }


def detect_connect_link(payload: Any) -> tuple[str, str] | None:
    """Inspect a Composio response payload for a Connect Link.

    Returns ``(url, toolkit_hint)`` if a Connect Link is detected,
    else ``None``. The toolkit hint is best-effort — derived from a
    ``toolkit`` / ``app`` field when present, otherwise the empty
    string (caller substitutes a sensible default).

    Walks one level of nesting (top-level dict, then nested dicts under
    common keys like ``error``, ``meta``, ``connection``). Deep search
    is intentionally avoided — false positives from a stray "url" field
    elsewhere in the payload would be worse than missing a Connect Link.
    """
    if not isinstance(payload, dict):
        return None

    url = _extract_url(payload)
    if url is None:
        # Check one level deeper for the most-likely nesting containers
        for container_key in ("error", "meta", "connection", "details", "data"):
            inner = payload.get(container_key)
            if isinstance(inner, dict):
                url = _extract_url(inner)
                if url is not None:
                    break

    if url is None:
        return None

    # Require either an explicit auth-flag or that the URL came from
    # one of the auth-named keys. Otherwise we'd mis-fire on any
    # response that happens to contain a URL.
    has_flag = any(_truthy(payload.get(flag)) for flag in _AUTH_FLAGS)
    if not has_flag and not _has_url_key(payload):
        return None

    toolkit = payload.get("toolkit") or payload.get("app") or payload.get("app_name") or ""
    return url, str(toolkit)


def maybe_emit_connect_link(
    payload: Any,
    settings: Settings | None = None,
) -> bool:
    """Detect + push a Connect Link inline-Ripple SSE event.

    Returns ``True`` when an event was pushed, ``False`` otherwise.

    The push uses the same SSE sink as the rest of the cloud chat
    (``pocketpaw_ee.cloud.chat.agent_service.push_sse_event``) under the event
    name ``ripple_inline``. The frontend already handles inline Ripple
    via that channel — no new wire format required.

    Gated by ``settings.composio_connect_link_inline``; when False, this
    is a no-op and the agent's response surfaces the raw URL string.
    """
    s = settings or Settings.load()
    if not s.composio_connect_link_inline:
        return False

    detection = detect_connect_link(payload)
    if detection is None:
        return False

    url, toolkit_hint = detection
    toolkit = toolkit_hint or "this integration"
    spec = as_inline_ripple(url, toolkit)

    try:
        from pocketpaw_ee.cloud.chat.agent_service import push_sse_event
    except ImportError:
        logger.debug("composio.connect_link: chat module unavailable, skip emit")
        return False

    push_sse_event("ripple_inline", {"source": "composio", "toolkit": toolkit, "spec": spec})
    return True


def _extract_url(d: dict[str, Any]) -> str | None:
    """Return the first non-empty URL value at the top level of ``d``."""
    for key in _URL_KEYS:
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _has_url_key(d: dict[str, Any]) -> bool:
    """True if any of the recognized URL keys is set on this dict."""
    return any(isinstance(d.get(key), str) and d.get(key) for key in _URL_KEYS)


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "yes", "1")
    return bool(v)


__all__ = [
    "as_inline_ripple",
    "detect_connect_link",
    "maybe_emit_connect_link",
]

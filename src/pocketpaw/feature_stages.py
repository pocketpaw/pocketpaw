# Feature stages: the one code-reviewed list of which surfaces are alpha,
# hidden, or preview in this release. Served by GET /api/v1/config/features.
#
# Ids are paw-enterprise route paths ("/calendar"), which the client matches
# by path segment (``p == id or p.startswith(id + "/")``, so "/belt" never
# matches "/belt-next"), or runtime ids "channel:<adapter>" / "backend:<name>".
#
# - hidden: off every nav surface, and the route redirects to "/". This is a
#   UI rule, not access control: the APIs behind a hidden route stay up.
# - alpha: keeps its nav entry and shows the Alpha pill.
# - preview: keeps its nav entry; opening it shows an "in the works" modal
#   once per device. The value names the modal variant.
#
# backend:* ids are NOT listed here. They come from ``BackendInfo.beta`` on
# each registered backend, so each backend keeps one marker. The three stages
# must stay disjoint (tests/test_feature_stages.py).

from __future__ import annotations

HIDDEN: frozenset[str] = frozenset(
    {
        "/belt",
        "/agent-health-lab",
        "/belt-lab",
        "/chat-lab",
        "/features-lab",
        "/knowledge-lab",
        "/no-ui-lab",
        "/shell-lab",
        "/ui-lab",
        "/sites/native-lab",
        "/browserpod-gate",
        "/cel-playground",
        "/home-console-test",
        "/test",
        "/webcontainer-spike",
        "/quickask",
        "/decisions-graph",
        "/automations",
        "/code",
        "/studio/vector",
        "/studio/photo",
        "/studio/design",
        # LiveKit is not confirmed on the hosted deployment yet.
        "/meetings",
    }
)

ALPHA: frozenset[str] = frozenset(
    {
        "/calendar",
        "/growth",
        "/deep-work",
        "/agents/activity",
        "/agent-health",
        "/discover",
        "/browser",
        "/partners",
        "/sidepanel",
        "/studio/quick",
        "channel:whatsapp",
        "channel:signal",
        "channel:gchat",
        "channel:neonize",
    }
)

PREVIEW: dict[str, str] = {
    "/foresight": "in-the-works",
    "/belt-next": "in-the-works",
}


def beta_backend_ids() -> set[str]:
    """``backend:<name>`` for every registered backend marked ``beta``.

    Same lookup as ``GET /api/v1/backends``: backend modules import their SDKs
    lazily, and a backend whose module fails to import is skipped.
    """
    from pocketpaw.agents.registry import get_backend_info, list_backends

    ids = set()
    for name in list_backends():
        info = get_backend_info(name)
        if info is not None and info.beta:
            ids.add(f"backend:{name}")
    return ids


def stages() -> dict:
    """The JSON contract: ``{"alpha": [...], "hidden": [...], "preview": {...}}``."""
    return {
        "alpha": sorted(ALPHA | beta_backend_ids()),
        "hidden": sorted(HIDDEN),
        "preview": dict(PREVIEW),
    }

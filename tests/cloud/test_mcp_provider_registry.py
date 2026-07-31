# tests/cloud/test_mcp_provider_registry.py — the structural guard for the
# ``pocketpaw.mcp_servers`` entry-point registry.
#
# WHY THIS EXISTS. SHIP-4 added ``CloudShipMcpProvider`` by pasting a class
# INTO the middle of ``CloudExternalActionsMcpProvider`` — between its
# ``build_server`` and its ``tool_ids``. Python accepted it silently: the new
# class adopted the orphaned ``tool_ids`` (so /ship advertised the EXTERNAL
# ACTION tool ids) and the external-actions provider was left with no
# ``tool_ids`` at all. Neither failure was visible, because
# ``claude_sdk._collect_mcp_tool_ids`` wraps each provider in
# ``except Exception: continue`` — so the AttributeError was swallowed and the
# only symptom was that none of the sixteen ``mcp__pocketpaw_ship__*`` ids ever
# reached the SDK allowlist, i.e. every ship verb the agent called was refused.
# The whole /ship agent surface was dead in production while 207 ship tests and
# all of CI stayed green.
#
# The lesson is that a per-provider unit test cannot catch a provider that
# simply LOST a method — you have to walk the registry itself. These tests do
# that: every registered provider must expose both halves of the contract, and
# each provider's ids must actually belong to the server it builds.
#
# Created 2026-07-29 (fix/ship-review-p0): new module.

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

_GROUP = "pocketpaw.mcp_servers"


def _providers() -> list[tuple[str, object]]:
    """Every registered mcp_servers provider, instantiated."""
    out: list[tuple[str, object]] = []
    for ep in entry_points(group=_GROUP):
        cls = ep.load()
        out.append((ep.name, cls()))
    return out


def test_registry_is_not_empty() -> None:
    """A guard on the guard — an empty registry would make the rest vacuous."""
    assert _providers(), f"no providers registered under {_GROUP}"


@pytest.mark.parametrize("name,provider", _providers(), ids=lambda v: getattr(v, "__name__", v))
def test_every_provider_exposes_both_contract_halves(name: str, provider: object) -> None:
    """Both ``build_server`` and ``tool_ids`` must exist and be callable.

    A class pasted into the middle of another silently steals or orphans one of
    these; ``_collect_mcp_tool_ids`` swallows the resulting AttributeError, so
    only a registry walk surfaces it.
    """
    for method in ("build_server", "tool_ids"):
        assert hasattr(provider, method), (
            f"provider {name!r} ({type(provider).__name__}) is missing {method}() — "
            "a class pasted mid-definition can orphan it"
        )
        assert callable(getattr(provider, method)), f"{name}.{method} is not callable"


@pytest.mark.parametrize("name,provider", _providers(), ids=lambda v: getattr(v, "__name__", v))
def test_tool_ids_belong_to_the_server_the_provider_builds(name: str, provider: object) -> None:
    """A provider's tool ids must be namespaced to its OWN server.

    This is the assertion that catches the SHIP-4 swap directly: ship returning
    ``mcp__pocketpaw_external_actions__*`` is exactly the shape of the bug, and
    it is invisible to any test that only checks "tool_ids is non-empty".
    """
    built = provider.build_server()  # type: ignore[attr-defined]
    if built is None:
        return  # optional dependency absent — the loop skips it in production too
    server_name, _server = built
    ids = list(provider.tool_ids())  # type: ignore[attr-defined]
    if not ids:
        return  # a provider may legitimately expose no scoped ids
    prefix = f"mcp__{server_name}__"
    mismatched = [t for t in ids if not t.startswith(prefix)]
    assert not mismatched, (
        f"provider {name!r} builds server {server_name!r} but reports tool ids "
        f"belonging to another server: {mismatched[:3]}"
    )


def test_every_provider_class_is_registered_as_an_entry_point() -> None:
    """A provider class in extensions.py that nobody registered is dead code.

    The sibling trap to the method-theft bug: ``CloudShipMcpProvider`` can exist,
    import cleanly and pass its own unit tests while never being loaded, because
    loading happens through the ``pocketpaw.mcp_servers`` entry-point table in
    ``ee/pyproject.toml``. (It also fails this way after an entry-point is ADDED
    but the editable install's metadata is stale — re-run ``uv sync --group ee``.)
    """
    import inspect

    from pocketpaw_ee import extensions

    defined = {
        n
        for n, obj in inspect.getmembers(extensions, inspect.isclass)
        if n.endswith("McpProvider") and obj.__module__ == extensions.__name__
    }
    registered = {ep.load().__name__ for ep in entry_points(group=_GROUP)}
    unregistered = defined - registered
    assert not unregistered, (
        f"McpProvider classes defined but not registered under {_GROUP}: "
        f"{sorted(unregistered)} — add them to ee/pyproject.toml, then re-sync"
    )

# tests/cloud/test_pulley_mcp.py — pulley block-engine MCP server (A1).
#
# Created: 2026-09-12 (feat/belt-factory, belt factory slice A1).
# Guards the pulley MCP wiring for the cloud chat (claude_agent_sdk) backend:
#   * PULLEY_TOOL_IDS — the 5 namespaced tool ids (mcp__pulley__search_catalog
#     etc.) the surface allow-list + the SDK allowlist machinery key on.
#   * build_pulley_server() — None when pulley_path is unset, None when the
#     server.ts is missing, None when bun can't be resolved, and the correct
#     ("pulley", <stdio config dict>) when both resolve.
#   * The --app flag is present only when pulley_app_path is set.
#   * _resolve_bun_bin — explicit path → PATH → ~/.bun/bin/bun discovery order.
#   * The BELT surface profile's allow_mcp_tool_ids carries the pulley ids and a
#     non-BELT surface's does not (apply_plan writes to disk).
#
# Updated: 2026-09-12 (A1b, belt factory) — added the DENY-FOLD block at the
# bottom. The allow-list only binds surfaces that HAVE one, and pulley is an
# AMBIENT server: a surface with ``allow_mcp_tool_ids=None`` (/chat and every
# unmapped kind — the default case) could call ``mcp__pulley__apply_plan``, which
# writes files to disk. ``service._deny_off_surface`` now folds the pulley ids
# into the deny set of every non-BELT surface, the same chokepoint BR-1 used for
# the agentic browser; these tests pin the unmapped case, the BELT exemption,
# every named surface, the surviving browser floor, and the independent
# ``pulley_tool_ids()`` load that keeps an unrelated ImportError from emptying
# the deny.
#
# build_pulley_server reads its config through pocketpaw.config.get_settings (a
# cached singleton, the same pattern loom / media / sites use), so every test
# patches get_settings.
#
# NOTE ON THE FAKE BUN: this box has bun both on PATH and at ~/.bun/bin/bun, so
# a test that leans on the default pulley_bin="bun" passes here and fails on a
# box without it. Every test below either injects an explicit fake binary or
# patches BOTH shutil.which and Path.home — never relies on bun's absence.

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pocketpaw_ee.agent.mcp_servers.pulley as pulley
import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
from pocketpaw_ee.cloud.surface.service import resolve_profile
from pocketpaw_ee.extensions import CloudPulleyMcpProvider


def _settings(
    *,
    pulley_path: str | None,
    pulley_bin: str = "bun",
    pulley_app_path: str | None = None,
) -> MagicMock:
    """A stand-in for get_settings() carrying only the fields the server reads."""
    return MagicMock(
        pulley_path=pulley_path,
        pulley_bin=pulley_bin,
        pulley_app_path=pulley_app_path,
    )


def _fake_checkout(tmp_path) -> tuple[str, str]:
    """A fake pulley checkout + a fake bun. Returns (pulley_path, bun_path)."""
    server_ts = tmp_path / "pulley" / "mcp" / "server.ts"
    server_ts.parent.mkdir(parents=True)
    server_ts.write_text("// fake\n")
    bun = tmp_path / "bun"
    bun.write_text("#!/bin/sh\n")
    bun.chmod(0o755)
    return str(tmp_path / "pulley"), str(bun)


# --- tool ids ---------------------------------------------------------------


def test_pulley_tool_ids_are_namespaced() -> None:
    """All 5 ids use the ``mcp__pulley__<tool>`` form the allowlist matches."""
    assert pulley.SERVER_NAME == "pulley"
    assert pulley.PULLEY_TOOL_IDS == (
        "mcp__pulley__search_catalog",
        "mcp__pulley__describe_block",
        "mcp__pulley__plan_install",
        "mcp__pulley__apply_plan",
        "mcp__pulley__doctor",
    )
    assert all(t.startswith("mcp__pulley__") for t in pulley.PULLEY_TOOL_IDS)


def test_provider_tool_ids_match_module() -> None:
    """The provider surfaces exactly the module's tool ids for the allowlist."""
    assert CloudPulleyMcpProvider().tool_ids() == list(pulley.PULLEY_TOOL_IDS)


def test_pulley_is_ambient_not_opt_in() -> None:
    """pulley is ambient — the /belt surface scopes it via its profile
    allowlist, so it must NOT be in the opt-in set. Adding it there would
    silently strip the tools from /belt, which is the only surface that has
    them. Mirrors ``test_loom_is_ambient_not_opt_in``."""
    from pocketpaw.agents.claude_sdk import OPT_IN_MCP_SERVERS

    assert pulley.SERVER_NAME not in OPT_IN_MCP_SERVERS


# --- build_server: disabled / degraded paths -------------------------------


def test_build_server_none_when_pulley_path_unset() -> None:
    """pulley_path unset → disabled → None (chat keeps working without blocks)."""
    with patch("pocketpaw.config.get_settings", return_value=_settings(pulley_path=None)):
        assert pulley.build_pulley_server() is None
        assert CloudPulleyMcpProvider().build_server() is None


def test_build_server_none_when_server_ts_missing(tmp_path) -> None:
    """A configured checkout with no mcp/server.ts → None, not a crash."""
    empty = tmp_path / "not-pulley"
    empty.mkdir()
    with patch("pocketpaw.config.get_settings", return_value=_settings(pulley_path=str(empty))):
        assert pulley.build_pulley_server() is None


def test_build_server_none_when_bun_missing(tmp_path) -> None:
    """A real server.ts but an unresolvable bun → None (degrades gracefully)."""
    pulley_path, _bun = _fake_checkout(tmp_path)
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    with (
        patch(
            "pocketpaw.config.get_settings",
            return_value=_settings(pulley_path=pulley_path, pulley_bin="bun-nonexistent-xyz"),
        ),
        patch("pocketpaw_ee.agent.mcp_servers.pulley.shutil.which", return_value=None),
        patch("pocketpaw_ee.agent.mcp_servers.pulley.Path.home", return_value=empty_home),
    ):
        assert pulley.build_pulley_server() is None


# --- build_server: happy path ----------------------------------------------


def test_build_server_returns_stdio_config(tmp_path) -> None:
    """Checkout + resolvable bun → ("pulley", <McpStdioServerConfig dict>) with
    the server.ts path as the only arg. ``--registry`` is deliberately absent:
    pulley derives both its root and its registry from the server file's own
    location (mcp/lib/belt.ts resolves pulleyRoot to <server.ts>/../..)."""
    pulley_path, bun = _fake_checkout(tmp_path)
    with patch(
        "pocketpaw.config.get_settings",
        return_value=_settings(pulley_path=pulley_path, pulley_bin=bun),
    ):
        built = pulley.build_pulley_server()

    assert built is not None
    name, config = built
    assert name == "pulley"
    assert config == {
        "type": "stdio",
        "command": bun,
        "args": [f"{pulley_path}/mcp/server.ts"],
    }


def test_app_flag_only_when_app_path_set(tmp_path) -> None:
    """pulley_app_path set → ``--app <dir>`` rides the launch line; unset → the
    flag is omitted entirely (the tools then require an explicit ``app`` arg,
    which is what the server advertises in that mode)."""
    pulley_path, bun = _fake_checkout(tmp_path)
    app = tmp_path / "client-app"
    app.mkdir()

    with patch(
        "pocketpaw.config.get_settings",
        return_value=_settings(pulley_path=pulley_path, pulley_bin=bun, pulley_app_path=str(app)),
    ):
        built = pulley.build_pulley_server()
    assert built is not None
    assert built[1]["args"] == [f"{pulley_path}/mcp/server.ts", "--app", str(app)]

    with patch(
        "pocketpaw.config.get_settings",
        return_value=_settings(pulley_path=pulley_path, pulley_bin=bun),
    ):
        built_no_app = pulley.build_pulley_server()
    assert built_no_app is not None
    assert "--app" not in built_no_app[1]["args"]


# --- bun resolution order ---------------------------------------------------


def test_resolve_bun_prefers_explicit_path(tmp_path) -> None:
    """An explicit executable path is returned verbatim."""
    explicit = tmp_path / "bun"
    explicit.write_text("#!/bin/sh\n")
    explicit.chmod(0o755)
    assert pulley._resolve_bun_bin(str(explicit)) == str(explicit)


def test_resolve_bun_falls_back_to_path(tmp_path) -> None:
    """A bare name not on disk resolves via PATH (shutil.which)."""
    found = str(tmp_path / "bun-on-path")
    with patch("pocketpaw_ee.agent.mcp_servers.pulley.shutil.which", return_value=found):
        assert pulley._resolve_bun_bin("bun") == found


def test_resolve_bun_falls_back_to_bun_home(tmp_path) -> None:
    """Not an explicit file and not on PATH → ~/.bun/bin/bun fallback."""
    home = tmp_path / "home"
    (home / ".bun" / "bin").mkdir(parents=True)
    bun = home / ".bun" / "bin" / "bun"
    bun.write_text("#!/bin/sh\n")
    bun.chmod(0o755)
    with (
        patch("pocketpaw_ee.agent.mcp_servers.pulley.shutil.which", return_value=None),
        patch("pocketpaw_ee.agent.mcp_servers.pulley.Path.home", return_value=home),
    ):
        assert pulley._resolve_bun_bin("bun") == str(bun)


def test_resolve_bun_returns_none_when_nothing_found(tmp_path) -> None:
    """Nothing on disk matches → None so the caller degrades to None."""
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    with (
        patch("pocketpaw_ee.agent.mcp_servers.pulley.shutil.which", return_value=None),
        patch("pocketpaw_ee.agent.mcp_servers.pulley.Path.home", return_value=empty_home),
    ):
        assert pulley._resolve_bun_bin("bun-nope") is None


# --- surface scoping: BELT only --------------------------------------------


def test_belt_profile_allows_pulley_and_studio_does_not() -> None:
    """The pulley ids are on /belt's allow-list and nowhere else. STUDIO is the
    comparison because its allow-list is non-empty (the media ids) — an
    unrestricted surface carries ``None`` and would make the assertion vacuous.
    ``apply_plan`` writes to disk, which is why this scoping is the point."""
    belt = resolve_profile(SurfaceKind.BELT, SurfaceMeta())
    assert belt.allow_mcp_tool_ids is not None
    for tool_id in pulley.PULLEY_TOOL_IDS:
        assert tool_id in belt.allow_mcp_tool_ids

    studio = resolve_profile(SurfaceKind.STUDIO, SurfaceMeta())
    assert studio.allow_mcp_tool_ids is not None
    for tool_id in pulley.PULLEY_TOOL_IDS:
        assert tool_id not in studio.allow_mcp_tool_ids


# --- the deny-fold: an allow-list alone does not scope an AMBIENT server ------
#
# The allow-list above only binds surfaces that HAVE one. pulley is registered
# ambiently (like loom), so a surface with ``allow_mcp_tool_ids=None`` — /chat
# and every unmapped kind, the default case — sees every ambient tool, including
# ``mcp__pulley__apply_plan``, which WRITES FILES TO DISK. ``service.
# _deny_off_surface`` closes that at the one chokepoint every ``(kind, meta)``
# passes through, exactly as BR-1 did for the agentic browser.


def test_unmapped_surface_denies_pulley() -> None:
    """The case the allow-list CANNOT reach: a surface with no allow-list.

    /chat carries ``allow_mcp_tool_ids=None`` (asserted first, so this proves
    the unrestricted default rather than a vacuous membership check) and would
    otherwise reach ``apply_plan`` beside the send-capable connector tools.
    """
    chat = resolve_profile(SurfaceKind.CHAT, SurfaceMeta())
    assert chat.allow_mcp_tool_ids is None, "CHAT must stay the unrestricted case"
    assert frozenset(pulley.PULLEY_TOOL_IDS) <= chat.deny_mcp_tool_ids


def test_belt_allows_pulley_and_does_not_deny_it() -> None:
    """The owner surface keeps them: on the allow-list AND off the deny set.

    A deny is subtracted from the allowed tools before the SDK launches, so
    folding the ids into BELT's deny too would silently strip the block engine
    from the only surface that is supposed to have it.
    """
    belt = resolve_profile(SurfaceKind.BELT, SurfaceMeta())
    ids = frozenset(pulley.PULLEY_TOOL_IDS)
    assert ids <= (belt.allow_mcp_tool_ids or frozenset())
    assert not (ids & belt.deny_mcp_tool_ids)


def test_every_non_belt_surface_denies_pulley() -> None:
    """Every named surface but /belt denies all five ids — STUDIO and SHIP
    included, and the unmapped GENERIC default with them.

    THE MUTATION THAT BREAKS THIS: drop the ``_deny_off_surface`` call from
    ``resolve_profile``, or drop the ``pulley_tool_ids()`` branch inside it —
    every surface's deny set loses the pulley ids and an ambient ``apply_plan``
    becomes reachable from /chat.
    """
    ids = frozenset(pulley.PULLEY_TOOL_IDS)
    for kind in SurfaceKind:
        profile = resolve_profile(kind, SurfaceMeta())
        if kind is SurfaceKind.BELT:
            assert not (ids & profile.deny_mcp_tool_ids), kind
        else:
            assert ids <= profile.deny_mcp_tool_ids, kind


def test_browser_deny_fold_still_applies() -> None:
    """A1b generalized ``_deny_browser_off_surface`` into ``_deny_off_surface``;
    BR-1's floor must survive that. /belt owns pulley, not the browser, so it is
    the sharpest surface to check: it must deny the browser ids and keep pulley.
    """
    from pocketpaw_ee.agent.mcp_servers.browser import BROWSER_TOOL_IDS

    browser_ids = frozenset(BROWSER_TOOL_IDS)
    assert browser_ids <= resolve_profile(SurfaceKind.CHAT, SurfaceMeta()).deny_mcp_tool_ids
    assert browser_ids <= resolve_profile(SurfaceKind.BELT, SurfaceMeta()).deny_mcp_tool_ids

    browser = resolve_profile(SurfaceKind.BROWSER, SurfaceMeta())
    assert browser_ids <= (browser.allow_mcp_tool_ids or frozenset())
    assert not (browser_ids & browser.deny_mcp_tool_ids)


@pytest.fixture
def _clear_tool_id_caches():
    """Reset the three memo caches around a test that fakes an import failure."""
    from pocketpaw_ee.cloud.surface import surface_registry as reg

    reg._MCP_TOOL_IDS_CACHE = None
    reg._BROWSER_TOOL_IDS_CACHE = None
    reg._PULLEY_TOOL_IDS_CACHE = None
    yield
    reg._MCP_TOOL_IDS_CACHE = None
    reg._BROWSER_TOOL_IDS_CACHE = None
    reg._PULLEY_TOOL_IDS_CACHE = None


def test_unrelated_import_failure_still_denies_pulley(_clear_tool_id_caches) -> None:
    """An unrelated sibling module breaking must NOT unlock pulley on /chat.

    ``_load_mcp_tool_ids`` wraps a dozen imports in ONE try/except, so sourcing
    the deny ids from it would mean a palette ImportError empties the deny while
    ``CloudPulleyMcpProvider`` — which imports the pulley module on its own path
    — still registers the server. ``pulley_tool_ids()`` therefore loads
    independently. Mirrors the browser regression this fail-open was found in.

    THE MUTATION THAT BREAKS THIS: serve ``pulley_tool_ids()`` out of
    ``_mcp_tool_ids()`` instead of its own import.
    """
    import builtins

    from pocketpaw_ee.cloud.surface import surface_registry as reg

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        # A module pulley has nothing to do with.
        if name == "pocketpaw_ee.agent.mcp_servers.palette":
            raise ImportError("simulated unrelated breakage")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _fake_import
    try:
        ids = reg.pulley_tool_ids()
        chat_profile = resolve_profile(SurfaceKind.CHAT, SurfaceMeta())
        # Sampled INSIDE the patch: proves the shared block really did degrade,
        # so the test is exercising the fail-open path and not a healthy one.
        shared_block_degraded = reg._mcp_tool_ids().loaded is False
    finally:
        builtins.__import__ = real_import

    assert shared_block_degraded, "shared import block did not degrade; test proves nothing"
    assert ids == frozenset(pulley.PULLEY_TOOL_IDS)
    assert frozenset(pulley.PULLEY_TOOL_IDS) <= chat_profile.deny_mcp_tool_ids

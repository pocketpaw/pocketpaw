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

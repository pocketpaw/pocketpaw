# Local-folder /code: native Read / Grep / Glob instead of the delegated read
# tools, gated on ``code_local_native_tools`` and refused in multi-tenant cloud.
# Every other /code turn must resolve to the unchanged default profile.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile
from pocketpaw_ee.cloud.surface.handlers import code as code_handler
from pocketpaw_ee.cloud.surface.system_prompts import (
    CODE_LOCAL_SYSTEM_PROMPT,
    CODE_SYSTEM_PROMPT,
)

_DELEGATED_READS = {
    "mcp__pocketpaw_code__readFile",
    "mcp__pocketpaw_code__search",
    "mcp__pocketpaw_code__listDir",
}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    monkeypatch.setattr(
        code_handler, "get_settings", lambda: SimpleNamespace(code_local_native_tools=True)
    )


def _default():
    return resolve_profile(SurfaceKind.CODE, SurfaceMeta())


def _meta(current_dir: str | None) -> SurfaceMeta:
    return SurfaceMeta(route_path="/code", project_name="proj", current_dir=current_dir)


def test_local_folder_gets_native_reads(flag_on, tmp_path) -> None:
    profile = resolve_profile(SurfaceKind.CODE, _meta(str(tmp_path)))

    deny = set(profile.deny_mcp_tool_ids)
    assert {"Read", "Glob", "Grep"}.isdisjoint(deny)
    assert {"Bash", "Write", "Edit", "Agent", "Skill"} <= deny
    assert _DELEGATED_READS <= deny
    assert profile.allow_mcp_tool_ids == _default().allow_mcp_tool_ids
    assert profile.system_message_override == CODE_LOCAL_SYSTEM_PROMPT
    assert profile.ripple_mode == "off"


async def test_local_preamble_names_the_root(flag_on, tmp_path) -> None:
    preamble = await code_handler.build_preamble("ws", "u", _meta(str(tmp_path)))

    assert str(tmp_path) in preamble.text
    assert "`Read`" in preamble.text
    assert str(tmp_path) in preamble.cache_key


@pytest.mark.parametrize(
    "current_dir",
    [None, "", "relative/path", "/definitely/not/a/real/dir", "/"],
)
def test_unusable_path_keeps_the_default(flag_on, current_dir) -> None:
    assert resolve_profile(SurfaceKind.CODE, _meta(current_dir)) == _default()


def test_flag_off_keeps_the_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    monkeypatch.setattr(
        code_handler, "get_settings", lambda: SimpleNamespace(code_local_native_tools=False)
    )
    profile = resolve_profile(SurfaceKind.CODE, _meta(str(tmp_path)))

    assert profile == _default()
    assert profile.system_message_override == CODE_SYSTEM_PROMPT


def test_multi_tenant_marker_keeps_the_default(flag_on, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")

    assert resolve_profile(SurfaceKind.CODE, _meta(str(tmp_path))) == _default()


async def test_default_preamble_is_unchanged_by_the_flag(flag_on) -> None:
    on = await code_handler.build_preamble("ws", "u", _meta(None))

    assert "`readFile`" in on.text
    assert on.cache_key == code_handler.meta_key("code", "/code", "proj")

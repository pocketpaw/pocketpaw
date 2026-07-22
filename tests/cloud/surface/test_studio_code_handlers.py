# tests/cloud/surface/test_studio_code_handlers.py — STUDIO + CODE surface
# handlers and their ripple-OFF SurfaceProfiles.
#
# Created: 2026-06-10 (feat/studio-code-migration) — Guards the two new
# describe→do surfaces:
#   * /studio — the agent GENERATES media (image + video) and lays it out in a
#     gallery. Preamble must orient to media (not a dashboard), prefer the
#     `studio` skill, and name the media MCP fallback tools.
#   * /code — the agent EDITS + RUNS code. Preamble must orient to coding (not a
#     dashboard), prefer the `code` skill, name the built-in tools, and demand
#     verification before "done".
# Also pins both profiles: ripple_mode="off" (so neither inherits the ripple
# "default to ui-spec" LAW), the per-surface skill, and STUDIO's media-tool
# allow-list / CODE's SDK-tool allow-list.
#
# Changes: 2026-07-22 (feat/code-surface-profile, CD-3) — the /code half was
# guarding the WRONG MACHINE, so its assertions were rewritten rather than
# extended. The old tests demanded the preamble NAME Bash/Read/Write/Edit/Glob/
# Grep and that the profile carry them in ``allowed_sdk_tools``; both encoded the
# stale belief that the /code agent works on a filesystem it can see. It does
# not — it runs on the backend server, and the user's project is reachable only
# through the ``code_mode`` tool. The replacements assert the inverse: the
# resolved profile DENIES every file/shell built-in (plus ``Agent``), allows
# exactly ``code_mode``, and surfaces NO skill; and the rendered preamble leaks
# no path and names no Daytona MCP tool — including when a legacy client still
# stamps the old ``current_dir`` / ``storage_root`` / ``workspace_vm`` /
# ``is_cloud_storage`` hints. The registry-import assertion is exercised too,
# since the profile now leans on two module-level literals.
#
# The load-bearing one is
# ``test_code_profile_grants_no_filesystem_tool_in_the_effective_allowlist``: it
# drives the REAL ``ClaudeSDKBackend._build_options`` and reads the allowlist the
# SDK would launch with. A membership check on ``deny_mcp_tool_ids`` alone would
# have gone green against the OLD profile too — that one named all six built-ins
# in the additive ``allowed_sdk_tools`` and shipped them anyway — so the negative
# has to be asserted against the COMPUTED result, not the declaration.

from __future__ import annotations

from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile
from pocketpaw_ee.cloud.surface.handlers import code as code_handler
from pocketpaw_ee.cloud.surface.handlers import studio as studio_handler

# pytest-asyncio runs in auto mode (see pyproject [tool.pytest] asyncio_mode),
# so async tests are detected automatically — no module-level mark needed (a
# module mark would wrongly tag the sync profile/util tests below).

WORKSPACE = "ws-surface-studiocode"
USER = "u-studiocode"


# --- /studio handler ---


async def test_studio_handler_carries_media_orientation() -> None:
    """The preamble orients to media generation on the studio surface — and must
    NOT frame the deliverable as a dashboard or a pocket."""
    preamble = await studio_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/studio")
    )

    assert '<surface kind="studio"' in preamble
    lower = preamble.lower()
    # Mentions the surface + the media deliverable.
    assert "studio" in lower
    assert "image" in lower
    assert "video" in lower
    assert "gallery" in lower
    # Not a dashboard build.
    assert "dashboard" not in lower or "not" in lower  # only as the thing to avoid
    assert "build a pocket" not in lower


async def test_studio_handler_prefers_studio_skill_and_names_media_tools() -> None:
    """The procedure PREFERS the `studio` skill and names the media MCP tools as
    the fallback so the generate→gallery flow never breaks."""
    preamble = await studio_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/studio")
    )

    assert "prefer" in preamble.lower()
    assert "`studio`" in preamble or "studio skill" in preamble.lower()
    # The in-process media MCP tools — the SDK backend only sees these.
    assert "mcp__pocketpaw_media__image_generate" in preamble
    assert "mcp__pocketpaw_media__video_generate" in preamble


async def test_studio_handler_relays_provider_errors() -> None:
    """The procedure tells the agent to relay provider/key errors plainly and
    never fake a generated asset."""
    preamble = await studio_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/studio")
    )
    lower = preamble.lower()
    assert "error" in lower
    assert "phantom" in lower or "never claim" in lower


# --- /code handler ---


# The file/shell built-ins the /code agent must not hold. They address the
# backend server, which is not the machine the user's project is on.
_FILESYSTEM_BUILTINS = frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep"})

# The Daytona MCP tool names the old preamble advertised. They address an older
# cloud-projects model that knows nothing of the current codeproject +
# CodeFileSession runtime, so naming ANY of them sends the agent at a machine
# that will not have the user's code.
_STALE_DAYTONA_TOOLS = (
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "run_python",
    "sync_to_s3",
    "start_server",
    "preview_url",
    "daytona",
)

# The legacy storage hints the old handler branched on. A current /code page
# stamps none of them, but an older client might — and if it does, none of the
# values may reach the prompt as a place to work.
_LEGACY_STORAGE_META = SurfaceMeta(
    route_path="/code",
    current_dir="/srv/pocketpaw/uploads/projects/ws1/u1/my-app/",
    storage_root="projects/ws1/u1/my-app/",
    project_name="my-app",
    is_cloud_storage="true",
    workspace_vm="true",
)


async def test_code_handler_carries_coding_orientation() -> None:
    """The preamble orients to editing + running code on the code surface — and
    must NOT frame the deliverable as a dashboard or a pocket."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))

    assert '<surface kind="code" ' in preamble
    lower = preamble.lower()
    assert "code" in lower
    assert "workspace" in lower
    # Not a dashboard / pocket build.
    assert "build a pocket" not in lower


async def test_code_handler_routes_all_work_through_code_mode() -> None:
    """`code_mode` is named as the ONLY route to the user's project."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))

    assert "code_mode" in preamble
    lower = preamble.lower()
    assert "only" in lower


async def test_code_handler_forbids_the_filesystem_builtins() -> None:
    """The procedure names the file/shell built-ins ONLY to forbid them — the
    agent has no filesystem on this surface."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))

    for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep"):
        assert tool in preamble, f"preamble should name the {tool} tool to forbid it"
    lower = preamble.lower()
    assert "do not attempt" in lower
    assert "no filesystem" in lower


async def test_code_handler_calls_code_mode_immediately_on_a_selection() -> None:
    """An edit scoped to a selection the user already made goes straight to
    `code_mode` — no exploratory retrieval first (the path is two model calls
    deep, so a redundant round-trip is expensive)."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))
    lower = preamble.lower()

    assert "selection" in lower
    assert "immediately" in lower
    assert "retrieval" in lower


async def test_code_handler_names_no_daytona_tool() -> None:
    """The stale Daytona MCP tool names are gone — they point at a runtime that
    knows nothing of codeproject / CodeFileSession."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))
    lower = preamble.lower()

    for tool in _STALE_DAYTONA_TOOLS:
        assert tool not in lower, f"preamble must not name the stale {tool!r} tool"


async def test_code_handler_leaks_no_filesystem_path_even_from_legacy_meta() -> None:
    """No path reaches the prompt — not the backend's workspace root, not the
    cloud-projects REST route, and not the legacy storage hints even when an
    older client still stamps them.

    This is the regression the rewrite exists for: the old handler fell through
    to "your working directory is the workspace root", which is the BACKEND
    SERVER's filesystem, and the agent would edit it and report success."""
    for meta in (SurfaceMeta(route_path="/code"), _LEGACY_STORAGE_META):
        preamble = await code_handler.build_preamble(WORKSPACE, USER, meta)
        lower = preamble.lower()

        assert "/srv/pocketpaw" not in lower
        assert "projects/ws1/u1/my-app" not in lower
        assert "/cloud/projects" not in lower
        assert "~/.pocketpaw" not in lower
        assert "working directory is" not in lower
        assert "workspace root" not in lower
        # Nothing to cd into, so no shell navigation instruction either.
        assert "cd to" not in lower
        assert "navigate to" not in lower


async def test_code_handler_forbids_phantom_success() -> None:
    """The agent reports only what `code_mode` confirmed — a tool error is
    surfaced plainly, never dressed up as a completed change."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))
    lower = preamble.lower()

    assert "error" in lower
    assert "never describe a change as made" in lower


# --- Profiles (ripple-OFF on both) ---


def test_studio_profile_ripple_off_media_tools_and_skill() -> None:
    """The /studio profile turns ripple OFF (so the agent generates media, not a
    ui-spec dashboard), scopes to the media MCP tools, and surfaces the studio
    skill."""
    from pocketpaw_ee.agent.mcp_servers.media import (
        IMAGE_GENERATE_TOOL_ID,
        VIDEO_GENERATE_TOOL_ID,
    )

    profile = resolve_profile(SurfaceKind.STUDIO, SurfaceMeta())
    assert profile.ripple_mode == "off"
    assert "studio" in profile.skill_names
    assert profile.allow_mcp_tool_ids is not None
    assert IMAGE_GENERATE_TOOL_ID in profile.allow_mcp_tool_ids
    assert VIDEO_GENERATE_TOOL_ID in profile.allow_mcp_tool_ids


def test_code_profile_ripple_off_and_scoped_to_code_mode() -> None:
    """The /code profile turns ripple OFF (so the agent edits code, not a
    dashboard) and scopes the MCP surface to exactly the ``code_mode`` tool."""
    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())

    assert profile.ripple_mode == "off"
    assert profile.allow_mcp_tool_ids == frozenset({"mcp__pocketpaw_code__code_mode"})


def test_code_profile_surfaces_no_skill() -> None:
    """/code carries NO skill.

    It used to carry `code`, whose body is entirely about the built-ins this
    profile now denies. Under the deny that is an injected instruction to call
    tools the agent does not have — it attempts them, takes hard errors, and
    burns turns before falling back to the path the preamble gave it. The skill's
    edit→run→verify discipline is retargeted onto ``code_mode`` in CD-2."""
    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())

    assert profile.skill_names == frozenset()


def test_code_profile_denies_the_filesystem_builtins_and_subagents() -> None:
    """The deny set names every file/shell built-in, plus ``Agent``.

    ``allowed_sdk_tools`` cannot express this — it is ADDITIVE
    (``effective = (agent_tools ∪ allow) − deny``) and the built-ins are in the
    SDK's default set already, so listing them there restricted nothing.
    ``allow_mcp_tool_ids`` cannot either: it filters only ``mcp__*`` ids. Deny is
    the only lever. ``Agent`` is in the set because a spawned subagent is a
    second path to tools that this profile does not supervise.

    Membership only — ``test_code_profile_grants_no_filesystem_tool_in_the_
    effective_allowlist`` below is the one that proves the effect."""
    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())

    assert _FILESYSTEM_BUILTINS <= profile.deny_mcp_tool_ids
    assert "Agent" in profile.deny_mcp_tool_ids
    # Not smuggled back in through the additive allow-list either.
    assert not (profile.allowed_sdk_tools or frozenset()) & _FILESYSTEM_BUILTINS
    # Research tools survive — neither reaches a filesystem.
    assert not {"WebSearch", "WebFetch", "Skill"} & profile.deny_mcp_tool_ids


async def test_code_profile_grants_no_filesystem_tool_in_the_effective_allowlist() -> None:
    """THE test: drive the REAL allowlist computation and assert the built-ins
    are absent from what the SDK would actually launch with.

    A membership check on ``deny_mcp_tool_ids`` is not enough — the bug this task
    fixes is precisely one a membership-only test passes. The old profile listed
    all six built-ins in ``allowed_sdk_tools`` and any "are they named in the
    profile?" assertion went green, while the effective allowlist still carried
    them because that field is additive. So this test asks the question that
    matters: feed the resolved CODE profile through
    ``ClaudeSDKBackend._build_options`` (pure assembly — no client, no network,
    the same call ``run`` and ``prewarm`` make) and read ``allowed_tools`` off
    the options the SDK would receive.

    It guards TWO wrong-machine paths, because the surface had two. The
    file/shell built-ins are one. The ``mcp__pocketpaw_daytona__*`` family is the
    other — ``shell`` / ``run_python`` / ``read_file`` / ``write_file`` /
    ``edit_file`` / ``sync_to_s3`` all reach the SUPERSEDED cloud-projects
    sandbox, which knows nothing of the ``codeproject`` + ``CodeFileSession``
    runtime the user's project actually lives in. ``allow_mcp_tool_ids`` closes
    that path (``pocketpaw_daytona`` is not in ``ALWAYS_ALLOWED_MCP_SERVERS``),
    and the scan below is what pins it. Deliberately a PREFIX scan, not a list of
    the eight tool names known today, so a daytona tool added later is caught
    without anyone remembering to update this test.

    Note we assert only ABSENCE. ``code_mode`` itself will not appear here until
    CD-2 lands its MCP server: ``allow_mcp_tool_ids`` is a FILTER over the tools
    that exist, not a grant that conjures one. Its presence is pinned at the
    profile level in ``test_code_profile_ripple_off_and_scoped_to_code_mode``."""
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.config import get_settings

    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())
    backend = ClaudeSDKBackend(get_settings())

    built = await backend._build_options(
        "refactor this",
        system_prompt="you are on the code surface",
        history=None,
        session_key=None,
        deny_mcp_tool_ids=profile.deny_mcp_tool_ids,
        allow_sdk_tools=profile.allowed_sdk_tools or frozenset(),
        allow_mcp_tool_ids=profile.allow_mcp_tool_ids,
        skill_names=profile.skill_names,
        stderr_sink=[],
    )
    effective = set(built.options_kwargs["allowed_tools"])

    for tool in sorted(_FILESYSTEM_BUILTINS):
        assert tool not in effective, (
            f"{tool} reached the SDK's effective allowlist on /code — the agent "
            f"would use it against the BACKEND SERVER's filesystem, not the "
            f"user's project. Effective allowlist: {sorted(effective)}"
        )
    assert "Agent" not in effective, (
        "Agent reached the effective allowlist — a spawned subagent is an "
        "unsupervised second path to the tools denied above"
    )

    # The SECOND wrong-machine path: the Daytona sandbox tools. Prefix scan, so a
    # daytona tool added after this test was written is caught automatically.
    leaked_daytona = sorted(t for t in effective if t.startswith("mcp__pocketpaw_daytona__"))
    assert not leaked_daytona, (
        f"Daytona tools reached the effective allowlist on /code: {leaked_daytona}. "
        f"These operate on the SUPERSEDED cloud-projects sandbox, not the user's "
        f"project — the agent would edit a machine that does not hold their code "
        f"and report success. The user's project is reachable only via code_mode."
    )

    # Positive controls, so none of the assertions above can pass vacuously.
    # WebSearch proves the built-in path ran and was not emptied wholesale.
    assert "WebSearch" in effective
    # And at least one MCP id must survive, or a future change that nulls out MCP
    # resolution entirely would make every `not in` above trivially true while the
    # surface silently loses code_mode too. Not pinned to a specific server — any
    # surviving mcp__ id proves the MCP filter ran on a populated list.
    assert any(t.startswith("mcp__") for t in effective), (
        "no mcp__ tool survived the allow-list filter — MCP resolution produced "
        "nothing, so the absence assertions above prove nothing"
    )


def test_code_profile_is_static_and_meta_independent() -> None:
    """The /code profile does not vary with the client's storage hints.

    The old handler branched on ``workspace_vm`` / ``is_cloud_storage`` /
    ``current_dir``; the surface no longer has storage flavours, so a legacy
    client stamping them must not shift the tool policy."""
    plain = resolve_profile(SurfaceKind.CODE, SurfaceMeta())
    legacy = resolve_profile(SurfaceKind.CODE, _LEGACY_STORAGE_META)

    assert plain == legacy


def test_surface_registry_import_assertion_still_passes() -> None:
    """The module-import completeness assertion survives the CODE row rewrite —
    every ``SurfaceKind`` still has exactly one spec, and every spec a real
    kind."""
    from pocketpaw_ee.cloud.surface.surface_registry import (
        SURFACES,
        _assert_registry_complete,
    )

    _assert_registry_complete()  # raises if the registry drifted
    code_specs = [spec for spec in SURFACES if spec.kind is SurfaceKind.CODE]
    assert len(code_specs) == 1
    # CODE stays STATIC — a literal profile, not a resolver.
    assert code_specs[0].profile is not None
    assert code_specs[0].profile_resolver is None


def test_studio_and_code_kinds_map_from_wire_strings() -> None:
    """The wire ``surface`` strings 'studio' / 'code' resolve to the new kinds
    (not the GENERIC fallback)."""
    from pocketpaw_ee.cloud.surface.service import _resolve_kind

    assert _resolve_kind("studio") is SurfaceKind.STUDIO
    assert _resolve_kind("code") is SurfaceKind.CODE

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
# through the file tools (readFile / search / listDir / writeFile — originally a
# single ``code_mode`` tool, replaced 2026-07-24). The replacements assert the
# inverse: the resolved profile DENIES every file/shell built-in (plus
# ``Agent``), allows exactly those file tools, and surfaces NO skill; the preamble
# leaks
# no path and names no Daytona MCP tool — including when a legacy client still
# stamps the old ``current_dir`` / ``storage_root`` / ``workspace_vm`` /
# ``is_cloud_storage`` hints. The registry-import assertion is exercised too,
# since the profile now leans on two module-level literals.
#
# Changes: 2026-07-22 (fix/code-surface-denies-pocket-authoring) — added the
# pocket-authoring guards. CD-3's tests proved the /code agent could not reach the
# wrong MACHINE; nothing proved it could not build the wrong DELIVERABLE, and it
# did: "Let's build an employee management app, with components, nice design etc"
# produced a pocket and a ripple ui-spec on a surface holding a React project.
# ``test_code_profile_builds_no_pocket_in_the_effective_allowlist`` is the repro,
# driven through the same real ``_build_options`` for the same reason — the
# bypasses that caused the bug (``POCKET_CREATION_GRANT``, ``WIDGET_TOOL_IDS``,
# ``ALWAYS_ALLOWED_MCP_SERVERS``) live in the COMPUTATION, so a profile-membership
# assertion would have gone green while the agent still held the tools.
# ``test_code_deny_covers_every_widget_tool_and_the_pocket_creation_grant`` pins
# the deny literals against those real constants so a tool added to either family
# later reopens the hole loudly instead of silently.
#
# The load-bearing one is
# ``test_code_profile_grants_no_filesystem_tool_in_the_effective_allowlist``: it
# drives the REAL ``ClaudeSDKBackend._build_options`` and reads the allowlist the
# SDK would launch with. A membership check on ``deny_mcp_tool_ids`` alone would
# have gone green against the OLD profile too — that one named all six built-ins
# in the additive ``allowed_sdk_tools`` and shipped them anyway — so the negative
# has to be asserted against the COMPUTED result, not the declaration.
#
# Changes: 2026-07-24 (feat/code-surface-cleanup, CX-4) — dropped the two
# pocket-authoring guards (``test_code_profile_builds_no_pocket_in_the_effective_
# allowlist`` and ``test_code_deny_covers_every_widget_tool_and_the_pocket_
# creation_grant``) and the ``_POCKET_AUTHORING_TOOLS`` literal they read. The
# surface profile's ``_CODE_POCKET_DENY`` MCP deny-list they pinned was removed:
# MCP tool restriction for /code is now enforced structurally by the dedicated
# ``code`` agent's ``tool_mode="exclusive"`` policy, and the pocket repro dying at
# the allowlist level is proven in ``tests/cloud/agents/test_code_agent_seed.py``.
# The BUILT-IN denies this surface still owns (file/shell + ``Agent``, and
# ``Skill``) — which the MCP cap cannot reach — stay guarded here by
# ``test_code_profile_denies_the_filesystem_builtins_and_subagents``,
# ``test_code_profile_grants_no_filesystem_tool_in_the_effective_allowlist``, and
# ``test_code_profile_denies_the_skill_tool``.

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


async def test_code_handler_routes_all_work_through_the_file_tools() -> None:
    """The file tools are named as the ONLY route to the user's project."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))

    for tool in ("readFile", "search", "listDir", "writeFile"):
        assert tool in preamble, f"the preamble must name the {tool} tool"
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


async def test_code_handler_acts_immediately_on_a_selection() -> None:
    """An edit scoped to a selection the user already made is acted on at once —
    no re-reading the project first (the selection and its file are already in
    context, so a redundant round-trip is a wasted wait)."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))
    lower = preamble.lower()

    assert "selection" in lower
    assert "immediately" in lower
    assert "re-read" in lower


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


async def test_code_handler_reads_build_an_app_as_code() -> None:
    """The preamble re-points the vocabulary that caused the bug.

    A blanket "do not create a pocket" was already present and still lost to a
    request whose every noun ("app", "components", "design") matches the
    create-pocket skill. So the prose has to claim those words for the front-end
    reading, not just forbid the outcome."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))
    lower = preamble.lower()

    assert "component" in lower
    # The words are named and reassigned, not merely banned.
    assert "ui-spec" in lower or "ui spec" in lower
    assert "withheld" in lower or "do not reach for a skill" in lower


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


def test_code_profile_ripple_off_and_scoped_to_the_file_tools() -> None:
    """The /code profile turns ripple OFF (so the agent edits code, not a
    dashboard) and scopes the MCP surface to exactly the four file tools the main
    agent drives."""
    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())

    assert profile.ripple_mode == "off"
    assert profile.allow_mcp_tool_ids == frozenset(
        {
            "mcp__pocketpaw_code__readFile",
            "mcp__pocketpaw_code__search",
            "mcp__pocketpaw_code__listDir",
            "mcp__pocketpaw_code__writeFile",
        }
    )


def test_code_profile_denies_the_skill_tool() -> None:
    """``Skill`` is denied, which is the only way to withhold create-pocket.

    The bundled skills load as a Claude Code LOCAL PLUGIN, from the SDK
    ``plugins=`` option — independent of ``skill_names``. So an empty
    ``skill_names`` (what CD-3 set) does NOT keep ``pocketpaw-create-pocket``
    away from this surface, and that skill's description matches "build an app
    with components and nice design" almost word for word. Denying the tool that
    invokes skills is the lever that actually exists."""
    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())

    assert "Skill" in profile.deny_mcp_tool_ids
    # Research still works — neither reaches a filesystem or builds a pocket.
    assert not {"WebSearch", "WebFetch"} & profile.deny_mcp_tool_ids


def test_code_profile_carries_its_own_system_prompt() -> None:
    """/code replaces the shared deliverable stack with its own system prompt.

    The deny set makes a pocket unreachable; this makes CODE the default instead
    of merely the permitted option. Both were needed — ``ripple_mode="off"`` plus
    a preamble forbidding pockets still lost, because a prohibition does not
    create a default."""
    from pocketpaw_ee.cloud.surface.system_prompts import CODE_SYSTEM_PROMPT

    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())

    assert profile.system_message_override == CODE_SYSTEM_PROMPT
    # It has to say what the surface DOES build, not only what it must not — and
    # name the tools it actually grants.
    for tool in ("readFile", "search", "listDir", "writeFile"):
        assert tool in CODE_SYSTEM_PROMPT
    assert "WORKING CODE" in CODE_SYSTEM_PROMPT
    # And it must not promise a tool the profile denies.
    for denied in ("deliver_artifact", "get_widget_spec", "pocket_specialist"):
        assert denied not in CODE_SYSTEM_PROMPT, (
            f"the /code system prompt names {denied}, which this surface's "
            f"profile withholds — a prompt that promises a denied tool is the "
            f"contradiction this override exists to remove"
        )


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
    #
    # ``Skill`` was in this list until 2026-07-22 on the same reasoning. It is
    # now denied (see ``test_code_profile_denies_the_skill_tool``): the bundled
    # ``pocketpaw-create-pocket`` skill loads as a local plugin regardless of
    # ``skill_names``, so this is the only lever that withholds it, and its
    # description is a near-exact match for the request that triggered the bug.
    assert not {"WebSearch", "WebFetch"} & profile.deny_mcp_tool_ids


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

    Note this test asserts only ABSENCE — the file tools' PRESENCE is pinned in
    ``test_file_tools_reach_the_effective_allowlist`` (in test_code_mcp_server)
    and at the profile level in
    ``test_code_profile_ripple_off_and_scoped_to_the_file_tools``."""
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
    # surface silently loses its file tools too. Not pinned to a specific server —
    # any surviving mcp__ id proves the MCP filter ran on a populated list.
    assert any(t.startswith("mcp__") for t in effective), (
        "no mcp__ tool survived the allow-list filter — MCP resolution produced "
        "nothing, so the absence assertions above prove nothing"
    )


# NOTE (2026-07-24, CX-4): the "/code must not BUILD A POCKET" guarantee no longer
# lives at the SURFACE level and so is no longer asserted here. It used to be
# enforced by the surface profile's ``_CODE_POCKET_DENY`` MCP deny-list, which this
# file pinned via ``test_code_profile_builds_no_pocket_in_the_effective_allowlist``
# and ``test_code_deny_covers_every_widget_tool_and_the_pocket_creation_grant``.
# Both were REMOVED with the deny-list: MCP tool restriction for /code is now
# enforced structurally by the dedicated ``code`` agent's exclusive tool policy
# (``tool_mode="exclusive"`` caps the run's ``mcp__*`` surface to exactly the four
# file ids). The pocket repro dying at the allowlist level is now proven end to end
# in ``tests/cloud/agents/test_code_agent_seed.py::
# test_seeded_code_agent_config_drives_exclusive_allowlist``. What THIS surface
# profile still owns — and still tests below — is the BUILT-IN tools the MCP cap
# cannot reach: the file/shell built-ins + ``Agent`` (``_CODE_BUILTIN_DENY``) and
# ``Skill`` (``_CODE_SKILL_DENY``).


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

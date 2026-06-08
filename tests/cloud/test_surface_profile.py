# test_surface_profile.py — SurfaceProfile descriptor + resolve_profile resolver.
#
# Created: 2026-06-05 (feat/surface-profile-bias-kill) — RED-first tests for the
# new typed SurfaceProfile primitive and its per-kind resolver, the data
# backbone of the "ripple-default bias" fix.
#
# Modified: 2026-06-05 (feat/sites-svelte-engine) — RED-first tests for making
# ``resolve_profile`` META-AWARE on the /sites row. The static table from PR 1
# turned ripple OFF for /sites UNCONDITIONALLY, but /sites has THREE modes and
# only ONE should lose ripple:
#   * create + svelte  (meta.engine == "svelte", no pocket_id)  → ripple OFF,
#     deny the two ripple-create tools, surface the create-svelte-site skill.
#   * create + ripple  (meta.engine None/"ripple", no pocket_id) → ripple ON,
#     NO deny — this is the default marketing brain that AUTHORS a ripple page.
#   * refine           (meta.pocket_id set, any engine)          → ripple ON,
#     NO deny — edits the existing ripple landing spec via pocket_specialist__edit.
# refine WINS over engine (a pocket_id present means refine even if engine=="svelte").
# The svelte-create assertions below stay GREEN (the static row already matched
# that case); the ripple-create + refine assertions are the RED drivers — they
# expect ``ripple_mode="on"`` / empty deny but the static table returns "off"
# with the deny set for every /sites meta today.

from __future__ import annotations

from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, SurfaceProfile, resolve_profile


def test_sites_profile_turns_ripple_off():
    """The /sites SVELTE-CREATE surface hand-authors a Svelte Paw Site — the
    ripple LAW is wrong there, so its profile turns ripple off.

    NOTE (feat/sites-svelte-engine): this asserts the SVELTE-create mode only.
    The original PR-1 version passed a bare ``SurfaceMeta()`` (which now resolves
    to the ripple-CREATE mode that KEEPS ripple), so it was the over-reach that
    turned ripple off for ALL /sites. It is now pinned to ``engine="svelte"`` so
    it stays a valid svelte-omit assertion and does not contradict
    ``test_resolve_profile_sites_ripple_create_keeps_ripple``."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="svelte"))
    assert isinstance(profile, SurfaceProfile)
    assert profile.ripple_mode == "off"


def test_sites_profile_declares_deny_set_and_skill():
    """The SVELTE-create /sites profile denies the ripple-authoring MCP tools
    and surfaces the svelte-site skill so the agent is physically unable to
    fall back to a ripple landing page.

    NOTE (feat/sites-svelte-engine): pinned to ``engine="svelte"`` for the same
    reason as ``test_sites_profile_turns_ripple_off`` — the deny set + skill
    belong to the svelte-create mode, not to every /sites meta."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="svelte"))
    assert profile.deny_mcp_tool_ids == frozenset(
        {
            "mcp__pocketpaw_sites_manager__create_landing_site",
            "mcp__pocketpaw_pocket_specialist__create",
        }
    )
    assert "create-svelte-site" in profile.skill_names


def test_generic_profile_keeps_ripple_on():
    """The catch-all GENERIC surface keeps ripple on — it's an ordinary chat
    surface where the widget LAW is correct."""
    profile = resolve_profile(SurfaceKind.GENERIC, SurfaceMeta())
    assert profile.ripple_mode == "on"


def test_unmapped_kind_defaults_to_ripple_on():
    """Any kind NOT in the table (e.g. CALENDAR) falls through to the safe
    default — ripple on. This is the zero-regression guarantee: every surface
    except /sites behaves exactly as it does today."""
    profile = resolve_profile(SurfaceKind.CALENDAR, SurfaceMeta())
    assert profile.ripple_mode == "on"


def test_default_profile_has_no_denies_or_skills():
    """The default (ripple-on) profile carries empty deny / skill sets and no
    SDK-tool allowlist — it imposes no surface-specific policy."""
    profile = resolve_profile(SurfaceKind.POCKETS_LIST, SurfaceMeta())
    assert profile.ripple_mode == "on"
    assert profile.deny_mcp_tool_ids == frozenset()
    assert profile.skill_names == frozenset()
    assert profile.allowed_sdk_tools is None
    assert profile.system_message_override is None


# ---------------------------------------------------------------------------
# /sites is META-AWARE: only the svelte-create mode loses ripple.
# (feat/sites-svelte-engine)
#
# The PR-1 static row turned ripple OFF for /sites for EVERY meta. That is
# wrong: /sites carries three modes, distinguished by ``meta``:
#   * svelte create  — engine="svelte", no pocket_id  → ripple OFF + deny set.
#   * ripple create  — engine None/"ripple", no pocket_id → ripple ON, no deny.
#   * refine         — pocket_id set (any engine)      → ripple ON, no deny.
# ``resolve_profile`` must read ``meta`` to tell them apart, with refine winning
# over engine (pocket_id present ⇒ refine even if engine=="svelte").
# ---------------------------------------------------------------------------


_RIPPLE_CREATE_DENY = frozenset(
    {
        "mcp__pocketpaw_sites_manager__create_landing_site",
        "mcp__pocketpaw_pocket_specialist__create",
    }
)


def test_resolve_profile_sites_svelte_create_disables_ripple():
    """GUARD (passes today): the svelte-CREATE mode (engine="svelte", no
    pocket_id) hand-authors SvelteKit, so ripple is OFF, the two ripple-create
    tools are denied, and the create-svelte-site skill is surfaced. This is the
    ONLY /sites mode that loses ripple."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="svelte"))
    assert profile.ripple_mode == "off"
    assert profile.deny_mcp_tool_ids == _RIPPLE_CREATE_DENY
    assert "create-svelte-site" in profile.skill_names


def test_resolve_profile_sites_ripple_create_keeps_ripple():
    """RED DRIVER (fails today): the ripple-CREATE mode AUTHORS a ripple
    marketing landing page, so it KEEPS ripple on and denies NOTHING. Both the
    default (``engine=None``) and the explicit ``engine="ripple"`` resolve the
    same way. The PR-1 static table wrongly returns ``ripple_mode="off"`` with
    the deny set for these."""
    for meta in (SurfaceMeta(engine=None), SurfaceMeta(engine="ripple")):
        profile = resolve_profile(SurfaceKind.SITES, meta)
        assert profile.ripple_mode == "on", f"ripple-create meta {meta!r} must keep ripple"
        assert profile.deny_mcp_tool_ids == frozenset(), (
            f"ripple-create meta {meta!r} must deny nothing — it authors a ripple page"
        )


def test_resolve_profile_sites_refine_keeps_ripple():
    """RED DRIVER (fails today): the REFINE mode (pocket_id set) edits the
    existing RIPPLE landing spec via pocket_specialist__edit, so it KEEPS ripple
    on and denies NOTHING. Refine WINS over engine — a pocket_id present means
    refine even when ``engine="svelte"`` is also stamped (a published site being
    re-opened). The PR-1 static table wrongly returns ``ripple_mode="off"`` with
    the deny set here too."""
    for meta in (
        SurfaceMeta(pocket_id="pkt_1"),
        SurfaceMeta(pocket_id="pkt_1", engine="svelte"),  # refine wins over engine
    ):
        profile = resolve_profile(SurfaceKind.SITES, meta)
        assert profile.ripple_mode == "on", f"refine meta {meta!r} must keep ripple"
        assert profile.deny_mcp_tool_ids == frozenset(), (
            f"refine meta {meta!r} must deny nothing — it edits an existing ripple spec"
        )


# ---------------------------------------------------------------------------
# Per-mode restrictive MCP allow-list (feat/per-mode-mcp-allowlist). Foresight /
# Files / Sites carry a lean allow_mcp_tool_ids; Chat stays unrestricted. The
# "general everywhere" set (widgets, pocket lifecycle, connectors) is enforced
# by the OSS backend, so these tests only pin the per-mode SPECIALIZED scoping.
# ---------------------------------------------------------------------------


def test_chat_profile_has_no_mcp_restriction():
    assert resolve_profile(SurfaceKind.CHAT, SurfaceMeta()).allow_mcp_tool_ids is None


def test_foresight_profile_scopes_to_foresight_mcp_tools():
    from pocketpaw_ee.agent.mcp_servers.foresight import RUN_SCENARIO_TOOL_ID

    allow = resolve_profile(SurfaceKind.FORESIGHT, SurfaceMeta()).allow_mcp_tool_ids
    assert allow is not None
    assert RUN_SCENARIO_TOOL_ID in allow
    assert "mcp__pocketpaw_tasks__create_task" not in allow


def test_files_profile_is_general_only():
    # Empty allow-list = general-everywhere only (document scaffolding rides
    # the built-in tools, which the allow-list never touches).
    assert resolve_profile(SurfaceKind.FILES, SurfaceMeta()).allow_mcp_tool_ids == frozenset()


def test_sites_profiles_scope_to_sites_mcp_tools():
    from pocketpaw_ee.agent.mcp_servers.sites import CREATE_SVELTE_SITE_TOOL_ID

    ripple_create = resolve_profile(SurfaceKind.SITES, SurfaceMeta())
    assert ripple_create.allow_mcp_tool_ids is not None
    assert CREATE_SVELTE_SITE_TOOL_ID in ripple_create.allow_mcp_tool_ids

    svelte_create = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="svelte"))
    assert svelte_create.allow_mcp_tool_ids is not None
    assert CREATE_SVELTE_SITE_TOOL_ID in svelte_create.allow_mcp_tool_ids
    # deny still applies on top (runs after allow downstream).
    assert "mcp__pocketpaw_sites_manager__create_landing_site" in svelte_create.deny_mcp_tool_ids

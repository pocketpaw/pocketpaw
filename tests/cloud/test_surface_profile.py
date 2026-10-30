# test_surface_profile.py — SurfaceProfile descriptor + resolve_profile resolver.
#
# /sites is meta-aware: a NEW-site create (no pocket, no site id, no brief) is
# always a project, so it drops ripple, denies the old per-engine authoring tools
# and loads the ``pocketpaw-site`` skill whatever create-time engine hint it has;
# a REFINE (pocket set) keeps ripple only for a ripple site or an unknown engine
# and names its own per-engine skill allowlist.
# Every other kind falls through to the ripple-on default.

from __future__ import annotations

from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, SurfaceProfile, resolve_profile


# Every non-BROWSER surface now carries the agentic-browser tool ids in its deny
# set (BR-1, ``service._deny_browser_off_surface``). The assertions below are
# about each SURFACE'S OWN denies, so strip that always-on floor rather than
# restating it in every expected set — and rather than narrowing the deny, which
# is the boundary /chat depends on. ``test_browser_surface.py`` asserts the floor
# itself, for every kind.
def _own_deny(profile) -> frozenset[str]:
    from pocketpaw_ee.agent.mcp_servers.browser import BROWSER_TOOL_IDS

    return profile.deny_mcp_tool_ids - frozenset(BROWSER_TOOL_IDS)


def test_sites_profile_turns_ripple_off():
    """A new-site create hand-authors files, so its profile turns ripple off for
    every create-time engine hint, ripple included."""
    for engine in (None, "html", "ripple", "svelte", "react", "project"):
        profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine=engine))
        assert isinstance(profile, SurfaceProfile)
        assert profile.ripple_mode == "off", engine


def test_sites_profile_declares_deny_set_and_skill():
    """The create profile denies the old per-engine authoring tools and surfaces the
    one platform skill, so the agent cannot fall back to an old create track."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="svelte"))
    assert _own_deny(profile) == _OLD_AUTHORING_DENY | _SITES_BUILTIN_DENY
    assert "pocketpaw-site" in profile.skill_names
    assert "pocketpaw-create-svelte-site" not in profile.skill_names


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
    """The default (ripple-on) profile carries no denies OF ITS OWN, empty skill
    sets and no SDK-tool allowlist — it imposes no surface-specific policy.

    Since BR-1 every non-BROWSER profile also carries the always-on browser deny
    floor; ``_own_deny`` strips it so this stays an assertion about the default
    profile rather than about that floor."""
    profile = resolve_profile(SurfaceKind.POCKETS_LIST, SurfaceMeta())
    assert profile.ripple_mode == "on"
    assert _own_deny(profile) == frozenset()
    assert profile.skill_names == frozenset()
    assert profile.allowed_sdk_tools is None
    assert profile.system_message_override is None


# ---------------------------------------------------------------------------
# /sites is META-AWARE: create (any engine hint) vs refine (the site's own engine).
# ---------------------------------------------------------------------------


_OLD_AUTHORING_DENY = frozenset(
    {
        "mcp__pocketpaw_sites_manager__create_landing_site",
        "mcp__pocketpaw_sites_manager__create_dynamic_site",
        "mcp__pocketpaw_sites_manager__create_html_site",
        "mcp__pocketpaw_sites_manager__create_svelte_site",
        "mcp__pocketpaw_sites_manager__create_react_site",
        "mcp__pocketpaw_sites_manager__edit_html_file",
        "mcp__pocketpaw_sites_manager__edit_svelte_component",
        "mcp__pocketpaw_sites_manager__edit_react_component",
        "mcp__pocketpaw_pocket_specialist__create",
    }
)

# Every /sites mode also drops the file/shell built-ins (a dedicated sites agent
# authors through MCP tools, never the file system). Mirrors
# surface_registry._SITES_BUILTIN_DENY.
_SITES_BUILTIN_DENY = frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent"})


def test_resolve_profile_sites_svelte_create_disables_ripple():
    """A svelte hint on a new site only picks the sveltekit template in the
    preamble; the profile is the shared create profile."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="svelte"))
    assert profile.ripple_mode == "off"
    assert _own_deny(profile) == _OLD_AUTHORING_DENY | _SITES_BUILTIN_DENY


def test_resolve_profile_sites_ripple_create_no_longer_keeps_ripple():
    """Ripple is out for new sites: a ripple hint maps to the static template and
    gets the same ripple-off create profile."""
    profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="ripple"))
    assert profile.ripple_mode == "off"
    assert _own_deny(profile) == _OLD_AUTHORING_DENY | _SITES_BUILTIN_DENY


def test_resolve_profile_sites_refine_keeps_ripple():
    """A REFINE (pocket_id set) of a ripple site, or one whose engine is unknown, edits
    the RIPPLE landing spec via pocket_specialist__edit, so it KEEPS ripple on and
    denies only the file/shell built-ins. On refine ``engine`` is the source pocket's
    (``run_core._sites_refine_meta``)."""
    for meta in (
        SurfaceMeta(pocket_id="pkt_1"),
        SurfaceMeta(pocket_id="pkt_1", engine="ripple"),
    ):
        profile = resolve_profile(SurfaceKind.SITES, meta)
        assert profile.ripple_mode == "on", f"refine meta {meta!r} must keep ripple"
        assert _own_deny(profile) == _SITES_BUILTIN_DENY, (
            f"refine meta {meta!r} keeps the ripple/pocket tools, drops only the built-ins"
        )


def test_resolve_profile_sites_source_refine_drops_ripple():
    """A refine of an html / svelte / react site edits a source map, not a widget spec,
    so it runs without the inline ripple prompt (asking via ask_user), denies the same
    built-ins, and is NOT the create branch (no create deny, and its own skill
    allowlist, which never includes an old create brain)."""
    for engine in ("html", "svelte", "react"):
        profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(pocket_id="pkt_1", engine=engine))
        assert profile.ripple_mode == "off", engine
        assert _own_deny(profile) == _SITES_BUILTIN_DENY, engine
        assert "pocketpaw-create-paw-site" not in profile.skill_names, engine
        assert "pocketpaw-create-react-site" not in profile.skill_names, engine


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

# tests/ee/agent/test_sites_mcp_server/test_site_skill_contract.py — the platform
# contract every NEW site is built on.
#
# Pins four things the one-site model depends on:
#   * the ``pocketpaw-site`` bundled skill stays under its byte cap and carries the
#     loop, the template rule, the pointer to ``sites-app-plan`` for apps and games,
#     and the integrations/secrets contract;
#   * the ``sites-app-plan`` bundled skill exists with valid frontmatter, stays
#     under 500 lines, and carries the plan, the user check, the milestones and the
#     3D/game quality rules;
#   * ``connect_site_integration``'s per-provider wiring (``_INTEGRATION_NEXT_STEPS``)
#     has a line for EVERY registered provider, naming each public action the
#     provider really exposes, in client (paw.js) and server (``pawCall``) form;
#   * the project tools gate on the same Sites plan the old create tools use, so the
#     ``static`` starter is open to everyone who could create a site before, and the
#     refusal tells the agent to stop rather than hand the site over another way.
from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

#: The skill is loaded on every new-site turn; keep it well under the old project
#: skill's 6,000-byte cap. Raising it needs a reason in the commit body (5,200: room
#: for the app-plan pointer and the preview_site parameters).
SITE_SKILL_MAX_BYTES = 5_200


def _site_skill():
    from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

    root = bundled_skills_plugin_dir()
    assert root is not None, "the bundled skills plugin is missing"
    return root / "skills" / "pocketpaw-site" / "SKILL.md"


def test_the_site_skill_stays_under_its_byte_cap() -> None:
    path = _site_skill()
    size = path.stat().st_size
    assert size > 2_000, f"{path} is {size} bytes: a broken read, not a lean skill"
    assert size <= SITE_SKILL_MAX_BYTES, (
        f"pocketpaw-site is {size} bytes, over its {SITE_SKILL_MAX_BYTES}-byte cap. "
        "Per-provider detail belongs in _INTEGRATION_NEXT_STEPS, framework detail in "
        "the template's AGENTS.md."
    )


def test_the_site_skill_carries_the_platform_contract() -> None:
    text = _site_skill().read_text("utf-8")
    assert text.startswith("---\nname: pocketpaw-site\n")
    # The loop, in order of use.
    for tool in (
        "list_site_templates",
        "start_site_from_template",
        "list_site_recipes",
        "apply_site_recipe",
        "connect_site_integration",
        "request_site_secret",
        "patch_site_file",
        "write_site_files",
        "run_site_build",
        "get_site_build_log",
        "verify_site",
        "publish",
    ):
        assert tool in text, tool
    assert "ONCE" in text and "AGENTS.md" in text
    # The template rule: static for content pages only, a framework for apps,
    # npm packages, server needs, or when the user named one.
    assert "`static` only for content pages" in text
    assert "server logic" in text and "named the framework" in text
    # Integrations, written once for client and server.
    for phrase in (
        "data-paw-checkout",
        "data-paw-subscribe",
        "Paw.call(provider, action, args)",
        "paw-integrations",
        "pawCall(env, provider, action, args)",
        "PAW_GATEWAY_TOKEN",
    ):
        assert phrase in text, phrase
    # Secrets, plans, and where framework knowledge comes from.
    assert "Never write a secret value into any file" in text
    assert "plan refusal" in text
    assert "search the web" in text
    # No old per-engine create tool is named.
    for old in (
        "create_html_site",
        "create_svelte_site",
        "create_react_site",
        "create_landing_site",
    ):
        assert old not in text, old


def test_the_site_skill_sends_client_heavy_apps_to_a_framework() -> None:
    """Regression: a complex browser 3D game (three.js / React Three Fiber) was
    built on the no-build `static` starter because the template rule only named
    SERVER needs. Apps and npm-package needs are framework triggers too."""
    text = _site_skill().read_text("utf-8")
    lower = text.lower()
    assert "no npm packages, no bundler" in lower
    for trigger in ("a game", "3d/webgl", "canvas", "simulation", "dashboard"):
        assert trigger in lower, trigger
    assert "needs npm packages" in lower
    assert "@react-three/fiber" in lower
    assert "prefer `vite-react-hono`" in text
    assert "when unsure between `static` and a framework" in lower
    assert "pick the framework" in lower
    assert "state the template you chose and a one-line reason" in lower


def test_the_site_skill_sends_apps_and_games_to_the_plan_skill() -> None:
    """An app or game is planned before it is built: the pointer sits right after
    the template rule, so it is read at the moment a framework is picked."""
    text = _site_skill().read_text("utf-8")
    pick = text.index("## Pick the template")
    apps = text.index("## Apps and games")
    loop = text.index("## The loop")
    assert pick < apps < loop
    section = " ".join(text[apps:loop].split())
    assert "`sites-app-plan`" in section
    assert "`PLAN.md`" in section
    assert "before any UI" in section
    assert "one milestone at a time" in section


def _app_plan_skill():
    return _site_skill().parent.parent / "sites-app-plan" / "SKILL.md"


def test_the_app_plan_skill_has_valid_frontmatter() -> None:
    import yaml

    text = _app_plan_skill().read_text("utf-8")
    assert text.startswith("---\nname: sites-app-plan\n")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)
    assert meta["name"] == "sites-app-plan"
    description = meta["description"].lower()
    for trigger in ("game", "3d", "webgl", "simulation", "editor", "tool", "dashboard"):
        assert trigger in description, trigger
    assert "multi-view" in description
    assert len(text.splitlines()) < 500
    assert body.strip()


def test_the_app_plan_skill_plans_before_it_builds() -> None:
    text = _app_plan_skill().read_text("utf-8")
    lower = text.lower()
    # The plan, before any UI file, with every section the brief needs.
    assert "PLAN.md" in text and "before you write a component" in lower
    for section in (
        "core loop",
        "v1 scope",
        "screens and states",
        "controls",
        "tech",
        "assets",
        "milestones",
    ):
        assert section in lower, section
    assert "never guess" in lower and "web search" in lower
    # The user check, then a stop.
    assert "mcp__pocketpaw_ask__ask_user" in text
    assert "Build this plan" in text and "Change the scope" in text
    assert "STOP" in text and "just build it" in text
    # The plan shows in chat as a card, so the summary stays short and the card current.
    assert "PLAN.md card" in text and "never paste the plan" in lower
    assert "3 to 5 line" in text
    # Milestones, each built and looked at.
    for milestone in ("**M1", "**M2", "**M3", "**M4"):
        assert milestone in text, milestone
    assert "greybox" in lower
    for tool in ("run_site_build", "preview_site", "patch_site_file", "list_site_assets"):
        assert tool in text, tool
    assert "`path`" in text and "`wait_ms`" in text
    assert "never call a milestone done without having seen it" in lower
    # 3D and game quality rules.
    assert "never hand-type vertex data" in lower
    for source in ("Kenney", "Quaternius", "Poly Pizza"):
        assert source in text, source
    assert "licence" in lower
    assert "flashlight" in lower and "fog" in lower
    assert "pixel ratio" in lower and "instance" in lower and "dispose" in lower
    assert "user gesture" in lower and "pointer lock" in lower
    assert "no single giant file" in lower
    # Non-game apps, and the shared rules referred to rather than restated.
    assert "apps that are not games" in lower
    assert "facts rule" in lower and "stored-data rule" in lower
    assert "localStorage" in text


def test_the_app_plan_skill_builds_every_milestone_after_approval() -> None:
    """Regression: after "Build this plan" the agent built M1, replied "stage 1 done"
    and waited. The skill let it ("if the turn runs long, stop at a milestone boundary")
    and its per-milestone "tell the user what to try" read as a turn-ending report.
    Once approved, every milestone is built back to back in one turn, with short
    progress lines and one final report; only a real blocker stops it."""
    text = _app_plan_skill().read_text("utf-8")
    lower = " ".join(text.lower().split())
    assert "stop at a milestone boundary" not in lower
    assert "every milestone back to back" in lower
    assert "a milestone boundary is not a reason to stop" in lower
    assert 'never ask "shall i continue?"' in lower
    assert "one final report" in lower
    assert "progress line" in lower
    # The only stops: a real blocker, or a finished plan.
    assert "real blocker" in lower
    for blocker in ("a secret", "a decision only the user can make", "a missing fact"):
        assert blocker in lower, blocker
    # The frontmatter and the checklist say the same thing.
    _, front, _ = text.split("---", 2)
    assert "one milestone at a time" not in " ".join(front.lower().split())


def _registered_providers():
    from pocketpaw_ee.integrations.providers import _PROVIDERS

    return list(_PROVIDERS)


def test_integration_next_steps_cover_every_provider() -> None:
    """Every provider in the registry gets a wiring line, and no line is orphaned."""
    from pocketpaw_ee.agent.mcp_servers.sites_create import _INTEGRATION_NEXT_STEPS

    ids = {p.id for p in _registered_providers()}
    assert ids, "the provider registry is empty"
    missing = sorted(ids - set(_INTEGRATION_NEXT_STEPS))
    assert not missing, f"providers with no wiring line: {missing}"
    orphaned = sorted(set(_INTEGRATION_NEXT_STEPS) - ids)
    assert not orphaned, f"wiring lines for providers that do not exist: {orphaned}"
    for provider_id, line in _INTEGRATION_NEXT_STEPS.items():
        assert isinstance(line, str) and len(line) > 40, provider_id


@pytest.mark.parametrize("provider", _registered_providers(), ids=lambda p: p.id)
def test_each_gateway_provider_line_names_its_real_actions(provider) -> None:
    """A line may not invent an action: every PUBLIC action the provider exposes is
    named, in the client form (paw.js) and the server form (``pawCall``)."""
    from pocketpaw_ee.agent.mcp_servers.sites_create import _INTEGRATION_NEXT_STEPS

    public = [name for name, spec in provider.actions.items() if spec.public]
    line = _INTEGRATION_NEXT_STEPS[provider.id]
    if not public:
        assert "pawCall" not in line, f"{provider.id} has no gateway actions"
        return
    for action in public:
        assert f"'{action}'" in line, f"{provider.id}: {action} is not named"
    assert "Paw.call(" in line or "data-paw-" in line, provider.id
    assert f"pawCall(env, '{provider.id}'" in line, provider.id
    assert "PAW_GATEWAY_TOKEN" in line, provider.id


async def test_the_project_tools_use_the_same_sites_plan_gate(monkeypatch) -> None:
    """The ``static`` starter must open for every workspace that can create a site
    today: ``_begin`` (every project tool) runs the exact gate the old create tools
    run, and nothing stricter."""
    from pocketpaw_ee.agent.mcp_servers import sites_create, sites_project
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.sites import service

    monkeypatch.setattr(sites_project, "_identity", lambda: ("ws1", "u1"))
    monkeypatch.setattr(sites_project, "record_tool_call", lambda **_kw: None)
    checked: list[str] = []

    async def _allowed(workspace_id: str) -> None:
        checked.append(workspace_id)

    monkeypatch.setattr(service, "require_sites_plan", _allowed)
    ws, user, err = await sites_project._begin("start_site_from_template")
    assert (ws, user, err) == ("ws1", "u1", None)
    assert checked == ["ws1"]

    async def _denied(_workspace_id: str) -> None:
        raise Forbidden("plan.feature_denied", "Sites requires the Go plan")

    monkeypatch.setattr(service, "require_sites_plan", _denied)
    _ws, _user, err = await sites_project._begin("start_site_from_template")
    assert err == await sites_create._require_sites_plan_or_error(
        "ws1", tool="start_site_from_template"
    )
    assert "plan.feature_denied" in err["content"][0]["text"]
    # Tool results can show in the activity feed, so the refusal is a plain sentence
    # for the user, says what the call did NOT do for its kind of tool, and carries
    # no agent instruction (that lives in the preamble's blocked rule).
    assert err["content"][0]["text"].endswith("Nothing was created.")
    for tool, outcome in (
        ("create_html_site", "Nothing was created."),
        ("edit_html_file", "Nothing was changed."),
        ("patch_site_file", "Nothing was changed."),
        ("read_site_source", None),
        ("list_site_secrets", None),
        ("verify_site", None),
        ("preview_site", None),
    ):
        text = (await sites_create._require_sites_plan_or_error("ws1", tool=tool))["content"][0][
            "text"
        ]
        assert "Sites requires the Go plan" in text, tool
        if outcome:
            assert text.endswith(outcome), (tool, text)
        else:
            assert "Nothing was" not in text, (tool, text)
        assert "Tell the user" not in text and "stop" not in text, (tool, text)


async def test_the_plan_refusal_names_the_real_upgrade_path(monkeypatch) -> None:
    """The service message is what the user ends up reading, so it names the place
    an upgrade actually happens."""
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.workspace import service as workspace_service
    from pocketpaw_ee.sites import service

    async def _free(_workspace_id: str) -> str:
        return "free"

    monkeypatch.setattr(workspace_service, "get_workspace_plan", _free)
    with pytest.raises(Forbidden) as exc:
        await service.require_sites_plan("ws1")
    assert "Settings → Billing" in exc.value.message


def test_the_site_skill_looks_after_every_build() -> None:
    """Every new site is a project, and an agent that never looked shipped UI bugs it
    assumed away. Looking is a step of the loop, not an optional extra."""
    text = _site_skill().read_text("utf-8")
    assert "After EVERY `built`" in text
    for phrase in ("preview_site", "device: mobile", "`path`", "`wait_ms`"):
        assert phrase in text, phrase
    assert "Never" in text and "not seen" in text

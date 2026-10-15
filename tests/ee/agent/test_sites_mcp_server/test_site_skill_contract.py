# tests/ee/agent/test_sites_mcp_server/test_site_skill_contract.py — the platform
# contract every NEW site is built on.
#
# Pins three things the one-site model depends on:
#   * the ``pocketpaw-site`` bundled skill stays under its byte cap and carries the
#     loop, the template rule and the integrations/secrets contract;
#   * ``connect_site_integration``'s per-provider wiring (``_INTEGRATION_NEXT_STEPS``)
#     has a line for EVERY registered provider, naming each public action the
#     provider really exposes, in client (paw.js) and server (``pawCall``) form;
#   * the project tools gate on the same Sites plan the old create tools use, so the
#     ``static`` starter is open to everyone who could create a site before.
from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

#: The skill is loaded on every new-site turn; keep it well under the old project
#: skill's 6,000-byte cap. Raising it needs a reason in the commit body.
SITE_SKILL_MAX_BYTES = 5_000


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
    # The template rule: static by default, a framework only when needed or named.
    assert "`static` is the default" in text
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
    assert err == await sites_create._require_sites_plan_or_error("ws1")
    assert "plan.feature_denied" in err["content"][0]["text"]

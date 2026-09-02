"""A proxy request has to say which workspace pays for it.

Created 2026-09-02 (feat/proxy-spend-by-workspace).

The bug these pin, stated once: both agent backends send
``settings.litellm_api_key`` — one key for the whole deployment — so the billing
cutover's per-tenant spend read (``/spend/logs?api_key=<the tenant's virtual
key>``) matched no chat row at all. In ``live`` mode, where per-run metering is
gated off so exactly one meter charges, chat therefore billed zero for everyone.
Production logged ``ingested spend for 3/3 tenants -> 0 credits`` against runs
the proxy had priced in real dollars, and nothing anywhere errored.

Nothing about a run's behaviour changes here. What changes is that the request
carries the workspace id, which is the only thing that later makes its cost
attributable. So these tests assert on the wire-bound value and on the two ways
it can silently go missing: sent for the wrong provider, or cached from another
tenant's run.
"""

from __future__ import annotations

import sys
import types

import pytest

from pocketpaw.agents import spend_attribution
from pocketpaw.agents.deep_agents import DeepAgentsBackend
from pocketpaw.agents.pydantic_ai import PydanticAIBackend
from pocketpaw.config import Settings

_EE_IDENTITY_MODULE = "pocketpaw_ee.cloud.chat.agent_service"


@pytest.fixture
def bound_workspace(monkeypatch):
    """Bind a workspace the way a cloud chat dispatch does, without needing EE.

    The backends import ``end_user_id_for`` at module scope, and it resolves
    ``current_workspace_id`` from this module's globals on every call, so one
    patch here reaches both backends.
    """

    def _bind(workspace: str | None) -> None:
        monkeypatch.setattr(spend_attribution, "current_workspace_id", lambda: workspace)

    return _bind


def _settings(**over) -> Settings:
    base = {
        "litellm_api_base": "http://example.local",
        "litellm_api_key": "sk-deployment-wide",
    }
    base.update(over)
    return Settings(**base)


# ===========================================================================
# The helper — where the "which id, and when" decision actually lives.
# ===========================================================================


def test_only_our_own_proxy_gets_a_tenant_id():
    # The id names a paying tenant. It goes to infrastructure we run and nowhere
    # else — ``openai_compatible`` is excluded on purpose even though an operator
    # CAN point it at the proxy, because its base URL can equally be a third
    # party we would be handing customer identifiers to.
    assert spend_attribution.is_proxy_provider("litellm") is True
    for elsewhere in ("openai", "openai_compatible", "openrouter", "anthropic", "ollama"):
        assert spend_attribution.is_proxy_provider(elsewhere) is False, elsewhere


def test_no_id_is_sent_off_the_proxy_path_even_with_a_workspace(bound_workspace):
    bound_workspace("ws_alpha")
    assert spend_attribution.end_user_id_for("litellm") == "ws_alpha"
    assert spend_attribution.end_user_id_for("openai") is None
    assert spend_attribution.end_user_id_for("openrouter") is None


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_workspace_is_no_workspace(monkeypatch, blank):
    """``user=""`` is not "unattributed" to the proxy — it is a customer named
    empty string. Every untagged run would pool under it, and the ingest's
    coverage check would then read as fully attributed while nobody was billed.

    Asserted at ``current_workspace_id`` because that is the ONE place the
    normalisation lives; a second guard downstream would let a mutation to
    either one survive.
    """
    fake = types.ModuleType(_EE_IDENTITY_MODULE)
    fake.current_workspace_id = lambda: blank  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _EE_IDENTITY_MODULE, fake)

    assert spend_attribution.current_workspace_id() is None
    assert spend_attribution.end_user_id_for("litellm") is None


def test_a_community_install_with_no_ee_package_tags_nothing(monkeypatch):
    # The identity ContextVar lives in the EE package. A community install has no
    # workspaces to bill, so the absence is correct, not a miss.
    monkeypatch.setitem(sys.modules, _EE_IDENTITY_MODULE, None)
    assert spend_attribution.current_workspace_id() is None


def test_a_broken_identity_lookup_never_fails_the_run(monkeypatch):
    """Attribution is worth less than the run it rides on.

    If this raised, a fault in the identity layer would take out chat itself —
    trading a billing gap (which the ingest's coverage check surfaces) for an
    outage (which it cannot).
    """
    fake = types.ModuleType(_EE_IDENTITY_MODULE)

    def _explode() -> str:
        raise RuntimeError("identity layer unavailable (simulated)")

    fake.current_workspace_id = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _EE_IDENTITY_MODULE, fake)

    assert spend_attribution.current_workspace_id() is None


def test_the_workspace_comes_from_the_run_identity(monkeypatch):
    fake = types.ModuleType(_EE_IDENTITY_MODULE)
    fake.current_workspace_id = lambda: "ws_from_identity"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _EE_IDENTITY_MODULE, fake)

    assert spend_attribution.current_workspace_id() == "ws_from_identity"


def test_the_real_identity_contextvar_is_the_one_we_read():
    """Against the actual EE module, not a stand-in — the stand-ins above would
    keep passing if ``attach_agent_identity`` renamed what it publishes."""
    ee = pytest.importorskip(_EE_IDENTITY_MODULE)

    tokens = ee.attach_agent_identity(workspace_id="ws_real", user_id="user_real")
    try:
        assert spend_attribution.current_workspace_id() == "ws_real"
    finally:
        ee.detach_agent_identity(tokens)

    assert spend_attribution.current_workspace_id() is None


# ===========================================================================
# pydantic_ai — the id rides on per-run model settings.
# ===========================================================================


def _pyd_settings(backend_settings: Settings) -> dict:
    backend = PydanticAIBackend(backend_settings)
    return dict(backend._run_model_settings() or {})


def test_pydantic_ai_names_the_paying_workspace_on_the_proxy(bound_workspace):
    bound_workspace("ws_alpha")
    out = _pyd_settings(_settings(pydantic_ai_model="litellm:gpt-5.2"))
    assert out.get("openai_user") == "ws_alpha"


def test_pydantic_ai_sends_no_tenant_id_to_a_third_party(bound_workspace):
    bound_workspace("ws_alpha")
    for spec in ("openai:gpt-5.2", "openrouter:x/y", "openai_compatible:z"):
        out = _pyd_settings(_settings(pydantic_ai_model=spec))
        assert "openai_user" not in out, spec


def test_pydantic_ai_keeps_the_output_cap_alongside_the_tenant_id(bound_workspace):
    """Both are per-run settings and they share one dict. An earlier shape set
    ``model_settings`` only when a cap resolved, so adding the id there would
    have dropped it on every model without a cap."""
    bound_workspace("ws_alpha")
    backend = PydanticAIBackend(_settings(pydantic_ai_model="litellm:gpt-5.2"))
    backend._resolve_max_output_tokens = lambda: 4096  # type: ignore[method-assign]

    out = dict(backend._run_model_settings() or {})

    assert out.get("max_tokens") == 4096
    assert out.get("openai_user") == "ws_alpha"


def test_pydantic_ai_sends_no_settings_at_all_when_neither_applies(bound_workspace):
    # Byte-for-byte the behaviour before either setting existed: no
    # ``model_settings`` key on the run rather than an empty dict.
    bound_workspace(None)
    backend = PydanticAIBackend(_settings(pydantic_ai_model="litellm:gpt-5.2"))
    backend._resolve_max_output_tokens = lambda: None  # type: ignore[method-assign]

    assert backend._run_model_settings() is None


def test_pydantic_ai_uses_the_setting_name_the_sdk_maps_to_the_user_field():
    """``openai_user`` is pydantic-ai's spelling of the OpenAI ``user`` body
    field. Nothing in our code enforces that mapping — the SDK does — so this
    pins the name against an upgrade that renames or drops it, which would
    otherwise stop attribution silently on the next dependency bump."""
    from pydantic_ai.models.openai import OpenAIChatModelSettings

    assert "openai_user" in OpenAIChatModelSettings.__annotations__


# ===========================================================================
# deep_agents — the id is baked into the model, so the graph cache must move.
# ===========================================================================


def _deep_model_kwargs(backend_settings: Settings) -> dict:
    backend = DeepAgentsBackend(backend_settings)
    backend._sdk_available = True
    model = backend._build_model()
    return dict(getattr(model, "model_kwargs", {}) or {})


def test_deep_agents_names_the_paying_workspace_on_the_proxy(bound_workspace):
    bound_workspace("ws_alpha")
    mk = _deep_model_kwargs(_settings(deep_agents_model="litellm:litellm_proxy/gpt-5.2"))
    assert mk.get("user") == "ws_alpha"


def test_deep_agents_sends_nothing_when_there_is_no_workspace(bound_workspace):
    bound_workspace(None)
    mk = _deep_model_kwargs(_settings(deep_agents_model="litellm:litellm_proxy/gpt-5.2"))
    assert "user" not in mk


def test_deep_agents_sends_no_tenant_id_to_a_third_party(bound_workspace):
    bound_workspace("ws_alpha")
    for spec in ("openrouter:x/y", "openai_compatible:z"):
        backend = DeepAgentsBackend(
            _settings(
                deep_agents_model=spec,
                openai_compatible_base_url="http://third.party",
                openrouter_api_key="sk-openrouter",
                openai_compatible_api_key="sk-compatible",
            )
        )
        backend._sdk_available = True
        model = backend._build_model()
        mk = dict(getattr(model, "model_kwargs", {}) or {})
        assert "user" not in mk, spec


def test_deep_agents_tenant_id_survives_the_thinking_flag(bound_workspace):
    """Both write ``model_kwargs`` and the thinking block runs second. It copies
    the dict rather than replacing it; if that ever changes, the tenant id is
    what gets dropped, and it gets dropped only on deployments that disable
    thinking — the kind of gap nobody finds by reading."""
    bound_workspace("ws_alpha")
    mk = _deep_model_kwargs(
        _settings(
            deep_agents_model="litellm:litellm_proxy/deepseek-v4-flash",
            deep_agents_disable_thinking=True,
        )
    )
    assert mk.get("user") == "ws_alpha"
    assert mk.get("extra_body") == {"thinking": {"type": "disabled"}}


def test_deep_agents_never_serves_one_workspaces_graph_to_another(monkeypatch, bound_workspace):
    """The cross-tenant billing hazard, and the reason the cache key moved.

    ``create_deep_agent`` bakes the model — tenant id and all — into the compiled
    graph, while ``AgentPool`` drives runs through one cached instance. Without
    the id in the key, the second workspace's runs are charged to the first, and
    every observable thing about the run still looks right.
    """
    import deepagents

    compiled: list = []

    def _capture(**kwargs):
        compiled.append(kwargs.get("model"))
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", _capture)

    backend = DeepAgentsBackend(_settings(deep_agents_model="litellm:litellm_proxy/gpt-5.2"))
    backend._sdk_available = True

    def _agent_for(workspace: str):
        bound_workspace(workspace)
        return backend._get_or_create_agent(backend._build_model(), "you are an agent")

    first = _agent_for("ws_alpha")
    again = _agent_for("ws_alpha")
    second = _agent_for("ws_beta")

    assert again is first, "the same workspace must still reuse its compiled graph"
    assert second is not first, "a second workspace was served the first's graph"

    assert len(compiled) == 2
    assert compiled[0].model_kwargs.get("user") == "ws_alpha"
    assert compiled[1].model_kwargs.get("user") == "ws_beta"

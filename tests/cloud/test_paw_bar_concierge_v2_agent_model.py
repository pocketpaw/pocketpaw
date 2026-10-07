# tests/cloud/test_paw_bar_concierge_v2_agent_model.py — the v2 concierge answers
# with the model its OWNER picked on the concierge's agent.
#
# v2 stays one streamed pydantic_ai call with no tools, whatever backend the agent
# runs on: only the agent's provider + model is followed, never its runtime. The
# agent is the widget's bound one, else the site's dedicated ``concierge-<site_id>``.
# A blank model on a non-pydantic_ai backend (a Claude Agent SDK agent left on
# "auto") is that backend's own default, the model the agent itself runs. No usable
# model on it falls back to ``pawbar_concierge_model``, then the backend
# default. ``answer_model`` names the same pick for the owner's dashboard. The
# resolved spec drives the model build AND the proxy attribution, and
# a changed agent model is picked up once the per-agent memo expires.
#
# The model seam is ``concierge_runtime._build_model(settings, spec)``; the
# recorder below notes the spec each turn was built with (an old one-argument call
# reads as the deployment setting, which is what the runner used before).

from __future__ import annotations

from typing import Any

import pytest

from tests.cloud.test_paw_bar_concierge_v2 import (
    _HOURS_KB,
    _chat,
    _seed_kb,
    _site,
    _widget,
    concierge_client,  # noqa: F401 — fixture
)

_DEPLOYMENT_SPEC = "anthropic:deployment-default"


class _SpecRecorder:
    """The ``_build_model`` seam: records the spec and the AgentInfo of each turn."""

    def __init__(self) -> None:
        self.specs: list[str | None] = []
        self.infos: list[Any] = []

    def build(self, settings: Any, *args: Any) -> Any:
        from pocketpaw_ee.paw_bar import concierge_runtime
        from pydantic_ai.models.function import FunctionModel

        self.specs.append(args[0] if args else concierge_runtime._model_spec(settings))

        async def _stream(_messages, info):
            self.infos.append(info)
            yield "Hello."

        return FunctionModel(stream_function=_stream, model_name="fake-concierge")


@pytest.fixture
def rec(monkeypatch) -> _SpecRecorder:
    from pocketpaw_ee.paw_bar import concierge_runtime

    from pocketpaw.config import get_settings

    recorder = _SpecRecorder()
    monkeypatch.setattr(concierge_runtime, "_build_model", recorder.build)
    pinned = get_settings().model_copy(
        update={
            "pawbar_concierge_model": _DEPLOYMENT_SPEC,
            "claude_sdk_provider": "litellm",
        }
    )
    monkeypatch.setattr(concierge_runtime, "_settings", lambda: pinned)
    # Every test starts with an empty per-agent memo.
    concierge_runtime.forget_agent_model()
    _seed_kb(monkeypatch, _HOURS_KB)
    return recorder


async def _agent(slug: str, *, backend: str = "pydantic_ai", model: str = "", **ov: Any) -> str:
    from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig

    doc = Agent(
        workspace=ov.pop("workspace", "ws-1"),
        name=slug,
        slug=slug,
        owner="user:maya",
        config=AgentConfig(backend=backend, model=model),
        **ov,
    )
    await doc.insert()
    return str(doc.id)


async def _set_model(agent_id: str, model: str) -> None:
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.agent import Agent

    doc = await Agent.get(PydanticObjectId(agent_id))
    assert doc is not None
    doc.config.model = model
    await doc.save()


@pytest.mark.asyncio
async def test_the_bound_agents_model_builds_the_turn(concierge_client, rec):  # noqa: F811
    client, store = concierge_client
    await _site()
    agent_id = await _agent("my-concierge", model="litellm:owner-pick")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == ["litellm:owner-pick"]


@pytest.mark.asyncio
async def test_a_claude_sdk_agent_still_gets_one_tool_free_call_on_its_model(
    concierge_client,  # noqa: F811
    rec,
):
    """The agent runs on the Claude Agent SDK; the visitor turn does NOT. It stays
    the single no-tools pydantic_ai call, on the agent's model through the
    provider that backend is configured with."""
    client, store = concierge_client
    await _site()
    agent_id = await _agent("claude-one", backend="claude_agent_sdk", model="claude-sonnet-4-6")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == ["litellm:claude-sonnet-4-6"]
    (info,) = rec.infos
    assert info.function_tools == []
    assert info.output_tools == []


@pytest.mark.asyncio
async def test_a_backend_model_pydantic_ai_cannot_express_falls_back(
    concierge_client,  # noqa: F811
    rec,
):
    client, store = concierge_client
    await _site()
    agent_id = await _agent("codex-one", backend="codex_cli", model="gpt-5.3-codex")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == [_DEPLOYMENT_SPEC]


@pytest.mark.asyncio
async def test_an_agent_with_no_model_falls_back_to_the_deployment_setting(
    concierge_client,  # noqa: F811
    rec,
):
    client, store = concierge_client
    await _site()
    agent_id = await _agent("plain", model="")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == [_DEPLOYMENT_SPEC]


def _pin_settings(monkeypatch, **update: Any) -> None:
    from pocketpaw_ee.paw_bar import concierge_runtime

    from pocketpaw.config import get_settings

    pinned = get_settings().model_copy(
        update={"pawbar_concierge_model": _DEPLOYMENT_SPEC, **update}
    )
    monkeypatch.setattr(concierge_runtime, "_settings", lambda: pinned)


@pytest.mark.asyncio
async def test_a_claude_sdk_agent_with_no_model_answers_with_its_backends_model(
    concierge_client,  # noqa: F811
    rec,
    monkeypatch,
):
    """A blank model on a Claude Agent SDK agent means "the backend's default",
    so the turn uses the model that agent itself would run (``resolve_model``),
    never the deployment's concierge model."""
    _pin_settings(monkeypatch, claude_sdk_provider="litellm", claude_sdk_model="claude-opus-4-6")
    client, store = concierge_client
    await _site()
    agent_id = await _agent("claude-blank", backend="claude_agent_sdk", model="")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == ["litellm:claude-opus-4-6"]


@pytest.mark.asyncio
async def test_a_claude_sdk_agent_with_nothing_set_gets_the_providers_default_claude(
    concierge_client,  # noqa: F811
    rec,
    monkeypatch,
):
    from pocketpaw.llm.providers.base import PROVIDER_DEFAULT_MODELS

    _pin_settings(
        monkeypatch, claude_sdk_provider="anthropic", claude_sdk_model="", anthropic_model=""
    )
    client, store = concierge_client
    await _site()
    agent_id = await _agent("claude-bare", backend="claude_agent_sdk", model="")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == [f"anthropic:{PROVIDER_DEFAULT_MODELS['anthropic']}"]


@pytest.mark.asyncio
async def test_the_dedicated_agent_answers_an_unbound_widget(concierge_client, rec):  # noqa: F811
    client, store = concierge_client
    site = await _site()
    await _agent(f"concierge-{site.id}", model="litellm:dedicated-pick")
    widget = await store.create_widget(_widget(agent_id=""))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == ["litellm:dedicated-pick"]


@pytest.mark.asyncio
async def test_the_bound_agent_wins_over_the_dedicated_one(concierge_client, rec):  # noqa: F811
    client, store = concierge_client
    site = await _site()
    await _agent(f"concierge-{site.id}", model="litellm:dedicated-pick")
    bound = await _agent("hand-bound", model="litellm:bound-pick")
    widget = await store.create_widget(_widget(agent_id=bound))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == ["litellm:bound-pick"]


@pytest.mark.asyncio
async def test_an_agent_in_another_workspace_is_never_followed(
    concierge_client,  # noqa: F811
    rec,
):
    client, store = concierge_client
    await _site()
    foreign = await _agent("elsewhere", model="litellm:foreign", workspace="ws-other")
    widget = await store.create_widget(_widget(agent_id=foreign))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert rec.specs == [_DEPLOYMENT_SPEC]


@pytest.mark.asyncio
async def test_proxy_attribution_follows_the_resolved_provider(
    concierge_client,  # noqa: F811
    rec,
):
    """The deployment default is a direct provider (no proxy fields); the agent's
    pick goes through the proxy, so the turn must carry the proxy attribution."""
    client, store = concierge_client
    await _site()
    agent_id = await _agent("proxied", model="litellm:owner-pick")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    (info,) = rec.infos
    assert info.model_settings["openai_user"] == "ws-1"
    assert "tags" in info.model_settings["extra_body"]["metadata"]


@pytest.mark.asyncio
async def test_a_changed_agent_model_is_picked_up_after_the_memo_expires(
    concierge_client,  # noqa: F811
    rec,
    monkeypatch,
):
    from pocketpaw_ee.paw_bar import concierge_runtime

    clock = [1000.0]
    monkeypatch.setattr(concierge_runtime, "_now", lambda: clock[0])
    client, store = concierge_client
    await _site()
    agent_id = await _agent("changing", model="litellm:first")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    await _chat(client, widget.id)
    await _set_model(agent_id, "litellm:second")
    await _chat(client, widget.id)  # still inside the memo window
    clock[0] += concierge_runtime._AGENT_MODEL_TTL_S + 1
    await _chat(client, widget.id)
    await _set_model(agent_id, "litellm:third")
    concierge_runtime.forget_agent_model(agent_id)
    await _chat(client, widget.id)

    assert rec.specs == ["litellm:first", "litellm:first", "litellm:second", "litellm:third"]


def test_usage_prices_the_resolved_model_when_the_response_names_none(monkeypatch):
    from types import SimpleNamespace

    from pocketpaw_ee.paw_bar import concierge_runtime

    from pocketpaw.config import get_settings

    seen: list[str | None] = []

    class _Builder:
        def _usage_event_from(self, _usage: Any, *, model_name: str | None) -> Any:
            seen.append(model_name)
            return SimpleNamespace(metadata={"model": model_name})

        def _parse_provider_model(self, spec: str | None) -> tuple[str, str]:
            provider, _, model = (spec or "").partition(":")
            return provider, model

    monkeypatch.setattr(concierge_runtime, "_builder", lambda _s: _Builder())
    result = SimpleNamespace(usage=SimpleNamespace(), response=SimpleNamespace(model_name=None))

    usage = concierge_runtime._usage(get_settings(), result, "litellm:owner-pick")

    assert seen == ["owner-pick"]
    assert usage["model"] == "owner-pick"


@pytest.mark.asyncio
async def test_answer_model_names_what_a_turn_would_use(
    concierge_client,  # noqa: F811
    rec,
    monkeypatch,
):
    """The dashboard's label and the turn agree: both go through the same resolution."""
    from pocketpaw_ee.paw_bar import concierge_runtime

    _pin_settings(monkeypatch, claude_sdk_provider="anthropic", claude_sdk_model="claude-opus-4-6")
    client, store = concierge_client
    site = await _site()
    agent_id = await _agent("claude-label", backend="claude_agent_sdk", model="")
    widget = await store.create_widget(_widget(agent_id=agent_id))

    label = await concierge_runtime.answer_model(widget, site, "ws-1")
    await _chat(client, widget.id)

    assert label == "anthropic:claude-opus-4-6"
    assert rec.specs == [label]

"""The cloud's default agent backend, and the line it must not cross.

Created 2026-08-01.

Lives in ``tests/`` rather than ``tests/cloud/`` deliberately: the pyproject
addopts carry ``--ignore=tests/cloud``, so a guard placed there runs on nobody's
machine but the author's and never in CI. The EE assertions skip individually
when ``pocketpaw_ee`` is absent, which keeps the OSS-only job running the two
that matter most to it.

A new cloud agent should run ``pydantic_ai``: the cloud's binding constraint is
concurrency, and the Claude SDK backend spawns a CLI subprocess per concurrent
run. A self-hosted install should not, because ``pydantic_ai`` is dispatch-only
- no shell, no filesystem - and PocketPaw self-hosted is a local agent. Two
different right answers, so both ends are pinned.
"""

from __future__ import annotations

import pytest


def _cloud_default() -> str:
    pytest.importorskip("pocketpaw_ee", reason="pocketpaw-ee not installed")
    from pocketpaw_ee.cloud.agents.defaults import CLOUD_DEFAULT_AGENT_BACKEND

    return CLOUD_DEFAULT_AGENT_BACKEND


def test_a_new_cloud_agent_defaults_to_the_in_process_backend():
    """Every schema a new agent can arrive through, not just one.

    These were three independent literals: the Beanie document, the domain value
    object and the create-request DTO. An agent created through the API arrives
    via the DTO, one created by the planner via the domain, one written directly
    via the document - so a default living in only two of them is a default that
    depends on the caller.
    """
    default = _cloud_default()
    from pocketpaw_ee.cloud.agents.domain import AgentConfigSpec
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest
    from pocketpaw_ee.cloud.models.agent import AgentConfig

    assert default == "pydantic_ai"
    assert AgentConfig().backend == default
    assert AgentConfigSpec().backend == default
    assert CreateAgentRequest(name="A", slug="a").backend == default


def test_an_explicit_backend_still_wins():
    """A default is not a policy. An operator who picked a backend keeps it."""
    _cloud_default()
    from pocketpaw_ee.cloud.models.agent import AgentConfig

    assert AgentConfig(backend="claude_agent_sdk").backend == "claude_agent_sdk"


def test_the_self_hosted_default_is_untouched():
    """The scope line, and the reason this change is cloud-only.

    ``pydantic_ai`` has no shell and no filesystem by design, because one
    process serves every tenant and the builtin jail is process-global. That
    trade is right in cloud and wrong on a laptop, so flipping this one too
    would quietly take local execution away from every self-hosted user.
    """
    from pocketpaw.config import Settings

    assert Settings.model_fields["agent_backend"].default == "claude_agent_sdk"


def test_the_pool_fallback_does_not_follow_the_cloud_default():
    """A document with no ``backend`` key predates the field, which means it
    predates this change - so the right answer for it is the OLD default, not
    today's. Following the cloud default here would silently re-home the oldest
    agents in the estate."""
    import inspect

    from pocketpaw.agents.pool import AgentPool

    src = inspect.getsource(AgentPool._build)
    assert 'config.get("backend", "claude_agent_sdk")' in src


def test_the_default_names_a_registered_backend():
    """A default that does not resolve is a boot failure for every new agent."""
    from pocketpaw.agents.registry import get_backend_class, list_backends

    default = _cloud_default()
    assert default in list_backends()
    assert get_backend_class(default) is not None

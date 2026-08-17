# Tests for the narration registry seam in ``agents/tool_bridge.py`` (HTN-2).
# Created: 2026-08-15 — every ToolRegistry the bridge builds used to be dropped
# at function exit, so a caller holding an agent had no way to reach that
# agent's live tool instances and a tool's declared ``Narration`` was
# unreadable in production. These tests pin the seam that fixes it: the
# registry is retained under the agent's own ToolPolicy, resolved through the
# public ``narration_registry_for``, and released when the policy dies.
#
# The lifetime test is the load-bearing one. This process serves every tenant,
# so a retention that outlived the agent would be a leak, and a retention keyed
# process-wide by tool NAME would be ambiguous — EE registers DaytonaShellTool
# under the same ``shell`` name as the OSS builtin.

from __future__ import annotations

import gc
import weakref

import pytest

from pocketpaw.agents import tool_bridge
from pocketpaw.tools.narration import narration_for_tool, render
from pocketpaw.tools.policy import ToolPolicy


class _FakeBackend:
    """Stands in for a bridged backend.

    Every backend that bridges tools through ``tool_bridge`` (pydantic_ai,
    deep_agents, openai_agents, google_adk) exposes this exact public method,
    which is why the seam duck-types on it instead of reaching for ``_policy``.
    """

    def __init__(self, policy: ToolPolicy):
        self._policy = policy

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy


@pytest.fixture
def bridged_backend():
    policy = ToolPolicy(profile="full")
    registry = tool_bridge._build_tool_registry("pydantic_ai", policy)
    return _FakeBackend(policy), registry


def test_the_agents_registry_is_reachable_from_its_backend(bridged_backend):
    backend, registry = bridged_backend

    assert tool_bridge.narration_registry_for(backend) is registry
    assert registry.has("web_search"), "the bridged surface should carry the builtin tools"


def test_web_search_still_renders_its_declared_phrase(bridged_backend):
    """THE no-regression test.

    Before HTN-2 this phrase came from a one-entry name -> (module, class) map
    that INSTANTIATED ``WebSearchTool`` to read the property off it. Deleting
    that map without this seam would have silently downgraded the one tool that
    already worked, from the interpolated phrase to a bare one.
    """
    backend, _ = bridged_backend
    registry = tool_bridge.narration_registry_for(backend)

    assert render(narration_for_tool("web_search", registry), {"query": "quarterly filings"}) == (
        "Searching the web for quarterly filings"
    )


def test_an_unannotated_tool_in_the_registry_still_derives(bridged_backend):
    """The registry answering does not disable the rest of the chain — a tool
    the registry holds that declares nothing falls through to derivation."""
    backend, _ = bridged_backend
    registry = tool_bridge.narration_registry_for(backend)

    assert registry.has("write_file")
    assert render(narration_for_tool("write_file", registry), {}) == "Writing the file"


def test_two_agents_resolve_their_own_registries():
    """Why this is keyed per policy rather than by tool name process-wide: on a
    cloud process two agents are live at once, and EE registers
    ``DaytonaShellTool`` under the same name as the OSS ``ShellTool``. Each
    agent must resolve ITS instance — a shared name index would have to pick
    one nondeterministically or refuse to narrate ``shell`` at all."""
    policy_a, policy_b = ToolPolicy(profile="full"), ToolPolicy(profile="full")
    registry_a = tool_bridge._build_tool_registry("pydantic_ai", policy_a)
    registry_b = tool_bridge._build_tool_registry("deep_agents", policy_b)

    assert registry_a is not registry_b
    assert tool_bridge.narration_registry_for(_FakeBackend(policy_a)) is registry_a
    assert tool_bridge.narration_registry_for(_FakeBackend(policy_b)) is registry_b


class _DeclaringShell:
    """A tool named ``shell`` that declares its own phrase."""

    def __init__(self, phrase: str):
        self._phrase = phrase

    name = "shell"

    @property
    def narration(self):
        from pocketpaw.tools.narration import Narration

        return Narration(active=self._phrase, bare=self._phrase)


def test_an_ee_substituted_tool_narrates_from_its_own_declaration():
    """The case that decided the seam's shape.

    On cloud, EE registers ``DaytonaShellTool`` under the SAME ``shell`` name as
    the OSS ``ShellTool`` and it wins by last-writer-wins registration
    (``extensions.py:1297``, ``daytona/tools.py:763``). Two agents can be live in
    one process with different instances behind that one name. A process-global
    name index would have to pick one nondeterministically, or refuse to narrate
    ``shell`` at all; resolving through the agent's OWN registry makes both
    answers correct at once.

    Neither shell tool declares a ``Narration`` yet — HTN-3 writes those — so
    this uses two declaring stand-ins registered exactly the way EE substitutes,
    which pins the property now instead of waiting on annotations to exist.
    """
    oss_policy = ToolPolicy(profile="full")
    oss_registry = tool_bridge._build_tool_registry("pydantic_ai", oss_policy)
    oss_registry.register(_DeclaringShell("Running a shell command"))

    cloud_policy = ToolPolicy(profile="full")
    cloud_registry = tool_bridge._build_tool_registry("pydantic_ai", cloud_policy)
    cloud_registry.register(_DeclaringShell("Running a command in the sandbox"))

    oss_backend = _FakeBackend(oss_policy)
    cloud_backend = _FakeBackend(cloud_policy)

    oss_phrase = render(
        narration_for_tool("shell", tool_bridge.narration_registry_for(oss_backend)), {}
    )
    cloud_phrase = render(
        narration_for_tool("shell", tool_bridge.narration_registry_for(cloud_backend)), {}
    )

    assert oss_phrase == "Running a shell command"
    assert cloud_phrase == "Running a command in the sandbox"
    assert oss_phrase != cloud_phrase, (
        "both agents resolved the same instance — the lookup is not per-agent"
    )


def test_the_retained_registry_is_released_with_its_policy():
    """Lifetime — the reason this hangs off the policy and not a module map.

    Retention is bounded by the agent's own lifecycle: the pool evicts an idle
    AgentInstance, the backend and its policy go with it, and the registry and
    its ~76 tool instances go too.

    The first draft of this seam used a module-level ``WeakKeyDictionary``
    keyed by policy, and THIS test is what caught that it leaks: the registry
    holds a reference back to its policy, so the entry's value keeps its own
    weak key alive and nothing is ever collected. Keep this test pointed at
    collection, not at a container's length, or that bug comes back invisible.
    """
    policy = ToolPolicy(profile="full")
    registry_ref = weakref.ref(tool_bridge._build_tool_registry("pydantic_ai", policy))
    assert registry_ref() is not None

    del policy
    gc.collect()

    assert registry_ref() is None, "the registry outlived the policy that anchors it"


@pytest.mark.parametrize(
    ("label", "backend"),
    [
        ("no backend at all", None),
        ("a backend that never bridged tools", object()),
        (
            "a policy getter returning None",
            type("_B", (), {"get_tool_policy": lambda self: None})(),
        ),
        (
            "a policy getter that raises",
            type(
                "_B",
                (),
                {"get_tool_policy": lambda self: (_ for _ in ()).throw(RuntimeError("no"))},
            )(),
        ),
    ],
)
def test_a_backend_without_a_bridged_registry_resolves_nothing(label, backend):
    """The Claude SDK backend surfaces its tools over MCP rather than through
    this bridge, so there is genuinely no registry to find. That must resolve to
    None quietly — narration then derives from the tool name."""
    assert tool_bridge.narration_registry_for(backend) is None, label


def test_an_unretained_policy_resolves_nothing():
    """A policy that never built a surface has no registry — and asking must not
    invent one."""
    assert tool_bridge.narration_registry_for(_FakeBackend(ToolPolicy(profile="full"))) is None

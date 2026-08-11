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

Changes:
  - 2026-08-11: added the packaging half of the same contract. Naming a default
    backend and shipping an install that cannot import it are two independent
    facts, and only the first one was pinned here — so the estate ran an image
    whose every new agent defaulted to a backend that was not installed. The
    two tests below close that: the ``pocketpaw-ee`` wheel must REQUEST the
    extra, and the extra ``Dockerfile.enterprise`` installs the core with must
    RESOLVE to it. Both derive the extra from the backend's own
    ``install_hint`` rather than restating ``pydantic-ai``, so renaming the
    extra fails here instead of drifting.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
PYPROJECT = _REPO / "pyproject.toml"
EE_PYPROJECT = _REPO / "ee" / "pyproject.toml"


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


# ---------------------------------------------------------------------------
# The packaging half: registered is not installed.
#
# ``test_the_default_names_a_registered_backend`` above passes on an install
# that cannot run a single agent. The registry maps a name to a module path; it
# does not import it, and ``PydanticAIBackend._initialize`` swallows the
# ImportError into a WARNING. So a wheel that never pulled pydantic-ai still
# registers the backend, still accepts it as every new agent's default, and
# fails one log line deep at the first run.
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _split_requirement(spec: str) -> tuple[str, set[str]]:
    """Split a PEP 508 requirement into ``(name, extras)``.

    ``pocketpaw[pydantic-ai,litellm]~=0.4.16`` -> ``("pocketpaw", {...})``.
    Deliberately small: these are first-party specs, not arbitrary input, and
    pulling ``packaging.Requirement`` in would add a test-only dependency.
    """
    head = spec.split(";", 1)[0].strip()
    name, _, rest = head.partition("[")
    if not rest:
        for delim in ("~=", "==", ">=", "<=", "!=", ">", "<", "@"):
            name = name.split(delim, 1)[0]
        return name.strip().lower(), set()
    inner = rest.split("]", 1)[0]
    return name.strip().lower(), {e.strip().lower() for e in inner.split(",") if e.strip()}


def _required_extra() -> str:
    """The core extra that installs the cloud default backend's SDK.

    Read off the backend's own ``install_hint`` rather than written down here,
    so renaming the extra breaks this test instead of leaving it green against
    a spec nobody publishes.
    """
    from pocketpaw.agents.registry import get_backend_info

    info = get_backend_info(_cloud_default())
    assert info is not None
    spec = info.install_hint.get("pip_spec", "")
    name, extras = _split_requirement(spec)
    assert name == "pocketpaw" and len(extras) == 1, (
        f"install_hint pip_spec should name one pocketpaw extra, got {spec!r}"
    )
    return extras.pop()


def _closure(extra: str, optional: dict[str, list[str]]) -> set[str]:
    """Distribution names an extra installs, following ``pocketpaw[...]`` self-refs.

    ``all-backends`` is written as a self-referential extra list, so a resolver
    that only reads the literal strings would report it as installing nothing.
    """
    seen: set[str] = set()
    pending = [extra]
    while pending:
        current = pending.pop()
        for spec in optional.get(current, []):
            name, extras = _split_requirement(spec)
            if name == "pocketpaw":
                pending.extend(e for e in extras if e not in seen)
                seen.update(extras)
            else:
                seen.add(name)
    return seen


def test_the_ee_wheel_requests_the_cloud_default_backends_extra():
    """``pip install pocketpaw-ee`` must bring the backend it defaults every agent to.

    This is the durable half of the fix — it holds no matter how the image
    installs the core, whereas the Dockerfile's extra is one line an operator
    can change. EE depends on the core WITHOUT extras, so the cloud shipped
    with its own default backend absent from its dependency closure.
    """
    extra = _required_extra()
    deps = _load(EE_PYPROJECT)["project"]["dependencies"]
    core = [(name, extras) for name, extras in map(_split_requirement, deps) if name == "pocketpaw"]
    assert core, "ee/pyproject.toml does not depend on `pocketpaw` at all"
    requested = {e for _, extras in core for e in extras}
    assert extra in requested, (
        f"pocketpaw-ee defaults every new cloud agent to "
        f"{_cloud_default()!r} but requires the core without the {extra!r} "
        f"extra, so installing the EE wheel never brings its SDK. "
        f"Requested extras: {sorted(requested)}"
    )


def test_the_extra_the_enterprise_image_builds_with_covers_the_default_backend():
    """``Dockerfile.enterprise`` builds the production image with ``pip install '.[all]'``.

    ``all`` and ``all-backends`` each enumerate the backends by hand, and both
    omitted this one — so the extra that advertises itself as everything was
    the reason production had no ``pydantic_ai``.
    """
    from pocketpaw.agents.registry import get_backend_info

    optional = _load(PYPROJECT)["project"]["optional-dependencies"]
    pip_package = get_backend_info(_cloud_default()).install_hint["pip_package"].lower()

    for extra in ("all", "all-backends"):
        assert pip_package in _closure(extra, optional), (
            f"the {extra!r} extra does not install {pip_package!r}, which the "
            f"cloud's default backend imports"
        )

# tests/test_context_builder_skill_filter.py
# Created: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A2) —
# pins the non-SDK path's skill filtering: ``build_system_prompt(skill_names=...)``
# advertises ONLY the named skills in the "Available Skills" block when the set
# is non-empty, and advertises ALL installed skills (legacy behavior) when it is
# None / empty.

from __future__ import annotations

import pytest

from pocketpaw.bootstrap.context_builder import AgentContextBuilder
from pocketpaw.bootstrap.protocol import BootstrapContext
from pocketpaw.skills import loader as loader_mod
from pocketpaw.skills.loader import Skill, SkillLoader

pytestmark = pytest.mark.asyncio


class _StubBootstrap:
    async def get_context(self) -> BootstrapContext:
        return BootstrapContext(
            name="Test",
            identity="id",
            soul="soul",
            style="style",
        )


def _skill(name: str) -> Skill:
    return Skill(name=name, description=f"desc {name}", content="x", path=None)  # type: ignore[arg-type]


@pytest.fixture
def installed(monkeypatch):
    fake = SkillLoader()
    fake._skills = {
        "github": _skill("github"),
        "calendar-sync": _skill("calendar-sync"),
        "weather": _skill("weather"),
    }
    fake._loaded = True
    monkeypatch.setattr(loader_mod, "_skill_loader", fake)
    return fake


def _builder() -> AgentContextBuilder:
    return AgentContextBuilder(bootstrap_provider=_StubBootstrap())


async def test_filters_to_named_skills(installed):
    prompt = await _builder().build_system_prompt(
        include_memory=False,
        skill_names=frozenset({"github"}),
    )
    assert "**github**" in prompt
    assert "**calendar-sync**" not in prompt
    assert "**weather**" not in prompt


async def test_no_filter_advertises_all(installed):
    prompt = await _builder().build_system_prompt(include_memory=False)
    assert "**github**" in prompt
    assert "**calendar-sync**" in prompt
    assert "**weather**" in prompt


async def test_empty_set_advertises_all(installed):
    """An empty (falsy) skill_names is the legacy all-skills path, not 'hide all'."""
    prompt = await _builder().build_system_prompt(
        include_memory=False,
        skill_names=frozenset(),
    )
    assert "**github**" in prompt
    assert "**weather**" in prompt

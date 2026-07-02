# tests/atlas/test_primer_block.py — the "Paw OS Primer" context_builder
# block (AT-3). Created: 2026-07-02 (feat/atlas-surface). Pins that
# build_system_prompt renders the atlas primer: OS identity + one line per
# primitive generated from the atlas store + the atlas_search / surface-route
# instruction; that the block stays under the ~500-token budget (2000 chars,
# the _INJECTION_CAPS['atlas_primer'] ceiling); and that an atlas load
# failure degrades gracefully — the prompt still builds, minus the primer
# (same try/except pattern as the skills block #8).

from __future__ import annotations

import pytest

import pocketpaw.atlas.store as atlas_store_mod
from pocketpaw.bootstrap.context_builder import _INJECTION_CAPS, AgentContextBuilder
from pocketpaw.bootstrap.protocol import BootstrapContext

pytestmark = pytest.mark.asyncio

# Hard budget from the task brief: the primer must stay ≤ 500 tokens. The
# builder budgets in chars; 4 chars/token is the standard approximation.
_TOKEN_BUDGET = 500
_CHARS_PER_TOKEN = 4


class _StubBootstrap:
    async def get_context(self) -> BootstrapContext:
        return BootstrapContext(
            name="Test",
            identity="id",
            soul="soul",
            style="style",
        )


def _builder() -> AgentContextBuilder:
    return AgentContextBuilder(bootstrap_provider=_StubBootstrap())


@pytest.fixture(autouse=True)
def _fresh_atlas_singleton(monkeypatch):
    """Isolate the module-level atlas store singleton per test."""
    monkeypatch.setattr(atlas_store_mod, "_store", None)
    yield
    atlas_store_mod._store = None


class TestPrimerContent:
    async def test_primer_renders_in_system_prompt(self):
        prompt = await _builder().build_system_prompt(include_memory=False)
        assert "# Paw OS Primer" in prompt
        assert "you run inside paw-os" in prompt.lower()
        assert "atlas_search" in prompt
        # The surface-route instruction: point users at routes like /sites.
        assert "/sites" in prompt

    async def test_primer_lists_all_primitives_from_the_store(self):
        """The primitive lines are generated from the atlas store at build
        time — every primitive name must appear, none hard-coded."""
        primer = AgentContextBuilder._build_atlas_primer()
        for entry in atlas_store_mod.get_atlas_store().entries:
            if entry.kind == "primitive":
                assert f"- {entry.name}:" in primer

    async def test_primer_excludes_surface_entries_from_the_list(self):
        """Only the 10 primitives get a line; surface entries are pointed at
        via atlas_search, not enumerated (budget)."""
        primer = AgentContextBuilder._build_atlas_primer()
        assert "- Mission Control:" not in primer
        assert "- Decision Graph:" not in primer


class TestPrimerBudget:
    async def test_primer_stays_under_token_budget(self):
        primer = AgentContextBuilder._build_atlas_primer()
        assert primer
        est_tokens = len(primer) / _CHARS_PER_TOKEN
        assert est_tokens <= _TOKEN_BUDGET, (
            f"primer is ~{est_tokens:.0f} est. tokens ({len(primer)} chars); "
            f"budget is {_TOKEN_BUDGET}"
        )

    async def test_injection_cap_matches_budget(self):
        """The assembler-side cap must also enforce the ~500-token ceiling."""
        cap = _INJECTION_CAPS.get("atlas_primer")
        assert cap is not None
        assert cap <= _TOKEN_BUDGET * _CHARS_PER_TOKEN


class TestPrimerResilience:
    async def test_atlas_failure_does_not_break_prompt_building(self, monkeypatch):
        """If the atlas store fails to load, the prompt still builds — the
        primer block is simply absent (same pattern as skills / health)."""

        def _boom():
            raise RuntimeError("seed file corrupted")

        monkeypatch.setattr(atlas_store_mod, "get_atlas_store", _boom)
        prompt = await _builder().build_system_prompt(include_memory=False)
        assert prompt  # identity block still present
        assert "# Paw OS Primer" not in prompt

    async def test_empty_store_yields_no_primer(self, monkeypatch):
        from pocketpaw.atlas.model import AtlasModel
        from pocketpaw.atlas.store import AtlasStore

        empty = AtlasStore(AtlasModel(entries=[]))
        monkeypatch.setattr(atlas_store_mod, "get_atlas_store", lambda: empty)
        assert AgentContextBuilder._build_atlas_primer() == ""

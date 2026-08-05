# tests/cloud/chat/test_prompt_assembly_cost.py — the system prompt is not
# allowed to re-parse the config.
# Created: 2026-08-04 (perf/prompt-assembly).
#
# WHAT THIS CAUGHT. ``build_behavior_instructions`` measured 120.9 ms per chat
# turn and produced 36,608 chars, so it looked like slow string assembly.
# Profiling said otherwise — 11.9 MILLION Python function calls for five
# invocations, essentially all of them under one line:
#
#     build_behavior_instructions
#       -> composio.service.is_enabled()          (no settings argument)
#         -> Settings.load()                      114.9 ms, measured
#           -> pydantic_settings re-parses the whole model from .env
#
# One uncached config read WAS the entire cost. ``get_settings()`` is the
# ``lru_cache``d accessor and measures 0.000 ms; after the swap the same call
# is 0.0 ms warm.
#
# The irony is that ``is_enabled``'s own docstring warned about it — "accepts an
# optional ``Settings`` so callers that already have one don't pay the
# ``Settings.load()`` cost twice" — while its default path did exactly that.
#
# WHY THIS TEST IS NOT A TIMER. A "must run in under N ms" assertion flakes on a
# loaded CI box and tells you nothing about why. The real invariant is
# structural: assembling a prompt must not re-parse the config. That is exact,
# fast, and names the defect when it fails.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted (``scripts/mutate.py``).

from __future__ import annotations

from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud.chat import agent_service as A

from pocketpaw.config import Settings, get_settings


def _ctx() -> A.ScopeContext:
    return A.ScopeContext(
        kind=A.ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="ws1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


@pytest.fixture(autouse=True)
def _warm_settings_cache():
    """Prime the cache so a legitimate first-ever read is not miscounted.

    The invariant under test is "assembly does not re-parse", not "nothing ever
    parses". Without this the very first ``get_settings()`` in the process would
    itself call ``Settings.load()`` and the spy could not tell the two apart.
    """
    get_settings()
    yield


class TestAssemblyDoesNotReparseConfig:
    def test_building_instructions_never_calls_settings_load(self) -> None:
        """The regression guard, stated structurally rather than as a stopwatch.

        THE MUTATION THAT BREAKS THIS: restore ``s = settings or
        Settings.load()`` in ``composio.service.is_enabled``. Run: the spy
        recorded one call and this failed. (Applied 2026-08-04.)
        """
        calls: list[int] = []
        real = Settings.load

        def _spy(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with patch.object(Settings, "load", _spy):
            out = A.build_behavior_instructions(_ctx(), backend_name="pydantic_ai")

        assert out, "assembly produced nothing — the test is measuring the wrong thing"
        assert not calls, (
            f"assembling the system prompt re-parsed the config {len(calls)}x. "
            "Settings.load() costs ~115ms a call; use get_settings()."
        )

    def test_is_enabled_does_not_reparse_either(self) -> None:
        """The specific offender, pinned on its own.

        The test above would also fail if some OTHER call site regressed, which
        makes it a good gate and a poor diagnosis. This one names the function.

        THE MUTATION THAT BREAKS THIS: same as above. Run: one call recorded.
        (Applied 2026-08-04.)
        """
        from pocketpaw_ee.cloud.composio import service as composio_service

        calls: list[int] = []
        real = Settings.load

        def _spy(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with patch.object(Settings, "load", _spy):
            composio_service.is_enabled()

        assert not calls, "composio.is_enabled() re-parsed the whole config"

    def test_an_explicit_settings_argument_still_wins(self) -> None:
        """Callers holding a fresher snapshot must not be overridden by the cache.

        This is the property that makes the swap safe: nothing that already had
        a ``Settings`` changed behaviour, so a caller mid-config-write still
        reads what it passed.

        THE MUTATION THAT BREAKS THIS: ignore the argument and always use
        ``get_settings()``. Run: the explicitly-disabled settings reported
        enabled and this failed. (Applied 2026-08-04.)
        """
        from pocketpaw_ee.cloud.composio import service as composio_service

        enabled = Settings(composio_api_key="k", composio_enterprise_id="e")
        disabled = Settings(composio_api_key=None, composio_enterprise_id=None)

        assert composio_service.is_enabled(enabled) is True
        assert composio_service.is_enabled(disabled) is False

    def test_a_config_write_is_still_observable(self) -> None:
        """Caching must not make a config change invisible.

        Every config-write path in this codebase already calls
        ``get_settings.cache_clear()`` (api/v1/settings.py, budget.py,
        memory.py, bus/commands.py, dashboard.py), so the invalidation contract
        predates this change. This asserts the half that matters here: after a
        clear, the next read reflects reality.

        THE MUTATION THAT BREAKS THIS: make ``get_settings`` ignore
        ``cache_clear`` (wrap it in a module-level singleton instead). Run: the
        stale value survived the clear and this failed. (Applied 2026-08-04.)
        """
        first = get_settings()
        get_settings.cache_clear()
        second = get_settings()

        assert first is not second, (
            "get_settings() returned the same object after cache_clear() — a "
            "config write would be invisible to every reader"
        )

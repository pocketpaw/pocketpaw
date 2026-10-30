# tests/test_claude_sdk_prewarm.py
# Created: 2026-06-13 (feat/claude-sdk-prewarm) — pins the PREWARM follow-up
# stacked on fix/claude-sdk-warm-client-skills (#1456). The warm-client fix made
# skill/tool-bearing turns REUSE the warm CLI subprocess across turns, but the
# FIRST send of every new session still paid a cold ``connect()`` (~12s live)
# because the warm client was only created lazily inside the first ``run()``.
# ``ClaudeSDKBackend.prewarm`` eagerly assembles the SAME options the first turn
# will use (via the extracted ``_build_options`` helper) and ``connect()``s the
# subprocess when a session opens, so the first real turn reuses it.
#
# Three behavioral guarantees, all driven through the fake-SDK connect-counter
# harness reused from tests/test_claude_sdk_skill_warm_reuse.py:
#   1. REUSE (no skills) — prewarm a session, then run() on it → connect_count
#      == 1 (prewarm created the client; the run reused it, no second connect).
#   2. REUSE (with skills) — prewarm with skills X, then run with the SAME skills
#      X → connect_count == 1 (the prewarmed client's cache key, incl. the
#      plugin digest, matches the first turn's key, so it is reused not evicted).
#   3. SWALLOW — a prewarm whose connect() raises must NOT propagate and must NOT
#      leave broken state: a later run on the same session still completes.

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"
_MODEL_ROUTER = "pocketpaw.agents.model_router.ModelRouter"
# ``run()`` / ``prewarm()`` import these lazily from ``pocketpaw.skills`` —
# patch them there.
_MATERIALIZE = "pocketpaw.skills.materialize_run_skills"
_CLEANUP = "pocketpaw.skills.cleanup_run_skills"


# ===========================================================================
# Fakes — same shape as the warm-reuse harness (connect() bumps a counter).
# ===========================================================================


def _make_settings(**overrides):
    defaults = {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        # Smart routing OFF: the model stays constant regardless of message, so
        # a prewarm with no real message assembles the SAME ``model`` the first
        # turn will. With routing ON the model is message-derived and a
        # placeholder-message prewarm could mismatch — the trigger skips it then.
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": None,
        "claude_sdk_max_turns": None,
        # Bundled-skills plugin OFF → ``bundled`` is deterministically False.
        "sdk_load_bundled_skills": False,
        "anthropic_api_key": "sk-test-key",
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


class _Options:
    """Real options object so ``_plugin_digest`` / ``_client_cache_key`` and the
    dispatch can read ``system_prompt`` / ``model`` / ``allowed_tools`` / ``plugins``."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])


class _ResultMsg:
    def __init__(self):
        self.is_error = False
        self.result = "ok"
        self.total_cost_usd = None
        self.usage = {}


class _FakeClient:
    """Warm persistent client whose connect() bumps a shared counter. An
    optional ``fail_connect`` makes connect() raise once, to prove prewarm
    swallows the failure."""

    def __init__(self, counter: list[int], options=None, *, fail_connect=False, **_kw):
        self._counter = counter
        self.options = options
        self._fail_connect = fail_connect
        self.connected = False
        self.disconnected = False
        self.queries: list[str] = []

    async def connect(self, prompt=None):
        self._counter[0] += 1
        if self._fail_connect:
            raise RuntimeError("simulated cold connect failure")
        self.connected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_messages(self):
        yield _ResultMsg()

    async def disconnect(self):
        self.connected = False
        self.disconnected = True

    async def interrupt(self):
        pass


def _wire_fakes(sdk, counter, *, fail_connect=False):
    sdk._ClaudeAgentOptions = _Options
    sdk._ResultMessage = _ResultMsg
    sdk._ClaudeSDKClient = lambda **kwargs: _FakeClient(
        counter, fail_connect=fail_connect, **kwargs
    )
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None


def _make_sdk(counter, settings=None, *, fail_connect=False):
    s = settings or _make_settings()
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(s)
    sdk._sdk_available = True
    sdk._cli_available = True
    _wire_fakes(sdk, counter, fail_connect=fail_connect)
    return sdk


def _patched(fn):
    """Run ``fn()`` (an async no-arg coroutine factory) under the standard LLM /
    router / MCP patches the run+prewarm paths both need."""

    async def _inner():
        selection = ModelSelection(
            complexity=TaskComplexity.MODERATE,
            model="claude-sonnet-4-5-20250929",
            reason="test",
        )
        with patch(_LLM_CLIENT) as mock_resolve:
            mock_llm = MagicMock()
            mock_llm.is_ollama = False
            mock_llm.is_openai_compatible = False
            mock_llm.is_gemini = False
            mock_llm.is_litellm = False
            mock_llm.is_openrouter = False
            mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
            mock_resolve.return_value = mock_llm
            with patch(_MODEL_ROUTER) as MockRouter:
                MockRouter.return_value.classify.return_value = selection
                with patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}):
                    return await fn()

    return _inner


async def _drive_run(sdk, message, *, skill_names=frozenset(), session_key="s1"):
    """Drive one full run() turn to completion under the standard patches."""

    async def _go():
        events = []
        async for ev in sdk.run(
            message,
            system_prompt="identity",
            session_key=session_key,
            skill_names=skill_names,
        ):
            events.append(ev)
        return events

    return await _patched(_go)()


async def _drive_prewarm(sdk, *, skill_names=frozenset(), session_key="s1"):
    """Drive one prewarm() call to completion under the standard patches."""

    async def _go():
        await sdk.prewarm(
            session_key=session_key,
            system_prompt="identity",
            skill_names=skill_names,
        )

    return await _patched(_go)()


# ===========================================================================
# 1. REUSE (no skills) — prewarm then run reuses, connect_count == 1
# ===========================================================================


async def test_prewarm_then_run_reuses_warm_client_no_skills():
    """prewarm a session, then run() on the SAME session → connect_count == 1.

    FAILS today (``prewarm`` does not exist → AttributeError). PASSES after the
    fix: prewarm eagerly connects the warm client; the first run finds a
    matching cache key and reuses it instead of paying a second connect."""
    counter = [0]
    sdk = _make_sdk(counter)

    await _drive_prewarm(sdk, session_key="s1", skill_names=frozenset())
    assert counter[0] == 1, "prewarm must eagerly connect the warm client exactly once"

    ev = await _drive_run(sdk, "first real turn", session_key="s1", skill_names=frozenset())
    assert any(e.type == "done" for e in ev)
    assert counter[0] == 1, (
        f"the first real turn must REUSE the prewarmed client (connect_count == 1); "
        f"got {counter[0]} — the prewarmed client was evicted, so prewarm bought "
        f"nothing (a net loss: it paid a connect the run then threw away)"
    )


# ===========================================================================
# 2. REUSE (with skills) — matched plugin digest, connect_count == 1
# ===========================================================================


async def test_prewarm_then_run_same_skills_reuses_warm_client(tmp_path):
    """prewarm with skills X, then run with the SAME skills X → connect_count
    == 1. The prewarmed client's cache key (incl. the plugin digest) must match
    the first turn's key, or the run evicts it and re-connects."""
    counter = [0]
    sdk = _make_sdk(counter)
    stable = tmp_path / "skills_plugin"
    stable.mkdir()

    with patch(_MATERIALIZE, return_value=stable), patch(_CLEANUP):
        await _drive_prewarm(sdk, session_key="s1", skill_names=frozenset({"skillA"}))
        assert counter[0] == 1, "prewarm must connect the skill-bearing client once"

        ev = await _drive_run(
            sdk, "first real turn", session_key="s1", skill_names=frozenset({"skillA"})
        )

    assert any(e.type == "done" for e in ev)
    assert counter[0] == 1, (
        f"a same-skill run after a same-skill prewarm must REUSE the warm client "
        f"(connect_count == 1); got {counter[0]} — the prewarmed client's plugin "
        f"digest did not match the run's, so prewarm was wasted (evict-churn)"
    )


# ===========================================================================
# 3. SWALLOW — a failing prewarm must never break a later turn
# ===========================================================================


async def test_prewarm_connect_failure_is_swallowed_and_run_still_works():
    """A prewarm whose connect() raises must NOT propagate and must NOT poison
    later turns: a subsequent run() on the same session still completes."""
    counter = [0]
    # First client (the prewarm's) raises in connect(); rewire to a healthy
    # factory before the run so the run's fresh connect succeeds.
    sdk = _make_sdk(counter, fail_connect=True)

    # Must not raise even though connect() blows up inside prewarm.
    await _drive_prewarm(sdk, session_key="s1", skill_names=frozenset())

    # Prewarm's failed connect must have left no live/poisoned client behind.
    assert sdk._client is None, "a failed prewarm must not leave a broken client wired in"
    assert sdk._client_in_use is False, "a failed prewarm must not leave the lease held"

    # Heal the SDK factory and prove a real turn still works end to end.
    _wire_fakes(sdk, counter, fail_connect=False)
    ev = await _drive_run(sdk, "turn after failed prewarm", session_key="s1")
    assert any(e.type == "done" for e in ev), (
        "a run after a failed prewarm must still complete — prewarm errors are "
        "swallowed and never break a later turn"
    )


# ===========================================================================
# 4. CONCURRENCY — a prewarm racing the first run must not double-connect
# ===========================================================================


async def test_concurrent_prewarm_and_run_single_connect():
    """prewarm and the first run() fire CONCURRENTLY (the real trigger fires
    prewarm as a background task just before the run). The client lock must
    serialize them so exactly ONE connect() happens and the run still completes
    — never two subprocesses, never the run evicting a mid-connect prewarm."""
    import asyncio

    counter = [0]
    sdk = _make_sdk(counter)

    # Make connect() slow enough that the two coroutines genuinely overlap: the
    # run will reach _get_or_create_client while prewarm is still inside it.
    orig_factory = sdk._ClaudeSDKClient

    def _slow_factory(**kwargs):
        c = orig_factory(**kwargs)
        orig_connect = c.connect

        async def _slow_connect(prompt=None):
            await asyncio.sleep(0.02)
            await orig_connect(prompt)

        c.connect = _slow_connect
        return c

    sdk._ClaudeSDKClient = _slow_factory

    async def _go():
        prewarm_task = asyncio.create_task(
            _drive_prewarm(sdk, session_key="s1", skill_names=frozenset())
        )
        # Give prewarm a beat to enter the lock + start its slow connect, then
        # start the run so the two overlap inside _get_or_create_client.
        await asyncio.sleep(0.005)
        ev = await _drive_run(sdk, "first turn", session_key="s1", skill_names=frozenset())
        await prewarm_task
        return ev

    ev = await _patched(_go)()
    assert any(e.type == "done" for e in ev), "the run must complete despite the race"
    assert counter[0] == 1, (
        f"a prewarm racing the first run must yield exactly ONE connect "
        f"(the lock serializes them; the loser reuses the winner's client); "
        f"got {counter[0]} — the race double-connected or churned the subprocess"
    )


def test_prewarm_exists_with_expected_signature() -> None:
    """Guard the public contract: ``prewarm`` is an async method taking a
    keyword ``session_key`` and an optional ``skill_names`` frozenset."""
    import inspect

    assert inspect.iscoroutinefunction(ClaudeSDKBackend.prewarm)
    params = inspect.signature(ClaudeSDKBackend.prewarm).parameters
    assert "session_key" in params
    assert "skill_names" in params
    assert params["skill_names"].default == frozenset()

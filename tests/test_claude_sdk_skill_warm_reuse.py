# tests/test_claude_sdk_skill_warm_reuse.py
# Created: 2026-06-13 (fix/claude-sdk-warm-client-skills) — pins the latency fix
# that lets a skill/tool-bearing run REUSE the warm persistent CLI subprocess
# instead of re-spawning a fresh stateless query every turn (~6s/turn floor).
#
# Four groups:
#   1. KEY-LEVEL (pure)   — ``_plugin_digest`` / ``_client_cache_key`` fold in
#      the skill IDENTITY (sorted names + bundled flag), never the materialized
#      PATH (which is a fresh ``mkdtemp`` per run and would defeat reuse).
#   2. BEHAVIORAL         — the latency invariant: two identical-skill turns on
#      one session ``connect()`` the subprocess ONCE (re-spawn → count==2 today,
#      reuse → count==1 after the fix).
#   3. NO-LEAK            — a skill turn then a non-skill turn forces a fresh
#      connect and the non-skill turn's options carry NO per-run skills plugin.
#   4. LIFECYCLE          — the materialized dir a live warm client references
#      SURVIVES turn 1's finally and is removed on ``cleanup()``.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"
_MODEL_ROUTER = "pocketpaw.agents.model_router.ModelRouter"
# ``run()`` imports these lazily from ``pocketpaw.skills`` — patch them there.
_MATERIALIZE = "pocketpaw.skills.materialize_run_skills"
_CLEANUP = "pocketpaw.skills.cleanup_run_skills"


# ===========================================================================
# 1. KEY-LEVEL (pure) — digest the skill identity, never the path
# ===========================================================================


def test_plugin_digest_same_skill_set_is_stable() -> None:
    """Same skill names + same bundled flag → identical digest. This stability
    across runs is the whole point: ``materialize_run_skills`` mints a fresh
    ``mkdtemp`` path every turn, so keying on identity (not path) is what lets
    the warm client be reused."""
    d1 = ClaudeSDKBackend._plugin_digest(frozenset({"skillA", "skillB"}), bundled=True)
    d2 = ClaudeSDKBackend._plugin_digest(frozenset({"skillB", "skillA"}), bundled=True)
    assert d1 == d2 and d1 != ""


def test_plugin_digest_differs_by_skill_set() -> None:
    """A different skill set must produce a different digest (and thus a
    different cache key) so the warm subprocess rebuilds with the new skills."""
    a = ClaudeSDKBackend._plugin_digest(frozenset({"skillA"}), bundled=False)
    b = ClaudeSDKBackend._plugin_digest(frozenset({"skillB"}), bundled=False)
    assert a != b


def test_plugin_digest_skillset_vs_empty_no_collision() -> None:
    """A skill run and a no-skill run must NOT collide — otherwise a warm client
    connected for a skill turn would be reused (silently skill-less) on a plain
    turn, or vice versa."""
    empty = ClaudeSDKBackend._plugin_digest(frozenset(), bundled=False)
    with_skill = ClaudeSDKBackend._plugin_digest(frozenset({"skillA"}), bundled=False)
    assert empty == "" and with_skill != ""
    assert empty != with_skill


def test_plugin_digest_bundled_flag_changes_digest() -> None:
    """The bundled-skills plugin participating or not is part of the plugin set
    identity — flipping it must change the digest."""
    no_bundle = ClaudeSDKBackend._plugin_digest(frozenset({"skillA"}), bundled=False)
    with_bundle = ClaudeSDKBackend._plugin_digest(frozenset({"skillA"}), bundled=True)
    assert no_bundle != with_bundle
    # Bundled-only (no per-entity skills) is still a non-empty digest.
    assert ClaudeSDKBackend._plugin_digest(frozenset(), bundled=True) != ""


def _opts(system_prompt="IDENTITY", *, model="claude", tools=("Read",)):
    return SimpleNamespace(
        model=model,
        allowed_tools=list(tools),
        system_prompt=system_prompt,
    )


def test_cache_key_folds_in_plugin_digest() -> None:
    """The persistent-client cache key now folds in the plugin digest, so equal
    skill sets → equal key and different skill sets → different key. This is the
    direct inverse of the OLD behavior (key ignored plugins entirely)."""
    base = _opts()
    digest_a = ClaudeSDKBackend._plugin_digest(frozenset({"skillA"}), bundled=False)
    digest_b = ClaudeSDKBackend._plugin_digest(frozenset({"skillB"}), bundled=False)

    key_a1 = ClaudeSDKBackend._client_cache_key(base, session_key="s1", plugin_digest=digest_a)
    key_a2 = ClaudeSDKBackend._client_cache_key(base, session_key="s1", plugin_digest=digest_a)
    key_b = ClaudeSDKBackend._client_cache_key(base, session_key="s1", plugin_digest=digest_b)
    key_none = ClaudeSDKBackend._client_cache_key(base, session_key="s1")

    assert key_a1 == key_a2, "same skill set → same key (warm reuse)"
    assert key_a1 != key_b, "different skill set → different key (rebuild)"
    assert key_a1 != key_none, "skill set vs no-skill must not collide"


def test_cache_key_default_plugin_digest_unchanged() -> None:
    """Omitting ``plugin_digest`` (the default) must produce the same key as an
    empty digest, so every existing non-skill call site is byte-for-byte
    unaffected."""
    base = _opts()
    assert ClaudeSDKBackend._client_cache_key(
        base, session_key="s1"
    ) == ClaudeSDKBackend._client_cache_key(base, session_key="s1", plugin_digest="")


# ===========================================================================
# Behavioral / lifecycle harness — drive the full run() with fakes, the same
# way tests/test_fast_path.py does.
# ===========================================================================


def _make_settings(**overrides):
    defaults = {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": None,
        "claude_sdk_max_turns": None,
        # Keep the bundled-skills plugin OFF so ``bundled`` is deterministically
        # False and no real bundled plugin dir is wired into options.
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
    """Minimal stand-in for the SDK ResultMessage sentinel."""

    def __init__(self):
        self.is_error = False
        self.result = "ok"
        self.total_cost_usd = None
        self.usage = {}


class _FakeClient:
    """Warm persistent client whose connect() bumps a shared counter."""

    def __init__(self, counter: list[int], options=None, **_kw):
        self._counter = counter
        self.options = options
        self.connected = False
        self.disconnected = False
        self.queries: list[str] = []

    async def connect(self, prompt=None):
        self._counter[0] += 1
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


def _wire_fakes(sdk, counter):
    sdk._ClaudeAgentOptions = _Options
    sdk._ResultMessage = _ResultMsg
    sdk._ClaudeSDKClient = lambda **kwargs: _FakeClient(counter, **kwargs)
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None


def _make_sdk(counter, settings=None):
    s = settings or _make_settings()
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(s)
    sdk._sdk_available = True
    sdk._cli_available = True
    _wire_fakes(sdk, counter)
    return sdk


async def _drive(sdk, message, *, skill_names=frozenset(), session_key="s1"):
    """Drive one full run() turn to completion under the standard patches."""
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
                events = []
                async for ev in sdk.run(
                    message,
                    system_prompt="identity",
                    session_key=session_key,
                    skill_names=skill_names,
                ):
                    events.append(ev)
    return events


# ===========================================================================
# 2. BEHAVIORAL — the latency invariant
# ===========================================================================


async def test_two_same_skill_turns_reuse_warm_client(tmp_path):
    """THE FIX: two turns with the SAME ``skill_names`` on one session must
    ``connect()`` the subprocess exactly ONCE.

    FAILS today (skill runs bypass the warm client and re-spawn → count == 2).
    PASSES after the fix (the plugin digest is in the cache key, so the warm
    client is reused → count == 1).
    """
    counter = [0]
    sdk = _make_sdk(counter)
    stable = tmp_path / "skills_plugin"
    stable.mkdir()

    with patch(_MATERIALIZE, return_value=stable) as mat, patch(_CLEANUP):
        ev1 = await _drive(sdk, "turn one", skill_names=frozenset({"skillA"}))
        ev2 = await _drive(sdk, "turn two", skill_names=frozenset({"skillA"}))

    assert any(e.type == "done" for e in ev1)
    assert any(e.type == "done" for e in ev2)
    # Isolate key behavior from mkdtemp: materialize is patched to a stable path,
    # so any re-spawn is a cache-key/lifecycle failure, not a path-churn artifact.
    assert mat.call_count >= 1
    assert counter[0] == 1, (
        f"expected the warm subprocess to be reused across two identical-skill "
        f"turns (connect_count == 1); got {counter[0]} — a skill run is still "
        f"re-spawning a fresh subprocess every turn (the ~6s/turn latency bug)"
    )


# ===========================================================================
# 3. NO-LEAK — a skill turn must not poison a later non-skill turn
# ===========================================================================


async def test_skill_then_non_skill_turn_rebuilds_without_plugin(tmp_path):
    """A skill turn followed by a non-skill turn on the same session must (a)
    rebuild the subprocess (different plugin digest → fresh connect, count == 2)
    and (b) the non-skill turn's options must carry NO per-run skills plugin."""
    counter = [0]
    sdk = _make_sdk(counter)
    stable = tmp_path / "skills_plugin"
    stable.mkdir()

    captured_clients: list[_FakeClient] = []
    orig_factory = sdk._ClaudeSDKClient

    def _factory(**kwargs):
        c = orig_factory(**kwargs)
        captured_clients.append(c)
        return c

    sdk._ClaudeSDKClient = _factory

    with patch(_MATERIALIZE, return_value=stable), patch(_CLEANUP):
        await _drive(sdk, "skill turn", skill_names=frozenset({"skillA"}))
        await _drive(sdk, "plain turn", skill_names=frozenset())

    assert counter[0] == 2, (
        f"a non-skill turn after a skill turn must rebuild the subprocess "
        f"(different plugin digest); got connect_count == {counter[0]}"
    )
    # The most recent client (the plain turn) must carry no per-run skills plugin.
    plain_opts = captured_clients[-1].options
    plugins = getattr(plain_opts, "plugins", []) or []
    paths = [p.get("path") for p in plugins if isinstance(p, dict)]
    assert str(stable) not in paths, (
        f"the non-skill turn's options leaked the per-run skills plugin {stable}: plugins={plugins}"
    )


# ===========================================================================
# 4. LIFECYCLE — the live subprocess's dir survives the per-run finally
# ===========================================================================


async def test_warm_client_dir_survives_finally_and_cleaned_on_cleanup(tmp_path):
    """Two same-skill turns reuse the client; the materialized dir the live
    subprocess references must still EXIST after turn 1's per-run finally (the
    subprocess holds that path from its first connect), and must be removed on
    ``backend.cleanup()``."""
    counter = [0]
    sdk = _make_sdk(counter)

    stable = tmp_path / "skills_plugin"

    def _materialize(skill_names, run_id=None):
        # Re-create the stable dir on disk if a prior cleanup removed it; return
        # the SAME path every call (what an identity-keyed reuse depends on).
        stable.mkdir(exist_ok=True)
        return stable

    import shutil

    def _cleanup(root):
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    with patch(_MATERIALIZE, side_effect=_materialize), patch(_CLEANUP, side_effect=_cleanup):
        await _drive(sdk, "turn one", skill_names=frozenset({"skillA"}))
        # Turn 1's finally has run. The warm client is still live and references
        # ``stable`` — it must NOT have been rmtree'd by the per-run cleanup.
        assert stable.exists(), (
            "the per-run finally removed the materialized dir that the live warm "
            "subprocess still references — turn 2 would point at a deleted path"
        )

        await _drive(sdk, "turn two", skill_names=frozenset({"skillA"}))
        assert counter[0] == 1, "turn two must reuse the warm client (connect_count == 1)"
        assert stable.exists(), "the reused dir must still exist after turn two"

        # cleanup() tears down the warm client AND sweeps its materialized dir.
        await sdk.cleanup()

    assert sdk._client is None
    assert not stable.exists(), (
        "cleanup() must remove the materialized skills dir once the warm client "
        "that owned it is disconnected"
    )


def test_run_still_accepts_skill_names_kwarg() -> None:
    """Guard the public contract the fix must preserve: ``run`` still accepts
    ``skill_names`` defaulting to an empty frozenset."""
    import inspect

    params = inspect.signature(ClaudeSDKBackend.run).parameters
    assert "skill_names" in params
    assert params["skill_names"].default == frozenset()

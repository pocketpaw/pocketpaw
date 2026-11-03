# tests/test_claude_sdk_skill_warm_reuse.py
# Created: 2026-06-13 (fix/claude-sdk-warm-client-skills) — pins the latency fix
# that lets a skill/tool-bearing run REUSE the warm persistent CLI subprocess
# instead of re-spawning a fresh stateless query every turn (~6s/turn floor).
#
# Six groups:
#   1. KEY-LEVEL (pure)   — ``_plugin_digest`` / ``_client_cache_key`` fold in
#      the skill IDENTITY (sorted names + bundled flag + content fingerprint),
#      never a per-run path, so unchanged skills keep the key stable.
#   2. BEHAVIORAL         — the latency invariant: two identical-skill turns on
#      one session ``connect()`` the subprocess ONCE (re-spawn → count==2 today,
#      reuse → count==1 after the fix).
#   3. NO-LEAK            — a skill turn then a non-skill turn forces a fresh
#      connect and the non-skill turn's options carry NO per-run skills plugin.
#   4. LIFECYCLE          — the materialized dir a live warm client references
#      SURVIVES turn 1's finally and is removed on ``cleanup()``.
#   5. LIVE PATH          — with the real materializer: no query ever reaches a
#      client whose connected plugin dir is gone (same-digest rebuild, prewarm
#      eviction, prewarm losing the lease, stateless sibling, reaped dir).
#   6. OWNERSHIP          — an in-flight run keeps its dir through a concurrent
#      cleanup(), an edited skill rebuilds the warm client, prewarm leaks no
#      reference, and a leased client with no plugin dir is never driven.

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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


async def test_warm_client_dir_survives_finally_and_cleaned_on_cleanup(real_skills):
    """Two same-skill turns reuse the client; the materialized dir the live
    subprocess references must still EXIST after turn 1's per-run finally (the
    subprocess holds that path from its first connect), and must be removed on
    ``backend.cleanup()``. Uses the real reference-counted materializer: a fake
    cleanup that rmtrees on every call would ignore the other holders."""
    counter = [0]
    sdk = _make_sdk(counter)

    await _drive(sdk, "turn one", skill_names=_SKILLS)
    (stable,) = (Path(p["path"]) for p in sdk._client.options.plugins)
    # Turn 1's finally has run. The warm client is still live and references
    # ``stable`` — the per-run release must NOT have removed it.
    assert stable.exists(), (
        "the per-run finally removed the materialized dir that the live warm "
        "subprocess still references — turn 2 would point at a deleted path"
    )

    await _drive(sdk, "turn two", skill_names=_SKILLS)
    assert counter[0] == 1, "turn two must reuse the warm client (connect_count == 1)"
    assert stable.exists(), "the reused dir must still exist after turn two"

    # cleanup() tears down the warm client AND releases its materialized dir.
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


# ===========================================================================
# 5. LIVE PATH — a client is never sent a query while its plugin dir is gone
#
# Driven with the REAL ``materialize_run_skills`` / ``cleanup_run_skills`` over
# a throwaway installed-skill set, so the assertions are about the filesystem:
# at every ``query()`` the ``plugins=`` paths the client was CONNECTED with must
# exist. In production the CLI resolves the Skill tool against that path, and a
# deleted dir surfaced as "Unknown skill: pocketpaw-site ... (path not found)".
# ===========================================================================

_SKILLS = frozenset({"site-skill", "craft-skill"})


@pytest.fixture
def real_skills(tmp_path, monkeypatch):
    """A SkillLoader with two on-disk skills and no packaged bundle, so the real
    materializer copies them into a plugin dir."""
    from pocketpaw.skills import loader as loader_mod
    from pocketpaw.skills import materialize as materialize_mod
    from pocketpaw.skills.loader import Skill, SkillLoader

    src = tmp_path / "installed"
    skills = {}
    for name in _SKILLS:
        d = src / name
        d.mkdir(parents=True)
        md = d / "SKILL.md"
        md.write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8")
        skills[name] = Skill(name=name, description="d", content="body", path=md)
    fake_loader = SkillLoader()
    fake_loader._skills = skills
    fake_loader._loaded = True
    monkeypatch.setattr(loader_mod, "_skill_loader", fake_loader)
    monkeypatch.setattr(materialize_mod, "_bundled_plugin_root", lambda: None)
    return skills


class _PathCheckingClient(_FakeClient):
    """Records, at every ``query()``, which local plugin paths it was connected
    with no longer exist on disk."""

    def __init__(self, counter, log, options=None, **kw):
        super().__init__(counter, options=options, **kw)
        self._log = log

    def plugin_paths(self) -> list[Path]:
        plugins = getattr(self.options, "plugins", None) or []
        return [Path(p["path"]) for p in plugins if isinstance(p, dict) and p.get("path")]

    async def query(self, prompt, session_id="default"):
        missing = [str(p) for p in self.plugin_paths() if not p.exists()]
        self._log.append((prompt, missing))
        await super().query(prompt, session_id)


def _make_checking_sdk(counter, log):
    sdk = _make_sdk(counter)
    sdk._ClaudeSDKClient = lambda **kwargs: _PathCheckingClient(counter, log, **kwargs)
    return sdk


@contextlib.contextmanager
def _run_patches():
    selection = ModelSelection(
        complexity=TaskComplexity.MODERATE, model="claude-sonnet-4-5-20250929", reason="test"
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
                yield


async def _turn(sdk, message, **kwargs):
    with _run_patches():
        return [
            ev
            async for ev in sdk.run(
                message, system_prompt="identity", session_key="s1", skill_names=_SKILLS, **kwargs
            )
        ]


def _assert_no_query_saw_missing_dir(log):
    assert log, "no query reached a persistent client"
    bad = [(prompt[:40], missing) for prompt, missing in log if missing]
    assert not bad, (
        "a warm client was sent a query after the skills plugin dir it was "
        f"connected with had been deleted (the CLI answers 'Unknown skill'): {bad}"
    )


async def test_rebuild_with_same_skill_set_keeps_new_clients_dir(real_skills):
    """A rebuild that keeps the SAME skill set (here a per-send model pick; in
    production also a prompt-digest or history mismatch) must not delete the
    dir the replacement client is being connected with, and later turns that
    reuse that client must still find it."""
    counter, log = [0], []
    sdk = _make_checking_sdk(counter, log)
    try:
        await _turn(sdk, "turn one")
        await _turn(sdk, "turn two", model_override="claude-opus-4-1")
        await _turn(sdk, "turn three", model_override="claude-opus-4-1")
        assert counter[0] == 2, "turn three must reuse the client turn two rebuilt"
        for p in sdk._client.plugin_paths():
            assert p.exists(), f"the live warm client's plugin dir {p} was deleted"
        _assert_no_query_saw_missing_dir(log)
    finally:
        await sdk.cleanup()


async def test_prewarm_then_turns_keep_the_warm_clients_dir(real_skills):
    """The production sequence: prewarm, turn one evicts the prewarmed client
    (its key differs, same skills), turn two reuses turn one's client. Every
    query must find the plugin dir its client was connected with."""
    counter, log = [0], []
    sdk = _make_checking_sdk(counter, log)
    try:
        with _run_patches():
            await sdk.prewarm(
                session_key="s1",
                system_prompt="identity",
                skill_names=_SKILLS,
                system_prompt_digest="prewarm-digest",
            )
        assert counter[0] == 1
        await _turn(sdk, "turn one")
        await _turn(sdk, "turn two")
        assert counter[0] == 2, "turn two must reuse turn one's client"
        _assert_no_query_saw_missing_dir(log)
    finally:
        await sdk.cleanup()


async def test_prewarm_losing_the_lease_keeps_the_runs_dir(real_skills):
    """A prewarm that finds a run already holding the lease returns without a
    client. The run built its options from the same cached dir, so prewarm must
    not delete it out from under the run."""
    counter, log = [0], []
    sdk = _make_checking_sdk(counter, log)
    run_paths: list[Path] = []
    orig = sdk._get_or_create_client

    async def _racing(options, **kwargs):
        if kwargs.get("prewarm"):
            # The first run builds its options now (reusing the dir prewarm
            # cached) and takes the lease before prewarm reaches the lock.
            built = await sdk._build_options(
                "turn one",
                system_prompt="identity",
                session_key="s1",
                deny_mcp_tool_ids=frozenset(),
                allow_sdk_tools=frozenset(),
                allow_mcp_tool_ids=None,
                skill_names=_SKILLS,
                stderr_sink=[],
            )
            run_paths.extend(Path(p["path"]) for p in built.options.plugins if isinstance(p, dict))
            sdk._client_in_use = True
            try:
                return await orig(options, **kwargs)
            finally:
                sdk._client_in_use = False
        return await orig(options, **kwargs)

    sdk._get_or_create_client = _racing
    try:
        with _run_patches():
            await sdk.prewarm(session_key="s1", system_prompt="identity", skill_names=_SKILLS)
        assert run_paths, "the racing run must have been given a plugin dir"
        for p in run_paths:
            assert p.exists(), f"prewarm deleted the dir {p} the racing run connects with"
    finally:
        await sdk.cleanup()


async def test_stateless_sibling_run_does_not_delete_warm_dir(real_skills):
    """A run that falls back to stateless because a sibling holds the lease owns
    its own plugin reference; releasing it must never remove the dir the warm
    client is connected with."""
    counter, log = [0], []
    sdk = _make_checking_sdk(counter, log)
    try:
        await _turn(sdk, "turn one")
        warm_paths = sdk._client.plugin_paths()
        sdk._client_in_use = True  # a sibling run holds the lease
        sdk._query = MagicMock(side_effect=RuntimeError("no stateless CLI in tests"))
        await _turn(sdk, "sibling turn")
        sdk._client_in_use = False
        for p in warm_paths:
            assert p.exists(), f"the stateless sibling removed the warm client's dir {p}"
        await _turn(sdk, "turn two")
        _assert_no_query_saw_missing_dir(log)
    finally:
        await sdk.cleanup()


async def test_reuse_rebuilds_when_plugin_dir_vanished(real_skills):
    """Defence in depth: if the dir a warm client was connected with disappears
    anyway (an external tmp reaper) and the skills changed meanwhile, so the
    fresh dir lands at a new path, the next turn must rebuild the client
    instead of reusing one that cannot load skills."""
    counter, log = [0], []
    sdk = _make_checking_sdk(counter, log)
    try:
        await _turn(sdk, "turn one")
        for p in sdk._client.plugin_paths():
            shutil.rmtree(p)
        md = real_skills["site-skill"].path
        md.write_text(md.read_text(encoding="utf-8") + "\nupdated\n", encoding="utf-8")
        await _turn(sdk, "turn two")
        assert counter[0] == 2, "a client whose plugin dir vanished must be rebuilt"
        _assert_no_query_saw_missing_dir(log)
    finally:
        await sdk.cleanup()


async def test_vanished_dir_with_same_skills_is_rematerialized_in_place(real_skills):
    """A reaped dir with unchanged skills is rebuilt at the SAME content path,
    so the warm client is reused and finds its plugin again."""
    counter, log = [0], []
    sdk = _make_checking_sdk(counter, log)
    try:
        await _turn(sdk, "turn one")
        for p in sdk._client.plugin_paths():
            shutil.rmtree(p)
        await _turn(sdk, "turn two")
        assert counter[0] == 1
        _assert_no_query_saw_missing_dir(log)
    finally:
        await sdk.cleanup()


async def test_cleanup_releases_materialized_dir(real_skills):
    """``cleanup()`` releases the dir once no live client in the process uses it."""
    counter, log = [0], []
    sdk = _make_checking_sdk(counter, log)
    await _turn(sdk, "turn one")
    paths = sdk._client.plugin_paths()
    assert paths and all(p.exists() for p in paths)
    await sdk.cleanup()
    assert not any(p.exists() for p in paths), "cleanup() must release the skills dir"


# ===========================================================================
# 6. OWNERSHIP — every in-flight run holds its own reference, skill edits
#    rebuild the warm client, and no reference leaks.
# ===========================================================================


class _HookedClient(_PathCheckingClient):
    """Runs ``before_query`` (if set on the class instance) before checking the
    plugin paths, to act out something another session does mid-turn."""

    before_query = None

    async def query(self, prompt, session_id="default"):
        hook = type(self).before_query
        if hook is not None:
            await hook(self, prompt)
        await super().query(prompt, session_id)


def _make_hooked_sdk(counter, log, clients):
    sdk = _make_sdk(counter)

    def _factory(**kwargs):
        c = _HookedClient(counter, log, **kwargs)
        clients.append(c)
        return c

    sdk._ClaudeSDKClient = _factory
    return sdk


async def test_cleanup_mid_turn_keeps_in_flight_leased_runs_dir(real_skills, monkeypatch):
    """``stop()`` for another session reaches ``cleanup()``, which releases the
    cache's references. A run streaming on a supervisor-leased client holds its
    own reference, so its dir must still exist when its query lands."""
    from pocketpaw.agents.backend import LeasedClient

    counter, log, clients = [0], [], []
    sdk = _make_hooked_sdk(counter, log, clients)
    captured: dict = {}

    def _on_built(client, key, teardown):
        captured.update(client=client, key=key)

    try:
        await _turn(sdk, "turn one", on_client_built=_on_built)
        lease = LeasedClient(client=captured["client"], options_key=captured["key"])

        async def _stop_other_session(client, prompt):
            if prompt == "turn two":
                await sdk.cleanup()  # what stop(session_key=<other>) ends in

        monkeypatch.setattr(_HookedClient, "before_query", _stop_other_session)
        await _turn(sdk, "turn two", warm_client=lease)
        assert captured["client"].queries[-1] == "turn two", "turn two must reuse the lease"
        _assert_no_query_saw_missing_dir(log)
    finally:
        await sdk.cleanup()


async def test_skill_edit_rebuilds_warm_client_on_new_dir(real_skills):
    """An edited skill must reach the warm client: the content change alters
    the key, the client is rebuilt on the new dir, and the old dir is released."""
    counter, log, clients = [0], [], []
    sdk = _make_hooked_sdk(counter, log, clients)
    try:
        await _turn(sdk, "turn one")
        (old_dir,) = sdk._client.plugin_paths()
        md = real_skills["site-skill"].path
        md.write_text(md.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
        await _turn(sdk, "turn two")
        assert counter[0] == 2, "an edited skill must rebuild the warm client"
        (new_dir,) = sdk._client.plugin_paths()
        assert new_dir != old_dir
        body = (new_dir / "skills" / "site-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "edited" in body
        assert not old_dir.exists(), "the superseded dir must be released"
        await _turn(sdk, "turn three")
        assert counter[0] == 2, "unchanged skills must still reuse the warm client"
        _assert_no_query_saw_missing_dir(log)
    finally:
        await sdk.cleanup()


async def test_prewarm_built_during_a_run_leaks_no_reference(real_skills):
    """Prewarm whose options were built while a run held the lease, then
    connected after it finished, must leave nothing behind after cleanup()."""
    counter, log, clients = [0], [], []
    sdk = _make_hooked_sdk(counter, log, clients)
    orig_build = sdk._build_options

    async def _build_while_leased(*args, **kwargs):
        sdk._client_in_use = True
        try:
            return await orig_build(*args, **kwargs)
        finally:
            sdk._client_in_use = False

    sdk._build_options = _build_while_leased
    with _run_patches():
        await sdk.prewarm(session_key="s1", system_prompt="identity", skill_names=_SKILLS)
    assert counter[0] == 1
    paths = sdk._client.plugin_paths()
    assert paths and all(p.exists() for p in paths)
    await sdk.cleanup()
    assert not any(p.exists() for p in paths), "prewarm leaked a reference to the skills dir"


async def test_leased_client_with_vanished_dir_is_not_reused(real_skills):
    """The leased-dispatch guard: a key-matching leased client whose plugin dir
    is gone at dispatch time is rebuilt, never driven."""
    from pocketpaw.agents.backend import LeasedClient

    counter, log, clients = [0], [], []
    sdk = _make_hooked_sdk(counter, log, clients)
    captured: dict = {}

    def _on_built(client, key, teardown):
        captured.update(client=client, key=key)

    try:
        await _turn(sdk, "turn one", on_client_built=_on_built)
        leased = captured["client"]
        lease = LeasedClient(client=leased, options_key=captured["key"])
        orig_dispatch = sdk._leased_dispatch

        async def _reap_then_dispatch(**kwargs):
            for p in leased.plugin_paths():
                shutil.rmtree(p, ignore_errors=True)
            return await orig_dispatch(**kwargs)

        sdk._leased_dispatch = _reap_then_dispatch
        await _turn(sdk, "turn two", warm_client=lease, on_client_built=_on_built)
        assert leased.queries == ["turn one"], "a leased client with no plugin dir was driven"
        assert captured["client"] is not leased, "the turn must build a fresh client"
    finally:
        await sdk.cleanup()

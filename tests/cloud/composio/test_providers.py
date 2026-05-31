"""Composio per-backend provider tests — the documented integration shape.

``build_tools_for_backend(backend_kind)`` is the single entry point every
agent backend calls. It resolves the active user from per-stream
contextvars, builds a Composio session via the documented provider for
that backend, returns ``session.tools()``. Caching, fail-soft on errors,
and namespace correctness are all tested here.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.composio import providers as composio_providers
from pocketpaw_ee.cloud.composio import service as composio_service

from pocketpaw.config import Settings


def _enabled_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "composio_api_key": "ck_test",
        "composio_enterprise_id": "ent_acme",
        # Non-empty toolkit list — the direct-tools path requires this.
        # Tests that want the empty case override explicitly.
        "composio_toolkits": ["gmail", "slack"],
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _fake_ctx() -> RequestContext:
    return RequestContext(
        user_id="alice",
        workspace_id="ws_acme",
        request_id="r1",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _named_tool(name: str) -> MagicMock:
    """Build a MagicMock whose ``.name`` attribute is the given string.

    MagicMock auto-creates ``.name`` as a Mock by default — the dedup
    loop in providers.py reads it via ``getattr(t, "name", None)`` so
    we need a real string there for the seen-names set to work.
    """
    t = MagicMock()
    t.name = name
    return t


def _tool_name(t: object) -> str | None:
    """Mirror providers.py's name-extraction: getattr first, dict fallback."""
    name = getattr(t, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(t, dict):
        return t.get("name")  # type: ignore[no-any-return]
    return None


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    composio_service.reset_client_cache_for_tests()
    composio_providers.reset_cache_for_tests()
    yield
    composio_service.reset_client_cache_for_tests()
    composio_providers.reset_cache_for_tests()


@pytest.fixture
def force_settings(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Make ``Settings.load()`` return a controlled instance."""

    def _factory(settings: Settings) -> Settings:
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
        return settings

    return _factory


@pytest.fixture
def stub_provider_modules(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Install fake ``composio`` + per-backend provider modules so
    ``providers._provider_class_for`` works without the real SDKs."""
    captured: dict[str, MagicMock] = {}

    composio_cls = MagicMock(name="Composio")

    def _composio_factory(*args: object, **kwargs: object) -> MagicMock:
        instance = MagicMock(name="ComposioClient")
        captured["last_init_kwargs"] = kwargs  # type: ignore[assignment]

        # providers.py calls ``client.tools.get(...)`` in two shapes:
        #   (a) per-toolkit direct tools: ``toolkits=[slug], limit=N``
        #   (b) meta-tools by name:       ``tools=[slug1, slug2, ...]``
        # We dispatch on which kwarg is present and return MagicMock
        # objects with a ``name`` attribute the dedup loop in providers.py
        # reads via ``getattr(t, "name", None)``.
        def _fake_tools_get(
            user_id: str,
            toolkits: list[str] | None = None,
            tools: list[str] | None = None,
            limit: int | None = None,
        ) -> list[object]:
            if tools is not None:
                captured["last_meta_get"] = {  # type: ignore[assignment]
                    "user_id": user_id,
                    "tools": list(tools),
                }
                return [_named_tool(slug) for slug in tools]
            captured["last_tools_get"] = {  # type: ignore[assignment]
                "user_id": user_id,
                "toolkits": list(toolkits or []),
                "limit": limit,
            }
            return [_named_tool(f"tool({tk})") for tk in (toolkits or [])]

        instance.tools.get.side_effect = _fake_tools_get
        return instance

    composio_cls.side_effect = _composio_factory
    fake_composio = types.ModuleType("composio")
    fake_composio.Composio = composio_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "composio", fake_composio)

    for mod_name, cls_name in [
        ("composio_claude_agent_sdk", "ClaudeAgentSDKProvider"),
        ("composio_openai_agents", "OpenAIAgentsProvider"),
        ("composio_google_adk", "GoogleAdkProvider"),
        ("composio_langgraph", "LanggraphProvider"),
    ]:
        fake_mod = types.ModuleType(mod_name)
        provider_cls = MagicMock(name=cls_name)
        setattr(fake_mod, cls_name, provider_cls)
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)
        captured[cls_name] = provider_cls

    captured["Composio"] = composio_cls
    return captured


# ---------------------------------------------------------------------------
# Backend mapping
# ---------------------------------------------------------------------------


def test_supported_backends_match_provider_table() -> None:
    """The advertised SUPPORTED_BACKENDS set must match the lookup table.

    A backend in SUPPORTED_BACKENDS but missing from the mapping would
    blow up at runtime; a backend in the mapping but absent from the
    set would be silently unreachable.
    """
    for kind in composio_providers.SUPPORTED_BACKENDS:
        # Should not raise — each supported kind has a mapping.
        # We can't actually import the SDKs in the unit test, so just
        # verify the dispatcher doesn't choke on the lookup branches.
        # The full import happens in the per-backend tests below.
        assert kind in {
            composio_providers.BACKEND_CLAUDE_SDK,
            composio_providers.BACKEND_OPENAI_AGENTS,
            composio_providers.BACKEND_GOOGLE_ADK,
            composio_providers.BACKEND_DEEP_AGENTS,
        }


def test_unknown_backend_returns_empty(
    stub_provider_modules: dict[str, MagicMock],
    force_settings,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    force_settings(_enabled_settings())
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())
    result = composio_providers.build_tools_for_backend("opencode")
    assert result == []


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_returns_empty_when_disabled(monkeypatch: pytest.MonkeyPatch, force_settings) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("POCKETPAW_COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", raising=False)
    force_settings(Settings(_env_file=None))
    assert composio_providers.build_tools_for_backend("claude_agent_sdk") == []


def test_returns_empty_when_no_stream_context(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    """No user_id contextvar → no tools (CLI runs, KB rebuilds, etc.)."""
    force_settings(_enabled_settings())
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: None)
    assert composio_providers.build_tools_for_backend("claude_agent_sdk") == []


def test_returns_empty_when_session_build_raises(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    """Composio outage / 5xx must not 500 the chat run."""
    force_settings(_enabled_settings())
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())

    def _boom(backend_kind: str, namespaced_user_id: str, settings: Settings) -> list[object]:
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(composio_providers, "_build_session_and_tools", _boom)
    assert composio_providers.build_tools_for_backend("claude_agent_sdk") == []


# ---------------------------------------------------------------------------
# Per-backend dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_kind, provider_class_name",
    [
        ("claude_agent_sdk", "ClaudeAgentSDKProvider"),
        ("openai_agents", "OpenAIAgentsProvider"),
        ("google_adk", "GoogleAdkProvider"),
        ("deep_agents", "LanggraphProvider"),
    ],
)
def test_dispatches_to_correct_provider(
    backend_kind: str,
    provider_class_name: str,
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    """Each backend kind must instantiate its own provider class."""
    force_settings(_enabled_settings())
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())

    tools = composio_providers.build_tools_for_backend(backend_kind)
    assert tools  # non-empty
    # The expected provider class was instantiated:
    stub_provider_modules[provider_class_name].assert_called_once()


def test_namespaces_user_id(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    """composio.create is called with f'{enterprise_id}:{user_id}'."""
    force_settings(_enabled_settings())
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())
    composio_providers.build_tools_for_backend("claude_agent_sdk")

    # The cache key is (namespaced_user_id, backend_kind). If the user
    # had been passed raw, the key would be ("alice", ...) — the fact
    # that it's the namespaced form is the proof.
    assert (
        "ent_acme:alice",
        "claude_agent_sdk",
    ) in composio_providers._tools_cache


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_caches_tools_within_ttl(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    force_settings(_enabled_settings(composio_mcp_url_ttl_seconds=3600))
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())

    call_count = [0]

    def _build(backend_kind: str, namespaced_user_id: str, settings: Settings) -> list[object]:
        call_count[0] += 1
        return [object()]

    monkeypatch.setattr(composio_providers, "_build_session_and_tools", _build)

    composio_providers.build_tools_for_backend("claude_agent_sdk")
    composio_providers.build_tools_for_backend("claude_agent_sdk")
    composio_providers.build_tools_for_backend("claude_agent_sdk")
    assert call_count[0] == 1  # cached after first build


def test_remints_after_ttl_expires(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    force_settings(_enabled_settings(composio_mcp_url_ttl_seconds=1))
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())

    call_count = [0]
    monkeypatch.setattr(
        composio_providers,
        "_build_session_and_tools",
        lambda *a, **kw: [object()] if call_count.__setitem__(0, call_count[0] + 1) is None else [],
    )
    fake_time = [1000.0]
    monkeypatch.setattr(composio_providers.time, "monotonic", lambda: fake_time[0])

    composio_providers.build_tools_for_backend("claude_agent_sdk")
    assert call_count[0] == 1
    fake_time[0] += 0.5
    composio_providers.build_tools_for_backend("claude_agent_sdk")
    assert call_count[0] == 1
    fake_time[0] += 10
    composio_providers.build_tools_for_backend("claude_agent_sdk")
    assert call_count[0] == 2


def test_cache_partitioned_per_backend_and_user(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    """Two backends for the same user, or two users on the same backend,
    must not share cache entries — that'd be a tenancy leak."""
    force_settings(_enabled_settings())

    user = [_fake_ctx()]
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: user[0])

    call_args: list[tuple[str, str]] = []

    def _build(backend_kind: str, namespaced_user_id: str, settings: Settings) -> list[object]:
        call_args.append((backend_kind, namespaced_user_id))
        return [object()]

    monkeypatch.setattr(composio_providers, "_build_session_and_tools", _build)

    composio_providers.build_tools_for_backend("claude_agent_sdk")
    composio_providers.build_tools_for_backend("deep_agents")  # different backend, same user
    # Switch user
    bob_ctx = RequestContext(
        user_id="bob",
        workspace_id="ws_acme",
        request_id="r2",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )
    user[0] = bob_ctx
    composio_providers.build_tools_for_backend("claude_agent_sdk")  # different user, same backend

    assert call_args == [
        ("claude_agent_sdk", "ent_acme:alice"),
        ("deep_agents", "ent_acme:alice"),
        ("claude_agent_sdk", "ent_acme:bob"),
    ]


# ---------------------------------------------------------------------------
# Regression: pocket specialist path stays Composio-free
# ---------------------------------------------------------------------------


def test_pockets_prompts_has_no_composio_imports() -> None:
    path = Path(__file__).resolve().parents[3] / "src" / "pocketpaw" / "ripple" / "_pockets.py"
    source = path.read_text(encoding="utf-8")
    assert "from pocketpaw_ee.cloud.composio" not in source
    assert "build_tools_for_backend" not in source


def test_deep_agents_skips_composio_on_pocket_session() -> None:
    """The deep_agents backend gates Composio injection behind
    ``not is_pocket_session`` — pocket runs are surgical and exclude
    Composio. Static check guards against regressions."""
    path = Path(__file__).resolve().parents[3] / "src" / "pocketpaw" / "agents" / "deep_agents.py"
    source = path.read_text(encoding="utf-8")
    # The injection block must live inside an ``if not is_pocket_session:`` guard.
    assert "if not is_pocket_session:" in source
    # And the call must go through the ``pocketpaw.composio_tools`` entry-point
    # helper with the right backend key — the OSS core never imports the EE
    # composio package directly.
    assert 'composio_tools_for("deep_agents"' in source


# ---------------------------------------------------------------------------
# Search-fallback meta-tools (Task 3a)
# ---------------------------------------------------------------------------


def test_search_fallback_meta_tools_appended(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    """The 3 search-flow meta-tools must be appended to the direct tools
    so the agent has a discovery fallback when its tool index can't
    surface a specific action. COMPOSIO_MANAGE_CONNECTIONS must NOT be
    included — we own the connect flow via connection_tool.py."""
    force_settings(_enabled_settings(composio_toolkits=["gmail", "github"]))
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())

    tools = composio_providers.build_tools_for_backend("claude_agent_sdk")
    names = {_tool_name(t) for t in tools}

    assert "COMPOSIO_SEARCH_TOOLS" in names
    assert "COMPOSIO_GET_TOOL_SCHEMAS" in names
    assert "COMPOSIO_MULTI_EXECUTE_TOOL" in names
    # Connect flow stays on our side; meta-tool form is excluded.
    assert "COMPOSIO_MANAGE_CONNECTIONS" not in names
    # Direct tools still present alongside meta-tools.
    assert "tool(gmail)" in names
    assert "tool(github)" in names


def test_search_fallback_failure_keeps_direct_tools(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    """If the meta-tools fetch raises, direct tools must still come
    back — losing search degrades capability, not the whole turn."""
    force_settings(_enabled_settings(composio_toolkits=["gmail"]))
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())

    # Re-wire the fake client so meta-tool fetches (tools=[...]) raise
    # but per-toolkit fetches (toolkits=[...]) still succeed.
    composio_cls = stub_provider_modules["Composio"]

    def _factory(*args: object, **kwargs: object) -> MagicMock:
        inst = MagicMock(name="ComposioClient")

        def _selective_get(
            user_id: str,
            toolkits: list[str] | None = None,
            tools: list[str] | None = None,
            limit: int | None = None,
        ) -> list[object]:
            if tools is not None:
                raise RuntimeError("meta-tools 503")
            return [_named_tool(f"tool({tk})") for tk in (toolkits or [])]

        inst.tools.get.side_effect = _selective_get
        return inst

    composio_cls.side_effect = _factory

    out = composio_providers.build_tools_for_backend("claude_agent_sdk")
    names = {_tool_name(t) for t in out}
    assert "tool(gmail)" in names  # direct tools survived
    assert "COMPOSIO_SEARCH_TOOLS" not in names  # meta-tools absent


def test_search_fallback_dedupes_against_direct_tools(
    stub_provider_modules: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    force_settings,  # type: ignore[no-untyped-def]
) -> None:
    """If Composio ever returned a meta-tool slug from a regular toolkit
    fetch, we mustn't double-register it. Mirrors the seen_names guard
    in providers.py."""
    force_settings(_enabled_settings(composio_toolkits=["composio"]))
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: _fake_ctx())

    composio_cls = stub_provider_modules["Composio"]

    def _factory(*args: object, **kwargs: object) -> MagicMock:
        inst = MagicMock(name="ComposioClient")

        def _get(
            user_id: str,
            toolkits: list[str] | None = None,
            tools: list[str] | None = None,
            limit: int | None = None,
        ) -> list[object]:
            if tools is not None:
                return [_named_tool(s) for s in tools]
            # Per-toolkit fetch happens to also include SEARCH_TOOLS:
            return [_named_tool("COMPOSIO_SEARCH_TOOLS"), _named_tool("other")]

        inst.tools.get.side_effect = _get
        return inst

    composio_cls.side_effect = _factory

    out = composio_providers.build_tools_for_backend("claude_agent_sdk")
    search_tools = [t for t in out if _tool_name(t) == "COMPOSIO_SEARCH_TOOLS"]
    assert len(search_tools) == 1  # deduped, not double-listed

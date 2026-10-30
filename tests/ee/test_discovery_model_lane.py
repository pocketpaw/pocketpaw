# tests/ee/test_discovery_model_lane.py — model-lane sovereignty posture tests.
#
# Created: 2026-06-22 (feat/szd-model-lane-configurable) — covers
# ``settings.discovery_sovereign_model``, the configurable posture for discovery's
# categorize (F2) / refine (F3) model call. The provider lane is no longer a code
# constant; it is chosen by the setting:
#
#   * True (DEFAULT, unchanged sovereign behavior): the resolver hard-pins the
#     on-box Ollama (``force_provider="ollama"``) — ``api_key is None``, nothing
#     leaves the box. The pre-existing sovereignty guarantee holds by default.
#   * False (explicit opt-in): the resolver uses the workspace's CONFIGURED
#     provider via ``resolve_llm_client(settings)`` (no force) — a cloud model is
#     allowed.
#
# Asserted here:
#   * default True → resolved client is ollama, api_key is None;
#   * False + a configured provider (openai) → that provider resolves, NOT ollama;
#   * False + no resolvable provider/key → graceful degrade (refine returns the
#     deterministic draft flagged "unavailable"; the categorize digester falls
#     back to convo ingest) — never raises, never a silent cloud leak;
#   * the kb ingest/build TRIPWIRE holds under BOTH postures (never reached).
#
# Fully mocked — no DB / network / Ollama / kb binary. Run with:
#   uv run --group ee pytest tests/ee/test_discovery_model_lane.py -q

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.discovery import _refine  # noqa: E402
from pocketpaw_ee.discovery.kb_compile import KbCompileDigester  # noqa: E402
from pocketpaw_ee.discovery.models import (  # noqa: E402
    DraftLink,
    DraftObject,
    DraftObjectType,
    OntologyDraft,
)

from pocketpaw.config import Settings  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _no_provider_settings(*, sovereign: bool) -> Settings:
    """A Settings with NO resolvable cloud provider.

    The local dev config file can leak real-looking keys into a bare
    ``Settings()`` (deepseek / litellm), so we explicitly null every provider
    key/url and pin ``llm_provider="auto"``. With nothing resolvable, ``auto``
    falls through to ollama — proving the opt-in posture never silently picks a
    cloud lane just because a stray key was on disk.
    """
    return Settings(
        llm_provider="auto",
        discovery_sovereign_model=sovereign,
        anthropic_api_key=None,
        openai_api_key=None,
        openai_compatible_base_url="",
        openai_compatible_api_key=None,
        google_api_key=None,
        openrouter_api_key=None,
        litellm_api_key=None,
    )


def _sample_draft() -> OntologyDraft:
    """A small deterministic draft (two near-duplicate types, one spurious link)."""
    return OntologyDraft(
        object_types=[
            DraftObjectType(
                name="Ticket",
                source_id_field="id",
                field_map={"subject": "subject"},
                confidence=0.6,
                key_confidence=0.8,
                record_count=3,
            ),
            DraftObjectType(
                name="Tickets",
                source_id_field="id",
                field_map={"subject": "subject"},
                confidence=0.4,
                key_confidence=0.5,
                record_count=2,
            ),
        ],
        objects=[
            DraftObject(type_name="Ticket", source_id="t1", properties={"subject": "hi"}),
        ],
        links=[
            DraftLink(
                from_type="Ticket",
                from_source_id="t1",
                to_type="Tickets",
                to_source_id="t9",
                link_type="related",
                via_field="ref",
                confidence=0.2,
            ),
        ],
        meta={"digester": "structured-shape"},
    )


# --------------------------------------------------------------------------- #
# 1) DEFAULT posture (sovereign True) — forces ollama, api_key is None.
#    The existing sovereignty guarantee holds by default.
# --------------------------------------------------------------------------- #
def test_sovereign_true_forces_ollama() -> None:
    settings = Settings()  # discovery_sovereign_model defaults to True
    assert settings.discovery_sovereign_model is True

    llm = _refine.resolve_on_box_descriptor(settings)

    assert llm.is_ollama is True
    assert llm.api_key is None
    assert llm.provider == "ollama"
    # The model name is the on-box model under the default posture.
    assert _refine.resolve_discovery_model_name(settings) == settings.ollama_model


def test_sovereign_true_forces_ollama_even_with_cloud_key() -> None:
    # A tenant with a cloud key set AND llm_provider="auto" would, without the
    # pin, resolve to anthropic. The default sovereign posture must still force
    # ollama — the leak the original slice exists to prevent.
    settings = Settings(anthropic_api_key="sk-ant-fake-cloud-key", llm_provider="auto")
    assert settings.discovery_sovereign_model is True

    llm = _refine.resolve_on_box_descriptor(settings)

    assert llm.is_ollama is True, "default posture must override the cloud key"
    assert llm.is_anthropic is False
    assert llm.api_key is None


# --------------------------------------------------------------------------- #
# 2) OPT-IN posture (sovereign False) + a configured provider — uses
#    resolve_llm_client(settings) with NO force, resolves to that provider
#    (NOT forced ollama).
# --------------------------------------------------------------------------- #
def test_sovereign_false_uses_configured_provider() -> None:
    settings = Settings(
        discovery_sovereign_model=False,
        llm_provider="openai",
        openai_api_key="sk-fake-openai-key",
    )

    llm = _refine.resolve_on_box_descriptor(settings)

    # The configured provider is honored — NOT forced back to ollama.
    assert llm.provider == "openai"
    assert llm.is_ollama is False
    assert llm.api_key == "sk-fake-openai-key"
    # The model name follows the configured provider, not the ollama model.
    assert _refine.resolve_discovery_model_name(settings) == settings.openai_model


def test_sovereign_false_resolves_via_no_force_resolve(monkeypatch) -> None:
    # Prove the opt-in posture calls resolve_llm_client WITHOUT force_provider.
    settings = Settings(
        discovery_sovereign_model=False,
        llm_provider="openai",
        openai_api_key="sk-fake-openai-key",
    )

    captured: dict[str, Any] = {}
    real_resolve = _refine.resolve_llm_client

    def _spy(s: Settings, *, force_provider: str | None = None):
        captured["force_provider"] = force_provider
        return real_resolve(s, force_provider=force_provider)

    monkeypatch.setattr(_refine, "resolve_llm_client", _spy)

    _refine.resolve_on_box_descriptor(settings)

    assert captured["force_provider"] is None, "opt-in posture must not force a provider"


# --------------------------------------------------------------------------- #
# 3) OPT-IN posture but NO resolvable provider/key — graceful degrade.
#    refine returns the deterministic draft flagged "unavailable"; the digester
#    falls back to convo ingest. Never raises, never a silent cloud leak.
# --------------------------------------------------------------------------- #
async def test_sovereign_false_with_no_provider_degrades_refine(monkeypatch) -> None:
    draft = _sample_draft()
    settings = _no_provider_settings(sovereign=False)

    # With nothing configured, auto falls through to ollama (no key) — no leak.
    descriptor = _refine.resolve_on_box_descriptor(settings)
    assert descriptor.is_ollama is True
    assert descriptor.api_key is None, "no resolvable provider must not pick up a stray cloud key"

    # The (unreachable) provider raises on connect → refine degrades to the floor.
    class _DownCompletions:
        async def create(self, **kwargs: Any) -> Any:
            raise ConnectionError("no provider reachable")

    class _DownChat:
        completions = _DownCompletions()

    class _DownClient:
        chat = _DownChat()

    monkeypatch.setattr(_refine, "resolve_on_box_client", lambda s: _DownClient())

    out = await _refine.refine_draft(draft, settings)

    assert out.meta["refine"] == "unavailable", "must degrade, never raise"
    assert {ot.name for ot in out.object_types} == {"Ticket", "Tickets"}
    assert len(out.links) == 1  # nothing dropped — refine never applied


def test_sovereign_false_with_no_provider_degrades_categorize(monkeypatch) -> None:
    # The digester resolves no usable client → falls back to the deterministic
    # convo-ingest path. Never raises.
    settings = _no_provider_settings(sovereign=False)

    def _down_resolve(s: Settings) -> Any:
        raise RuntimeError("no provider reachable")

    calls = _install_fake_kb(monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.discovery.kb_compile.resolve_on_box_client", _down_resolve)

    digester = KbCompileDigester(settings=settings)
    draft = digester.digest(
        {"support": ["Customer can't log in."]},
        {"connector": "zendesk", "workspace_id": "w1"},
    )

    assert isinstance(draft, OntologyDraft)
    # Fell back to convo ingest (the deterministic floor), never raised.
    cmds = [c[0] for c in calls]
    assert "convo" in cmds, "no-provider opt-in must fall back to convo ingest"
    assert "prepare" not in cmds, "no model client → agent mode must not run"


# --------------------------------------------------------------------------- #
# 4) The kb ingest/build TRIPWIRE is UNCONDITIONAL — never reached under either
#    posture, model client present or not.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sovereign", [True, False])
def test_tripwire_holds_regardless_of_setting(monkeypatch, sovereign: bool) -> None:
    settings = _no_provider_settings(sovereign=sovereign)

    calls = _install_fake_kb(monkeypatch)
    # No model client either way — exercises the full compile + read path.
    monkeypatch.setattr(
        "pocketpaw_ee.discovery.kb_compile.resolve_on_box_client",
        lambda s: (_ for _ in ()).throw(RuntimeError("no model")),
    )

    digester = KbCompileDigester(settings=settings)
    digester.digest(
        {"support": ["text one"], "refunds": ["text two"]},
        {"connector": "zendesk", "workspace_id": "w1"},
    )

    cmds = [c[0] for c in calls]
    assert "ingest" not in cmds, "sovereignty tripwire: kb ingest must never run"
    assert "build" not in cmds, "sovereignty tripwire: kb build must never run"


def test_tripwire_seam_refuses_ingest_and_build() -> None:
    # The mechanical assertion in the real _kb seam — independent of any setting.
    from pocketpaw_ee.discovery import kb_compile

    with pytest.raises(RuntimeError, match="sovereignty"):
        kb_compile._kb("ingest", "some/path")
    with pytest.raises(RuntimeError, match="sovereignty"):
        kb_compile._kb("build", "some/path")


# --------------------------------------------------------------------------- #
# Shared fake kb seam — records calls, refuses ingest/build, models the
# convo-ingest + list/show round-trip the digester reads back.
# --------------------------------------------------------------------------- #
def _install_fake_kb(monkeypatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    store: dict[str, list[dict]] = {}
    counter = {"n": 0}

    def _scope_of(args: tuple[str, ...]) -> str:
        if "--scope" in args:
            return args[args.index("--scope") + 1]
        return "default"

    def _fake_kb(*args: str, input_text: str | None = None, timeout: int = 120) -> Any:
        calls.append(args)
        # The real seam refuses these BEFORE the subprocess; mirror it so a test
        # that somehow routed here would also fail loud.
        assert args[0] not in ("ingest", "build"), (
            "sovereignty: KbCompileDigester must never call kb ingest/build"
        )
        scope = _scope_of(args)

        if args[0] == "convo":
            store.setdefault(scope, [])
            counter["n"] += 1
            store[scope].append(
                {
                    "id": f"convo-{counter['n']}",
                    "title": "Conversation",
                    "summary": "deterministic convo ingest",
                    "content": "body",
                    "concepts": [],
                    "categories": ["conversation"],
                }
            )
            return {"articles": 1}

        if args[0] == "list":
            return [
                {"id": a["id"], "title": a.get("title", ""), "summary": a.get("summary", "")}
                for a in store.get(scope, [])
            ]

        if args[0] == "show":
            article_id = args[1]
            for a in store.get(scope, []):
                if a["id"] == article_id:
                    return dict(a)
            return {}

        if args[0] == "graph":
            return {"scope": scope, "nodes": [], "edges": []}

        return {}

    monkeypatch.setattr("pocketpaw_ee.discovery.kb_compile._kb", _fake_kb)
    return calls

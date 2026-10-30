# tests/ee/test_discovery_refine.py — unit tests for SZD finish-slice F3
# (the on-box REFINE pass).
#
# Created: 2026-06-21 (F3 / feat/szd-finish-core) — covers
# ``pocketpaw_ee.discovery._refine`` and the un-stubbed ``opts.refine`` path on
# ``DiscoveryRun.run``. The Ollama client is STUBBED (no running Ollama, no
# network); these tests are the MECHANICAL enforcement of the sovereignty rule:
# refine MUST resolve through ``resolve_llm_client(settings, force_provider=
# "ollama")`` so tenant data never reaches a cloud model — even when a cloud
# (Anthropic) key is configured.
#
# Sovereignty / availability matrix asserted here:
#   * resolve_on_box_client → api_key is None, is_ollama is True;
#   * a fake cloud key set in settings → STILL forces ollama (no Anthropic);
#   * Ollama down (connection error) → return the deterministic draft with
#     meta["refine"]=="unavailable", NEVER raise, NEVER fall back to cloud;
#   * a stubbed cleaned-ontology JSON → draft reflects it + meta["refine"]==
#     "applied";
#   * run(opts.refine=True) invokes refine_draft; run(opts.refine=False) is the
#     unchanged deterministic path and never calls refine_draft.
#
# Fully mocked — no DB / network / Ollama. Run with:
#   uv run --group ee pytest tests/ee/test_discovery_refine.py -q

from __future__ import annotations

import json
from typing import Any

import pytest
from pocketpaw_ee.discovery import _refine
from pocketpaw_ee.discovery.models import (
    DraftLink,
    DraftObject,
    DraftObjectType,
    OntologyDraft,
)

from pocketpaw.config import Settings


# --------------------------------------------------------------------------- #
# Stub Ollama client (stands in for the AsyncOpenAI client create_openai_client
# returns). It records the model used and either returns a canned completion or
# raises a connection error.
# --------------------------------------------------------------------------- #
class _StubMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubChoice:
    def __init__(self, content: str) -> None:
        self.message = _StubMessage(content)


class _StubCompletion:
    def __init__(self, content: str) -> None:
        self.choices = [_StubChoice(content)]


class _StubCompletions:
    def __init__(self, content: str | None, raise_exc: Exception | None) -> None:
        self._content = content
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _StubCompletion:
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return _StubCompletion(self._content or "")


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubOpenAIClient:
    """Mirror of the AsyncOpenAI surface refine_draft touches: chat.completions."""

    def __init__(self, *, content: str | None = None, raise_exc: Exception | None = None) -> None:
        self.completions = _StubCompletions(content, raise_exc)
        self.chat = _StubChat(self.completions)
        self.closed = False

    async def close(self) -> None:  # tolerated-if-called cleanup hook
        self.closed = True


def _sample_draft() -> OntologyDraft:
    """A small deterministic draft for refine to clean.

    Two near-duplicate types (``Ticket`` / ``Tickets``) the model is asked to
    merge, and one spurious link the model is asked to drop.
    """
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
# resolve_on_box_client is hard-pinned to Ollama (the sovereignty enforcement
# point): api_key is None, is_ollama is True.
# --------------------------------------------------------------------------- #
def test_refine_resolves_ollama_only() -> None:
    settings = Settings()
    llm = _refine.resolve_on_box_descriptor(settings)

    assert llm.is_ollama is True
    assert llm.api_key is None
    assert llm.provider == "ollama"


# --------------------------------------------------------------------------- #
# Even with a cloud (Anthropic) key configured, refine STILL routes ollama —
# never an Anthropic client. This is the leak the slice exists to prevent.
# --------------------------------------------------------------------------- #
def test_refine_with_cloud_key_set_still_routes_ollama() -> None:
    # A tenant with a cloud key set AND llm_provider="auto" would, without the
    # hard pin, resolve to anthropic and leak raw tenant text.
    settings = Settings(anthropic_api_key="sk-ant-fake-cloud-key", llm_provider="auto")

    llm = _refine.resolve_on_box_descriptor(settings)

    assert llm.is_ollama is True, "force_provider='ollama' must override the cloud key"
    assert llm.is_anthropic is False
    assert llm.api_key is None, "no cloud key may ride into the on-box client"


# --------------------------------------------------------------------------- #
# Ollama down → deterministic draft returned, meta['refine']=='unavailable',
# NEVER raise, NEVER a cloud call.
# --------------------------------------------------------------------------- #
async def test_refine_unavailable_returns_deterministic_draft(monkeypatch) -> None:
    draft = _sample_draft()
    settings = Settings()

    stub = _StubOpenAIClient(raise_exc=ConnectionError("Cannot connect to Ollama"))
    monkeypatch.setattr(_refine, "resolve_on_box_client", lambda s: stub)

    out = await _refine.refine_draft(draft, settings)

    # The deterministic draft survives, untouched in shape, flagged unavailable.
    assert out.meta["refine"] == "unavailable"
    assert {ot.name for ot in out.object_types} == {"Ticket", "Tickets"}
    assert len(out.links) == 1  # nothing dropped — refine never ran


# --------------------------------------------------------------------------- #
# A model that returns a cleaned ontology JSON → draft reflects it, flagged
# 'applied'.
# --------------------------------------------------------------------------- #
async def test_refine_applied_cleans_draft(monkeypatch) -> None:
    draft = _sample_draft()
    settings = Settings()

    cleaned = {
        # The two near-duplicate types merged into one canonical type.
        "object_types": [
            {
                "name": "SupportTicket",
                "source_id_field": "id",
                "field_map": {"subject": "subject"},
                "confidence": 0.9,
                "key_confidence": 0.9,
            }
        ],
        # The spurious link dropped (empty list).
        "links": [],
    }
    stub = _StubOpenAIClient(content=json.dumps(cleaned))
    monkeypatch.setattr(_refine, "resolve_on_box_client", lambda s: stub)

    out = await _refine.refine_draft(draft, settings)

    assert out.meta["refine"] == "applied"
    type_names = {ot.name for ot in out.object_types}
    assert "SupportTicket" in type_names
    assert "Tickets" not in type_names, "near-duplicate type should be merged away"
    assert out.links == [], "spurious link should be dropped"

    # The model was asked for a JSON object response (sovereign one-shot shape).
    call = stub.completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["model"] == settings.ollama_model


# --------------------------------------------------------------------------- #
# A malformed model response degrades to the deterministic draft (never raises,
# never a cloud retry).
# --------------------------------------------------------------------------- #
async def test_refine_malformed_response_returns_deterministic_draft(monkeypatch) -> None:
    draft = _sample_draft()
    settings = Settings()

    stub = _StubOpenAIClient(content="not json at all {")
    monkeypatch.setattr(_refine, "resolve_on_box_client", lambda s: stub)

    out = await _refine.refine_draft(draft, settings)

    assert out.meta["refine"] == "unavailable"
    assert {ot.name for ot in out.object_types} == {"Ticket", "Tickets"}


# --------------------------------------------------------------------------- #
# run(opts.refine=True) invokes refine_draft on the deterministic draft.
# --------------------------------------------------------------------------- #
async def test_run_refine_true_calls_refine(monkeypatch) -> None:
    from dataclasses import dataclass

    from pocketpaw_ee.discovery import DiscoveryRun, DiscoveryRunOptions

    @dataclass
    class _Result:
        success: bool
        data: Any = None
        error: str | None = None

    @dataclass
    class _Schema:
        name: str
        method: str = "GET"
        trust_level: str = "auto"

    class _Adapter:
        async def actions(self) -> list[_Schema]:
            return [_Schema(name="list_rows")]

        async def execute(self, action: str, params: dict[str, Any]) -> _Result:
            return _Result(success=True, data=[{"id": "r1", "name": "a"}])

    class _Registry:
        async def ensure_connected(self, name: str, scope_key: str) -> _Adapter:
            return _Adapter()

    captured: dict[str, Any] = {}

    async def _fake_refine(draft: OntologyDraft, settings: Any) -> OntologyDraft:
        captured["called"] = True
        draft.meta["refine"] = "applied"
        return draft

    monkeypatch.setattr(_refine, "refine_draft", _fake_refine)

    run = DiscoveryRun(registry=_Registry())
    draft = await run.run("ws-1", ["c"], DiscoveryRunOptions(refine=True))

    assert captured.get("called") is True
    assert draft.meta["refine"] == "applied"


# --------------------------------------------------------------------------- #
# run(opts.refine=False) is the unchanged deterministic path — refine_draft is
# never called and the draft carries no refine marker.
# --------------------------------------------------------------------------- #
async def test_run_refine_false_is_deterministic(monkeypatch) -> None:
    from dataclasses import dataclass

    from pocketpaw_ee.discovery import DiscoveryRun, DiscoveryRunOptions

    @dataclass
    class _Result:
        success: bool
        data: Any = None
        error: str | None = None

    @dataclass
    class _Schema:
        name: str
        method: str = "GET"
        trust_level: str = "auto"

    class _Adapter:
        async def actions(self) -> list[_Schema]:
            return [_Schema(name="list_rows")]

        async def execute(self, action: str, params: dict[str, Any]) -> _Result:
            return _Result(success=True, data=[{"id": "r1", "name": "a"}])

    class _Registry:
        async def ensure_connected(self, name: str, scope_key: str) -> _Adapter:
            return _Adapter()

    called = {"refine": False}

    async def _fake_refine(draft: OntologyDraft, settings: Any) -> OntologyDraft:
        called["refine"] = True
        return draft

    monkeypatch.setattr(_refine, "refine_draft", _fake_refine)

    run = DiscoveryRun(registry=_Registry())
    draft = await run.run("ws-1", ["c"], DiscoveryRunOptions(refine=False))

    assert called["refine"] is False, "deterministic path must not call refine_draft"
    assert "refine" not in draft.meta
    assert not draft.is_empty


# --------------------------------------------------------------------------- #
# Sanity: refine never builds an Anthropic client even when one is available —
# the only client factory it may reach for is create_openai_client (ollama).
# --------------------------------------------------------------------------- #
def test_refine_client_factory_is_openai_compatible_only() -> None:
    settings = Settings(anthropic_api_key="sk-ant-fake-cloud-key")
    client = _refine.resolve_on_box_client(settings)

    # The real path returns an AsyncOpenAI bound to the ollama /v1 endpoint with
    # the literal "ollama" sentinel key — never a real cloud key.
    base_url = str(getattr(client, "base_url", ""))
    assert "/v1" in base_url
    assert "11434" in base_url or "ollama" in base_url.lower()
    api_key = getattr(client, "api_key", None)
    assert api_key == "ollama", "the on-box client must use the ollama sentinel key"


@pytest.mark.parametrize("refine_flag", [True, False])
def test_options_refine_flag_roundtrips(refine_flag: bool) -> None:
    from pocketpaw_ee.discovery import DiscoveryRunOptions

    opts = DiscoveryRunOptions(refine=refine_flag)
    assert opts.refine is refine_flag

# tests/ee/test_kb_compile_categorize.py — F2 on-box CATEGORIZATION tests.
#
# Created: 2026-06-21 (F2 / feat/szd-finish-core) — covers the prepare/accept
# agent-mode categorization path added to KbCompileDigester._compile_blobs.
#
# The slice: route unstructured exhaust through kb's agent-mode
# (`kb prepare` → on-box Ollama model → `kb accept`) WHEN a model is configured,
# so real text gets DOMAIN categories (SupportTicket, RefundRequest, ...) instead
# of the single hardcoded "conversation" category `kb convo ingest` always tags.
# When no on-box model is configured, the digester falls back to today's
# `kb convo ingest` path (graceful degrade, no error).
#
# Covers:
#   * prepare/accept path runs when a model is configured → 2 typed buckets;
#   * fallback to `convo ingest` when no model is configured (no error);
#   * the sovereignty tripwire holds across the WHOLE compile (never ingest/build);
#   * prepare/accept use the discovery scope `workspace:<wid>:discovery`.
#
# Both seams are stubbed: the `_kb` subprocess seam (canned prepare/accept/list/
# show output, in memory) AND the on-box Ollama client (canned categorized
# article JSON). No running Ollama, no real kb binary required.
#
# Run with:
#   uv run --group ee pytest tests/ee/test_kb_compile_categorize.py -q

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.discovery import OntologyDraft  # noqa: E402
from pocketpaw_ee.discovery.kb_compile import KbCompileDigester  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes — the kb-go subprocess seam, agent-mode aware (prepare/accept)
# --------------------------------------------------------------------------- #
def _make_fake_kb(monkeypatch) -> list[tuple[str, ...]]:
    """Patch the `_kb` seam with an agent-mode-aware in-memory fake.

    Models the real prepare → accept → list → show round-trip:

      * ``prepare <dir> --pattern ... --scope <s>`` → one ``item`` per blob file
        with a ``prompt`` (we don't parse the prompt; the model stub ignores it);
      * ``accept --scope <s>`` (article JSON on stdin) → stores the supplied
        articles under ``<s>`` with the MODEL's categories;
      * ``list --scope <s>`` → lean entries; ``show <id> --scope <s>`` → full body;
      * ``convo ingest`` (the FALLBACK) → stores a single "conversation"-tagged
        article so a fallback run still yields a (degraded) draft.

    Returns the ``calls`` list so a test can assert which path ran and that the
    sovereignty tripwire (never ``ingest`` / ``build``) holds.
    """
    calls: list[tuple[str, ...]] = []
    store: dict[str, list[dict]] = {}
    # A monotonic id so multiple accepted articles in one scope stay distinct.
    counter = {"n": 0}

    def _scope_of(args: tuple[str, ...]) -> str:
        if "--scope" in args:
            return args[args.index("--scope") + 1]
        return "default"

    def _fake_kb(*args: str, input_text: str | None = None, timeout: int = 120) -> Any:
        calls.append(args)
        # SOVEREIGNTY: the off-box LLM-compile paths must NEVER be reached.
        assert args[0] != "ingest", (
            "sovereignty: KbCompileDigester must not call `kb ingest` (off-box LLM)"
        )
        assert args[0] != "build", (
            "sovereignty: KbCompileDigester must not call `kb build` (off-box LLM)"
        )

        scope = _scope_of(args)

        if args[0] == "prepare":
            # Emit one prepare item per "file" — the digester writes ONE temp
            # file per label, so a single item per prepare call is realistic.
            return {
                "scope": scope,
                "items": [
                    {
                        "source": "blob.txt",
                        "hash": "deadbeef",
                        "raw_id": "deadbeef00000000",
                        "prompt": "Compile this text into a wiki article with categories.",
                    }
                ],
                "pending": 1,
                "cached": 0,
                "total": 1,
            }

        if args[0] == "accept":
            # The article JSON the digester compiled (from the model) is on stdin.
            store.setdefault(scope, [])
            try:
                payload = json.loads(input_text) if input_text else {}
            except json.JSONDecodeError:
                payload = {}
            arts = payload.get("articles", []) if isinstance(payload, dict) else []
            for a in arts:
                counter["n"] += 1
                art = dict(a)
                # `accept` slugifies the title into the id; emulate a stable id.
                art.setdefault("id", f"art-{counter['n']}")
                store[scope].append(art)
            return {"accepted": len(arts), "articles": len(arts), "concepts": 0}

        if args[0] == "convo":
            # FALLBACK: `convo ingest` hardcodes a single "conversation" article.
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


# --------------------------------------------------------------------------- #
# Fake on-box model — returns a categorized article per prompt
# --------------------------------------------------------------------------- #
class _FakeChatCompletions:
    def __init__(self, articles: list[dict]) -> None:
        # One canned article per model call, cycled in order.
        self._articles = list(articles)
        self._i = 0

    async def create(self, **kwargs: Any) -> Any:
        # Assert the call is shaped the way the slice requires.
        assert kwargs.get("response_format") == {"type": "json_object"}, (
            "categorization must request a JSON object response"
        )
        art = self._articles[self._i % len(self._articles)]
        self._i += 1
        content = json.dumps(art)

        class _Msg:
            def __init__(self, c: str) -> None:
                self.content = c

        class _Choice:
            def __init__(self, c: str) -> None:
                self.message = _Msg(c)

        class _Resp:
            def __init__(self, c: str) -> None:
                self.choices = [_Choice(c)]

        return _Resp(content)


class _FakeChat:
    def __init__(self, articles: list[dict]) -> None:
        self.completions = _FakeChatCompletions(articles)


class _FakeOllamaClient:
    """Stand-in for the AsyncOpenAI client resolve_on_box_client returns."""

    def __init__(self, articles: list[dict]) -> None:
        self.chat = _FakeChat(articles)


# Two DOMAIN-categorized articles the model "compiles" — proves the model path
# escapes the single "conversation" bucket the deterministic path is stuck on.
_MODEL_ARTICLES = [
    {
        "source": "blob.txt",
        "hash": "deadbeef",
        "raw_id": "deadbeef00000000",
        "title": "Login lockout after billing failure",
        "summary": "Customer cannot log in; billing lock triggered.",
        "content": "Full ticket body about a billing lockout.",
        "concepts": ["billing", "login"],
        "categories": ["SupportTicket"],
    },
    {
        "source": "blob.txt",
        "hash": "cafef00d",
        "raw_id": "cafef00d00000000",
        "title": "Refund requested on invoice 12",
        "summary": "Refund request tied to a billing dispute.",
        "content": "Full refund-request body referencing invoice 12.",
        "concepts": ["billing", "refund"],
        "categories": ["RefundRequest"],
    },
]


def _stub_on_box_client(monkeypatch, articles: list[dict]) -> _FakeOllamaClient:
    """Make resolve_on_box_client return ONE shared fake categorizing client.

    The digester resolves a client once per label (per ``_compile_blobs``); a
    single shared client makes its completion stub cycle through ``articles``
    across labels, so two labels get two distinct categories.
    """
    client = _FakeOllamaClient(articles)
    monkeypatch.setattr(
        "pocketpaw_ee.discovery.kb_compile.resolve_on_box_client",
        lambda settings: client,
    )
    return client


class _FakeSettings:
    """Minimal settings stub — only the fields the categorize path reads."""

    ollama_model = "llama3.2"


# --------------------------------------------------------------------------- #
# 1) prepare/accept path runs when a model is configured
# --------------------------------------------------------------------------- #
def test_compile_uses_prepare_accept_when_model_configured(monkeypatch) -> None:
    calls = _make_fake_kb(monkeypatch)
    _stub_on_box_client(monkeypatch, _MODEL_ARTICLES)

    digester = KbCompileDigester(settings=_FakeSettings())
    exhaust = {
        "support": ["Customer can't log in, billing locked."],
        "refunds": ["Refund requested on invoice 12."],
    }
    draft = digester.digest(exhaust, {"connector": "zendesk", "workspace_id": "w1"})

    assert isinstance(draft, OntologyDraft)
    # The model supplied DOMAIN categories → TWO typed buckets, not objects-only.
    names = sorted(ot.name for ot in draft.object_types)
    assert names == ["RefundRequest", "SupportTicket"], names
    # Each typed bucket keys on the article id (high key confidence).
    for ot in draft.object_types:
        assert ot.source_id_field == "id"
        assert ot.key_confidence >= 0.8
    # The subprocess calls were prepare + accept, NOT convo ingest.
    cmds = [c[0] for c in calls]
    assert "prepare" in cmds
    assert "accept" in cmds
    assert "convo" not in cmds, "model path must not fall back to convo ingest"


# --------------------------------------------------------------------------- #
# 2) fallback to convo ingest when no model configured
# --------------------------------------------------------------------------- #
def test_compile_falls_back_to_convo_ingest_without_model(monkeypatch) -> None:
    calls = _make_fake_kb(monkeypatch)
    # No on-box client stub — but guard against any accidental resolution by
    # making resolve raise (simulating "no ollama configured / unavailable").
    monkeypatch.setattr(
        "pocketpaw_ee.discovery.kb_compile.resolve_on_box_client",
        lambda settings: (_ for _ in ()).throw(RuntimeError("no ollama")),
    )

    # settings=None → no model configured → the digester takes the convo path.
    digester = KbCompileDigester(settings=None)
    draft = digester.digest({"zendesk": ["some ticket text"]}, {"workspace_id": "w1"})

    assert isinstance(draft, OntologyDraft)
    cmds = [c[0] for c in calls]
    # Fell back to the deterministic keyless path — no error raised.
    assert "convo" in cmds
    assert "prepare" not in cmds
    assert "accept" not in cmds


def test_compile_falls_back_when_model_resolution_fails(monkeypatch) -> None:
    """A configured-but-unavailable model degrades to convo ingest, no error."""
    calls = _make_fake_kb(monkeypatch)
    monkeypatch.setattr(
        "pocketpaw_ee.discovery.kb_compile.resolve_on_box_client",
        lambda settings: (_ for _ in ()).throw(RuntimeError("ollama serve is down")),
    )

    digester = KbCompileDigester(settings=_FakeSettings())
    draft = digester.digest({"zendesk": ["ticket text"]}, {"workspace_id": "w1"})

    assert isinstance(draft, OntologyDraft)
    cmds = [c[0] for c in calls]
    # Model configured but resolve failed → graceful degrade to convo ingest.
    assert "convo" in cmds
    assert "accept" not in cmds


# --------------------------------------------------------------------------- #
# 3) sovereignty tripwire across the whole compile (model path)
# --------------------------------------------------------------------------- #
def test_compile_never_calls_kb_ingest_or_build(monkeypatch) -> None:
    calls = _make_fake_kb(monkeypatch)
    _stub_on_box_client(monkeypatch, _MODEL_ARTICLES)

    digester = KbCompileDigester(settings=_FakeSettings())
    digester.digest(
        {"support": ["t1"], "refunds": ["t2"]},
        {"workspace_id": "w1", "connector": "zendesk"},
    )

    assert calls, "expected the digester to drive the kb seam at least once"
    # The load-bearing assertion: across the WHOLE compile (prepare/accept model
    # path included), the off-box LLM-compile commands are NEVER invoked.
    assert all(c[0] not in ("ingest", "build") for c in calls)
    # And the on-box agent-mode pair actually ran.
    assert any(c[0] == "prepare" for c in calls)
    assert any(c[0] == "accept" for c in calls)


def test_compile_never_calls_ingest_or_build_on_fallback(monkeypatch) -> None:
    """The tripwire also holds on the fallback (no-model) path."""
    calls = _make_fake_kb(monkeypatch)
    digester = KbCompileDigester(settings=None)
    digester.digest({"zendesk": ["t1", "t2"]}, {"workspace_id": "w1"})
    assert all(c[0] not in ("ingest", "build") for c in calls)


# --------------------------------------------------------------------------- #
# 4) prepare/accept use the discovery scope
# --------------------------------------------------------------------------- #
def test_prepare_accept_use_discovery_scope(monkeypatch) -> None:
    calls = _make_fake_kb(monkeypatch)
    _stub_on_box_client(monkeypatch, _MODEL_ARTICLES)

    digester = KbCompileDigester(settings=_FakeSettings())
    digester.digest({"support": ["t"]}, {"workspace_id": "w7"})

    expected_scope = "workspace:w7:discovery"

    def _scope_of(args: tuple[str, ...]) -> str | None:
        if "--scope" in args:
            return args[args.index("--scope") + 1]
        return None

    prepare_calls = [c for c in calls if c[0] == "prepare"]
    accept_calls = [c for c in calls if c[0] == "accept"]
    assert prepare_calls, "expected a prepare call"
    assert accept_calls, "expected an accept call"
    assert all(_scope_of(c) == expected_scope for c in prepare_calls)
    assert all(_scope_of(c) == expected_scope for c in accept_calls)
    # prepare also passes the txt pattern so it scans the transcript files.
    assert all("--pattern" in c for c in prepare_calls)

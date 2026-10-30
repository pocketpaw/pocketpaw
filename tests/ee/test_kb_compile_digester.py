# tests/ee/test_kb_compile_digester.py — unit + smoke tests for the KbCompileDigester (S2-K1).
#
# Created: 2026-06-20 (S2-K1 / feat/szd-slice2-discovery) — covers the on-box
# unstructured-exhaust → OntologyDraft digester. Mirrors the pure-logic style of
# test_structured_shape_digester.py: one `digester` fixture, a `fake_kb` fixture
# that REPLACES the `_kb` subprocess seam with an in-memory fake (no binary, no
# network, no Anthropic call). The fake records every call's argv so the
# SOVEREIGNTY tripwire can assert `kb ingest` / `kb build` are NEVER invoked
# across a digest() run (the keyless compile constraint encoded as a test, same
# spirit as the adapter.sync() tripwire in test_discovery_run.py).
#
# Covers:
#   * unstructured text → categorized articles → object types + properties;
#   * article `id` is the source_id_field with key_confidence >= 0.8;
#   * concept co-occurrence (kb graph) → a DraftLink with via_field=shared concept;
#   * FabricMapping(**ot.to_fabric_mapping_kwargs()) round-trips per inferred type;
#   * empty input → is_empty / degraded == "empty";
#   * uncategorized articles → degraded == "objects-only", source_id_field is None;
#   * isinstance(draft, OntologyDraft);
#   * SOVEREIGNTY TRIPWIRE: args[0] not in ("ingest", "build") across the run;
#   * smoke: real kb binary convo ingest → list round-trip in a tmp scope
#     (skipped cleanly when no binary is resolvable).
#
# Pure-logic + mocked subprocess. Run with:
#   uv run --group ee pytest tests/ee/test_kb_compile_digester.py -q

from __future__ import annotations

import shutil
import uuid

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.discovery import OntologyDraft  # noqa: E402
from pocketpaw_ee.discovery.kb_compile import KbCompileDigester  # noqa: E402

from pocketpaw.connectors.fabric_ingest import FabricMapping  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes — the kb-go subprocess seam, in memory
# --------------------------------------------------------------------------- #
def _make_fake_kb(
    monkeypatch,
    *,
    articles: list[dict],
    edges: list[dict] | None = None,
    nodes: list[dict] | None = None,
):
    """Patch the `_kb` seam the digester calls with an in-memory fake.

    ``articles`` is the canned list of WikiArticle-shaped dicts the fake's
    `list`/`show` serve back (keyed by the compile scope). ``nodes``/``edges``
    feed the `graph --format json` response (kb-go concept graph shape:
    nodes=[{id,label,kind,size}], edges=[{source,target,weight}]).

    Returns the ``calls`` list (every call's argv tuple) so a test can assert
    the keyless path and the sovereignty tripwire.
    """
    calls: list[tuple[str, ...]] = []
    store: dict[str, list[dict]] = {}

    def _scope_of(args: tuple[str, ...]) -> str:
        if "--scope" in args:
            return args[args.index("--scope") + 1]
        return "default"

    def _fake_kb(*args, input_text=None, timeout=120):
        calls.append(args)
        # SOVEREIGNTY: the keyed/off-box compile paths must NEVER be reached.
        assert args[0] != "ingest", (
            "sovereignty: KbCompileDigester must not call `kb ingest` (off-box LLM)"
        )
        assert args[0] != "build", (
            "sovereignty: KbCompileDigester must not call `kb build` (off-box LLM)"
        )

        scope = _scope_of(args)
        # Keyless compile: convo ingest <file> --scope <s> | accept --scope <s>.
        if args[0] in ("convo", "accept"):
            store.setdefault(scope, [])
            for a in articles:
                if a not in store[scope]:
                    store[scope].append(a)
            return {"articles": len(articles), "accepted": len(articles)}
        if args[0] == "list":
            # kb list omits concepts/categories — return the lean shape.
            return [
                {
                    "id": a["id"],
                    "title": a.get("title", ""),
                    "summary": a.get("summary", ""),
                }
                for a in store.get(scope, [])
            ]
        if args[0] == "show":
            article_id = args[1]
            for a in store.get(scope, []):
                if a["id"] == article_id:
                    return dict(a)
            return {}
        if args[0] == "graph":
            return {
                "scope": scope,
                "nodes": nodes or [],
                "edges": edges or [],
            }
        return {}

    monkeypatch.setattr("pocketpaw_ee.discovery.kb_compile._kb", _fake_kb)
    return calls


# Two well-categorized articles sharing the "billing" concept — the canonical
# happy-path fixture used by most tests.
_TWO_TYPED_ARTICLES = [
    {
        "id": "a1",
        "title": "Login lockout after billing failure",
        "summary": "Customer cannot log in; billing lock triggered.",
        "concepts": ["billing", "login"],
        "categories": ["SupportTicket"],
    },
    {
        "id": "a2",
        "title": "Refund requested on invoice 12",
        "summary": "Refund request tied to a billing dispute.",
        "concepts": ["billing", "refund"],
        "categories": ["RefundRequest"],
    },
]

# graph nodes (concept kind) + an edge sharing the "billing" concept across the
# two articles → drives a cross-type DraftLink via the shared concept.
_BILLING_GRAPH_NODES = [
    {"id": "c0", "label": "billing", "kind": "concept", "size": 2},
    {"id": "c1", "label": "login", "kind": "concept", "size": 1},
    {"id": "c2", "label": "refund", "kind": "concept", "size": 1},
]
_BILLING_GRAPH_EDGES = [
    {"source": "c0", "target": "c1", "weight": 1},
    {"source": "c0", "target": "c2", "weight": 1},
]


@pytest.fixture
def digester() -> KbCompileDigester:
    return KbCompileDigester()


# --------------------------------------------------------------------------- #
# Type + property inference
# --------------------------------------------------------------------------- #
def test_unstructured_text_compiles_to_typed_articles(digester, monkeypatch) -> None:
    calls = _make_fake_kb(monkeypatch, articles=_TWO_TYPED_ARTICLES)
    exhaust = {
        "zendesk": [
            "Customer can't log in, billing locked.",
            "Refund requested on invoice 12.",
        ]
    }
    draft = digester.digest(exhaust, {"connector": "zendesk", "workspace_id": "w1"})

    assert isinstance(draft, OntologyDraft)
    # categories → object types
    assert sorted(ot.name for ot in draft.object_types) == ["RefundRequest", "SupportTicket"]
    # the compile went through a KEYLESS path, never `kb ingest` / `kb build`
    assert any(c[0] in ("convo", "accept") for c in calls)
    assert all(c[0] not in ("ingest", "build") for c in calls)
    # provenance stamped
    assert draft.meta["digester"] == "kb-compile"
    assert draft.meta["connector"] == "zendesk"


def test_properties_inferred_over_article_fields(digester, monkeypatch) -> None:
    _make_fake_kb(monkeypatch, articles=_TWO_TYPED_ARTICLES)
    draft = digester.digest({"zendesk": ["body one", "body two"]})
    ot = draft.type_by_name("SupportTicket")
    assert ot is not None
    prop_names = {p.name for p in ot.properties}
    # properties span {title, summary, concepts, categories}
    assert {"title", "summary", "concepts", "categories"} <= prop_names


# --------------------------------------------------------------------------- #
# Primary key — the article id
# --------------------------------------------------------------------------- #
def test_article_id_is_the_source_id_field(digester, monkeypatch) -> None:
    _make_fake_kb(monkeypatch, articles=_TWO_TYPED_ARTICLES)
    draft = digester.digest({"zendesk": ["one ticket body"]})
    ot = draft.type_by_name("SupportTicket")
    assert ot is not None
    assert ot.source_id_field == "id"
    assert ot.key_confidence >= 0.8


def test_source_id_extracted_onto_objects(digester, monkeypatch) -> None:
    _make_fake_kb(monkeypatch, articles=_TWO_TYPED_ARTICLES)
    draft = digester.digest({"zendesk": ["one", "two"]})
    sids = sorted(o.source_id for o in draft.objects if o.source_id is not None)
    assert sids == ["a1", "a2"]


# --------------------------------------------------------------------------- #
# Concept co-occurrence → links
# --------------------------------------------------------------------------- #
def test_concept_cooccurrence_yields_links(digester, monkeypatch) -> None:
    _make_fake_kb(
        monkeypatch,
        articles=_TWO_TYPED_ARTICLES,
        nodes=_BILLING_GRAPH_NODES,
        edges=_BILLING_GRAPH_EDGES,
    )
    draft = digester.digest({"zendesk": ["a", "b"]})
    # the two articles share the "billing" concept → at least one cross-type link
    assert draft.links, "expected a concept-cooccurrence link"
    shared = [lk for lk in draft.links if lk.via_field == "billing"]
    assert shared, "expected a DraftLink whose via_field is the shared concept"
    lk = shared[0]
    assert lk.from_type != lk.to_type
    assert {lk.from_source_id, lk.to_source_id} <= {"a1", "a2"}
    assert 0.0 < lk.confidence <= 1.0


# --------------------------------------------------------------------------- #
# FabricMapping round-trip — the draft must be directly usable by ingest
# --------------------------------------------------------------------------- #
def test_draft_types_build_fabric_mapping(digester, monkeypatch) -> None:
    _make_fake_kb(monkeypatch, articles=_TWO_TYPED_ARTICLES)
    draft = digester.digest({"zendesk": ["x", "y"]})
    assert draft.object_types
    for ot in draft.object_types:
        mapping = FabricMapping(**ot.to_fabric_mapping_kwargs())
        assert mapping.type_name == ot.name
        assert mapping.source_id_field == "id"


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #
def test_empty_exhaust_yields_empty_draft(digester, monkeypatch) -> None:
    _make_fake_kb(monkeypatch, articles=[])
    assert digester.digest({}).is_empty
    assert digester.digest(None).is_empty
    draft = digester.digest({})
    assert draft.meta.get("degraded") == "empty"
    assert draft.meta["digester"] == "kb-compile"


def test_compile_yielding_no_articles_degrades_empty(digester, monkeypatch) -> None:
    # input present, but the compile produced nothing readable back.
    _make_fake_kb(monkeypatch, articles=[])
    draft = digester.digest({"zendesk": ["some text"]})
    assert draft.is_empty
    assert draft.meta.get("degraded") == "empty"


def test_uncategorized_articles_degrade_to_objects_only(digester, monkeypatch) -> None:
    uncategorized = [
        {
            "id": "u1",
            "title": "Misc note",
            "summary": "no category",
            "concepts": [],
            "categories": [],
        },
        {
            "id": "u2",
            "title": "Another note",
            "summary": "also no category",
            "concepts": [],
            "categories": [],
        },
    ]
    _make_fake_kb(monkeypatch, articles=uncategorized)
    draft = digester.digest({"zendesk": ["a", "b"]})
    assert draft.meta.get("degraded") == "objects-only"
    assert draft.object_types
    for ot in draft.object_types:
        assert ot.source_id_field is None
        assert ot.key_confidence < 0.3
    # objects still produced, with no source_id
    assert draft.objects
    assert all(o.source_id is None for o in draft.objects)
    assert draft.links == []


# --------------------------------------------------------------------------- #
# Sovereignty tripwire — non-negotiable
# --------------------------------------------------------------------------- #
def test_sovereignty_never_calls_ingest_or_build(digester, monkeypatch) -> None:
    calls = _make_fake_kb(
        monkeypatch,
        articles=_TWO_TYPED_ARTICLES,
        nodes=_BILLING_GRAPH_NODES,
        edges=_BILLING_GRAPH_EDGES,
    )
    digester.digest(
        {"zendesk": ["t1", "t2"], "intercom": ["t3"]},
        {"workspace_id": "w1", "connector": "zendesk"},
    )
    assert calls, "expected the digester to drive the kb seam at least once"
    # the load-bearing assertion: across the WHOLE digest run, the off-box
    # LLM-compile commands are never invoked.
    assert all(c[0] not in ("ingest", "build") for c in calls)
    # and at least one keyless compile actually ran.
    assert any(c[0] in ("convo", "accept") for c in calls)


def test_returns_ontology_draft_type(digester, monkeypatch) -> None:
    _make_fake_kb(monkeypatch, articles=_TWO_TYPED_ARTICLES)
    assert isinstance(digester.digest({"zendesk": ["t"]}), OntologyDraft)


# --------------------------------------------------------------------------- #
# Smoke — real kb binary, convo ingest → list round-trip (skip if absent)
# --------------------------------------------------------------------------- #
def _kb_binary_present() -> bool:
    from pocketpaw_ee.discovery import kb_compile

    bin_path = kb_compile.KB_BIN
    if shutil.which(bin_path):
        return True
    from pathlib import Path

    return Path(bin_path).exists()


@pytest.mark.skipif(not _kb_binary_present(), reason="kb binary not resolvable on this runner")
def test_smoke_real_kb_convo_ingest_roundtrip(digester) -> None:
    # A real, isolated scope so we never touch a shared KB. The digest drives
    # the keyless on-box compile end-to-end against the real binary.
    scope_tag = f"szd2smoke-{uuid.uuid4().hex[:8]}"
    exhaust = {
        scope_tag: [
            "Customer reports a billing lockout after a failed payment. "
            "The refund was requested and the account was unlocked.",
        ]
    }
    draft = digester.digest(exhaust, {"workspace_id": scope_tag, "connector": "smoke"})
    # The real binary must not have crashed; we get an OntologyDraft back either
    # way (empty if convo extraction produced nothing, populated otherwise).
    assert isinstance(draft, OntologyDraft)
    assert draft.meta["digester"] == "kb-compile"

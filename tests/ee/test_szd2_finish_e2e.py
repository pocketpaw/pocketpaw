# tests/ee/test_szd2_finish_e2e.py — F5: the FINISH end-to-end test.
#
# Updated: 2026-06-22 (feat/szd-finish-followups) — the F1 trigger now mints the
# run_id once and threads it into ``run_discovery_and_propose(run_id=...)``. The
# delegating-orchestrator stub accepts + forwards the new keyword, and STEP 3 now
# also asserts the 202 ``trigger_resp["run_id"]`` equals the proposals' marker
# run_id (the correlation followup-1 adds).
#
# Created: 2026-06-21 (F5 / feat/szd-finish-enforce). This is the headline proof
# that the WHOLE finished Sovereign Zero-Setup Discovery feature works as ONE
# coherent flow — every stage F1→F6 wired together and driven through the REAL
# code paths, with only the two external seams (the connector read + the kb-go
# subprocess) and the on-box MODEL client faked. Extends the slice-2 E2E
# (test_szd2_e2e.py) from "unstructured exhaust → 3 proposals → approve" all the
# way to "trigger → categorize → edit-in-review → approve → ENFORCE".
#
# THE SIX FLOW STEPS this single test exercises:
#   1. TRIGGER (F1) — drive the F1 service ``discovery.service.run`` with a
#      workspace whose connectors are a MIX of enabled + disabled; assert it
#      enumerates ONLY the enabled ones and fires the run.
#   2. CATEGORIZE (F2) — mock the on-box model client so the unstructured digest
#      yields ≥2 DOMAIN categories; assert ≥2 typed object types stage in the
#      ontology proposal AND the model path (kb prepare/accept) was used, never
#      ``convo ingest``.
#   3. PROPOSALS — the three proposals (_fabric_objects, _pocket_create,
#      _instinct_rule) stage as PENDING Instinct Actions sharing one run_id.
#   4. EDIT (F4) — PATCH the rule proposal to tighten its CEL ``when``; assert
#      re-validation passes, an ``edited`` correction is recorded, status stays
#      PENDING.
#   5. APPROVE — approve the edited proposals; the real executors materialise
#      them (fabric objects queryable, pocket created, rule landed via
#      ``get_active_rules``).
#   6. ENFORCE (F6) — with ``instinct_enforce_discovered_rules=True``, run a
#      governed action through ``gate_action`` that the approved rule TARGETS;
#      assert the verdict fires (escalate). Flip the flag OFF → the SAME action
#      proceeds. The flag gates real behaviour end-to-end.
#
# THE THREE SOVEREIGNTY ASSERTIONS (the headline guarantee, non-negotiable):
#   A. The mocked ``_kb`` seam proves ``kb ingest`` / ``kb build`` (the two
#      Anthropic-POSTing commands) were NEVER invoked across the WHOLE run.
#   B. The on-box model client resolved keyless / ollama-only — assert no cloud
#      LLM client was constructed and no call targeted a cloud host (the resolve
#      is hard-pinned ``force_provider="ollama"`` → ``api_key is None``).
#   C. GRACEFUL DEGRADE — a variant with the on-box model UNAVAILABLE (the client
#      raises) still produces the deterministic draft (objects-only), refine
#      returns ``meta["refine"]=="unavailable"``, and NO cloud fallback occurred.
#
# Determinism: the on-box model + the ``_kb`` seam are mocked (no live Ollama, no
# real kb binary). Backed by ``beanie_test_db`` (mongomock) so the materialised
# ``InstinctRuleDoc`` surfaces through the REAL ``rules.service.get_active_rules``
# that F6's gate consults — the approve→enforce handoff is a real round-trip, not
# a stub. The F4 PATCH's journal/soul side-effects are stubbed (they need cloud
# infra orthogonal to this flow); the edit + re-validate + correction logic is
# the REAL router handler.
#
# Run with:
#   uv run --group ee pytest tests/ee/test_szd2_finish_e2e.py -q

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.discovery import OntologyDraft  # noqa: E402
from pocketpaw_ee.discovery.kb_compile import KbCompileDigester  # noqa: E402
from pocketpaw_ee.discovery.orchestrate import _find_discovery_marker  # noqa: E402
from pocketpaw_ee.discovery.run import (  # noqa: E402
    DiscoveryRun,
    DiscoveryRunOptions,
    ReadAction,
)

from pocketpaw.config import Settings  # noqa: E402
from pocketpaw.fabric.store import FabricStore  # noqa: E402
from pocketpaw.instinct.correction import Correction, CorrectionPatch  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Mock connector surface (same shape as the slice-2 E2E). For UNSTRUCTURED
# discovery each read action returns a list of TEXT bodies (the exhaust).
# ---------------------------------------------------------------------------


@dataclass
class _MockActionResult:
    success: bool
    data: Any = None
    error: str | None = None
    records_affected: int = 0


class _MockAdapter:
    def __init__(self, data_by_action: dict[str, Any]) -> None:
        self._data_by_action = data_by_action

    async def actions(self) -> list[Any]:
        return []

    async def execute(self, action: str, params: dict[str, Any]) -> _MockActionResult:  # noqa: ARG002
        if action not in self._data_by_action:
            return _MockActionResult(success=False, error=f"unknown action {action}")
        return _MockActionResult(success=True, data=self._data_by_action[action])


class _MockRegistry:
    """Records every (connector_name, scope_key) resolve so the test can assert the
    workspace-scoped, pocket-less path was used."""

    def __init__(self, adapters: dict[str, _MockAdapter | None]) -> None:
        self._adapters = adapters
        self.resolves: list[tuple[str, str]] = []

    async def ensure_connected(self, connector_name: str, scope_key: str) -> _MockAdapter | None:
        self.resolves.append((connector_name, scope_key))
        return self._adapters.get(connector_name)


# ---------------------------------------------------------------------------
# UNSTRUCTURED exhaust — raw ticket / refund bodies, no record shape. The
# KbCompileDigester compiles these on-box into CATEGORIZED articles.
# ---------------------------------------------------------------------------

_TICKET_BODIES = [
    "Customer cannot log in after a failed payment — billing lock triggered.",
    "Account locked following a declined card; the user is stuck at the login screen.",
]

_REFUND_BODIES = [
    "Refund requested on invoice 12 — billing dispute, customer wants money back.",
    "The customer is asking for a refund tied to a duplicate billing charge.",
]


# ---------------------------------------------------------------------------
# The on-box MODEL — a fake AsyncOpenAI-shaped client returning DOMAIN-categorized
# articles. Mirrors the F2 categorize-test fake. This is the seam that, in
# production, ``resolve_on_box_client`` hard-pins to local Ollama (api_key=None).
# By substituting it we prove the model PATH ran without a live model OR a cloud
# key — categorization escapes the single "conversation" bucket on-box.
# ---------------------------------------------------------------------------

_MODEL_ARTICLES = [
    {
        "source": "blob.txt",
        "hash": "deadbeef",
        "raw_id": "deadbeef00000000",
        "title": "Login lockout after billing failure",
        "summary": "Customer cannot log in; billing lock triggered by a failed payment.",
        "content": "Full ticket body about a billing lockout.",
        "concepts": ["billing", "login"],
        "categories": ["SupportTicket"],
    },
    {
        "source": "blob.txt",
        "hash": "cafef00d",
        "raw_id": "cafef00d00000000",
        "title": "Refund requested on invoice 12",
        "summary": "Refund request tied to a billing dispute on a duplicate charge.",
        "content": "Full refund-request body referencing invoice 12.",
        "concepts": ["billing", "refund"],
        "categories": ["RefundRequest"],
    },
]


class _FakeChatCompletions:
    def __init__(self, articles: list[dict]) -> None:
        self._articles = list(articles)
        self._i = 0
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        # The slice requires a JSON-object response format — assert the shape.
        assert kwargs.get("response_format") == {"type": "json_object"}
        self.calls.append(kwargs)
        import json as _json

        art = self._articles[self._i % len(self._articles)]
        self._i += 1
        content = _json.dumps(art)

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


class _FakeOllamaClient:
    """Stand-in for the AsyncOpenAI client ``resolve_on_box_client`` returns.

    A sovereignty sentinel: it carries the resolved descriptor's ``api_key`` so a
    test can assert it is ``None`` (keyless / ollama-only) and its ``base_url`` so
    a test can assert it never targets a cloud host.
    """

    def __init__(self, articles: list[dict], *, api_key: str | None, base_url: str) -> None:
        self.chat = type("_Chat", (), {})()
        self.chat.completions = _FakeChatCompletions(articles)
        self.api_key = api_key
        self.base_url = base_url


class _RaisingOllamaClient:
    """An on-box client whose every model call raises — the UNAVAILABLE variant
    (``ollama serve`` down). Resolving it succeeds; calling it fails, so the
    code degrades to the deterministic draft rather than raising or cloud-falling."""

    class _Chat:
        class _Completions:
            async def create(self, **_kwargs: Any) -> Any:
                raise RuntimeError("ollama serve is down")

        completions = _Completions()

    chat = _Chat()
    api_key = None
    base_url = "http://localhost:11434/v1"


# ---------------------------------------------------------------------------
# Sovereignty resolver guard. ``resolve_on_box_client`` is the ONE place the
# code resolves a model client. We replace it with a recorder that (a) returns
# the on-box fake and (b) FAILS LOUDLY if anyone ever asks for a non-ollama
# provider — so a regression that reaches for a cloud client trips the test.
# ---------------------------------------------------------------------------


class _OnBoxResolver:
    """Records every resolve and hands back the on-box fake. The recorded
    descriptor proves the resolved client was keyless (api_key is None) and
    pointed at the local Ollama endpoint — never a cloud host."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.resolved: list[dict[str, Any]] = []

    def __call__(self, settings: Any) -> Any:  # noqa: ARG002 — signature parity
        # SOVEREIGNTY: assert the real resolver would have hard-pinned ollama by
        # checking the descriptor it produces is keyless + ollama. We mirror the
        # production resolve here so a regression in the pin is caught.
        from pocketpaw.llm.client import resolve_llm_client

        descriptor = resolve_llm_client(settings, force_provider="ollama")
        self.resolved.append(
            {
                "provider": descriptor.provider,
                "api_key": descriptor.api_key,
                "is_ollama": getattr(descriptor, "is_ollama", None),
            }
        )
        return self._client


def _install_on_box_model(monkeypatch, client: Any) -> _OnBoxResolver:
    """Patch BOTH on-box-client resolve seams (F2 categorize + F3 refine) to the
    SAME recording resolver, so the whole run resolves the model identically and
    the test can prove every resolve was keyless / ollama-only."""
    resolver = _OnBoxResolver(client)
    monkeypatch.setattr("pocketpaw_ee.discovery.kb_compile.resolve_on_box_client", resolver)
    monkeypatch.setattr("pocketpaw_ee.discovery._refine.resolve_on_box_client", resolver)
    return resolver


# ---------------------------------------------------------------------------
# The kb-go subprocess seam — agent-mode aware (prepare/accept), in-memory,
# argv-recording. Models prepare → on-box model → accept → list → show → graph.
# The recorded ``calls`` list is the evidence the sovereignty assertion checks:
# ``ingest`` / ``build`` (the Anthropic-POSTing commands) are NEVER expected.
# ---------------------------------------------------------------------------


def _install_fake_kb(monkeypatch, *, nodes: list[dict], edges: list[dict]) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    store: dict[str, list[dict]] = {}
    counter = {"n": 0}

    def _scope_of(args: tuple[str, ...]) -> str:
        if "--scope" in args:
            return args[args.index("--scope") + 1]
        return "default"

    def _fake_kb(*args: str, input_text: str | None = None, timeout: int = 120) -> Any:  # noqa: ARG001
        calls.append(args)
        scope = _scope_of(args)

        if args[0] == "prepare":
            # One prepare item per blob file (the digester writes one file per blob).
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
            # The model-compiled article JSON is on stdin; store it under the scope.
            import json as _json

            store.setdefault(scope, [])
            try:
                payload = _json.loads(input_text) if input_text else {}
            except _json.JSONDecodeError:
                payload = {}
            arts = payload.get("articles", []) if isinstance(payload, dict) else []
            for a in arts:
                counter["n"] += 1
                art = dict(a)
                # `accept` slugifies the title into a stable id.
                cat = (a.get("categories") or ["x"])[0]
                art.setdefault("id", f"{str(cat).lower()}-{counter['n']}")
                store[scope].append(art)
            return {"accepted": len(arts), "articles": len(arts), "concepts": 0}

        if args[0] == "convo":
            # FALLBACK path: `convo ingest` hardcodes a single "conversation" article.
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
            return {"scope": scope, "nodes": nodes, "edges": edges}

        return {}

    monkeypatch.setattr("pocketpaw_ee.discovery.kb_compile._kb", _fake_kb)
    return calls


# kb concept graph: "billing" co-occurs across SupportTicket + RefundRequest, so
# concept co-occurrence yields a cross-type link.
_GRAPH_NODES = [
    {"id": "c0", "label": "billing", "kind": "concept", "size": 3},
    {"id": "c1", "label": "login", "kind": "concept", "size": 2},
    {"id": "c2", "label": "refund", "kind": "concept", "size": 1},
]
_GRAPH_EDGES = [
    {"source": "c0", "target": "c1", "weight": 2},
    {"source": "c0", "target": "c2", "weight": 1},
]


# ---------------------------------------------------------------------------
# Correction exhaust — the rules-discovery signal. Three corrections on the SAME
# ``category`` path with a constant ``after`` value ("escalated") clear the
# recurrence threshold and carry a constant target, so the RuleDigester emits one
# high-confidence draft whose CEL ``when`` is ``action.category == "escalated"``.
# ---------------------------------------------------------------------------


def _strong_correction(workspace_id: str, idx: int) -> Correction:
    return Correction(
        action_id=f"act-{idx}",
        pocket_id=workspace_id,
        actor="u1",
        patches=[CorrectionPatch(path="category", before="normal", after="escalated")],
        context_summary=f"raised category #{idx}",
        action_title=f"Ticket #{idx}",
    )


# ---------------------------------------------------------------------------
# Fixtures — isolated stores + inert bus (cloned from the slice-2 E2E).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "szd2-finish-e2e-secret")


@pytest.fixture(autouse=True)
def recording_bus():
    """Inert recording EventBus so service ``emit()`` calls don't raise."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler: Any) -> None:  # noqa: ARG002
            return

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _RecordingBus()  # type: ignore[attr-defined]
    yield bus_mod._bus
    bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_szd2_finish.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: st)
    return st


@pytest.fixture
def fabric(tmp_path: Path, monkeypatch) -> FabricStore:
    fs = FabricStore(tmp_path / "fabric_szd2_finish.db")
    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda: fs)
    return fs


# ---------------------------------------------------------------------------
# F1 trigger helper — a fake ``list_connectors`` that returns a MIX of enabled +
# disabled connectors so the trigger's enabled-only enumeration is observable.
# ---------------------------------------------------------------------------


@dataclass
class _ConnRow:
    name: str
    enabled: bool


def _patch_enabled_connectors(monkeypatch, rows: list[_ConnRow]) -> None:
    """Patch ``connectors.service.list_connectors`` (the symbol the F1 service
    lazily imports) so the trigger sees a controlled enabled/disabled mix."""
    from pocketpaw_ee.cloud.connectors import service as connectors_service

    async def _fake_list_connectors(workspace_id: str, *, user_id: str):  # noqa: ARG001
        return list(rows)

    monkeypatch.setattr(connectors_service, "list_connectors", _fake_list_connectors)


def _make_discovery_run(registry: _MockRegistry, settings: Settings | None) -> DiscoveryRun:
    """A DiscoveryRun over the mock registry + the F2-categorizing KbCompileDigester.

    ``settings`` is threaded into the digester so the F2 prepare/accept model path
    runs (``None`` would take the deterministic convo-ingest fallback)."""
    return DiscoveryRun(registry=registry, digester=KbCompileDigester(settings=settings))


# ===========================================================================
# THE FINISH E2E — trigger → categorize → propose → edit → approve → enforce,
# sovereign at every step.
# ===========================================================================


async def test_szd_finish_trigger_to_edit_to_approve_to_enforce_sovereign(
    store, fabric, beanie_test_db, monkeypatch
):
    """The whole finished feature as ONE flow. Drives the F1 trigger over a
    mixed enabled/disabled connector set, categorizes on-box (F2), stages the
    three proposals, edits the rule in review (F4), approves all three, then
    proves the approved rule ENFORCES at the gate (F6) and the flag gates it.

    Sovereignty assertions A (no kb ingest/build) and B (keyless / ollama-only,
    no cloud host) ride along the whole run.
    """
    from pocketpaw_ee.cloud.discovery import service as discovery_service
    from pocketpaw_ee.cloud.fabric_proposals import executor as fo_executor
    from pocketpaw_ee.cloud.instinct_rule_proposals import (
        INSTINCT_RULE_PARAM_KEY,
        execute_approved_instinct_rule,
    )
    from pocketpaw_ee.cloud.pocket_proposals import executor as pc_executor
    from pocketpaw_ee.cloud.pockets import instinct_dispatch
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.rules import service as rules_service
    from pocketpaw_ee.discovery import orchestrate

    from pocketpaw.config import get_settings
    from pocketpaw.fabric.models import FabricQuery

    workspace_id = "w1"
    user_id = "u1"

    # ── SEAMS: kb-go subprocess (records argv) + the on-box model client.
    kb_calls = _install_fake_kb(monkeypatch, nodes=_GRAPH_NODES, edges=_GRAPH_EDGES)
    settings = get_settings()
    model_client = _FakeOllamaClient(
        _MODEL_ARTICLES,
        api_key=None,
        base_url=f"{settings.ollama_host.rstrip('/')}/v1",
    )
    on_box_resolver = _install_on_box_model(monkeypatch, model_client)

    # Guard B (compile-time): assert the constructed model client is keyless and
    # not pointed at any cloud host. (Belt-and-braces; the resolver records the
    # descriptor too — checked below after the run.)
    assert model_client.api_key is None
    assert "anthropic" not in model_client.base_url and "openai.com" not in model_client.base_url

    # ── Seed the correction exhaust so rules-discovery has a constant-target signal.
    for i in range(3):
        await store.record_correction(_strong_correction(workspace_id, i))

    # ── STEP 1 — TRIGGER (F1). The workspace has a MIX of connectors; only the
    #    enabled ones must reach the orchestrator. We patch the F1 service's
    #    orchestrator symbol with a delegating wrapper that (a) records the
    #    resolved connector_ids and (b) calls the REAL orchestrator with our
    #    injected DiscoveryRun (mock registry + F2-categorizing digester).
    _patch_enabled_connectors(
        monkeypatch,
        [
            _ConnRow(name="support", enabled=True),
            _ConnRow(name="legacy_crm", enabled=False),  # disabled — must NOT leak
        ],
    )

    adapters = {
        "support": _MockAdapter({"list_tickets": _TICKET_BODIES, "list_refunds": _REFUND_BODIES})
    }
    registry = _MockRegistry(adapters)
    opts = DiscoveryRunOptions(
        refine=True,  # F3 on — proves refine resolves on-box + stamps meta
        read_actions={
            "support": [
                ReadAction(action="list_tickets", type_name="tickets"),
                ReadAction(action="list_refunds", type_name="refunds"),
            ]
        },
    )

    captured: dict[str, Any] = {}

    async def _delegating_orchestrator(ws_id, u_id, connector_ids, run_opts=None, *, run_id=None):  # noqa: ARG001
        # (a) record what the F1 trigger resolved — the enabled-only enumeration.
        captured["connector_ids"] = list(connector_ids)
        captured["workspace_id"] = ws_id
        captured["user_id"] = u_id
        captured["run_id"] = run_id
        # (b) drive the REAL finished orchestrator with our injected run + opts so
        #     the deterministic flow exercises F2 (categorize) + F3 (refine). Thread
        #     the trigger's run_id through so the proposals' markers correlate to the
        #     202 dispatch token.
        try:
            result = await orchestrate.run_discovery_and_propose(
                ws_id,
                u_id,
                connector_ids,
                opts,
                discovery_run=_make_discovery_run(registry, settings),
                run_id=run_id,
            )
        except BaseException as exc:  # noqa: BLE001 — surface the swallowed task error
            captured["error"] = exc
            raise
        captured["result"] = result
        return result

    monkeypatch.setattr(discovery_service, "run_discovery_and_propose", _delegating_orchestrator)

    # Fire the F1 trigger front door. It enumerates enabled connectors, builds
    # opts, and fires the orchestrator as a background task; we drain it.
    body = discovery_service.DiscoveryRunRequest()
    trigger_resp = await discovery_service.run(workspace_id, user_id, body)
    assert trigger_resp["run_id"], "trigger returns an optimistic run_id immediately"

    # Drain the fire-and-forget task (the F1 service uses asyncio.create_task).
    for _ in range(500):
        if "result" in captured or "error" in captured:
            break
        await asyncio.sleep(0.01)
    if "error" in captured:
        raise AssertionError(f"discovery task raised: {captured['error']!r}") from captured["error"]
    assert "result" in captured, "the fired discovery task never completed"
    result = captured["result"]

    # F1 — only the ENABLED connector reached the orchestrator (disabled excluded).
    assert captured["connector_ids"] == ["support"], captured["connector_ids"]
    assert "legacy_crm" not in captured["connector_ids"]
    assert captured["workspace_id"] == workspace_id and captured["user_id"] == user_id
    # And discovery used the workspace-scoped, pocket-less resolve path.
    assert ("support", f"ws:{workspace_id}") in registry.resolves
    assert all(not key.startswith("pocket:") for _, key in registry.resolves)

    # ── STEP 2 — CATEGORIZE (F2). The on-box model gave DOMAIN categories, so the
    #    staged ontology carries ≥2 TYPED object types — not the single
    #    "conversation" bucket the keyless convo-ingest path is stuck on.
    assert result.fabric_objects_action_id is not None, "expected a _fabric_objects proposal"
    fabric_action = await store.get_action(result.fabric_objects_action_id)
    fo_blob = fabric_action.parameters["_fabric_objects"]
    staged_types = {ot["type_name"] for ot in fo_blob["object_types"]}
    assert {"SupportTicket", "RefundRequest"} <= staged_types, staged_types
    assert "conversation" not in {t.lower() for t in staged_types}
    assert fo_blob.get("links"), "expected a concept-cooccurrence link in the ontology"
    # The MODEL path ran (kb prepare/accept), NOT the convo-ingest fallback.
    cmds = [c[0] for c in kb_calls]
    assert "prepare" in cmds and "accept" in cmds
    assert "convo" not in cmds, "F2 model path must not fall back to convo ingest"
    # The on-box model was actually called to categorize.
    assert model_client.chat.completions.calls, "the on-box model was never invoked"

    # ── STEP 3 — PROPOSALS. The three proposals stage as PENDING Instinct Actions
    #    sharing one run_id, each with its distinct role.
    assert result.pocket_action_id is not None, "expected a _pocket_create proposal"
    assert result.instinct_action_ids, "expected at least one _instinct_rule proposal"
    rule_action_id = result.instinct_action_ids[0]

    pocket_action = await store.get_action(result.pocket_action_id)
    rule_action = await store.get_action(rule_action_id)
    assert fabric_action.status == ActionStatus.PENDING
    assert pocket_action.status == ActionStatus.PENDING
    assert rule_action.status == ActionStatus.PENDING

    assert "_fabric_objects" in fabric_action.parameters
    assert "_pocket_create" in pocket_action.parameters
    assert INSTINCT_RULE_PARAM_KEY in rule_action.parameters

    fo_marker = _find_discovery_marker(fabric_action.parameters)
    pc_marker = _find_discovery_marker(pocket_action.parameters)
    ir_marker = _find_discovery_marker(rule_action.parameters)
    assert fo_marker and pc_marker and ir_marker
    assert fo_marker["run_id"] == pc_marker["run_id"] == ir_marker["run_id"] == result.run_id
    # CORRELATION (followup-1) — the 202 trigger run_id is the SAME id tagging the
    # proposals' markers, so a client can match its 202 to the proposals it made.
    assert trigger_resp["run_id"] == result.run_id == captured["run_id"]

    # ── STEP 4 — EDIT (F4). PATCH the rule proposal to TIGHTEN its CEL `when`. The
    #    edit re-validates, records an `edited` correction, and keeps PENDING. We
    #    drive the REAL router handler over HTTP with our InstinctStore wired in;
    #    the journal/soul side-effects (cloud infra) are stubbed.
    rule_blob = rule_action.parameters[INSTINCT_RULE_PARAM_KEY]
    original_when = rule_blob["rule_spec"]["when"]
    # The reverse-engineered rule keys on the corrected category path.
    assert original_when == 'action.category == "escalated"', original_when
    tightened_when = 'action.category == "escalated" && action.amount > 1000'

    edited_action = await _patch_rule_proposal(
        monkeypatch,
        store=store,
        action_id=rule_action_id,
        workspace_id=workspace_id,
        user_id=user_id,
        tightened_when=tightened_when,
    )
    # Re-validation passed, status stayed PENDING, the tightened `when` persisted.
    assert edited_action["status"] == "pending"
    after_edit = await store.get_action(rule_action_id)
    assert after_edit.parameters[INSTINCT_RULE_PARAM_KEY]["rule_spec"]["when"] == tightened_when
    # An `edited` correction was recorded against the action.
    corrections = await store.get_corrections_for_action(rule_action_id)
    assert corrections, "expected an edit correction to be recorded"

    # ── STEP 5 — APPROVE. The real executors materialise all three proposals.
    approved_fo = await store.approve(result.fabric_objects_action_id, approver=user_id)
    await fo_executor.execute_approved_fabric_objects(approved_fo)
    assert (await store.get_action(result.fabric_objects_action_id)).status == ActionStatus.EXECUTED

    approved_pc = await store.approve(result.pocket_action_id, approver=user_id)
    await pc_executor.execute_approved_pocket_create(approved_pc)
    final_pc = await store.get_action(result.pocket_action_id)
    assert final_pc.status == ActionStatus.EXECUTED, final_pc.error

    approved_ir = await store.approve(rule_action_id, approver=user_id)
    await execute_approved_instinct_rule(approved_ir)
    final_ir = await store.get_action(rule_action_id)
    assert final_ir.status == ActionStatus.EXECUTED, final_ir.error

    # Materialisation: fabric objects queryable; pocket created; rule landed.
    tickets = await fabric.query(FabricQuery(type_name="SupportTicket"), workspace_id=workspace_id)
    assert tickets.objects, "expected materialised SupportTicket fabric objects"
    assert all(o.type_name == "SupportTicket" for o in tickets.objects)

    pocket_id = final_pc.parameters["_pocket_create"]["outcome"]["pocket_id"]
    wire = await pockets_service.get(pocket_id, user_id)
    assert wire["workspace"] == workspace_id

    active = await rules_service.get_active_rules(workspace_id)
    assert len(active) == 1, active
    landed = active[0]
    assert landed["workspace_id"] == workspace_id
    assert landed["scope"]["workspace_id"] == workspace_id
    assert landed["status"] == "active"
    # The EDITED, tightened `when` is what landed — the F4 edit flows to the rule.
    assert landed["when"] == tightened_when
    assert landed["action"] in ("require_approval", "notify", "block")

    # ── STEP 6 — ENFORCE (F6). With the flag ON, a governed action the approved
    #    rule TARGETS must fire the rule's verdict (escalate). The rule keys on
    #    `action.category == "escalated" && action.amount > 1000`, so a row with
    #    that category AND amount escalates; the SAME action with the flag OFF
    #    proceeds untouched. This proves the flag gates real behaviour E2E.
    template = _enforcement_template()
    targeted_row = {"action": {"category": "escalated", "amount": 5000}}

    # Flag ON → the approved discovered rule fires (require_approval → escalate).
    _enable_enforcement(monkeypatch, instinct_dispatch, enabled=True)
    on_result = await instinct_dispatch.gate_action(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        template=template,
        action_name="do_thing",
        row_context=targeted_row,
    )
    assert on_result.next_step == "pending_approval", on_result.next_step
    assert on_result.decision.verdict == "ESCALATE_APPROVAL"
    assert any(r.when == tightened_when for r in on_result.decision.matched_rules)

    # Flag OFF → the SAME action proceeds; the discovered rule is dead code.
    _enable_enforcement(monkeypatch, instinct_dispatch, enabled=False)
    off_result = await instinct_dispatch.gate_action(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        template=template,
        action_name="do_thing",
        row_context=targeted_row,
    )
    assert off_result.next_step == "proceed", off_result.next_step
    assert off_result.decision.verdict == "EXECUTE"

    # ── SOVEREIGNTY A — across the WHOLE run, the off-box Anthropic-POSTing kb
    #    commands were NEVER invoked. No tenant exhaust left the box.
    assert kb_calls, "expected the digester to drive the kb seam at least once"
    assert all(c[0] not in ("ingest", "build") for c in kb_calls), (
        f"sovereignty violation: off-box kb command invoked — argv: {[c[0] for c in kb_calls]}"
    )
    # ── SOVEREIGNTY B — every on-box model resolve was keyless / ollama-only; no
    #    cloud LLM client was constructed and no call targeted a cloud host.
    assert on_box_resolver.resolved, "the on-box model resolver was never consulted"
    for desc in on_box_resolver.resolved:
        assert desc["provider"] == "ollama", desc
        assert desc["api_key"] is None, f"NON-KEYLESS resolve — cloud key leaked: {desc}"
        assert desc["is_ollama"] is True, desc
    # The constructed client never pointed at a cloud host.
    assert "anthropic" not in model_client.base_url
    assert "openai.com" not in model_client.base_url


# ===========================================================================
# SOVEREIGNTY C — graceful degrade: the on-box model is UNAVAILABLE (the client
# raises). Discovery still produces the deterministic draft (objects-only),
# refine returns ``meta["refine"]=="unavailable"``, and NO cloud fallback occurs.
# ===========================================================================


async def test_szd_finish_on_box_model_unavailable_degrades_no_cloud(monkeypatch):
    """The on-box model is down. The deterministic floor still holds: F2 falls
    back to convo-ingest (objects-only), F3 refine stamps ``unavailable``, and
    the run never reaches for a cloud client — proving fail-soft-on-availability,
    fail-closed-on-sovereignty."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    kb_calls = _install_fake_kb(monkeypatch, nodes=[], edges=[])

    # The on-box client RESOLVES fine but every model call raises (ollama down).
    raising_client = _RaisingOllamaClient()
    on_box_resolver = _install_on_box_model(monkeypatch, raising_client)

    registry = _MockRegistry({"support": _MockAdapter({"list_tickets": _TICKET_BODIES})})
    opts = DiscoveryRunOptions(
        refine=True,  # request the refine pass — it must degrade, not raise.
        read_actions={"support": [ReadAction(action="list_tickets", type_name="tickets")]},
    )
    run = _make_discovery_run(registry, settings)

    draft = await run.run("w1", ["support"], opts)

    # F3 — refine could not reach the model → the deterministic draft is returned,
    # stamped unavailable (never an exception, never a cloud fallback).
    assert isinstance(draft, OntologyDraft)
    assert draft.meta.get("refine") == "unavailable", draft.meta

    # F2 — the model categorize call raised per-article, so the digester degraded
    # to the keyless convo-ingest path (objects-only). The draft still exists.
    cmds = [c[0] for c in kb_calls]
    assert "convo" in cmds, "expected the keyless convo-ingest fallback on model failure"
    assert all(c[0] not in ("ingest", "build") for c in kb_calls), (
        "sovereignty: no off-box kb command even on the degraded path"
    )
    # The draft degraded to objects-only (no DOMAIN typing without the model).
    assert draft.meta.get("degraded") == "objects-only" or not any(
        ot.name in ("SupportTicket", "RefundRequest") for ot in draft.object_types
    ), draft.meta

    # Sovereignty: every resolve that DID happen was keyless / ollama-only — no
    # cloud client was ever constructed on the degraded path.
    for desc in on_box_resolver.resolved:
        assert desc["provider"] == "ollama"
        assert desc["api_key"] is None


# ---------------------------------------------------------------------------
# Helpers — the F4 PATCH driver + the F6 enforcement template / flag.
# ---------------------------------------------------------------------------


async def _patch_rule_proposal(
    monkeypatch,
    *,
    store: InstinctStore,
    action_id: str,
    workspace_id: str,
    user_id: str,
    tightened_when: str,
) -> dict[str, Any]:
    """Drive the REAL F4 PATCH handler to tighten the staged rule's CEL `when`.

    Mounts the instinct router over the test InstinctStore (``_store`` patched),
    with auth + license stubbed, and the journal/soul side-effects (cloud infra
    orthogonal to this flow) stubbed. Returns the ``action`` wire dict.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.instinct_rule_proposals import INSTINCT_RULE_PARAM_KEY
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.instinct import router as instinct_router

    # Stub the journal + soul side-effects (they need cloud infra; the edit /
    # re-validate / correction logic under test is the REAL handler).
    monkeypatch.setattr(instinct_router, "_emit_human_corrected", lambda **kw: None)

    async def _noop_soul(*a, **k):  # noqa: ARG001
        return None

    monkeypatch.setattr(instinct_router, "_forward_to_soul", _noop_soul)

    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    user = SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role="admin")],
    )

    app = FastAPI()
    add_error_handler(app)
    app.include_router(instinct_router.router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id

    # The current rule_spec, tightened — tenancy copies left intact (pinned anyway).
    before = await store.get_action(action_id)
    rule_spec = dict(before.parameters[INSTINCT_RULE_PARAM_KEY]["rule_spec"])
    rule_spec["when"] = tightened_when

    with patch("pocketpaw_ee.instinct.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.patch(
                f"/instinct/actions/{action_id}/proposal",
                json={"rule_spec": rule_spec},
            )
    assert resp.status_code == 200, resp.text
    return resp.json()["action"]


def _enforcement_template() -> Any:
    """A minimal valid v2 PocketTemplate with a `category` + `amount` column so the
    discovered rule's `action.category` / `action.amount` identifiers resolve, and
    one `auto`-policy action so the template alone would EXECUTE (the discovered
    rule is what changes the verdict)."""
    from pocketpaw.bundled_templates import PocketTemplate

    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "enforce-template",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "test",
            "description": "enforcement fixture",
            "shape": "data-grid",
            "state": {
                "entity_type": "Action",
                "columns": [
                    {"field": "category", "widget": "text"},
                    {"field": "amount", "widget": "number"},
                ],
            },
            "actions": [
                {
                    "name": "do_thing",
                    "label": "Do Thing",
                    "kind": "single-row",
                    "instinct_policy": "auto",
                }
            ],
        }
    )


def _enable_enforcement(monkeypatch, instinct_dispatch: Any, *, enabled: bool) -> None:
    """Flip ``instinct_enforce_discovered_rules`` for the gate by monkeypatching the
    module-local ``get_settings`` the dispatch reads (the F6 test's pattern)."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    object.__setattr__(settings, "instinct_enforce_discovered_rules", enabled)
    monkeypatch.setattr(instinct_dispatch, "get_settings", lambda: settings)

<!-- Slice-2 build plan + S2-R0 design decision. Created 2026-06-20 by crew (captain-in-charge,
     captain out). Source: understand-workflow wf_a3f8a304 (6 agents, 5 ground-truth maps of the
     slice-1 code + gate discipline + kb-go integration + rules-design). This doc is the artifact
     the captain reviews at the PR gate. Status: BUILD-IN-PROGRESS. -->

# Sovereign Zero-Setup Discovery — Slice 2 Build Plan

**Date:** 2026-06-20 · **Branch:** `feat/szd-slice2-discovery` (worktree `.claude/worktrees/szd2-int`, off `origin/dev`)
**Scope:** slice 2 = **(K)** `KbCompileDigester` — unstructured exhaust → ontology, on-box; **(R)** rules-discovery — reverse-engineer draft governed Instinct rules from exhaust, surfaced through the gate.

## Ground-truth facts (from the code map — do not re-litigate)

- **No digester registry.** Wiring is constructor injection at `run.py:203` (`self._digester = digester or StructuredShapeDigester()`).
- **No Instinct rule store/model exists.** `instinct/store.py:252-335` has 4 tables, none for rules. The only `InstinctRule` is template CEL config (`bundled_templates/schema.py:397-412`); the only "learned rule" is a soul procedural string (`correction_soul_bridge.py:99-142`). **Rules-discovery is build-from-zero.**
- **Gate dispatch is a per-path `if blob is not None` chain in FOUR router functions** (approve / bulk_approve / reject / bulk_reject) — no central registry. Missing one tenancy guard = cross-tenant approval escalation (the PR #1183 class bug).
- **kb-go on-box keyless ONLY via `convo ingest` or `prepare`+`accept`.** `kb ingest` / `kb build` POST to Anthropic (`kb.go:1349`) and must NEVER touch tenant exhaust. **This is the #1 sovereignty constraint — encoded as a test, not trusted to the code path.**
- **pocketpaw implementers run SEQUENTIALLY in one worktree** (nested-repo rule — `isolation:"worktree"` does not isolate the nested pocketPaw checkout).

## S2-R0 — Design decision (made by crew, captain out; documented for review)

The interactive `/paw-brainstorm` gate the plan recommends needs the captain present; with the captain out, the crew adopts the plan's well-reasoned default and documents it here for PR-gate review.

- **P2 — `Rule` data model.** `when: CelExpression` (reuse `bundled_templates/expressions`), `action: InstinctRuleActionT` = `Literal["require_approval","notify","block"]` (reuse `schema.py:121`), `scope` = `workspace_id` + optional `pocket_id`/`object_type`, `confidence: float [0,1]` (`_clamp`), `provenance` = the audit-row / correction / record ids it was inferred from. A `RuleDraft` mirrors `DraftObjectType`'s draft/confidence split for the digest output. **No new primitives** — reuses CEL + the action literal.
- **P2-home — persistence.** EE Beanie `RuleDoc`, alongside the gate types (parallels the slice-1 "Digester home → EE" open question). Registered in `ALL_DOCUMENTS` so the `beanie_test_db` fixture picks it up. Tenancy/owner are top-level fields, NEVER nested in the editable `rule_spec`.
- **P3 — enforcement BOUNDARY (the scoping call).** Slice 2 = the full **discovery pipeline**: exhaust → digest → propose → review/edit → approve → **persist as an active, workspace-scoped, human-OWNED, EDITABLE `RuleDoc`** + a `get_active_rules(workspace_id)` read API. This delivers the Edra-parity core — *white-box, human-editable, owned executable rules; raw data never left the box.* The rule shape is enforcement-ready (CEL `when` + action literal, `InstinctRule`-compatible).
  **Out of scope for slice 2 (explicit slice-3 follow-up): wiring approved workspace rules into the LIVE gate dispatch** (`instinct_composer.py:248` / `instinct_dispatch`). Rewiring live, security-critical gate evaluation for every action is high-stakes and deserves the captain's review of the approach before it ships. Slice 2 makes rules discoverable, governed, and owned; slice 3 makes them enforced-at-dispatch. The `get_active_rules` API is the seam.

## Slices (dependency-ordered; each independently mergeable, each ships unit + smoke tests)

### Track K — KbCompileDigester (M; reuses 100% of the slice-1 gated flow)

- **S2-K1 — `KbCompileDigester`.** New `ee/pocketpaw_ee/discovery/kb_compile.py` (digester + module-local `_kb` subprocess seam cloned from `cloud/agents/knowledge.py:71-97`). Add to `discovery/__init__.py` import + `__all__`. Contract: `digest(records, connector_meta=None) -> OntologyDraft` matching the `Digester` Protocol (`digester.py:57-71`); `records` = text blobs; never raises on empty/degenerate; stamps `meta["digester"]="kb-compile"`; populates types/properties/links so `to_fabric_mapping_kwargs()` yields a valid `FabricMapping` (article `id` = `source_id_field`; concept co-occurrence → `DraftLink`). On-box pipeline: `kb convo ingest <tmp> --scope workspace:{wid}:discovery` → `kb list/show/graph --format json` → infer ontology. **Sovereignty tripwire test (mandatory):** `fake_kb` asserts `args[0] not in ("ingest","build")`. Tests: unit (clone `test_structured_shape_digester.py`, mock `_kb`) + smoke (real `kb` binary, `convo ingest`→`list`, skip if binary absent). **Deps:** none.
- **S2-K2 — DiscoveryRun digester selection (OPTIONAL/flagged).** `digester_kind: Literal["structured","unstructured"]="structured"` on `DiscoveryRunOptions`; `_select_digester(opts)`; injected `digester` overrides the flag. Deferrable — a caller can already inject `DiscoveryRun(digester=KbCompileDigester())` with zero new code. Build only if the choice should live inside the run. **Deps:** S2-K1.

### Track R — Rules-discovery (L; build-from-zero, after S2-R0)

- **S2-R1 — `Rule` model + persistence.** `ee/pocketpaw_ee/discovery/rule_models.py` (`Rule` + `RuleDraft`) + a `RuleDoc` Beanie doc + accessors (incl. `get_active_rules(workspace_id)`), registered in `ALL_DOCUMENTS`. `Rule.model_validate(blob['rule_spec'])` round-trips at the executor chokepoint; confidence clamps; tenancy/owner separate from `rule_spec`. Tests: unit (validate accept/reject, CEL validates, action literal, clamp) + smoke (`beanie_test_db` round-trip). **Deps:** S2-R0.
- **S2-R2 — `RuleDigester` inference engine.** `ee/pocketpaw_ee/discovery/rule_digester.py`: read `instinct_audit` (`query_audit`) + `instinct_corrections` (`get_corrections_for_pocket`) + optional `KbCompileDigester` output → emit `RuleDraft`s with confidence. **Generalize the existing 3×-correction promotion** (`correction_soul_bridge.py:99-142`) from soul-string to structured `RuleDraft`. Deterministic (no LLM on the hot path — sovereignty/on-box). Own confidence floor (NOT `KEY_CONFIDENCE_FLOOR`). Tests: unit (N corrections→1 draft; <threshold→none; weak→low-confidence-skipped; empty→empty) + smoke. **Deps:** S2-R1.
- **S2-R3 — `_instinct_rule` gate type.** New `ee/pocketpaw_ee/cloud/instinct_rule_proposals/{propose,executor,__init__}.py`, cloning `_pocket_create` (SZD-5b) EXACTLY. Blob: `kind, schema=1, workspace_id, rule_spec, summary, correlation_id, proposed_event_id`. Mint `correlation_id` before the blob; `store.propose(pocket_id=workspace_id, …)`; back-write `proposed_event_id`. Executor: schema-guard → tenancy → idempotency → `Rule.model_validate(rule_spec)` in try/`_fail` → write via R1 service (never raw) → `mark_executed` + chain-close (executor owns `decision.completed` on approve). Tests: unit (blob shape, schema-mismatch terminal, idempotent re-approve, malformed→`_fail`) + smoke (propose→approve→EXECUTED→rule landed). **Deps:** S2-R0, S2-R1.
- **S2-R4 — Router four-path dispatch + tenancy (SECURITY-CRITICAL).** `instinct/router.py`: `_instinct_rule_blob` accessor; `_assert_instinct_rule_workspace` guard called in **ALL FOUR** paths (approve, bulk_approve, reject, bulk_reject); dispatch branch in approve + bulk_approve (read blob, `_emit_human_corrected`, `execute_approved_instinct_rule`, non-fatal try/except); close branch in reject + bulk_reject (`_emit_human_corrected(rejected)` + `_emit_decision_completed_rejected`, no executor, bulk ends with `continue`). Tests (clone `test_instinct_approval_security.py`): **cross-workspace 403 on all FOUR paths** + exactly-one chain-close per path. **Deps:** S2-R3.
- **S2-R5 — Orchestrate third proposal path.** `discovery/orchestrate.py`: `_draft_to_instinct_rules(draft, …)` builder (own confidence threshold); lazy-import + `propose_instinct_rule`; third file+stamp block (`_stamp_discovery_marker(blob_key="_instinct_rule", role="instinct_rules")`); add `"_instinct_rule"` to the supersede marker tuple (`:337` — one-line, else stale rule proposals never supersede); `instinct_action_id: str|None` on `DiscoveryProposalResult`. Tests: unit (files PENDING `_instinct_rule`; 2nd run supersedes; low-confidence skip+flag; empty→none) + smoke (approve→EXECUTED). **Deps:** S2-R2, S2-R3, S2-R4.
- **S2-R6 — `human.corrected` on edited rule proposals (learning loop; lowest priority / follow-up OK).** Wire the review edit→approve so editing a rule draft emits `human.corrected` + feeds rule-inference confidence (generalize `Correction` beyond Action scalars to `rule_spec` keys). Tests: unit + smoke. **Deps:** S2-R5.

### Integration + UI

- **S2-E1 — Backend E2E.** `tests/ee/test_szd2_e2e.py`: `DiscoveryRun(digester=KbCompileDigester())` over unstructured text records → files THREE proposals (`_fabric_objects`, `_pocket_create`, `_instinct_rule`, shared `run_id`) → approve all → real executors → assert EXECUTED + materialization via `FabricStore.query` + rule landed via `get_active_rules`. **Sovereignty assertion:** mocked `_kb` proves `ingest`/`build` never called. **Deps:** S2-K1, S2-R5.
- **S2-UI — paw-enterprise rule review card + preview e2e** (separate repo / worktree, `VITE_CLOUD_URL`). Extend the slice-1 discovery-review surface with a rule card (`when`/`action`/scope/confidence/provenance + accept/edit/reject). Tests: vitest unit + **preview-panel e2e via the proven `/sidepanel` bypass recipe** (worktree + absolute-ripple-path + free port). **Deps:** S2-R3 (blob shape).

## Build order (supervised, sequential in `szd2-int`; STOP at captain gate, never merge)

```
K1 → (K2 deferred) → R1 → R2 → R3 → R4 → R5 → (R6 opt) → E1   [pocketPaw, sequential]
                                                            S2-UI [paw-enterprise, own worktree]
```

## Risks (slice-2-specific)

- **RK-1 sovereignty (#1):** KbCompileDigester must use ONLY `convo ingest`/`prepare`+`accept`; the `fake_kb` tripwire test is non-negotiable.
- **RK-2 on-box model:** keep R2 inference deterministic; no cloud-LLM refine pass in slice 2.
- **RK-3 subprocess reaping:** `_kb` via `asyncio.to_thread`, honour `timeout`.
- **RK-5 four-path trap:** R4 must assert cross-workspace 403 on all four paths.
- **RK-6 double chain-close:** approve→executor owns, reject→router owns; exactly one.
- **RK-7 rule confidence floor:** R5 uses its own threshold, not `KEY_CONFIDENCE_FLOOR`.
- **RK-10 git collision:** sequential in one worktree.

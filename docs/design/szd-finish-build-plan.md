All anchors confirmed across all four maps (the kb-go category hardcode is at `convo.go:625` exactly as Map C states, with the `"conversation"` filter also at `:766`/`:820`). I have everything I need to write the plan.

---

# Sovereign Zero-Setup Discovery — Finish-Build Plan (FULL polish)

**Worktrees:** pocketpaw integration = `…/pocketPaw/.claude/worktrees/szd2-int` (branch `feat/szd-slice2-discovery`); paw-enterprise UI = `…/paw-enterprise/.claude/worktrees/szd2-ui`. kb-go lives at `…/paw-workspace/kb-go` (separate repo).

**Six gaps → six slices:** F1 trigger (#1), F2 categorization (#3), F3 refine (#5), F4 edit-in-review (#4), F5 real-connector E2E (#6), **F6 live rule enforcement (#2 — captain-review gate)**.

---

## 1 · VERTICAL SLICES (dependency-ordered)

### F1 — Discovery TRIGGER endpoint + UI button (gap #1)
**Repo:** pocketpaw (backend) + paw-enterprise (frontend). **Worktree:** `szd2-int` + `szd2-ui`.
**Depends on:** nothing — orchestrator (`run_discovery_and_propose`) and proposal staging already ship. Build first; everything else is testable through it.

**Backend — create 4-file entity** `ee/pocketpaw_ee/cloud/discovery/{router.py, service.py, dto.py, domain.py}` (cloud 4-file rule):
- `router.py` — `POST /cloud/discovery/run`, prefix `/cloud/discovery`, `dependencies=[Depends(require_license)]`, route-gated `Depends(require_action_any_workspace("connector.execute"))`. Resolve workspace from `current_workspace_id` (`_core/deps.py:50`, re-exported `shared.deps`), user from `current_user_id` — **no `{workspace_id}` path param** (the UI auto-threads `X-Workspace-Id`, `api.ts:141`). Mirror `connectors/router.py:87-108`.
- `service.py` — `run(workspace_id, user_id, body)`: `connectors_service.list_connectors(workspace_id, user_id=user_id)` → `connector_ids = [r.name for r in rows if r.enabled]`; build `DiscoveryRunOptions(sample_cap=body.sample_cap or DEFAULT_SAMPLE_CAP)`; fire `run_discovery_and_propose(workspace_id, user_id, connector_ids, opts)` (signature `orchestrate.py:420`) via **`asyncio.create_task`** (mirror `chat/router.py:762`); return `202` with `run_id` immediately. **Do not** thread `digester_kind` — `DiscoveryRunOptions` (`run.py:74-93`) has no such field; structured-vs-unstructured is chosen inside `DiscoveryRun`. v1 lets the run default.
- `dto.py` — `DiscoveryRunRequest{sample_cap: int|None, connector_ids: list[str]|None}` + `DiscoveryRunResponse{run_id, fabric_objects_action_id, pocket_action_id, instinct_action_ids, materialised_types, skipped_types}` (mirrors `DiscoveryProposalResult`, `orchestrate.py:112-137`).
- **Mount:** `ee/pocketpaw_ee/cloud/__init__.py` — `app.include_router(discovery_router, prefix="/api/v1")` alongside the connectors/jobs includes (`__init__.py:225,244`).

**Frontend** — `paw-enterprise/src/lib/core/workspaces/connectors-api.ts` (or new `core/discovery/api.ts`): add `runWorkspaceDiscovery(body)` → `api.post<DiscoveryRunResult>('/cloud/discovery/run', body)` (chokepoint `api.ts:345`; never `runtime/api.ts`, that proxies OSS `instinct/actions`). In `ApprovalsPanel.svelte`: add `runDiscovery()` handler + `discovering = $state(false)`; place a Compass button in the list header (`:660-677`) and the empty-state CTA (`:692`, "No pending actions"). On success, reload via `listPendingActions()` (the onMount call, `:79-81`); proposals flow into the existing `discoveryActions` `$derived` (`:131`) → existing run group → `DiscoveryReviewCard` (zero new rendering).

**Contract:** `POST /api/v1/cloud/discovery/run {sample_cap?} → 202 {run_id, …action_ids}`. Proposals surface as pending Instinct Actions the ApprovalsPanel already reads — no extra surfacing.
**Tests:** (be) `test_run_enumerates_enabled_connectors_only` (disabled connectors excluded); `test_run_returns_202_with_run_id`; `test_run_requires_connector_execute_action` (403 without perm); `test_run_resolves_workspace_from_active_not_path`. (fe) smoke: button disabled while `discovering`; success path calls `listPendingActions`. Preview e2e: click "Discover my workspace" on empty state → spinner → list reload.

---

### F2 — Unstructured CATEGORIZATION via on-box model (gap #3)
**Repo:** kb-go (optional, see below) + pocketpaw (digester). **Worktree:** `szd2-int`. **Depends on:** F3's `_refine.py` client helper for the model call (build F3's helper first, or co-build — see Order). Mergeable independently of F1.

**Root cause (confirmed):** `kb convo ingest` hardcodes `Categories: []string{"conversation"}` (`kb-go/convo.go:625`), so `_partition_by_category` (`kb_compile.py:192`) sees one bucket → objects-only draft. The deterministic path *cannot* infer `SupportTicket` vs `RefundRequest` — that's a semantic judgment.

**Fix — route unstructured exhaust through kb agent-mode `prepare → on-box-model → accept`** in `KbCompileDigester._compile_blobs` (`kb_compile.py:284`), keeping `kb convo ingest` as the **no-model fallback** (graceful degrade to today's behavior):
1. Write blobs to `<tmp>/<label>.txt` (already done).
2. `kb prepare <tmpdir> --pattern "*.txt" --scope "workspace:<wid>:discovery" --json` → `{items:[{source,hash,raw_id,prompt}]}`. Keyless, no network (`kb.go:1956`).
3. For each `item.prompt`: send to the **on-box model** (F3's `resolve_llm_client(settings, force_provider="ollama")` → `create_openai_client()` → `chat.completions.create(response_format={"type":"json_object"})`). Parse → article JSON carrying `"categories":["broad","topics"]` (the prepare prompt asks for this, `kb.go:1342`).
4. `echo '<json>' | kb accept --scope … --json` → writes `WikiArticle` with model-supplied `Categories` (`kb.go:2178`). Keyless.
5. Unchanged read-back: `kb list/show --scope … --json` (`kb_compile.py:315-352`) — now yields real categories → `_partition_by_category` produces keyed/typed object types.

**Sovereignty tripwire (extend existing):** the digester already forbids `kb ingest`/`kb build` (`kb_compile.py:25-26,76-77` — those POST raw text to Anthropic, `kb.go:1348`). The new model call **must** resolve through `force_provider="ollama"` only. kb-go itself needs **no change** for this slice (prepare/accept already exist); the `convo.go:625` hardcode stays as the fallback path's behavior.

**Contract:** when an on-box model is configured, unstructured exhaust yields domain-categorized object types; when not, falls back to today's `conversation`-bucket behavior with no error.
**Tests:** `test_compile_uses_prepare_accept_when_model_configured` (stub Ollama client returns 2 categories → assert 2 typed buckets from `_partition_by_category`); `test_compile_falls_back_to_convo_ingest_without_model`; `test_compile_never_calls_kb_ingest_or_build` (the tripwire — assert subprocess never invoked with `ingest`/`build`); `test_prepare_accept_use_discovery_scope`. Run kb commands against a real `~/.knowledge-base` in CI temp HOME.

---

### F3 — On-box REFINE pass (gap #5, un-stub `opts.refine`)
**Repo:** pocketpaw. **Worktree:** `szd2-int`. **Depends on:** nothing structural; **co-requisite with F2** (both need the `_refine.py` Ollama client helper — build the helper here, F2 imports it).

`run.py:218-227` currently raises `NotImplementedError` on `opts.refine`. Wire it:
1. **New `ee/pocketpaw_ee/discovery/_refine.py`** — `resolve_llm_client(settings, force_provider="ollama")` (`client.py:152`) + `create_openai_client(timeout=120.0)` (`client.py:98`). **Hard-pin local** — never read `settings.llm_provider` directly (an `auto` resolve with a cloud key would pick Anthropic and leak raw tenant text). `force_provider="ollama"` is the sovereignty enforcement point.
2. **Refine the deterministic draft, not raw text.** Run `digest()` first; send the model the *draft* (type names, property names, sample summaries from already-compiled articles) and ask it to clean/merge/rename types + drop spurious links. Minimizes raw-text exposure; deterministic draft is the floor.
3. **Fail closed on sovereignty, soft on availability.** If Ollama can't connect (`ollama serve` down, formatted at `providers/ollama.py:91`), **return the deterministic draft** with `draft.meta["refine"]="unavailable"` — never raise, never fall back to cloud. Mirror the templated-fallback shape of `decisions/explain/extractor.py:193-213`, but the fallback is "the deterministic draft."
4. Replace the `NotImplementedError` block (`run.py:218-222`) with a call into `_refine.refine_draft(draft, settings)`.

**Contract:** `opts.refine=True` returns a cleaned draft when Ollama is up; returns the un-refined deterministic draft (never an error, never a cloud call) when down.
**Tests (sovereignty as mechanical assertion):** `test_refine_resolves_ollama_only` (assert `llm.api_key is None`, `llm.is_ollama is True`); `test_refine_with_cloud_key_set_still_routes_ollama` (set a fake cloud key → assert `force_provider="ollama"` still used, no Anthropic client built); `test_refine_unavailable_returns_deterministic_draft` (Ollama down → draft returned, `meta["refine"]=="unavailable"`, no raise); `test_refine_never_posts_raw_text_to_cloud`.

---

### F4 — EDIT-in-review (blob-mutate, gap #4)
**Repo:** pocketpaw (backend) + paw-enterprise (frontend). **Worktree:** `szd2-int` + `szd2-ui`. **Depends on:** F1 (need proposals on screen to edit). Independent of F2/F3/F6.

**Backend — new `PATCH /instinct/actions/{action_id}/proposal`** in `ee/pocketpaw_ee/instinct/router.py` (mutate editable blob sub-fields of a **PENDING** action; does **not** flip status — Approve stays a separate click). Handler:
1. **Load + status guard.** `before = await store.get_action(action_id)` (`store.py:724`); 404 if missing; **409** if `before.status != PENDING`.
2. **Tenancy gate first (security chokepoint).** Run the same three asserts approve uses, against the *current* blob, before any mutation: `_assert_fabric_objects_workspace` / `_assert_pocket_create_workspace` / `_assert_instinct_rule_workspace` (`router.py:568,611,654`).
3. **Resolve the one editable sub-field** via `_*_blob(before)` (`router.py:550,592,635`); 422 if payload kind ≠ blob kind.
4. **Immutability pin (core new logic).** Copy `before`'s blob, overwrite **only** the editable sub-key, force immutable fields back from `before`:
   - `_instinct_rule`: pin top-level `workspace_id`/`user_id`/`schema`/`kind`/`correlation_id`/`proposed_event_id`/`summary`. **Also pin `rule_spec.scope.workspace_id`** — the *second* tenancy copy the executor scopes by (`instinct_rule_proposals/executor.py`). Mismatch → 403.
   - `_pocket_create`: pin top-level `workspace_id`/`user_id`/`schema`; run incoming `pocket_spec` through `_normalize_pocket_spec` (`pocket_proposals/propose.py:197`) to strip sneaked `workspace`/`owner`.
   - `_fabric_objects`: pin top-level `workspace_id`/`schema`; replace only `object_types`/`objects`/`links`.
5. **Re-validate per blob (422, never persist broken):** `_instinct_rule` → `RuleDraft.model_validate(rule_spec)` (`rule_models.py:49` — parses CEL `when`); `_pocket_create` → `CreatePocketRequest.model_validate(pocket_spec)`; `_fabric_objects` → shape-check each item (`fabric_proposals/propose.py:32-39`).
6. **Persist** via thin `store.update_parameters(action_id, params)` (or reuse `_persist_edits` with `edited={"parameters"}`).
7. **Learning loop:** `compute_patches(before, after)` (`correction.py:52`) → build `Correction` → `await store.record_correction(...)` (`store.py:1189`) → `_emit_human_corrected(blob=new_blob, action=after, disposition="edited", …)` (`chain_emitters.py:141`). Lands `agent.proposed → human.corrected(edited)` *without* `decision.completed` (chain stays open for the eventual approve). Return `{action, correction}`.

**Frontend** — `runtime/api.ts`: add `editProposal(actionId, edits)` → `runtime<ApproveResponse>('instinct/actions/${id}/proposal', {method:'PATCH', …})` (reuse `ApproveResponse`, `api.ts:194`). `DiscoveryReviewCard.svelte`: add `onEdit?` prop + `editing = $state(false)`; make highest-value fields inline-editable behind a pencil — rule `when` (`:220`, `<code>`→`<input>`), rule `name` (`:209`), `action` (badge→`<select>` over require_approval/notify/block), fabric type-name chips (`:146`), drop-link `X` on `.drc-link-row` (`:178`), pocket name (`:196`). Keep a11y intact (real `<input>`/`<select>` + labels, no `svelte-ignore`). On Save, assemble only the touched sub-shape, call `onEdit`, replace the action with the server-returned fresh one (re-renders via `$derived` props). 422 → inline field error; 403 → non-recoverable toast. **Save does not approve.**

**Contract:** edit mutates blob, re-validates, captures `edited` correction, never flips status, never lets a client overwrite either tenancy copy.
**Tests:** `test_patch_pins_workspace_id_top_level` (client sends foreign `workspace_id` → pinned back, 403/ignored); `test_patch_pins_rule_scope_workspace_id` (the **second** tenancy copy — the security crux); `test_patch_rejects_non_pending_409`; `test_patch_bad_cel_returns_422_not_persisted`; `test_patch_emits_human_corrected_edited_without_completed`; `test_patch_kind_mismatch_422`. (fe) preview e2e: edit a rule `when`, Save, card re-renders with validated value, Approve still required.

---

### F5 — Real-connector live E2E (gap #6) — see §5 for the full spec.
**Repo:** pocketpaw. **Worktree:** `szd2-int`. **Depends on:** F1+F2+F3 merged (it drives the trigger end-to-end). Build last.

---

## 2 · F6 ENFORCEMENT DESIGN (for captain review)

> **This is the high-stakes slice. Read before it ships.** It wires *approved workspace-discovered* Instinct rules into the **live action gate**. Default OFF, fully backward-compatible, fail-safe on every CEL error. Repo: **pocketpaw**, worktree `szd2-int`. Depends on F1 (needs discovered rules to exist), but the enforcement code itself is independent — it can be the **last** thing merged so the captain reviews it in isolation.

### Core principle — merge discovered rules as extra `InstinctRule`s *inside* the composer call, flag-gated, fail-open per rule
The discovered `Rule.action` and template `InstinctRule.action` share the **identical** `Literal["require_approval","notify","block"]` vocabulary, and both `when` are CEL strings. `RuleResponse.when/action` (`rules/dto.py:64-65`) is byte-compatible with `InstinctRule`. So the safest merge converts each active discovered rule into an `InstinctRule` and rides the **exact same** 5-step evaluation, precedence, audit, and triage machinery that already ships. **No new verdict path, no new outcome mapping, no triage change.**

### WHERE — `gate_action` in `instinct_dispatch.py`, before `resolve_instinct` (`:342`)
This is the single impure entry point for *every* dispatch flow (executor gate 1.5, temporal dispatcher, bulk fan-out), it already has `workspace_id` in scope, and it's the designated impure layer. The composer (`instinct_composer.py`) is import-linter-pure (`:58`) and **must stay that way** — `get_active_rules` is a Beanie read, so the fetch cannot live there. **Do not** wire this in `action_executor`.

```python
# instinct_dispatch.py, inside gate_action, just before resolve_instinct (~:341)
effective_template = template
if get_settings().instinct_enforce_discovered_rules:        # NEW flag, default False
    discovered = await _load_discovered_instinct_rules(
        workspace_id, pocket_id, action_name, row_context, workspace_context,
    )
    if discovered:
        effective_template = _merge_discovered_rules(template, discovered)
decision = resolve_instinct(effective_template, action_name, row_context, ...)   # unchanged call
```

`_merge_discovered_rules` returns `template.model_copy(deep=False)` with a **copied** `InstinctRulesDef` whose `rules = list(discovered) + list(template.instinct_rules.rules)`. Discovered rules go **first** so a discovered `block` wins step-1's first-match short-circuit; for `require_approval`/`notify` order is immaterial. **The template object is never mutated** — 100% backward-compat for any other reader.

### HOW the vocabulary maps to gate outcomes (for free — identical literals)
| discovered `action` | composer step | verdict | live gate outcome |
|---|---|---|---|
| `block` | step 1 (`:256`) | `BLOCK` | `instinct_blocked` — action aborted (`action_executor.py:904`) |
| `require_approval` | step 2 (`:274`) | `ESCALATE_APPROVAL` | routes to `_route_escalation` → 4-lane triage → human-pending (or AUTO/OPTIMISTIC/DRY_RUN if workspace opted into TRIAGE) |
| `notify` | step 5 (`:287`) | unchanged + `notify_rules` | rides existing notify side-effect path |

A discovered `require_approval` produces the same `ESCALATE_APPROVAL` an operator-overlay rule does → flows through the unchanged 4-lane triage. A discovered `block` produces `BLOCK`, which triage rule 1 (`instinct_triage.py:151`) already treats as a non-overridable floor. **Zero new outcome wiring.**

### Scoping — bound the rule set to this action
`get_active_rules(workspace_id)` (`rules/service.py:119`) returns all active workspace rules. In `_load_discovered_instinct_rules`, **filter before conversion**: keep a rule only if `scope.pocket_id` is null (workspace-wide) **or** matches the current `pocket_id`. Keeps another pocket's rule from firing here. (`object_type` scoping is a later refinement.)

### THE FEATURE FLAG (off by default — backward-compat proof)
Add to `Settings` (`config.py`, alongside the instinct block at `:1343-1377`):
```python
instinct_enforce_discovered_rules: bool = Field(
    default=False,
    description="When true, approved workspace-discovered Instinct rules "
        "(rules.service.get_active_rules) are merged with template rules at the "
        "live gate. Off by default — template-rule path unchanged. "
        "Env: POCKETPAW_INSTINCT_ENFORCE_DISCOVERED_RULES.",
)
```
**Proof:** when `False`, `gate_action` never calls `get_active_rules`, `effective_template is template`, and `resolve_instinct` gets byte-identical args — the entire discovered branch is dead code on the default path. This is a **separate, narrower** flag than `instinct_approval_level`: enforcing *which CEL conditions fire* and activating *whether escalations can auto-resolve* are independent risk axes and must toggle independently. A workspace can enforce discovered rules while the triager stays dormant (every discovered `require_approval` still goes to a human).

### FAIL-SAFE — never block silently (the security-critical part)
Two failure points, two safe behaviors:

1. **`get_active_rules` read fails** (DB hiccup, bad workspace id): wrap in `try/except`, log WARNING, treat as **empty set** → fall through to the pure template path. Fail-**open** on the discovered layer — a store outage can never block/escalate an action the template alone would allow. Mirrors `resolve_workspace_approval_level`'s read-failure stance (`service.py:1296`).

2. **A discovered rule's CEL errors** — the dangerous one. Today `_eval_rule` (`instinct_composer.py:387`) re-raises eval failure as `InstinctResolutionError`, which `gate_action` maps to `NotFound` → the action **404s** (`:350-357`). For *template* rules that loud-fail is intentional (author bug). For *discovered* rules it's a silent denial-of-service — exactly the "must fail-safe, never block silently" hazard.
   **Fix — guarded probe in `_load_discovered_instinct_rules`, no composer change:**
   - **Parse failure** at `InstinctRule.model_validate({"when":…, "action":…})`: drop that one rule, log WARNING, keep the rest.
   - **Eval failure:** before merging, run each converted rule through a guarded `evaluate_cel(rule.when, merged_context, resolver, now)` probe; on `CelEvaluationError`, **drop that rule only** (log WARNING with workspace/rule id), merge only the clean ones. The composer's own eval is then a safe re-run.
   - Net: the discovered layer is **fail-open per rule** — a broken discovered rule is inert, never a block, never a 404. Asymmetry with template rules (which keep loud-fail) is correct: template rules are authored/version-controlled; discovered rules are inferred, lower-trust. A discovered rule can only ever *add* a block/escalate, never relax the template floor.

### One edge flagged for the captain
The guarded probe evaluates each surviving discovered rule **twice** (probe + composer). Negligible for a handful of pure-CEL rules (no I/O). If counts grow, the fast-follow is a `strict: bool` param on `resolve_instinct`/`_eval_rule` that swallows `CelEvaluationError → False` for the discovered subset — but that touches the import-pure composer signature and needs its own test pass. **Recommendation: ship the guarded-probe first (composer untouched = stronger backward-compat); `strict` is a profiling-gated fast-follow.**

### Files (all in `szd2-int`)
`ee/pocketpaw_ee/cloud/pockets/instinct_dispatch.py` (inject in `gate_action:282`, before `resolve_instinct:342`; verdict branch `:359-389`; `NotFound` map `:350-357`); `src/pocketpaw/bundled_templates/instinct_composer.py` (`resolve_instinct:181`, `_eval_rule:367`, `evaluate_cel:387`, purity `:58`); `src/pocketpaw/bundled_templates/schema.py` (`InstinctRule`/`InstinctRulesDef`); `ee/pocketpaw_ee/cloud/rules/service.py` (`get_active_rules:119`); `ee/pocketpaw_ee/cloud/rules/dto.py` (`RuleResponse.when/action:64-65`, `scope:67`); `ee/pocketpaw_ee/cloud/pockets/instinct_triage.py` (BLOCK floor `:151` — read-only); `src/pocketpaw/config.py` (new flag near `:1350`).

### Test plan (proves change-for-matching, inertness, fail-safe)
All in `tests/cloud/` (`uv sync --dev --group ee`), targeting `gate_action` with stubbed `get_active_rules` + real `resolve_instinct`:
- **Backward-compat (iron law):** `test_flag_off_does_not_call_get_active_rules` (patch it to raise; default flag → never called, byte-identical verdict).
- **Behavior change:** `test_discovered_block_rule_blocks_matching_action`; `test_discovered_require_approval_escalates`; `test_discovered_notify_rule_rides_proceed`.
- **Inertness:** `test_discovered_rule_inert_when_when_false`; `test_discovered_rule_scoped_to_other_pocket_is_inert`.
- **Precedence:** `test_discovered_block_beats_template_execute`.
- **Fail-safe (security-critical):** `test_get_active_rules_read_failure_falls_through_to_template` (raise → proceed, WARNING, no 404); `test_discovered_rule_cel_eval_error_is_dropped_not_blocking` (missing identifier → dropped, NOT blocked, NOT 404 — the "never block silently" proof); `test_discovered_rule_parse_failure_dropped_others_survive`.
- **Triage compose:** `test_discovered_escalate_flows_through_triage_lanes` (under `approval_level=TRIAGE` + trusted pocket+compensate → reaches AUTO/OPTIMISTIC exactly like a template escalation).

---

## 3 · BUILD ORDER (supervised sequential dispatch)

**pocketpaw implementers run sequentially in ONE worktree** (`isolation:"worktree"` on the main checkout does *not* isolate nested-repo cwd — concurrent pocketpaw committers collide on branch state). **Frontend runs in its own worktree** (`szd2-ui`), can parallel the backend.

```
ORDER  SLICE                 REPO          WORKTREE        BRANCH (stack on prior)
─────  ────────────────────  ────────────  ──────────────  ───────────────────────────────
  1    F1-be trigger         pocketpaw     szd2-int        feat/szd-f1-discovery-trigger
  2    F3 refine helper      pocketpaw     szd2-int        feat/szd-f3-refine  ← off F1 tip
  3    F2 categorization     pocketpaw     szd2-int        feat/szd-f2-categorize ← off F3 tip
  4    F4-be edit-PATCH      pocketpaw     szd2-int        feat/szd-f4-edit ← off F2 tip
  5    F6 enforcement        pocketpaw     szd2-int        feat/szd-f6-enforce ← off F4 tip ★captain-review
  6    F5 real-conn E2E      pocketpaw     szd2-int        feat/szd-f5-e2e ← off F6 tip
─────  ────────────────────  ────────────  ──────────────  ───────────────────────────────
  ‖    F1-fe button          paw-enterprise szd2-ui        feat/szd-f1-discovery-button   (parallel)
  ‖    F4-fe edit card       paw-enterprise szd2-ui        feat/szd-f4-edit-card ← off F1-fe tip
```

**Stacking rationale:** F2 and F3 both touch the discovery package (`_refine.py` ↔ `kb_compile.py`) and F4/F6 touch `instinct/` and `pockets/` — stack each on the prior tip so additive EOF insertions don't cosmetically conflict. Build F3's `_refine.py` helper **before** F2 (F2 imports it). **F6 is its own commit/PR on top of F4** so the captain reviews enforcement in isolation. **F5 last** (it exercises the full merged stack).

**Worktrees:** reuse the existing `szd2-int` (pocketpaw) and `szd2-ui` (paw-enterprise) — both already on the slice-2 discovery branches with the discovery plumbing in place. **No fresh worktrees needed.** Each PR targets `dev`. **Stop at the captain-review gate — never merge.** Stacked-PR base merges use `--rebase`, not `--squash`.

---

## 4 · RISKS (specific to this finish)

| # | Risk | Mitigation |
|---|---|---|
| R1 | **F6 enforcement breaks the live gate** — a discovered rule blocks/404s a legit action for *all* workspaces. | Default-OFF flag (dead code on default path); per-rule guarded-CEL probe drops broken rules (fail-open); store-read failure falls through to template; `test_flag_off_does_not_call_get_active_rules` proves byte-identical default behavior. **Captain reviews F6 in isolation before merge.** |
| R2 | **Sovereignty leak on the on-box model** (F2/F3) — an `auto` LLM resolve with a tenant cloud key POSTs raw tenant text to Anthropic. | Hard-pin `force_provider="ollama"` everywhere (never read `settings.llm_provider`); extend the `kb_compile` tripwire to forbid `kb ingest`/`kb build`; mechanical tests assert `llm.api_key is None`, `is_ollama`, and that a cloud key still routes to Ollama. Refine fails *closed* on sovereignty (returns deterministic draft, never cloud). |
| R3 | **F4 edit re-validation bypasses tenancy** — client overwrites `_instinct_rule.workspace_id` *or* the second copy `rule_spec.scope.workspace_id` (executor scopes by the latter). | Pin **both** tenancy copies from `before` (mismatch → 403); run tenancy asserts *before* merge; `_normalize_pocket_spec` strips sneaked keys; dedicated tests for each copy. |
| R4 | **Categorization quality** — the on-box model emits junk/over-broad categories → noisy object types. | Deterministic `convo ingest` stays the floor/fallback (no-model degrades to today's behavior, never worse); refine operates on the *draft* not raw text; edit-in-review (F4) lets a human rename/merge bad types before approve. Real-connector E2E (F5) asserts category sanity on a known fixture. |
| R5 | **Async trigger (F1) loses the run** — `asyncio.create_task` is fire-and-forget; a crash mid-digest yields no proposals and no error surfaced. | v1 returns `run_id` for optimistic confirm + the orchestrator stages proposals durably as Instinct Actions; log task exceptions. Documented upgrade path to ARQ enqueue (`jobs/service.py:106-114`) if digest latency/durability becomes an issue. |
| R6 | **kb-go `convo.go:625` fallback still tags `conversation`** — if model path silently disabled, drafts regress to objects-only without a signal. | Log which path was taken (`prepare/accept` vs `convo ingest`) in `_compile_blobs`; F5 asserts the model path produced ≥2 domain categories on the fixture, catching a silent fallback. |

---

## 5 · F5 — Real-connector live E2E: what it MUST exercise and assert (gap #6)

**Repo:** pocketpaw, worktree `szd2-int`, branch stacked on F6. Drive a **real** connector (not a stub) end-to-end through the finished stack.

**Must exercise (full path):**
1. Bind a real connector to a workspace (e.g. a fixture Gmail/GitHub/CSV connector with seeded, non-sensitive sample data) → enable it.
2. `POST /cloud/discovery/run` (F1) → `202 {run_id}`. Assert only enabled connectors enumerated.
3. Discovery samples the connector, runs the **unstructured** digest through `kb prepare → on-box model → kb accept` (F2) and the **refine** pass (F3) on a live Ollama. Assert ≥2 **domain** categories materialize (not just `conversation`) and typed object types appear — proving F2's model path fired, not the fallback.
4. Proposals stage as pending Instinct Actions; assert `fabric_objects_action_id`/`pocket_action_id`/`instinct_action_ids` populated and visible in `listPendingActions`.
5. **Edit** one proposal via `PATCH …/proposal` (F4) — tighten a rule `when` or rename a type; assert re-validation passes, an `edited` correction is recorded, status stays PENDING.
6. **Approve** the edited proposal → executor materializes it (rule active / pocket created / fabric ingested).
7. With `instinct_enforce_discovered_rules=true` (F6), run a governed action that the approved discovered rule targets → assert the verdict (`block`/`require_approval`) fires through the live gate and outcome (`instinct_blocked`/`instinct_pending`) is correct; flip the flag off → assert the same action proceeds (proves the flag gates real behavior).

**Must assert — sovereignty + no-PII-leak (non-negotiable):**
- **No-PII-leak / sovereignty:** capture all outbound HTTP during the run; assert **zero** requests to `api.anthropic.com` or any cloud LLM host — every model call hit the local Ollama endpoint only. Assert `kb ingest`/`kb build` subprocesses were **never** invoked (the tripwire). Assert no tenant raw text appears in any cloud-bound payload (there should be none). Assert the on-box client resolved with `api_key is None`.
- **Tenancy:** the F4 edit cannot cross workspaces — a PATCH with a foreign workspace id is pinned/403'd; the approved rule scopes to the correct workspace only.
- **Graceful degrade:** repeat the run with Ollama stopped → assert discovery still produces the deterministic draft (objects-only), refine returns `meta["refine"]=="unavailable"`, and **no** cloud fallback occurred — the sovereign floor holds even with the model down.
- **Idempotence/supersede:** a second `run` supersedes the prior proposals (assert `superseded_action_ids`).

---

**Plan files for implementers (verbatim handoff):** all backend paths under `…/pocketPaw/.claude/worktrees/szd2-int/`; all frontend paths under `…/paw-enterprise/.claude/worktrees/szd2-ui/`. F6 (§2) is the captain-review artifact — ship it as its own PR on top of F4. Never merge; stop at the captain-review gate.
# scripts/test_svelte_create.py
#
# Created: 2026-06-05 (feat/sites-svelte-engine) — runnable PASS/FAIL verifier for the
# Paw Sites "svelte create must use create_svelte_site, not ripple" fix, so the fix can
# be checked WITHOUT manually creating a site in the UI.
#
# Updated: 2026-06-05 (feat/sites-svelte-engine) — rewired to the THREADED
# ``deny_mcp_tool_ids`` path. The prompt-SNIFFING gate it used to validate
# (``_is_svelte_sites_create_prompt`` + ``_RIPPLE_CREATE_TOOL_IDS`` +
# ``[SVELTE-GATE-DBG]`` print in ``claude_sdk.run``) was DELETED. The tool-deny is
# now a typed ``SurfaceProfile.deny_mcp_tool_ids`` resolved per-meta by
# ``resolve_profile`` and threaded into ``ClaudeSDKBackend.run(...,
# deny_mcp_tool_ids=...)``, which subtracts it from ``allowed_tools`` before the
# SDK launches. This verifier now exercises THAT path.
#
# THE FIX UNDER TEST: ``resolve_profile(SurfaceKind.SITES, meta)`` returns a
# ``deny_mcp_tool_ids`` of {create_landing_site, pocket_specialist__create} ONLY
# for the svelte-create mode (engine="svelte", no pocket_id). The chat loop
# (run_core) passes that set into ``AgentPool.run`` → ``ClaudeSDKBackend.run``,
# which drops those ids from the allowlist — so the LLM is physically unable to
# fall back to building a rippleSpec landing page. The ripple-create and refine
# modes resolve to an EMPTY deny set, so their ripple tools survive.
#
# Two parts, ONE command (``uv run --project . python scripts/test_svelte_create.py``):
#   PART A — UNIT (fast, deterministic, no LLM): resolve the SurfaceProfile for each
#     /sites mode via ``resolve_profile``; assert the svelte-create mode's
#     ``deny_mcp_tool_ids`` is exactly the two ripple-create ids; assert subtracting
#     it from a full toolset drops those two but keeps create_svelte_site + publish +
#     edit; assert the REFINE and ripple-create modes resolve to an EMPTY deny set
#     (their tools survive).
#   PART B — E2E (the decisive test, real LLM turn ~30-60s, costs tokens): drive a REAL
#     svelte /sites create through the agent IN-PROCESS (no HTTP, no auth — replicates the
#     agent-invocation the licensed POST /cloud/chat/{scope}/{id}/agent endpoint performs:
#     resolve_surface_context(engine=svelte) -> build_knowledge_context (preamble) ->
#     resolve_profile(...).deny_mcp_tool_ids -> ClaudeSDKBackend.run(deny_mcp_tool_ids=...)).
#     Captures the ``allowed_tools`` actually handed to the SDK, the agent's tool_use
#     events, and the persisted pocket (ground truth from Mongo). Asserts:
#       (1) ripple create tools excluded from allowed_tools; create_svelte_site kept;
#       (2) agent called create_svelte_site, NOT create_landing_site /
#           pocket_specialist__create / pocket__add_widget;
#       (3) resulting pocket has engine="svelte" + a source map + rippleSpec is None;
#       (4) zero ``ripple_spec.unknown_widget_type`` warnings.
#     NOTE: pocket__add_widget is NOT in the deny set; if the agent uses add_widget to
#     build ripple, the script FLAGS it (the deny set may need to include it).
#
# Run: uv run --project . python scripts/test_svelte_create.py                # unit + e2e
#      uv run --project . python scripts/test_svelte_create.py --unit-only    # unit only
#      uv run --project . python scripts/test_svelte_create.py --e2e-only     # e2e only
# Env for e2e (the script sets sane defaults if unset): real Mongo on :27017,
#      PAW_SITES_GEN_CMD, PAW_SITES_LOCAL=1, PAW_SITES_LOCAL_DIR.
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import traceback
import uuid
from contextlib import redirect_stdout
from pathlib import Path

# ── repo bootstrap ──────────────────────────────────────────────────────────
# Allow ``python scripts/test_svelte_create.py`` from anywhere; ee/ is a sibling
# package the cloud agent flow lives in.
_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "src", _REPO / "ee"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ── tiny assertion harness (so this is runnable standalone, not just via pytest) ──
class _Results:
    def __init__(self) -> None:
        self.checks: list[tuple[bool, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks.append((bool(ok), label))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {label}"
        if detail:
            line += f"  -> {detail}"
        print(line, flush=True)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(ok for ok, _ in self.checks)

    @property
    def n_pass(self) -> int:
        return sum(1 for ok, _ in self.checks if ok)


# ════════════════════════════════════════════════════════════════════════════
# PART A — UNIT (fast, deterministic, no LLM)
# ════════════════════════════════════════════════════════════════════════════
def run_unit(res: _Results) -> None:
    print("\n=== PART A — UNIT (SurfaceProfile deny set + tool subtraction) ===", flush=True)

    from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

    _CREATE_LANDING = "mcp__pocketpaw_sites_manager__create_landing_site"
    _SPECIALIST_CREATE = "mcp__pocketpaw_pocket_specialist__create"
    _CREATE_SVELTE = "mcp__pocketpaw_sites_manager__create_svelte_site"
    _PUBLISH = "mcp__pocketpaw_sites_manager__publish"
    _SPECIALIST_EDIT = "mcp__pocketpaw_pocket_specialist__edit"

    # The exact subtraction the backend runs (claude_sdk.run): drop any denied id
    # from the allowlist. ``deny_mcp_tool_ids`` is resolved per-meta upstream.
    def apply_deny(allowed: list[str], deny: frozenset[str]) -> list[str]:
        return [t for t in allowed if t not in deny]

    # The svelte-create mode (engine="svelte", no pocket_id) is the ONLY /sites
    # mode that denies tools.
    svelte_profile = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="svelte"))
    svelte_deny = svelte_profile.deny_mcp_tool_ids

    # 1. The svelte-create profile drops ripple and denies exactly the two ripple
    #    create ids, and surfaces the create-svelte-site skill.
    res.check(
        svelte_profile.ripple_mode == "off",
        "svelte-create SurfaceProfile turns ripple OFF",
        f"ripple_mode={svelte_profile.ripple_mode!r}",
    )
    res.check(
        svelte_deny == frozenset({_CREATE_LANDING, _SPECIALIST_CREATE}),
        "svelte-create deny_mcp_tool_ids is exactly the two ripple create ids",
        f"deny={sorted(t.split('__')[-1] for t in svelte_deny)}",
    )
    res.check(
        "create-svelte-site" in svelte_profile.skill_names,
        "svelte-create profile surfaces the create-svelte-site skill",
    )

    # 2. Subtraction: drops both ripple create tools, keeps svelte create + publish + edit.
    full_toolset = [
        "Agent",
        "Bash",
        "Skill",
        _CREATE_LANDING,
        _CREATE_SVELTE,
        _PUBLISH,
        _SPECIALIST_CREATE,
        _SPECIALIST_EDIT,
    ]
    gated = apply_deny(full_toolset, svelte_deny)
    res.check(
        _CREATE_LANDING not in gated and _SPECIALIST_CREATE not in gated,
        "deny REMOVES create_landing_site + pocket_specialist__create",
        f"gated={[t.split('__')[-1] for t in gated if t.startswith('mcp__')]}",
    )
    res.check(
        _CREATE_SVELTE in gated and _PUBLISH in gated,
        "deny KEEPS create_svelte_site + publish",
    )
    res.check(_SPECIALIST_EDIT in gated, "deny KEEPS pocket_specialist__edit (refine path)")

    # 3. Negative: REFINE mode (pocket_id set, ANY engine) resolves to an EMPTY deny set.
    for refine_meta in (
        SurfaceMeta(route_path="/sites", pocket_id="pk-1"),
        SurfaceMeta(route_path="/sites", pocket_id="pk-1", engine="svelte"),  # refine wins
    ):
        refine_profile = resolve_profile(SurfaceKind.SITES, refine_meta)
        res.check(
            refine_profile.ripple_mode == "on" and refine_profile.deny_mcp_tool_ids == frozenset(),
            f"REFINE meta {refine_meta.engine or 'no-engine'} keeps ripple + denies nothing",
            f"ripple_mode={refine_profile.ripple_mode!r}",
        )
        refine_gated = apply_deny(full_toolset, refine_profile.deny_mcp_tool_ids)
        res.check(
            _CREATE_LANDING in refine_gated and _SPECIALIST_CREATE in refine_gated,
            f"REFINE meta {refine_meta.engine or 'no-engine'} is NOT gated (ripple tools survive)",
        )

    # 4. Negative: ripple-create mode (engine None/"ripple", no pocket_id) → EMPTY deny set.
    for ripple_meta in (SurfaceMeta(), SurfaceMeta(engine="ripple")):
        ripple_profile = resolve_profile(SurfaceKind.SITES, ripple_meta)
        res.check(
            ripple_profile.ripple_mode == "on" and ripple_profile.deny_mcp_tool_ids == frozenset(),
            f"ripple-create meta {ripple_meta.engine or 'no-engine'} keeps ripple + denies nothing",
            f"ripple_mode={ripple_profile.ripple_mode!r}",
        )

    # 5. Negative: a non-sites surface resolves to an EMPTY deny set (no gating).
    non_sites_profile = resolve_profile(SurfaceKind.POCKETS_LIST, SurfaceMeta())
    res.check(
        non_sites_profile.deny_mcp_tool_ids == frozenset(),
        "non-sites surface denies nothing (no tool gating)",
    )


# ════════════════════════════════════════════════════════════════════════════
# PART B — E2E (real LLM turn, in-process, no HTTP/auth)
# ════════════════════════════════════════════════════════════════════════════
_ADD_WIDGET = "mcp__pocketpaw_pocket__add_widget"
_CREATE_LANDING = "mcp__pocketpaw_sites_manager__create_landing_site"
_SPECIALIST_CREATE = "mcp__pocketpaw_pocket_specialist__create"
_CREATE_SVELTE = "mcp__pocketpaw_sites_manager__create_svelte_site"


def _set_e2e_env_defaults() -> None:
    """Set the env the svelte generator needs, only if the caller hasn't."""
    os.environ.setdefault(
        "PAW_SITES_GEN_CMD",
        "node /Users/prakash-1/Documents/paw-trees/paw-sites-svelte/dist/cli.js",
    )
    os.environ.setdefault("PAW_SITES_LOCAL", "1")
    os.environ.setdefault("PAW_SITES_LOCAL_DIR", "/tmp/paw-sites-local")
    # Real Mongo on :27017 (the workspace mongod) unless overridden.
    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")


async def _init_real_mongo(db_name: str):
    """Init Beanie against the REAL mongod on :27017 with the cloud document set.

    Uses a throwaway DB name so the e2e never touches a live tenant's data and the
    persisted pocket can be asserted on as ground truth, then dropped at teardown.
    """
    # Use the SAME client the production ``init_cloud_db`` uses: pymongo's native
    # async client. Beanie 2.x dropped Motor for ``pymongo.AsyncMongoClient`` — a
    # ``motor.AsyncIOMotorClient`` here fails on ``client.append_metadata`` inside
    # ``init_beanie``. ``serverSelectionTimeoutMS`` keeps the ping/init from hanging
    # when mongod is down.
    from beanie import init_beanie
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS
    from pymongo import AsyncMongoClient

    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    client = AsyncMongoClient(mongo_url, serverSelectionTimeoutMS=4000)
    # Fail loud and early if mongod isn't actually reachable.
    await client.admin.command("ping")
    db = client[db_name]
    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    return client, db


class _RecordingBus:
    """Stand-in EventBus so agent_create's emit(PocketCreated) doesn't raise
    (the real bus is only wired by init_realtime() at boot — not in this harness)."""

    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:  # noqa: ANN001
        self.events.append(event)

    def subscribe(self, event_type, handler) -> None:  # noqa: ANN001, ARG002
        return


async def run_e2e(res: _Results) -> None:
    print("\n=== PART B — E2E (real svelte /sites create through the agent) ===", flush=True)
    _set_e2e_env_defaults()
    print(
        "  env: PAW_SITES_GEN_CMD={!r} PAW_SITES_LOCAL={} "
        "PAW_SITES_LOCAL_DIR={} MONGO_URL={}".format(
            os.environ.get("PAW_SITES_GEN_CMD"),
            os.environ.get("PAW_SITES_LOCAL"),
            os.environ.get("PAW_SITES_LOCAL_DIR"),
            os.environ.get("MONGO_URL"),
        ),
        flush=True,
    )

    # Strip nesting-detection env vars so the Claude CLI subprocess starts cleanly
    # (this harness may itself be launched from inside a Claude Code session).
    for _k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        os.environ.pop(_k, None)

    from bson import ObjectId
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        attach_agent_identity,
        build_behavior_instructions,
        build_knowledge_context,
        detach_agent_identity,
    )
    from pocketpaw_ee.cloud.models.pocket import Pocket as PocketDoc
    from pocketpaw_ee.cloud.surface import resolve_profile, resolve_surface_context

    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.config import Settings

    # 1. Real Mongo (throwaway DB) ------------------------------------------------
    db_name = f"e2e_svelte_create_{uuid.uuid4().hex[:8]}"
    try:
        client, _db = await _init_real_mongo(db_name)
    except Exception as e:  # mongod down / unreachable
        res.check(False, "init Beanie against real Mongo :27017", f"BLOCKER: {e}")
        return
    res.check(True, "init Beanie against real Mongo :27017", f"db={db_name}")

    # Install a recording bus so PocketCreated emit doesn't blow up the create handler.
    prev_bus = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _RecordingBus()  # type: ignore[attr-defined]

    workspace_id = str(ObjectId())
    user_id = str(ObjectId())

    try:
        # 2. REAL surface context (engine=svelte) — exactly what post_agent_chat builds.
        #    body shape mirrors {"surface": body.surface, "meta": body.surface_meta}.
        surface_ctx = await resolve_surface_context(
            workspace_id,
            user_id,
            {"surface": "sites", "meta": {"engine": "svelte", "route_path": "/sites"}},
        )
        has_markers = (
            'kind="sites"' in surface_ctx.preamble and 'engine="svelte"' in surface_ctx.preamble
        )
        res.check(
            has_markers,
            "resolve_surface_context(engine=svelte) produced the svelte preamble",
            f"kind={surface_ctx.kind.value}",
        )

        # 3. ScopeContext (sites create = a fresh SESSION scope, no pocket anchor).
        ctx = ScopeContext(
            kind=ScopeKind.SESSION,
            scope_id=str(ObjectId()),
            workspace_id=workspace_id,
            user_id=user_id,
            members=[user_id, "agent-e2e"],
            target_agent_id="agent-e2e",
            surface_context=surface_ctx,
        )

        user_message = "Build a landing page for a bakery called Crumb & Ember"

        # 4. Build knowledge_context + behavior_instructions EXACTLY as run_core does.
        knowledge_context = await build_knowledge_context(ctx, user_message=user_message)
        behavior_instructions = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
        res.check(
            'engine="svelte"' in knowledge_context,
            "knowledge_context carries the svelte marker (preamble is prepended)",
        )

        # 5. Replicate AgentPool.run's system-prompt assembly (pool.py L200-240):
        #    instructions appended first, then knowledge_context under the KB wrapper.
        system_prompt = ""
        if behavior_instructions:
            system_prompt = behavior_instructions
        if knowledge_context:
            system_prompt = (
                f"{system_prompt}\n\n"
                "## Your Knowledge Base\n"
                "Use the following information from your knowledge base to answer questions. "
                "Always reference this data when relevant instead of "
                "making things up or using tools to search.\n\n"
                f"{knowledge_context}"
            )

        # 6. Real SDK backend (agent mode — Claude Code CLI subprocess, no API key).
        settings = Settings()
        backend = ClaudeSDKBackend(settings)
        if not backend._sdk_available:
            res.check(False, "Claude Agent SDK available", "BLOCKER: SDK import failed")
            return
        # Bind per-stream identity so the in-process create_svelte_site MCP tool can
        # read workspace/user to stamp the persisted pocket (the production seam).
        identity_tokens = attach_agent_identity(workspace_id=workspace_id, user_id=user_id)

        # 7. Capture seam for the EXACT allowed_tools handed to the SDK.
        #    Wrap _ClaudeAgentOptions so we record the kwarg the gate just trimmed.
        captured_allowed: list[list[str]] = []
        _RealOptions = backend._ClaudeAgentOptions

        def _capturing_options(**kwargs):  # noqa: ANN003
            captured_allowed.append(list(kwargs.get("allowed_tools") or []))
            return _RealOptions(**kwargs)

        backend._ClaudeAgentOptions = _capturing_options  # type: ignore[assignment]

        # 8. Drive the real agent turn. Capture stdout so the backend's
        #    "Surface tool-deny" log line is recorded alongside our own logging.
        session_key = f"e2e:{ctx.scope_id}"
        tool_calls: list[str] = []
        assistant_text: list[str] = []
        stdout_buf = io.StringIO()

        # Resolve the per-surface deny set EXACTLY as run_core does, and thread it
        # into backend.run — this is the production seam that strips the ripple
        # create tools (the typed replacement for the deleted prompt-sniff gate).
        surface_deny = resolve_profile(surface_ctx.kind, surface_ctx.meta).deny_mcp_tool_ids
        res.check(
            surface_deny == frozenset({_CREATE_LANDING, _SPECIALIST_CREATE}),
            "resolve_profile(svelte create) deny set is the two ripple create ids",
            f"deny={sorted(t.split('__')[-1] for t in surface_deny)}",
        )

        print(
            f"  running real LLM turn (~30-60s, costs tokens): {user_message!r}",
            flush=True,
        )
        try:
            with redirect_stdout(stdout_buf):
                async for event in backend.run(
                    user_message,
                    system_prompt=system_prompt,
                    history=None,
                    session_key=session_key,
                    deny_mcp_tool_ids=surface_deny,
                ):
                    etype = getattr(event, "type", None)
                    meta = getattr(event, "metadata", None) or {}
                    if etype == "tool_use":
                        name = meta.get("name", "")
                        if name:
                            tool_calls.append(name)
                    elif etype == "message":
                        assistant_text.append(getattr(event, "content", "") or "")
                    elif etype == "done":
                        break
        finally:
            backend._ClaudeAgentOptions = _RealOptions  # type: ignore[assignment]
            detach_agent_identity(identity_tokens)
            # Best-effort: tear down the persistent CLI subprocess.
            try:
                await backend.cleanup()
            except Exception:
                pass

        captured_stdout = stdout_buf.getvalue()
        # Echo the backend's "Surface tool-deny: excluded ..." log line back to the
        # real console (we swallowed stdout). This is the deterministic evidence the
        # threaded deny set reached claude_sdk.run and trimmed the allowlist.
        deny_log_lines = [ln for ln in captured_stdout.splitlines() if "Surface tool-deny" in ln]
        print("  --- captured 'Surface tool-deny' log ---", flush=True)
        for ln in deny_log_lines or ["(none captured — relying on allowed_tools below)"]:
            print(f"    {ln}", flush=True)

        # ── allowed_tools actually passed to the SDK ──
        final_allowed = captured_allowed[-1] if captured_allowed else []
        ripple_create_in_allowed = [
            t for t in (_CREATE_LANDING, _SPECIALIST_CREATE) if t in final_allowed
        ]
        svelte_in_allowed = _CREATE_SVELTE in final_allowed
        print(
            f"  allowed_tools handed to SDK ({len(final_allowed)} total); "
            f"svelte_create_present={svelte_in_allowed}; "
            f"ripple_create_present={ripple_create_in_allowed}",
            flush=True,
        )

        # ── which tools the agent actually called ──
        print(f"  agent tool_use sequence: {tool_calls or '(none)'}", flush=True)

        # ── ground truth: the persisted pocket ──
        site_pockets = await PocketDoc.find(
            PocketDoc.workspace == workspace_id, PocketDoc.type == "site"
        ).to_list()

        # ── ASSERTIONS ──────────────────────────────────────────────────────────
        # (1) threaded deny set fired / ripple create tools excluded from allowed_tools
        res.check(
            len(captured_allowed) > 0 and not ripple_create_in_allowed and svelte_in_allowed,
            "(1) ripple create tools EXCLUDED from allowed_tools; create_svelte_site kept",
            f"deny_log_seen={bool(deny_log_lines)}",
        )

        # (2) agent called create_svelte_site and NOT any ripple/landing/add_widget create tool
        called_svelte = _CREATE_SVELTE in tool_calls
        called_ripple = [
            t for t in tool_calls if t in (_CREATE_LANDING, _SPECIALIST_CREATE, _ADD_WIDGET)
        ]
        res.check(
            called_svelte,
            "(2a) agent CALLED create_svelte_site",
            f"tool_calls={tool_calls}",
        )
        res.check(
            not called_ripple,
            "(2b) agent did NOT call create_landing_site / "
            "pocket_specialist__create / pocket__add_widget",
            f"ripple_calls={called_ripple}",
        )
        if _ADD_WIDGET in tool_calls:
            print(
                "  !!! FLAG: agent used pocket__add_widget to build ripple — it is NOT in "
                "the svelte-create deny set, so the deny set may need to include it.",
                flush=True,
            )

        # (3) resulting pocket has engine="svelte" + a source map + rippleSpec is None
        if site_pockets:
            pk = site_pockets[0]
            res.check(
                pk.engine == "svelte",
                "(3a) persisted pocket engine == 'svelte'",
                f"engine={pk.engine!r} type={pk.type!r} pattern={pk.pattern!r}",
            )
            res.check(
                bool(pk.source) and isinstance(pk.source, dict),
                "(3b) persisted pocket has a Svelte source map",
                f"source_keys={sorted((pk.source or {}).keys())[:6]}",
            )
            res.check(
                pk.rippleSpec is None,
                "(3c) persisted pocket rippleSpec is None (no ripple fallback)",
                f"rippleSpec={'present' if pk.rippleSpec else 'None'}",
            )
        else:
            res.check(
                False,
                "(3) a site pocket was persisted",
                "no type='site' pocket found — agent did not complete a create_svelte_site",
            )

        # (4) zero ripple_spec.unknown_widget_type warnings anywhere in the captured output
        unknown_widget_hits = captured_stdout.lower().count("ripple_spec.unknown_widget_type")
        res.check(
            unknown_widget_hits == 0,
            "(4) zero ripple_spec.unknown_widget_type warnings",
            f"hits={unknown_widget_hits}",
        )

        if assistant_text:
            preview = " ".join(assistant_text)[:240].replace("\n", " ")
            print(f"  assistant said (first 240 chars): {preview!r}", flush=True)

    finally:
        bus_mod._bus = prev_bus  # type: ignore[attr-defined]
        # Drop the throwaway DB so the real mongod isn't left with test data.
        try:
            await client.drop_database(db_name)
        except Exception:
            pass
        # pymongo's AsyncMongoClient.close() is a coroutine — await it (Motor's
        # was sync). Tolerate sync-close clients too so the teardown never raises.
        try:
            _close_result = client.close()
            if asyncio.iscoroutine(_close_result):
                await _close_result
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--unit-only", action="store_true", help="run only PART A (no LLM)")
    ap.add_argument("--e2e-only", action="store_true", help="run only PART B (real LLM turn)")
    args = ap.parse_args()

    res = _Results()
    failed_part = False

    if not args.e2e_only:
        try:
            run_unit(res)
        except Exception:
            failed_part = True
            print("UNIT part raised:", flush=True)
            traceback.print_exc()

    if not args.unit_only:
        try:
            asyncio.run(run_e2e(res))
        except Exception:
            failed_part = True
            print("E2E part raised:", flush=True)
            traceback.print_exc()

    print("\n" + "=" * 64, flush=True)
    overall_ok = res.ok and not failed_part
    print(
        f"OVERALL: {'PASS' if overall_ok else 'FAIL'}  "
        f"({res.n_pass}/{len(res.checks)} checks passed"
        f"{'' if not failed_part else '; a part raised an exception'})",
        flush=True,
    )
    print("=" * 64, flush=True)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

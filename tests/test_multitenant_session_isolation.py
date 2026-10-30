# tests/test_multitenant_session_isolation.py
# Created 2026-06-30 (feat/session-supervisor SS-7) — the consolidated, ADVERSARIAL
# multi-tenant session-isolation GATE (OSS half — no Mongo). The captain treats
# this suite as a merge gate: a single place that tries to make one tenant observe,
# evict, or resume another tenant's agent session and proves it cannot.
#
# This file holds the assertions that need no live mongod (the durable Mongo twin
# lives in tests/cloud/test_multitenant_session_isolation.py). It covers four of
# the five SS-7 concerns:
#   1. Store isolation over a SHARED backing dict — the real "two tenants on one
#      SaaS box" shape. Two InMemorySessionStores over ONE backing, workspace A
#      and B, colliding on the SAME (project_key, session_id). B can never load,
#      list, or delete A's row; a both-write collision keeps two scoped rows.
#   3. Supervisor quota isolation — one tenant filling its warm quota only evicts
#      ITS OWN idle sessions; a busy runtime and another tenant's warm session are
#      never the victims, even when the other tenant's session is globally oldest.
#   4. End-to-end no-cross-tenant-resume — a leaked/guessed native id paired with a
#      workspace-B-bound store resolves to NOTHING, asserted both at the store
#      level and through the SDK's REAL resume path (materialize_resume_session).
#   5. Reliance contract on the Claude Agent SDK's resume guards — we name the
#      guarantees the design leans on and assert OUR side plus the SDK helpers
#      directly (UUID guard, subpath validation, refreshToken redaction, perms,
#      ephemeral mkdtemp+rmtree cleanup).
#
# Concern 2 (durable (workspace, session, agent) -> cli_session_id mapping
# isolation) needs Beanie, so it lives in the cloud twin.

from __future__ import annotations

import json
import uuid
from pathlib import Path

from claude_agent_sdk._internal.session_resume import (
    _is_safe_subpath,
    _rmtree_with_retry,
    _write_redacted_credentials,
    materialize_resume_session,
)
from claude_agent_sdk._internal.sessions import _validate_uuid, project_key_for_directory
from claude_agent_sdk.types import ClaudeAgentOptions

from pocketpaw.agents.backend import SessionHandle
from pocketpaw.agents.session_store import InMemorySessionStore
from pocketpaw.agents.session_supervisor import SessionSupervisor, SessionTier


def _key(project_key="proj", session_id="sess-1", subpath=None):
    k = {"project_key": project_key, "session_id": session_id}
    if subpath is not None:
        k["subpath"] = subpath
    return k


class FakeClock:
    """Deterministic monotonic clock — advance it explicitly in the test."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class Teardown:
    """Fake teardown callback that records how many times it was called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


# ===========================================================================
# 1. Store isolation over a SHARED backing dict (the real SaaS box)
# ===========================================================================


async def test_shared_backing_tenant_b_cannot_observe_or_delete_tenant_a() -> None:
    """Two tenants on ONE backing dict, colliding on the SAME (project_key,
    session_id). B never wrote it, yet the key string is identical to A's. B must
    see NOTHING — load is None, the listings exclude A, and B deleting "its" key
    (a no-op for B) leaves A's row fully intact. This is the adversarial version
    of the per-slice tenancy test: same physical store, same key string."""
    shared: dict = {}
    store_a = InMemorySessionStore("ws-A", backing=shared)
    store_b = InMemorySessionStore("ws-B", backing=shared)

    # A owns a main transcript + a subagent transcript under (proj, sess-1).
    await store_a.append(_key(), [{"type": "user", "uuid": "a1"}])
    await store_a.append(_key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "as1"}])

    # B, sharing the same backing, uses the SAME project_key + session_id string.
    # Every read is scoped to ws-B's namespace, so A's rows are invisible.
    assert await store_b.load(_key()) is None
    assert await store_b.list_sessions("proj") == []
    assert await store_b.list_subkeys(_key()) == []

    # "Guessed id" attack: B targets a session_id that exists ONLY under A.
    assert await store_b.load(_key(session_id="sess-1")) is None

    # B deleting that key is a no-op for B and must NOT touch A's rows.
    await store_b.delete(_key())
    assert await store_a.load(_key()) == [{"type": "user", "uuid": "a1"}]
    assert await store_a.list_subkeys(_key()) == ["subagents/agent-1"]


async def test_shared_backing_same_key_two_tenants_coexist_and_delete_is_scoped() -> None:
    """When BOTH tenants write the SAME (project_key, session_id) over one backing,
    two namespaced rows coexist. Each tenant loads ITS OWN content (never the
    other's), and a delete by B removes only B's row — A survives. Proves the
    namespace keys the rows apart, not just the read filter."""
    shared: dict = {}
    store_a = InMemorySessionStore("ws-A", backing=shared)
    store_b = InMemorySessionStore("ws-B", backing=shared)

    await store_a.append(_key(), [{"type": "user", "uuid": "a-row"}])
    await store_b.append(_key(), [{"type": "user", "uuid": "b-row"}])

    # Each tenant sees only its own write — B's append did not clobber A's row.
    assert await store_a.load(_key()) == [{"type": "user", "uuid": "a-row"}]
    assert await store_b.load(_key()) == [{"type": "user", "uuid": "b-row"}]
    assert [s["session_id"] for s in await store_a.list_sessions("proj")] == ["sess-1"]
    assert [s["session_id"] for s in await store_b.list_sessions("proj")] == ["sess-1"]

    # B deletes its row; A's identical-key row is untouched.
    await store_b.delete(_key())
    assert await store_b.load(_key()) is None
    assert await store_a.load(_key()) == [{"type": "user", "uuid": "a-row"}]


# ===========================================================================
# 3. Supervisor quota isolation — one tenant can't starve another
# ===========================================================================


async def test_per_tenant_quota_evicts_only_offending_tenant_sparing_busy_and_peer() -> None:
    """Tenant A floods its per-tenant warm quota; every eviction victim is one of
    A's OWN idle warm sessions. A busy runtime (active_runs>0) is never evicted
    even though it is A's oldest, and tenant B's warm session is never evicted
    even though it is the GLOBALLY oldest slot (a naive global LRU would take it
    first). Driven deterministically with the injected fake clock."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=10_000, max_warm_per_tenant=2, now=clock)

    # Tenant B's warm session is bound FIRST → globally the oldest last_active.
    # Per-tenant scoping must spare it from A's pressure.
    b = sup.acquire("ws-b", "s-b1", "agent", cli_session_id="cli-b1")
    td_b = Teardown()
    sup.bind_warm_slot(b.runtime, slot="b-slot", teardown=td_b)

    # Tenant A: a1 is the oldest AND busy — it must never be the victim.
    clock.advance(1)
    a1 = sup.acquire("ws-a", "s-a1", "agent", cli_session_id="cli-a1")
    td_a1 = Teardown()
    sup.bind_warm_slot(a1.runtime, slot="a1", teardown=td_a1)
    sup.mark_run_start(a1.runtime)  # busy — protected from eviction

    clock.advance(1)
    a2 = sup.acquire("ws-a", "s-a2", "agent", cli_session_id="cli-a2")
    td_a2 = Teardown()
    sup.bind_warm_slot(a2.runtime, slot="a2", teardown=td_a2)

    # 3rd warm for A → over cap (2). a1 is busy, so the oldest IDLE in A (a2) is
    # the only legal victim. a2 evicted; b untouched.
    clock.advance(1)
    a3 = sup.acquire("ws-a", "s-a3", "agent", cli_session_id="cli-a3")
    td_a3 = Teardown()
    sup.bind_warm_slot(a3.runtime, slot="a3", teardown=td_a3)

    assert td_a2.calls == 1 and a2.runtime.tier is SessionTier.COLD
    assert td_a1.calls == 0 and a1.runtime.tier is SessionTier.WARM  # busy spared
    assert td_a3.calls == 0 and a3.runtime.tier is SessionTier.WARM
    assert td_b.calls == 0 and b.runtime.tier is SessionTier.WARM  # peer spared

    # 4th warm for A → over cap again. a1 still busy, so the next oldest IDLE (a3)
    # is evicted. Still no peer / busy victim.
    clock.advance(1)
    a4 = sup.acquire("ws-a", "s-a4", "agent", cli_session_id="cli-a4")
    td_a4 = Teardown()
    sup.bind_warm_slot(a4.runtime, slot="a4", teardown=td_a4)

    assert td_a3.calls == 1 and a3.runtime.tier is SessionTier.COLD
    assert td_a4.calls == 0 and a4.runtime.tier is SessionTier.WARM
    # Final tally: every victim was tenant A's; a1 (busy) and b (peer) never touched.
    assert td_a1.calls == 0 and a1.runtime.active_runs == 1
    assert td_b.calls == 0 and b.runtime.warm_slot == "b-slot"


# ===========================================================================
# 4. End-to-end no-cross-tenant-resume — a leaked native id can't cross tenants
# ===========================================================================


async def test_leaked_native_id_paired_with_b_store_resolves_to_nothing() -> None:
    """The strongest end-to-end property: even if tenant A's native cli_session_id
    leaks (or is guessed) and is fed to a turn whose SessionHandle is bound to
    tenant B's store, the resume resolves to NOTHING — because the store load is
    tenant-scoped. Asserted (a) at the store level (the SessionHandle just pairs
    the id with the B-bound store) and (b) through the SDK's REAL resume path,
    materialize_resume_session, which returns None for the B-bound store while the
    SAME id over A's store loads A's transcript."""
    shared: dict = {}
    cwd = "/tmp/paw-ss7-tenant-box"
    project_key = project_key_for_directory(cwd)

    # A real native session id is a UUID (the SDK mints one; run_core persists it
    # verbatim). Use that shape so the SDK's _validate_uuid guard is satisfied and
    # the only thing that can stop the resume is the tenancy filter.
    leaked_native_id = str(uuid.uuid4())
    transcript = [{"type": "user", "uuid": "a-u1"}, {"type": "assistant", "uuid": "a-u2"}]

    store_a = InMemorySessionStore("ws-A", backing=shared)
    await store_a.append({"project_key": project_key, "session_id": leaked_native_id}, transcript)

    store_b = InMemorySessionStore("ws-B", backing=shared)

    # (a) Store level: the handle pairs the leaked id with the B-bound store; the
    # store load is tenant-scoped, so it returns None.
    handle = SessionHandle(cli_session_id=leaked_native_id, session_store=store_b)
    assert (
        await handle.session_store.load(
            {"project_key": project_key, "session_id": leaked_native_id}
        )
        is None
    )
    # Positive control: A's own store DOES load the same key (id isn't bogus).
    assert (
        await store_a.load({"project_key": project_key, "session_id": leaked_native_id})
        == transcript
    )

    # (b) SDK real resume path: materialize_resume_session returns None for the
    # B-bound store. It returns BEFORE any mkdtemp / auth-file copy (the load miss
    # short-circuits), so this is hermetic — no temp dir, no credentials touched.
    opts_b = ClaudeAgentOptions(cwd=cwd, resume=leaked_native_id, session_store=store_b)
    assert await materialize_resume_session(opts_b) is None


# ===========================================================================
# 5. Reliance contract on the Claude Agent SDK's resume guards
# ===========================================================================
#
# Native resume leans on guarantees inside
# claude_agent_sdk/_internal/session_resume.py (and sessions.py). Naming them
# here keeps the dependency explicit; a future SDK bump that weakens any of these
# should break this gate:
#
#   * Ephemeral materialization — each resume writes the transcript to a fresh
#     ``tempfile.mkdtemp(prefix="claude-resume-")`` (session_resume.py:147) and
#     removes it via ``cleanup()`` -> ``_rmtree_with_retry`` (lines 170-174,
#     201-231). No persistent, cross-tenant file is left on the box.
#   * ``_validate_uuid(options.resume)`` traversal guard (session_resume.py:138):
#     the resume id is a path component; a non-UUID is rejected, so a "../"
#     traversal can never reach the filesystem layout step.
#   * ``_is_safe_subpath`` subpath validation (session_resume.py:490-522): every
#     subagent subpath from the store is rejected if empty / absolute / contains
#     ``..`` / escapes the session dir before it is written.
#   * ``refreshToken`` redaction before the subprocess
#     (``_write_redacted_credentials``, lines 355-378): the single-use refresh
#     token is stripped from the materialized ``.credentials.json``.
#   * ``0o600`` file perms on every file the resume writes (lines 302, 378, 487).
#
# We assert OUR side of the contract where we can, and call the SDK helpers
# directly where the guarantee is SDK-internal.


async def test_native_ids_are_uuid_shaped_so_the_resume_uuid_guard_applies() -> None:
    """OUR side: the native ids we persist/resume are UUID-shaped (run_core stores
    the SDK's session_id event verbatim, and the SDK mints a UUID), so the SDK's
    _validate_uuid traversal guard accepts a real id and rejects a forged one —
    the resume path never treats attacker-controlled text as a path component."""
    # A representative real native id (the shape the SDK emits) passes the guard.
    assert _validate_uuid(str(uuid.uuid4())) is not None
    # Forged / traversal-shaped ids are rejected before any filesystem step.
    assert _validate_uuid("../../etc/passwd") is None
    assert _validate_uuid("not-a-uuid") is None
    assert _validate_uuid("") is None


async def test_our_store_only_emits_subpaths_the_sdk_guard_accepts() -> None:
    """OUR side: every subpath InMemorySessionStore.list_subkeys emits is one the
    SDK's _is_safe_subpath guard accepts — our store never hands the resume
    materializer an unsafe (escaping) subpath."""
    store = InMemorySessionStore("ws-A")
    await store.append(_key(), [{"type": "user", "uuid": "u1"}])
    await store.append(_key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "s1"}])
    await store.append(_key(subpath="subagents/agent-2"), [{"type": "user", "uuid": "s2"}])

    session_dir = Path("/tmp/paw-ss7-proj") / "sess-1"
    emitted = await store.list_subkeys(_key())
    assert sorted(emitted) == ["subagents/agent-1", "subagents/agent-2"]
    for sub in emitted:
        assert _is_safe_subpath(sub, session_dir), f"store emitted an unsafe subpath: {sub!r}"


def test_sdk_subpath_guard_rejects_traversal(tmp_path) -> None:
    """SDK guarantee (relied on): _is_safe_subpath rejects empty / absolute /
    parent-traversal subpaths and accepts the legitimate ``subagents/...`` shape.
    Called directly because the guard lives in the SDK we depend on."""
    session_dir = tmp_path / "sess"
    assert _is_safe_subpath("subagents/agent-7", session_dir) is True
    assert _is_safe_subpath("../escape", session_dir) is False
    assert _is_safe_subpath("subagents/../../escape", session_dir) is False
    assert _is_safe_subpath("/abs/path", session_dir) is False
    assert _is_safe_subpath("", session_dir) is False


def test_sdk_redacts_refresh_token_and_writes_0600(tmp_path) -> None:
    """SDK guarantee (relied on): _write_redacted_credentials strips the single-use
    ``refreshToken`` and writes the materialized creds file 0o600. Called directly
    because the redaction happens inside the SDK before it spawns the subprocess."""

    creds = json.dumps({"claudeAiOauth": {"accessToken": "keep-me", "refreshToken": "burn-me"}})
    dst = tmp_path / ".credentials.json"
    _write_redacted_credentials(creds, dst)

    written = json.loads(dst.read_text())
    assert "refreshToken" not in written["claudeAiOauth"]
    assert written["claudeAiOauth"]["accessToken"] == "keep-me"  # other fields kept
    assert (dst.stat().st_mode & 0o777) == 0o600


async def test_sdk_rmtree_cleanup_removes_materialized_dir(tmp_path) -> None:
    """SDK guarantee (relied on): the per-resume temp dir is removed by
    _rmtree_with_retry (the cleanup() the resume path schedules), so no
    cross-tenant transcript file persists. Called directly on a populated dir."""
    doomed = tmp_path / "claude-resume-xyz"
    (doomed / "projects").mkdir(parents=True)
    (doomed / "projects" / "x.jsonl").write_text("{}\n")
    assert doomed.exists()

    await _rmtree_with_retry(doomed)
    assert not doomed.exists()

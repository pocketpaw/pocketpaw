# Added 2026-05-31 (fix/home-backend-summary-per-turn): regression tests for
# the persistent-client cache key. The home agent baked the backend summary
# ({base_url, auth_type, configured}) into its STATIC system prompt; that
# prompt is applied to the SDK subprocess only at connect() time and ignored
# on warm reuse, so configuring a pocket's backend mid-session was frozen
# until a cold restart. The cache key now folds in a digest of the system
# prompt's stable behavioral prefix, so a config flip rebuilds the persistent
# client on the very next turn. Per-turn KB/memory/history (appended after the
# behavioral prefix) are excluded so the warm-client optimization still holds
# for normal turns.
#
# Modified: 2026-06-05 (feat/surface-profile-bias-kill) — Added
# test_sites_surface_behavior_prefix_changes_key. Mirrors the home flip guard for
# the surface dimension: a /sites-surface ctx and a non-sites ctx must produce
# DIFFERENT behavior instructions (sites omits the ripple block), so the
# persistent-client cache keys must differ.
# Modified: 2026-06-05 (feat/sites-svelte-engine) — re-pinned that guard's /sites
# ctx to ``SurfaceMeta(engine="svelte")``. The resolver is now META-AWARE, so a
# bare ``SurfaceMeta()`` /sites ctx is the ripple-CREATE mode that KEEPS ripple —
# its instructions no longer differ from a non-sites surface, which broke the
# assertion. The svelte-create mode is the only /sites mode that omits ripple, so
# pinning to it keeps the guard meaningful (a ripple-dropping surface change
# forces a warm-client rebuild).
from __future__ import annotations

from types import SimpleNamespace

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend


def _opts(system_prompt, *, model="claude-x", tools=("Agent", "WebSearch")):
    return SimpleNamespace(
        model=model,
        allowed_tools=list(tools),
        system_prompt=system_prompt,
    )


# The two volatile section markers ``pool.run`` appends AFTER the behavioral
# instructions. Anything from the first marker onward must NOT influence the
# key, or every per-turn KB/memory change would needlessly rebuild the client.
_KB_HEADER = "## Your Knowledge Base\nUse the following information..."
_MEM_HEADER = "## Relevant Past Memories\nBelow are memories..."


# ---------------------------------------------------------------------------
# Added 2026-07-01 (integration/warm-reuse): regression tests for the MID-prompt
# soul "# Key Knowledge" block. ``AgentPool._assemble_system_prompt`` splices it
# in right after the stable soul identity via
# ``f"{system_prompt}\n\n# Key Knowledge\n{knowledge_lines}"`` (pool.py:243-245).
# Its lines are ALL volatile soul state — self-image confidences, an
# incrementing bond level, a growing memory count, recalled memories
# (soul/_bridge.py) — none of them behavioral instructions. Live instrumentation
# proved warm-client reuse NEVER fired: two consecutive supervised turns'
# behavior prefixes differed by exactly 2 chars, the incrementing "Bond level"
# and "Memories" digits inside this block, so the prefix digest changed every
# turn and the subprocess was rebuilt every turn. The block sits EARLY (char
# ~1.4k of a ~44k prefix) between the stable identity and ~43KB of stable
# ``<runtime-identity>`` + tool docs, so it must be EXCISED IN PLACE — a naive
# tail-cut marker would strip ~97% of the real behavioral prefix.


# A realistic prompt shape: stable identity, then the mid-prompt soul block,
# then a large stable ``<runtime-identity>`` + tool-docs section, matching the
# real ``ctx.identity`` -> "# Key Knowledge" -> runtime layout in pool.py.
_IDENTITY = "<soul-identity>\nI am Paw, a persistent companion." + ("X" * 1300)
_RUNTIME_TAIL = "\n\n<runtime-identity>\nTool docs and skills...\n" + ("Y" * 43000)


def _soul_block(*, bond: str, memories: int) -> str:
    """The exact block pool.py builds: ``\\n\\n# Key Knowledge\\n`` + ``- `` lines
    of volatile soul state (self-image, bond, memory count)."""
    lines = "\n".join(
        [
            "- [creative] confidence=0.82",
            f"- Bond level: {bond}/100",
            f"- Memories: {memories}",
        ]
    )
    return f"\n\n# Key Knowledge\n{lines}"


def _full_prompt(*, bond: str, memories: int, kb: str) -> str:
    """Assemble a full system prompt exactly as ``AgentPool`` would: stable
    identity + soul block + stable runtime tail + a per-turn volatile KB tail."""
    return (
        _IDENTITY
        + _soul_block(bond=bond, memories=memories)
        + _RUNTIME_TAIL
        + "\n\n"
        + _KB_HEADER
        + "\n"
        + kb
    )


def test_incrementing_soul_block_keeps_key_stable():
    """THE reproduction: two consecutive supervised turns whose ONLY difference
    is the incrementing bond level / memory count inside the mid-prompt
    "# Key Knowledge" block must produce the SAME cache key, so the warm
    subprocess is reused instead of rebuilt every turn.

    FAILS on pre-fix code — the tail-only strip leaves the mid-prompt block in
    the hashed prefix, so the 50.0->50.5 / 0->1 drift changes the digest."""
    turn1 = _full_prompt(bond="50.0", memories=0, kb="turn-1 kb snippet")
    turn2 = _full_prompt(bond="50.5", memories=1, kb="turn-2 kb snippet")
    k1 = ClaudeSDKBackend._client_cache_key(_opts(turn1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(turn2), session_key="s1")
    assert k1 == k2, (
        "an incrementing soul '# Key Knowledge' block (bond level / memory "
        "count) must not rebuild the warm client — it carries no behavioral "
        "instructions, only per-turn soul state"
    )


def test_soul_block_stripped_but_behavioral_change_still_rekeys():
    """The excise must NOT over-strip: a genuinely different behavioral prefix
    (different persona/identity BEFORE the soul block) must still change the key
    even though both prompts carry an identical soul block, so a stale client is
    NOT reused for a different persona."""
    identity_a = "<soul-identity>\nI am Paw." + ("X" * 1300)
    identity_b = "<soul-identity>\nI am Rex, a DIFFERENT persona." + ("X" * 1300)
    block = _soul_block(bond="50.0", memories=0)
    prompt_a = identity_a + block + _RUNTIME_TAIL + "\n\n" + _KB_HEADER + "\nkb"
    prompt_b = identity_b + block + _RUNTIME_TAIL + "\n\n" + _KB_HEADER + "\nkb"
    ka = ClaudeSDKBackend._client_cache_key(_opts(prompt_a), session_key="s1")
    kb = ClaudeSDKBackend._client_cache_key(_opts(prompt_b), session_key="s1")
    assert ka != kb, (
        "a real behavioral change (different identity before the soul block) "
        "must still rebuild the warm client — the excise must not over-strip"
    )


def test_soul_block_strip_preserves_stable_runtime_tail():
    """Excising the mid-prompt block must keep the large stable section AFTER it
    (``<runtime-identity>`` + tool docs) in the behavioral prefix, so a change
    there still rekeys — proving we removed only the block, not everything after
    the header (the over-strip failure mode of a tail-cut marker)."""
    prompt = _full_prompt(bond="50.0", memories=0, kb="kb")
    prefix = ClaudeSDKBackend._behavior_prefix(prompt)
    assert "# Key Knowledge" not in prefix, "the soul block must be excised"
    assert "<soul-identity>" in prefix, "stable identity before the block must remain"
    assert "<runtime-identity>" in prefix, "stable runtime tail after the block must remain"
    # A tail-cut marker would have shrunk this to ~1.3k; the surgical excise
    # keeps the ~43KB runtime tail.
    assert len(prefix) > 40000, "the ~43KB stable runtime tail must survive the excise"


def test_soul_block_as_last_section_is_stripped():
    """Edge case: the soul block is the LAST section (no trailing blank line, no
    KB tail). The incrementing values must still be excised so the key is
    stable, and everything before the block preserved."""
    p1 = _IDENTITY + _soul_block(bond="50.0", memories=0)
    p2 = _IDENTITY + _soul_block(bond="51.5", memories=3)
    k1 = ClaudeSDKBackend._client_cache_key(_opts(p1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(p2), session_key="s1")
    assert k1 == k2, "a block-as-last-section drift must not rebuild the warm client"
    assert ClaudeSDKBackend._behavior_prefix(p1) == _IDENTITY


def test_absent_soul_block_is_byte_identical():
    """Edge case: empty ``ctx.knowledge`` means no block at all (and the legacy
    flag-off path). The behavioral prefix must be byte-identical to the input's
    stable head — the excise is a no-op when the header is absent."""
    no_block = _IDENTITY + _RUNTIME_TAIL + "\n\n" + _KB_HEADER + "\nkb"
    prefix = ClaudeSDKBackend._behavior_prefix(no_block)
    assert prefix == _IDENTITY + _RUNTIME_TAIL, "no block present → excise is a no-op"


# ---------------------------------------------------------------------------
# Hardening (integration/warm-reuse): the block is excised by STRUCTURE (its
# ``- ``-prefixed item run), not by "cut at the first blank line". These guard
# the failure modes a blank-line cut has: (1) a recalled memory whose content
# wraps onto a continuation line, (2) a plain-prose stable section (the ripple
# LAW ``instructions``) that follows the block and must NOT be over-stripped,
# and (3) a user-authored "# Key Knowledge" heading in persona prose that must
# not be mistaken for the machine block.


def _soul_block_with_memory(*, bond: str, memories: int, memory: str) -> str:
    """The block with a trailing recalled-memory item (soul/_bridge.py:106 builds
    it as ``- [semantic] {content}``). ``memory`` may contain newlines — a real
    multi-line memory ``content``."""
    return (
        "\n\n# Key Knowledge\n"
        "- [creative] confidence=0.82\n"
        f"- Bond level: {bond}/100\n"
        f"- Memories: {memories}\n"
        f"- [semantic] {memory}"
    )


def test_wrapped_multiline_memory_keeps_key_stable():
    """A recalled memory whose content wraps onto a continuation line (single
    ``\\n``, no blank line) is absorbed into the block. Two turns differing only
    in the incrementing bond/memory counts (and the wrapped memory text) must
    still hash identically — the wrapped line must not leak into the prefix."""
    turn1 = (
        _IDENTITY
        + _soul_block_with_memory(bond="50.0", memories=0, memory="note A\nwrapped tail 1")
        + _RUNTIME_TAIL
        + "\n\n"
        + _KB_HEADER
        + "\nturn-1 kb"
    )
    turn2 = (
        _IDENTITY
        + _soul_block_with_memory(bond="50.5", memories=1, memory="note B\nwrapped tail 2")
        + _RUNTIME_TAIL
        + "\n\n"
        + _KB_HEADER
        + "\nturn-2 kb"
    )
    k1 = ClaudeSDKBackend._client_cache_key(_opts(turn1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(turn2), session_key="s1")
    assert k1 == k2, "a wrapped multi-line recalled memory must not rebuild the warm client"
    prefix = ClaudeSDKBackend._behavior_prefix(turn1)
    assert "wrapped tail 1" not in prefix, "the wrapped memory line must be excised"
    assert "<runtime-identity>" in prefix, "the stable runtime tail must survive"


def test_plain_prose_instructions_after_block_not_overstripped():
    """The authoritative ``instructions`` (ripple LAW) are appended DIRECTLY
    after the block (pool.py:268-269) as ``\\n\\n{instructions}`` and can start
    in plain prose (no ``#``/``<`` anchor). Excising the block must keep them, so
    a real instructions change still rekeys — the over-strip guard that a
    heuristic "consume until the next heading/tag" terminator would fail."""
    instr_a = "\n\nYou must ALWAYS cite your sources and stay terse."
    instr_b = "\n\nYou must NEVER cite sources and may be verbose."
    block = _soul_block(bond="50.0", memories=0)
    p_a = _IDENTITY + block + instr_a + _RUNTIME_TAIL
    p_b = _IDENTITY + block + instr_b + _RUNTIME_TAIL
    prefix_a = ClaudeSDKBackend._behavior_prefix(p_a)
    assert "# Key Knowledge" not in prefix_a, "the soul block must be excised"
    assert "cite your sources" in prefix_a, "plain-prose instructions after the block must be kept"
    k_a = ClaudeSDKBackend._client_cache_key(_opts(p_a), session_key="s1")
    k_b = ClaudeSDKBackend._client_cache_key(_opts(p_b), session_key="s1")
    assert k_a != k_b, "a real instructions change after the block must still rebuild the client"


def test_persona_key_knowledge_heading_is_not_the_machine_block():
    """A user-authored "# Key Knowledge" heading inside persona/identity prose
    (NOT followed by ``- `` items) must not be mistaken for the machine block.
    The machine block (``rfind`` + ``- `` item guard) is excised; the persona
    prose stays in the prefix, so a persona edit still rekeys."""
    persona_a = "PERSONA\n\n# Key Knowledge\nI value honesty above all.\n\nmore persona."
    persona_b = "PERSONA\n\n# Key Knowledge\nI value brevity above all.\n\nmore persona."
    block = _soul_block(bond="50.0", memories=0)
    p_a = persona_a + block + _RUNTIME_TAIL
    p_b = persona_b + block + _RUNTIME_TAIL
    prefix_a = ClaudeSDKBackend._behavior_prefix(p_a)
    assert "I value honesty above all" in prefix_a, "persona prose heading must be preserved"
    assert "Bond level" not in prefix_a, "the machine soul block must still be excised"
    k_a = ClaudeSDKBackend._client_cache_key(_opts(p_a), session_key="s1")
    k_b = ClaudeSDKBackend._client_cache_key(_opts(p_b), session_key="s1")
    assert k_a != k_b, "a persona change (even under a matching heading) must still rekey"
    # And the machine block's own drift is still neutralized under a persona
    # heading collision.
    p_drift = persona_a + _soul_block(bond="99.9", memories=7) + _RUNTIME_TAIL
    assert ClaudeSDKBackend._behavior_prefix(p_a) == ClaudeSDKBackend._behavior_prefix(p_drift)


def test_config_flip_changes_key():
    """A home backend flipping configured:false -> configured:true changes the
    behavioral prefix, so the cache key must differ and force a client rebuild."""
    before = "<home-pocket>\nBackend: not configured\n...rest of static prompt..."
    after = (
        "<home-pocket>\nBackend: configured — https://api.acme.test (auth: bearer)\n"
        "...rest of static prompt..."
    )
    key_before = ClaudeSDKBackend._client_cache_key(_opts(before), session_key="s1")
    key_after = ClaudeSDKBackend._client_cache_key(_opts(after), session_key="s1")
    assert key_before != key_after, (
        "config flip must change the cache key so the warm client rebuilds with "
        "the fresh backend summary — otherwise mid-session config stays frozen"
    )


def test_per_turn_kb_change_keeps_key_stable():
    """Only the per-turn knowledge-base block changed between two turns; the
    behavioral prefix is identical, so the key MUST stay the same (warm reuse)."""
    behavior = "<home-pocket>\nBackend: configured — https://api.acme.test (auth: bearer)\n"
    turn1 = behavior + "\n\n" + _KB_HEADER + "\nsnippet about revenue"
    turn2 = behavior + "\n\n" + _KB_HEADER + "\na totally different KB snippet"
    k1 = ClaudeSDKBackend._client_cache_key(_opts(turn1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(turn2), session_key="s1")
    assert k1 == k2, "a per-turn KB change must not rebuild the warm client"


def test_per_turn_memory_change_keeps_key_stable():
    behavior = "<home-pocket>\nBackend: configured — https://api.acme.test (auth: bearer)\n"
    turn1 = behavior + "\n\n" + _MEM_HEADER + "\nremembered fact A"
    turn2 = behavior + "\n\n" + _MEM_HEADER + "\nremembered fact B"
    k1 = ClaudeSDKBackend._client_cache_key(_opts(turn1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(turn2), session_key="s1")
    assert k1 == k2


def test_history_change_keeps_key_stable():
    """Cold-start runs inject "# Recent Conversation" history into the prompt.
    That is per-turn and must not influence the key."""
    behavior = "<home-pocket>\nBackend: configured — https://api.acme.test (auth: bearer)\n"
    turn1 = behavior + "\n\n# Recent Conversation\n**User**: hi"
    turn2 = behavior + "\n\n# Recent Conversation\n**User**: hi\n**Assistant**: hello"
    k1 = ClaudeSDKBackend._client_cache_key(_opts(turn1), session_key="s1")
    k2 = ClaudeSDKBackend._client_cache_key(_opts(turn2), session_key="s1")
    assert k1 == k2


def test_model_and_tools_still_keyed():
    """The original key inputs (session, model, allowed_tools) still
    participate — a model or tool change must rebuild the client."""
    sp = "<home-pocket>\nBackend: not configured\n"
    base = ClaudeSDKBackend._client_cache_key(_opts(sp), session_key="s1")
    other_model = ClaudeSDKBackend._client_cache_key(_opts(sp, model="claude-y"), session_key="s1")
    other_tools = ClaudeSDKBackend._client_cache_key(_opts(sp, tools=("Agent",)), session_key="s1")
    other_session = ClaudeSDKBackend._client_cache_key(_opts(sp), session_key="s2")
    assert base != other_model
    assert base != other_tools
    assert base != other_session


def test_file_system_prompt_does_not_crash():
    """On Windows the SDK passes ``system_prompt`` as a ``{type, path}`` dict
    instead of a string. The key computation must tolerate that shape."""
    file_prompt = {"type": "file", "path": "/tmp/system_prompt.md"}
    key = ClaudeSDKBackend._client_cache_key(_opts(file_prompt), session_key="s1")
    assert isinstance(key, str) and key


def test_real_home_behavior_instructions_flip_changes_key():
    """End-to-end guard: build the ACTUAL home behavior instructions with a
    configured:false then configured:true backend summary (a mid-session
    config), wrap each as a system prompt the way ``AgentPool.run`` would, and
    assert the persistent-client cache key differs. This is the regression the
    smoke test surfaced: same session, config flipped, agent read stale state
    until a cold restart. If someone moves the backend summary VALUE into the
    per-turn volatile tail this guard still passes only because the behavioral
    instructions themselves carry it — keep it in the static prefix."""
    # build_behavior_instructions lives in the enterprise layer; skip when the
    # OSS-only test job runs without pocketpaw_ee installed.
    import pytest

    pytest.importorskip("pocketpaw_ee")
    from pocketpaw_ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        build_behavior_instructions,
    )

    def _ctx(summary):
        return ScopeContext(
            kind=ScopeKind.POCKET,
            scope_id="home-pocket-1",
            workspace_id="w1",
            user_id="u1",
            members=["u1"],
            target_agent_id="a1",
            agent_ids_in_scope=["a1"],
            pocket_id="home-pocket-1",
            pocket_type="home",
            backend_summary=summary,
        )

    before = build_behavior_instructions(
        _ctx({"configured": False}), backend_name="claude_agent_sdk"
    )
    after = build_behavior_instructions(
        _ctx({"configured": True, "base_url": "https://api.acme.test", "auth_type": "bearer"}),
        backend_name="claude_agent_sdk",
    )
    assert before != after, "behavior instructions must reflect the config flip"

    # Wrap as ``AgentPool.run`` does: behavior prefix, then a volatile per-turn
    # KB tail that differs between the two turns (it must NOT mask the flip).
    sp_before = f"persona\n\n{before}\n\n## Your Knowledge Base\nstale turn-1 snippet"
    sp_after = f"persona\n\n{after}\n\n## Your Knowledge Base\ndifferent turn-2 snippet"
    key_before = ClaudeSDKBackend._client_cache_key(_opts(sp_before), session_key="s1")
    key_after = ClaudeSDKBackend._client_cache_key(_opts(sp_after), session_key="s1")
    assert key_before != key_after, (
        "a mid-session backend config change must rebuild the warm client so "
        "the agent reads the CURRENT backend state on the next message"
    )


def test_sites_surface_behavior_prefix_changes_key():
    """Guard (feat/surface-profile-bias-kill, re-pinned feat/sites-svelte-engine):
    build the ACTUAL behavior instructions for a /sites SVELTE-CREATE surface ctx
    vs a non-sites ctx and assert the persistent-client cache keys differ.

    On /sites svelte-create the agent hand-authors a Svelte Paw Site, so its
    behavior instructions OMIT the ~20k-char INLINE_RIPPLE_SYSTEM_PROMPT
    ("default to ui-spec" LAW); a non-sites surface keeps it. Different
    instructions → different behavioral prefix → different prefix digest →
    different key, so switching surfaces mid-session rebuilds the warm client
    with the right instructions on the next message.

    NOTE (feat/sites-svelte-engine): this pins the /sites ctx to
    ``SurfaceMeta(engine="svelte")``. Its PR-1 form passed a bare
    ``SurfaceMeta()`` — which the now-META-AWARE resolver correctly treats as the
    ripple-CREATE mode that KEEPS ripple — so the /sites and non-sites
    instructions no longer differ for that meta and the assertion stopped holding.
    Pinned to the svelte engine (the only /sites mode that omits ripple) so the
    test keeps verifying what it means to: a surface change that drops the ripple
    block forces a warm-client rebuild."""
    # build_behavior_instructions / the surface value objects live in the
    # enterprise layer; skip when the OSS-only test job runs without it.
    import pytest

    pytest.importorskip("pocketpaw_ee")
    from pocketpaw_ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        build_behavior_instructions,
    )
    from pocketpaw_ee.cloud.surface import (
        SurfaceContext,
        SurfaceKind,
        SurfaceMeta,
        resolve_profile,
    )

    def _ctx(surface_kind, meta=None):
        resolved_meta = meta if meta is not None else SurfaceMeta()
        return ScopeContext(
            kind=ScopeKind.SESSION,
            scope_id="s1",
            session_id="s1",
            workspace_id="w1",
            user_id="u1",
            members=["u1"],
            target_agent_id="a1",
            agent_ids_in_scope=["a1"],
            surface_context=SurfaceContext(
                workspace_id="w1",
                user_id="u1",
                kind=surface_kind,
                meta=resolved_meta,
                preamble="",
            ),
            # entity-rooms chunk ①: build_behavior_instructions reads the
            # pre-resolved profile. Mirror the run-driver's once-per-run
            # resolution (no entity override here) so the ripple gate fires.
            resolved_profile=resolve_profile(surface_kind, resolved_meta),
        )

    sites = build_behavior_instructions(
        _ctx(SurfaceKind.SITES, SurfaceMeta(engine="svelte")), backend_name="claude_agent_sdk"
    )
    non_sites = build_behavior_instructions(
        _ctx(SurfaceKind.POCKETS_LIST), backend_name="claude_agent_sdk"
    )
    assert sites != non_sites, (
        "behavior instructions must differ by surface — /sites omits the ripple "
        "block that non-sites surfaces keep"
    )

    # Wrap each as ``AgentPool.run`` does: persona + behavior prefix, then a
    # volatile per-turn KB tail that differs between the two (it must NOT mask
    # the surface flip).
    sp_sites = f"persona\n\n{sites}\n\n## Your Knowledge Base\nsites turn snippet"
    sp_non_sites = f"persona\n\n{non_sites}\n\n## Your Knowledge Base\npockets turn snippet"
    key_sites = ClaudeSDKBackend._client_cache_key(_opts(sp_sites), session_key="s1")
    key_non_sites = ClaudeSDKBackend._client_cache_key(_opts(sp_non_sites), session_key="s1")
    assert key_sites != key_non_sites, (
        "the /sites and non-sites behavioral prefixes must produce different "
        "cache keys so switching surfaces mid-session rebuilds the warm client "
        "with surface-correct instructions"
    )

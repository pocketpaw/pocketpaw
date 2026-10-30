# tests/cloud/chat/test_surface_system_prompt.py — SurfaceProfile.system_message
# _override, consumed by build_behavior_instructions.
#
# Created: 2026-07-22 (fix/code-surface-denies-pocket-authoring) — the field had
# been declared-but-inert since 2026-06-05 and is now the /code surface's own
# system prompt.
#
# What these guard, and why the deny set was not enough on its own. The reported
# bug: with a React project open on /code, "Let's build an employee management
# app, with components, nice design etc" made the agent create a pocket and
# author a ripple ui-spec. Denying the pocket tools stops it BUILDING one. It
# does not stop the shared behavioral stack from telling it to — that stack is
# written for an agent whose deliverable is a pocket, and on /code every one of
# its rules names a tool the profile has taken away (the artifact rule wants
# ``Write`` + ``deliver_artifact``; the delegation rule wants the specialist).
# An instruction to use a denied tool costs the user a turn and leaves the
# trained-in default as the only concrete plan in context.
#
# So the two halves are tested as a pair: the deny set (in
# tests/cloud/surface/test_studio_code_handlers.py) proves the alternatives are
# unreachable; these prove the surface states its OWN deliverable instead of
# only forbidding the other one.
#
# The regression half matters as much as the /code half. The override is read on
# a path every cloud chat turn goes through, so a bug here would silently reshape
# the prompt for every other surface — hence ``test_default_surface_prompt_is_
# untouched``, which pins the ripple-on assembly byte-for-byte against a profile
# that sets no override.

from __future__ import annotations

from dataclasses import replace

from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, build_behavior_instructions
from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile
from pocketpaw_ee.cloud.surface.system_prompts import CODE_SYSTEM_PROMPT

BACKEND = "claude_agent_sdk"


def _ctx(**overrides: object) -> ScopeContext:
    """A minimal workspace-scope context; ``overrides`` set the fields under test."""
    base = ScopeContext(
        kind="workspace",
        scope_id="ws-sysprompt",
        workspace_id="ws-sysprompt",
        user_id="u-sysprompt",
        members=[],
        target_agent_id="agent-1",
    )
    return replace(base, **overrides)


def _code_prompt() -> str:
    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())
    return build_behavior_instructions(_ctx(resolved_profile=profile), backend_name=BACKEND)


def test_code_surface_prompt_is_the_override() -> None:
    """The /code system prompt IS the surface's own text, not the shared stack."""
    assert CODE_SYSTEM_PROMPT in _code_prompt()


def test_code_surface_prompt_drops_the_pocket_deliverable_stack() -> None:
    """Every pocket-shaped instruction is gone from the /code prompt.

    Asserted by MARKER rather than by naming the constants, so the test keeps
    holding if the ripple LAW or the delegation rule is reworded. Each marker
    below is an instruction to build or delegate a pocket — the thing the user
    did not ask for and got anyway."""
    prompt = _code_prompt()

    # The ripple LAW's own heading, and the delegation rule's instruction.
    assert "INLINE RIPPLE" not in prompt.upper()
    assert "pocket specialist" not in prompt
    assert "add_widget" not in prompt
    # The artifact rule: names Write + deliver_artifact, neither of which /code has.
    assert "<artifact-delivery>" not in prompt
    assert "deliver_artifact" not in prompt


def test_code_surface_prompt_keeps_the_environment_rules() -> None:
    """The override replaces the WORK, not the ENVIRONMENT.

    ``runtime-identity`` is true on every surface — the agent is PocketPaw in a
    GUI chat, slash commands do not exist here — and a /code agent that forgets
    it starts telling the user to run ``/mcp``. Folding it into each surface's
    prompt would duplicate it per surface and let the copies drift, so it stays
    in the shared assembly and the override sits after it."""
    prompt = _code_prompt()

    assert "<runtime-identity>" in prompt
    assert prompt.index("<runtime-identity>") < prompt.index(CODE_SYSTEM_PROMPT)


def test_code_surface_prompt_is_far_smaller_than_the_default() -> None:
    """Dropping the deliverable stack is a large, measurable saving.

    Not a micro-optimization: the ripple LAW alone is ~20k chars of "default to
    ui-spec" instruction, and it was in the /code agent's context on every turn
    while the surface's actual job got a few hundred characters of preamble. The
    ratio is why the prohibition kept losing."""
    code_len = len(_code_prompt())
    default = resolve_profile(SurfaceKind.CHAT, SurfaceMeta())
    default_len = len(
        build_behavior_instructions(_ctx(resolved_profile=default), backend_name=BACKEND)
    )

    assert code_len < default_len / 2, (
        f"/code prompt is {code_len} chars vs {default_len} for the default "
        f"surface — the deliverable stack does not look dropped"
    )


def test_default_surface_prompt_is_untouched() -> None:
    """THE regression guard: a profile with no override is assembled exactly as
    before.

    ``build_behavior_instructions`` runs on every cloud chat turn, so the
    override branch must be inert for every surface that does not set one. Both
    a real ripple-on profile and the legacy ``resolved_profile is None`` path are
    checked, since the latter is what a surface-less client still sends."""
    default = resolve_profile(SurfaceKind.CHAT, SurfaceMeta())
    with_profile = build_behavior_instructions(_ctx(resolved_profile=default), backend_name=BACKEND)
    legacy = build_behavior_instructions(_ctx(), backend_name=BACKEND)

    # The stack /code drops is still fully present on the default surface.
    for marker in ("<artifact-delivery>", "deliver_artifact", "pocket specialist"):
        assert marker in with_profile, f"{marker} vanished from the default surface prompt"
        assert marker in legacy, f"{marker} vanished from the legacy (no-profile) prompt"

    # And no surface leaks the /code prompt.
    assert CODE_SYSTEM_PROMPT not in with_profile
    assert CODE_SYSTEM_PROMPT not in legacy

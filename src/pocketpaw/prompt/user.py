"""The user layer — who the agent is talking to.

Created: 2026-08-03 (PA-5, feat/prompt-assembler-seam).

The ``<about-member>`` block: the member's name, role, team and one-line focus,
so the agent greets a person rather than a session and tailors its help to what
that person actually does.

THE BLOCK ALREADY EXISTS AND IS NOT MOVED HERE YET, which is the one thing to
know before extending this. ``ee/.../chat/agent_service.py`` renders it in
``_render_about_member_block`` and ``build_behavior_instructions`` appends it to
the END of the instruction stack, so today it reaches the prompt inside the
``instructions`` layer. Relocating it into this layer moves bytes — from below
the ripple LAW to above the surface preamble — and every byte above
``ClaudeSDKBackend._behavior_prefix``'s cut is hashed into the warm-client key,
so the move costs a reconnect for every client live at deploy. It also changes
``instructions``' key, which is a digest of exactly those bytes. PA-5's whole
acceptance is that no byte moves, so the move belongs to whichever task is
already moving prompt bytes — PA-6 reclaims identity-first ordering, PA-7 ports
the channel path. Until then this layer renders what it is given, and nothing
gives it anything.

THE KEY IS THE USER ID **AND** A DIGEST OF THE BLOCK, where PA-5 filed it as the
id alone. The id alone cannot see a profile EDIT: a member changes team, the
block changes, the id does not, and a backend that bakes the prompt into a
cached agent keeps introducing them by their old role. The usual answer to that
is a document revision, and there is not one to be had — ``updatedAt`` on the
cloud documents holds its construction-time value forever, because beanie 2.1.0
skips ``TimestampedDocument``'s ``_``-prefixed hooks and never registers them.
So the bytes are the only honest revision available.

The id is still carried, and it is not redundant. Two members with no
materialized ``Person`` both render nothing, so a digest alone would call them
one identity — true of the prompt, and a claim this layer should not be the one
making. It is the layer that exists to say WHO is talking; letting two people
share a key because neither has filled in a profile is the #1842 shape with a
sympathetic cause. The cost is one extra rebuild when switching between two
profile-less members on one agent, which is the safe direction.

WHAT THE DIGEST STILL MISSES, stated because the surface layer refuses a text
hash for a related reason. ``_render_about_member_block`` clamps the focus line
at 280 chars and hard-caps the whole block at 1500, so an edit beyond those
bounds moves the Person without moving these bytes. That is the same
lossy-view shape as the surface preamble's 12-of-N widgets, and it is treated
differently on purpose: a stale surface summary makes the agent describe a
pocket that no longer looks like that, whereas the truncated tail of a member's
focus line never reached the model at all. A key exists to track what the prompt
SAYS. This one does.

ORDER: THIRD, BETWEEN ``atlas`` AND ``surface``. The OS is the same for
everybody and the person is not, so it sits below the primer; and who is talking
governs how the agent reads every surface it is then handed, so it sits above
the surface. That is also the volatility ladder the prefix cache wants — the
primer changes on deploy, a member's block on a profile edit, the surface key on
every navigation. Pinned with its reason in
``tests/test_prompt_instructions_layer.py``'s ``_ORDER_RULES``.
"""

from __future__ import annotations

import hashlib

from pocketpaw.prompt.layer import LayerOutput, Priority, PromptContext


class UserInfoLayer:
    """Renders the about-member block and keys it on the member plus its bytes."""

    name = "user"
    # HIGH: below the persona and the rules, above everything else. An agent
    # that loses this still behaves correctly, it just addresses a stranger —
    # and the block is a few hundred chars, so it is never the reason a prompt
    # is over budget. Dropping it saves nothing worth the regression.
    priority: Priority = Priority.HIGH
    # 500 chars, from ``_INJECTION_CAPS["sender_block"]`` — the channel path's
    # equivalent "who is talking" block. Well under the EE renderer's own 1500
    # hard cap, so this bites only on a block that arrives from somewhere else.
    max_chars: int | None = 500

    async def render(self, ctx: PromptContext) -> LayerOutput:
        return LayerOutput(
            text=ctx.user_info,
            cache_key=f"{ctx.user_id or '-'}:{_short_digest(ctx.user_info)}",
        )


def _short_digest(value: str) -> str:
    """Bound the key regardless of how long a member's focus line is."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]

"""The channel blocks built from the BOX — health, skills, the OS, the repo, GWS.

Created: 2026-08-03 (PA-7a, feat/prompt-assembler-channel).

Five of the channel path's fifteen blocks, and they are exactly the five
``AgentContextBuilder.build_system_prompt`` had to wrap in ``try/except``:
health, skills, the atlas primer, AGENTS.md and the Google Workspace guidance.
That is not a coincidence and it is why they share a module. Each one reaches
for a loader that can be absent, stale or broken — a health engine that has not
started, a skills directory that does not exist, a corrupt atlas seed, an
unreadable AGENTS.md, an MCP config that fails to parse — and none of those may
cost the user their turn.

THE FIVE ``except`` CLAUSES ARE GONE, REPLACED BY ONE.
:func:`~pocketpaw.prompt.assembler.assemble` guards every ``layer.render()``: a
raising layer is logged with a traceback, contributes no text, and lands in
``AssembledPrompt.dropped``. Four of the five old handlers were ``logger.debug``
or a bare ``pass``, so this is strictly more visible than what it replaces —
``pass  # AGENTS.md failure never breaks prompt building`` swallowed the
exception type, the message and the traceback. It is also broader: the old
handlers wrapped only the block they were written for, whereas the guard covers
whatever a layer does. Pinned by
``tests/test_channel_prompt_layers.py::test_a_raising_environment_layer_does_not_fail_the_turn``,
which drives a real exception through each of the five and asserts the other
fourteen blocks still assemble.

ONE BEHAVIOURAL DIFFERENCE, AND IT IS INVISIBLE IN THE TEXT: the guard also
records the failed layer in the digest under a reserved failure key, which the
old code did not do (it had no digest). The channel path returns only
``AssembledPrompt.text``, so no channel byte moves; the effect is that if a
future channel caller ever reads ``stable_digest``, "the atlas failed" and "the
atlas rendered" are already different identities rather than the same one.

WHY THE ATLAS PRIMER'S BUILDER LIVES HERE. ``_build_atlas_primer`` was a
``@staticmethod`` on the builder whose only caller was the block it fed. Moving
it into the layer is what puts it under the render guard;
``AgentContextBuilder._build_atlas_primer`` stays as a one-line delegate because
``tests/atlas/test_primer_block.py`` calls it directly and the seed content it
pins is worth keeping under test at that name.

NOT the cloud path's :class:`~pocketpaw.prompt.atlas.AtlasPrimerLayer`, which
renders ``ctx.atlas_primer`` as plain data and is reserved for the day the cloud
path decides to pay ~1.5k chars a turn for the primer. Same block, same 2000-char
cap, two producers — the channel one builds it, the cloud one is handed it.
"""

from __future__ import annotations

import hashlib

from pocketpaw.prompt.channel.inputs import ChannelInputs
from pocketpaw.prompt.layer import LayerOutput, Priority, PromptContext

_NO_INPUTS = ChannelInputs()


def _inputs(ctx: PromptContext) -> ChannelInputs:
    return ctx.channel_inputs or _NO_INPUTS


def _nonblank(text: str) -> str:
    """``""`` unless there is something other than whitespace to say.

    See :mod:`pocketpaw.prompt.channel.request` — the new assembler skips only
    EMPTY text where the old one skipped whitespace-only content too.
    """
    return text if text.strip() else ""


def _short_digest(value: str) -> str:
    """16 hex chars of sha256 — the bound the rest of this package uses."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


class ChannelSkillsLayer:
    """What skills exist, so the agent stops recreating them."""

    name = "channel.skills_list"
    priority: Priority = Priority.MEDIUM
    max_chars: int | None = 2000  # _INJECTION_CAPS["skills_list"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        from pocketpaw.skills import get_skill_loader

        loader = get_skill_loader()
        skills = loader.get_all()
        # entity-rooms A2: when the entity pins a non-empty skill subset,
        # advertise ONLY those skills (the non-SDK equivalent of the SDK's
        # per-run materialized plugin). Empty / None → legacy all-skills
        # behavior, so every non-entity run is unchanged.
        skill_names = _inputs(ctx).skill_names
        if skill_names:
            skills = {n: s for n, s in skills.items() if n in skill_names}
        if not skills:
            return LayerOutput(text="", cache_key=_short_digest(""))

        skill_lines = []
        for s in skills.values():
            invocable = " (user-invocable)" if s.user_invocable else ""
            skill_lines.append(f"- **{s.name}**: {s.description}{invocable}")
        search_dirs = ", ".join(str(p) for p in loader.paths)
        block = (
            "\n# Available Skills\n"
            "The following skills have been created and are available. "
            "Do NOT recreate them or forget they exist.\n"
            + "\n".join(skill_lines)
            + f"\n\nSkills directories: {search_dirs}"
        )
        return LayerOutput(text=_nonblank(block), cache_key=_short_digest(block))


class ChannelAtlasPrimerLayer:
    """The Paw OS primer — what the OS is and what its words mean here."""

    name = "channel.atlas_primer"
    priority: Priority = Priority.MEDIUM
    # ~500 tokens, from ``_INJECTION_CAPS["atlas_primer"]``. Same number the
    # cloud-side ``AtlasPrimerLayer`` carries, and for the same reason: it has
    # bounded this exact block on the channel path since 2026-04-01.
    max_chars: int | None = 2000

    async def render(self, ctx: PromptContext) -> LayerOutput:
        primer = build_atlas_primer()
        return LayerOutput(text=_nonblank(primer), cache_key=_short_digest(primer))


class ChannelAgentsMdLayer:
    """The target repo's AGENTS.md constraints."""

    name = "channel.agents_md"
    priority: Priority = Priority.MEDIUM
    max_chars: int | None = 3000  # _INJECTION_CAPS["agents_md"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        agents_md_dir = _inputs(ctx).agents_md_dir
        if not agents_md_dir:
            return LayerOutput(text="", cache_key=_short_digest(""))

        from pocketpaw.agents_md import AgentsMdLoader

        agents_md = AgentsMdLoader().find_and_load(agents_md_dir)
        if not agents_md:
            return LayerOutput(text="", cache_key=_short_digest(""))
        block = agents_md.constraints_block
        return LayerOutput(text=_nonblank(block), cache_key=_short_digest(block))


class ChannelGwsLayer:
    """Google Workspace CLI guidance, when that MCP server is actually active."""

    name = "channel.gws_instructions"
    priority: Priority = Priority.MEDIUM
    max_chars: int | None = 1000  # _INJECTION_CAPS["gws_instructions"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        block = load_gws_instructions()
        return LayerOutput(text=_nonblank(block), cache_key=_short_digest(block))


class ChannelHealthLayer:
    """The health engine's section — present ONLY while something is wrong."""

    name = "channel.health_state"
    priority: Priority = Priority.LOW
    max_chars: int | None = 300  # _INJECTION_CAPS["health_state"]

    async def render(self, ctx: PromptContext) -> LayerOutput:
        from pocketpaw.health import get_health_engine

        block = get_health_engine().get_health_prompt_section()
        return LayerOutput(text=_nonblank(block or ""), cache_key=_short_digest(block or ""))


def build_atlas_primer() -> str:
    """Build the compact always-on "Paw OS primer" block (AT-3).

    Moved verbatim from ``AgentContextBuilder._build_atlas_primer`` (PA-7a), so
    it renders inside a layer and under the assembler's render guard. Three
    parts, ~500 tokens hard ceiling (enforced twice: the seed keeps each line
    short, and ``ChannelAtlasPrimerLayer.max_chars`` caps the rendered block at
    2000 chars):

    1. One-paragraph OS identity ("you run inside paw-os ...").
    2. One line per primitive — name + gist, generated at build time from the
       atlas store so a seed edit can never drift from the prompt. Prefer the
       entry's authored ``gist`` (a complete, self-contained one-liner that ends
       on a full clause). Only when a primitive has no ``gist`` fall back to a
       clause-aware truncation of ``summary`` — cut at the last full word before
       the cap so a line never dangles mid-phrase (the old fixed-108-char cut
       dropped load-bearing words like Belt's "Instinct gate" and Branch's
       "review/merge/publish").
    3. The standing instruction: ``atlas_search`` before guessing about OS
       capabilities, and include the ``surface`` route when pointing a user
       somewhere.

    Returns "" when the store has no primitives. RAISES on store load failure —
    the assembler's render guard is what keeps that from breaking the turn,
    where the caller's ``try/except`` used to.
    """
    from pocketpaw.atlas.store import get_atlas_store

    primitives = [e for e in get_atlas_store().entries if e.kind == "primitive"]
    if not primitives:
        return ""

    lines: list[str] = []
    for entry in primitives:
        gist = (entry.gist or "").strip().rstrip(".")
        if not gist:
            # No authored gist — fall back to the summary's first clause, cut
            # clause-aware at the last full word before the cap so the line
            # still ends cleanly rather than mid-phrase.
            gist = entry.summary.split(";")[0].strip().rstrip(".")
            if len(gist) > 110:
                gist = gist[:108].rsplit(" ", 1)[0]
                if gist.count("(") > gist.count(")"):
                    gist = gist[: gist.rindex("(")]
                gist = gist.rstrip(" ,.:;(") + "…"
        suffix = "" if gist.endswith("…") else "."
        lines.append(f"- {entry.name}: {gist}{suffix}")

    return (
        "\n# Paw OS Primer\n"
        "You run inside paw-os, an agentic workspace OS. Its primitives "
        "carry paw-specific meanings (a Pocket is a workspace app, not "
        "clothing), and users see results on frontend surfaces — routes "
        "like /sites.\n\n"
        "OS primitives:\n" + "\n".join(lines) + "\n\n"
        "Before guessing whether the OS can do something or which "
        "primitive fits, call atlas_search with your intent, then "
        "atlas_describe on the best id. When an action has a home "
        "surface, tell the user where to see it by route (e.g. "
        '"see it at /sites") — entries carry it in their `surface` field.'
    )


def load_gws_instructions() -> str:
    """Load GWS CLI guidance if the google-workspace MCP server is active.

    Moved from ``AgentContextBuilder._load_gws_instructions`` (PA-7a). ``gws.md``
    still ships beside the bootstrap package, so the directory is resolved from
    the installed ``pocketpaw`` package — see
    ``request.load_channel_instructions`` for why not ``__file__``.
    """
    from pocketpaw.mcp.config import load_mcp_config

    configs = load_mcp_config()
    gws_active = any(c.name == "google-workspace" and c.enabled for c in configs)
    if not gws_active:
        return ""

    from pocketpaw.prompt.channel.request import _bootstrap_dir

    path = _bootstrap_dir() / "gws.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()

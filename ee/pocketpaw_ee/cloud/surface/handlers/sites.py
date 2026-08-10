# sites.py — /sites surface preamble.
#
# Created: 2026-06-02 — Orients the chat agent when the user is on the /sites
# surface (the Paw Sites gallery + describe-to-create rail). Without it the
# surface fell back to GENERIC and the agent built + talked "pocket" instead of
# a publishable website (the operator-reported "agent builds pockets not sites"
# drift). Static orientation — no live data to fake.
#
# Updated: 2026-06-03 (pm) — Point the procedure back at the `pocketpaw-create-site`
# skill now that bundled skills actually load on the SDK backend. The earlier
# note here ("skills can't load under setting_sources=[]") is obsolete: the
# claude_agent_sdk backend now loads the bundled skills as a Claude Code local
# plugin via the SDK `plugins=` option (see `bundled_skills_plugin_dir` +
# `settings.sdk_load_bundled_skills`), so the agent can invoke the skill by
# natural-language intent — no slash command, no setting_sources change. The
# skill carries the full create→publish flow (build the source pocket via
# create-pocket, publish via the sites-manager tool, surface the live URL, relay
# errors), so the preamble PREFERS it and keeps the raw MCP tools only as a
# fallback for when the skill is unavailable (e.g. sdk_load_bundled_skills off).
# The rail still sends the user's description as PLAIN TEXT; intent invocation
# does not need a slash.
# Updated: 2026-06-03 (feat/sites-landing-brain) — Point the preamble at the new
# `pocketpaw-create-paw-site` marketing brain (the dedicated landing-page
# author) instead of the generic create-site path, and stamp the source pocket
# `type="site"` + `pattern="landing"`. Dropped the `form-layout` lead-form nudge:
# the `form`/`newsletter` widgets emit a nested `<form>` that is invalid inside
# the static site template's outer POST form, so the published page captured zero
# leads (the broken "Option A" render). The lead form must be FLAT native
# `input`/`button{type:submit}` with real `name=`. create-site Path A (publish an
# existing pocket) is unchanged; only a brand-new-site description routes here.
# Updated: 2026-06-04 (feat/sites-refine-surface) — The /sites surface now has
# TWO modes. The gallery (no pocket_id) keeps the create-a-new-site preamble
# above. The per-site refine chat at /sites/[siteId] stamps `pocket_id` (the
# site's source pocket) + `site_id` in the surface meta; when `pocket_id` is
# present, `build_preamble` branches to a LANDING-AWARE REFINE preamble that
# tells the agent to EDIT the existing published pocket via
# `mcp__pocketpaw_pocket_specialist__edit` — never rebuild from scratch, never
# treat it as a dashboard pocket — while preserving the landing structure and
# the same 5 SSR rules the create brain enforces. Refine-mode rules mirror
# `src/pocketpaw/bundled_skills/_bundled/skills/pocketpaw-create-paw-site/SKILL.md`.
# Updated: 2026-06-04 (feat/sites-svelte-engine) — the CREATE branch now forks on
# `meta.engine` ("ripple" | "svelte"), set by the /sites create UI's "Use Svelte
# pages" toggle. `engine="svelte"` returns a parallel create preamble that
# PREFERS the `pocketpaw-create-svelte-site` skill (the Svelte-track authoring
# brain — it writes hand-written SvelteKit components, no rippleSpec/catalog) and
# points the MCP fallback at `mcp__pocketpaw_sites_manager__create_svelte_site`.
# Every other engine value (None / "ripple") keeps the existing ripple marketing
# brain (`pocketpaw-create-paw-site` + create_landing_site) byte-for-byte. The
# refine branch (keyed on `pocket_id`) is untouched by the toggle.
# Updated: 2026-07-06 (feat/sites-crew-create-flow, SC-crew) — the CREATE branch
# now optionally runs a guided two-phase authoring-crew flow behind the
# `settings.sites_crew_enabled` flag (default OFF). When the flag is ON, a create
# meta returns `_crew_create_preamble`: PHASE 1 assesses the request's clarity
# and interviews the user (one round of 3–5 questions) when it is vague, with a
# "just build it" escape hatch; PHASE 2 retrieves a design system
# (`mcp__pocketpaw_design_systems__*`), pulls real assets (stock/icons/palette
# MCP tools), states a one-line brief, and BUILDS via the same engine skill the
# single-shot path uses (`pocketpaw-create-svelte-site` on svelte,
# `pocketpaw-create-paw-site` on ripple) — the skill still owns the SSR page
# assembly, the crew preamble only feeds it the design system, sections, copy,
# and assets. When the flag is OFF, the create branch returns
# `_create_preamble(meta)` byte-for-byte (no behaviour change). The refine/chat
# branches (keyed on `pocket_id`) are untouched.
# Updated: 2026-07-06 (feat/sites-crew-taste, SC-TASTE) — the crew svelte
# `build_step` now instructs the agent to author the components with the bundled
# `pocketpaw-design-taste` skill (premium, anti-slop, static-safe styling) on top
# of the design system's tokens. Svelte-only: the ripple `build_step` is unchanged.
# Updated: 2026-07-06 (feat/sites-crew-frontend-brief, SC-2) — added
# `_frontend_preamble(meta, brief)`, the ADDITIVE brief-driven twin of
# `_create_preamble`. It renders the Frontend stage's build instructions FROM a
# structured `DesignBrief` (the crew's baton): it walks the ordered `sitemap`,
# injects the matching `copy` blocks + real `asset_manifest` imagery, threads the
# branding design-system tokens/voice, and ROUTES by `brief.engine` ("svelte" →
# `create_svelte_site`; "ripple" → `create_landing_site` / the pocket specialist,
# noting `create_dynamic_site` for `pattern="dynamic"`). It preserves the same
# static-site (SSR) rules the create/refine preambles enforce. `build_preamble`'s
# live create/refine dispatch is UNCHANGED — `_frontend_preamble` is exercised by
# tests now and wired into the flow later by the orchestration slice (SC-9) behind
# a flag (SC-11); it is exported in `__all__` so that slice can import it.
# Updated: 2026-07-04 (feat/sites-chat-mode, CHAT-BE) — the refine branch (keyed
# on `pocket_id`) now forks on `meta.mode` ("build" default | "chat"), set by the
# /sites/[siteId] Build/Chat toggle. `mode="chat"` routes to `_chat_preamble`, a
# NO-MUTATION Q&A preamble: the user is asking a QUESTION about the existing site;
# ANSWER helpfully but DO NOT call `pocket_specialist__edit`, do NOT modify or
# republish the site, and do NOT create pockets. `mode="build"` (or unset) keeps
# `_refine_preamble` byte-for-byte (today's mutate-and-republish behavior). The
# create branch (no `pocket_id`) ignores `mode`.
# Updated: 2026-07-14 (fix/sites-unified-create-preamble) — collapsed the THREE
# create preambles (`_create_preamble`, `_svelte_create_preamble`,
# `_crew_create_preamble`) + the `sites_crew_enabled` flag gate into ONE always-on
# `_create_preamble`. The clarity gate (Phase 1: assess → ask_user chips when
# vague, "just build it" escape hatch) and the design phase (Phase 2:
# design-system → palette → real assets → brief) are engine-agnostic instructions,
# so they run for EVERY create regardless of engine — this fixes the bug where a
# flaky flag left the agent on the no-questions single-shot preamble and it built a
# default design without asking. Only the final BUILD step forks on `meta.engine`:
# "html" (the NEW DEFAULT when engine is unset) → hand-authored static HTML/CSS via
# `create_html_site`; "svelte" → hand-written components via `create_svelte_site`;
# "ripple" → the widget landing spec via the pocket specialist (the one engine that
# does NOT author markup by hand). `_crew_enabled()` and the two removed preambles
# are gone; `sites_crew_enabled` in config is now vestigial. The refine/chat
# branches (keyed on `pocket_id`) are untouched.
#
# Updated: 2026-07-17 (feat/sites-draft-first-create) — the CREATE preamble is now
# DRAFT-FIRST. The create tools already persist a reviewable DRAFT (they never
# publish); the preamble used to tell the agent to publish in the same turn
# ("auto-publishes to a live URL", step 6 "PUBLISH and SHOW the live url", each
# engine build step "then publish"), which shipped a site live off a plain "create a
# landing page" — no draft, no preview, and on a paid tier an unasked checkout. Now
# the orientation frames the pocket as a draft the user previews; each engine build
# step STOPS at the draft; and the final step points the user at the in-app Preview
# (/sites), offers to publish, and calls `mcp__pocketpaw_sites_manager__publish` IN
# THE SAME TURN only when the user's request already asked to go live ("publish",
# "make it live", "ship it"). The publish tool is still NAMED (the on-request path),
# so nothing regresses for an explicit publish. Refine/chat branches untouched.
#
# Updated: 2026-08-02 (fix/concierge-tools-for-site-agent) — EVERY preamble in this
# module now carries `_CONCIERGE_NOTE`. The reported bug was that an agent building
# a site "sometimes does not know about concierge at all"; the cause was not a
# tool-access gate but a total awareness blackout — "concierge" appeared zero times
# in this file, zero times in the sites MCP tool descriptions, and zero times in the
# bundled skills, so the only channel through which the agent could learn a site
# ships with one was model sampling. That is what made it intermittent. The block is
# unconditional: it is appended by `_create_preamble` (all three engines),
# `_refine_preamble`, `_chat_preamble`, and `_frontend_preamble`, so no engine, mode,
# flag, or lazily-loaded tool-id import can gate it off. It states what the concierge
# is, that PUBLISHING (not drafting) provisions it, that it answers visitors from the
# site's own synced knowledge, and that it can always reach a human — and it names NO
# tool, because widget catalog/actions are owner-authored and configuring them from
# chat is a real gap, not a capability to imply. See `_CONCIERGE_NOTE` for the
# per-claim code citations.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — ``build_preamble``
# returns a ``SurfacePreamble`` keyed on the five ``meta`` fields the three
# sub-preambles read (route, pocket_id, site_id, engine, mode). No I/O happens
# here, and no site content is read — refine and chat echo the pocket id and
# never describe the page — so a key built from meta is EXACT, and editing a
# site correctly leaves it still. The sub-builders keep returning ``str``; only
# the entry point answers the key. (The refine half of that claim no longer
# holds — see the next entry.)
#
# Changes: 2026-08-11 (fix/sites-refine-preamble-engine-fork) — ``_refine_preamble``
# FORKS BY ENGINE. It was written for ripple and shipped to all four: on a react,
# html or svelte site it commanded ``pocket_specialist__edit`` (which mutates a
# rippleSpec those pockets do not have — their content is a ``source`` map),
# asserted "renders STATICALLY (no JavaScript runs for the visitor)" (react ships a
# hydrating client bundle by default), claimed "the site auto-publishes from its
# source pocket" (nothing auto-publishes — publish is a tool call and a paid-tier
# checkout), and then listed five SSR rules about ripple WIDGET shapes
# (``pricing-table``/``tiers``, the ``accordion`` ban, the ``form``/``newsletter``
# ban) at a page with no widgets at all. Per pocketpaw/CLAUDE.md, "The prompt may
# not command a tool the agent doesn't have" — and its "naming an existing-but-wrong
# tool is the same defect" clause is exactly this: the specialist IS reachable here
# (``pocketpaw_pocket_specialist`` rides ``ALWAYS_ALLOWED_MCP_SERVERS``), so the
# agent got an edit tool that could not touch its pocket and improvised silently.
#
# THE ENGINE IS READ FROM THE POCKET, NOT FROM ``meta``. ``meta.engine`` is a
# CREATE hint — the /sites gallery's preset picker sets it, and the refine
# SurfaceMetaProvider (paw-enterprise ``routes/sites/[siteId]/+page.svelte``) stamps
# only ``site_id`` / ``pocket_id`` / ``focus_node_id`` / ``mode``. Forking refine on
# ``meta.engine`` would therefore have put EVERY site on the ``or "html"`` default:
# a regression for ripple and no fix for react. So refine resolves the engine from
# its source pocket through the canonical ``sites/engines.py::normalize_engine``
# (the same value publish branches on), which makes this preamble the first on this
# surface to read live state — hence ``content_key`` for the refine branch, per
# ``_helpers.content_key``'s own rule. Create and chat still answer ``meta_key``.
# An unreadable pocket (deleted, cross-tenant, DB error) or an engine this module
# has no branch for lands on ``_refine_unknown_engine_step``, which names NO edit
# tool and tells the agent to identify the engine first — the one honest answer,
# and the reason a fifth engine in ``engines.py`` degrades instead of silently
# inheriting react's rules.
#
# Per-branch, only what is TRUE for that engine: ripple keeps its widget rules and
# the specialist edit path byte-for-byte (it was always correct there); svelte gets
# ``edit_svelte_component``, which STAGES A DRAFT PREVIEW rather than publishing
# (the module header above still says "and republishes" — stale since
# feat/sites-diff-edit); react gets the react component-edit tool and the prerender
# contract as ``pocketpaw-create-react-site/SKILL.md`` states it; html gets the
# truth that it has NO chat edit tool at all — ``create_html_site`` takes no
# ``pocket_id`` and would mint a SECOND site, and ``edit_svelte_component``'s guard
# is svelte-only by design ("html has no per-component model — it edits by uid
# splice (HE-9), not here"), so the branch says so instead of naming something.
# The publish claim is fixed on every branch and the queued-build wording is gated
# on ``sites/service.py::build_runs_async`` — react alone today — so svelte starts
# telling the truth on its own if #1913 flips it. ASK-DON'T-ASSUME and the
# ``ask-user-questions`` widget survive unchanged on every branch: refine keeps
# ``ripple_mode="on"`` for all engines (``surface_registry._sites_profile``), so
# that mechanism is real here even where create would have used ``ask_user`` chips.

# Updated: 2026-08-11 (feat/sites-react-edit-lane, RX-3) — the react track finally
# has an EDIT tool, and two preambles were telling the agent the wrong thing about
# changing a react site:
#   * `_create_preamble`'s react `build_step` now says that any change after the
#     create goes through `mcp__pocketpaw_sites_manager__edit_react_component` with
#     the same pocket_id, NOT a second `create_react_site`. That is the reported
#     bug: with no edit tool registered, a follow-up "shorten the hero headline"
#     had one available move, and it minted a SECOND site pocket while the site the
#     user was looking at stayed unchanged.
#   * `_refine_preamble` now routes a react site to the new
#     `_react_refine_preamble`. The existing refine preamble names
#     `mcp__pocketpaw_pocket_specialist__edit` and then enumerates five rules about
#     ripple WIDGETS; a react pocket has no rippleSpec, so on that engine the
#     instruction is not merely useless, it points at a merge with nothing to merge
#     into. Per pocketpaw/CLAUDE.md, naming an existing-but-WRONG tool is the same
#     defect as naming an absent one. The react preamble replaces the widget rules
#     with the prerender rule (the same hazard in React spelling) and the
#     src/+public/ write scope the tool actually enforces.
# The refine fork reads `meta.engine`, which is documented as the CREATE hint, so it
# only fires when the per-site chat client stamps it — see
# `_react_refine_preamble`'s docstring. Absent it, refine behaves exactly as before.
#
# Updated: 2026-08-11 (fix/sites-refine-preamble-engine-fork) — RX-3's own caveat
# above was the whole problem, and it is now closed: `meta.engine` is never stamped
# on the refine surface, so `_react_refine_preamble` rendered for no one. Rather
# than send the engine from the client (another repo, and wrong-until-deployed),
# the engine is resolved SERVER-SIDE off the source pocket and `_refine_preamble`
# forks on it for all four engines. `_react_refine_preamble` is FOLDED INTO that
# fork's react branch rather than kept beside it: two react refine preambles, one
# of them dead, is the drift the fork exists to prevent. Its content carries over
# — the edit tool, the reserved shell, the dependency list, the prerender rule, the
# draft framing — with three corrections. It used `mcp__pocketpaw_ask__ask_user`,
# but refine holds `ripple_mode="on"` on every engine, so the `ask-user-questions`
# widget is the mechanism this surface actually renders. It named
# `pocket_specialist__edit` inside its prohibition; the create preamble's react
# branch forbids "the pocket specialist" by concept without the id, and this now
# matches that. And it told the agent to relay the publish result, which on react
# is a QUEUED build whose url is empty on a first publish and the previous deploy
# on a re-publish — see `_refine_publish_step`. RX-3's create-side change (the
# react `build_step` naming the edit tool) is untouched.

from __future__ import annotations

import functools
import logging
from typing import Any

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import content_key, meta_key
from pocketpaw_ee.sites.react_paths import (
    REACT_AUTHORABLE_PREFIXES,
    REACT_RESERVED_FILES,
    REACT_RESERVED_PREFIX,
)
from pocketpaw_ee.sites_crew.models import DesignBrief

logger = logging.getLogger(__name__)

# --- The concierge block (fix/concierge-tools-for-site-agent) -----------------
#
# THE BUG: "the agent building sites sometimes does not know about concierge at
# all." It was not a tool-access gate — it was a total AWARENESS blackout. Before
# this constant, the string "concierge" appeared ZERO times in every preamble in
# this module, ZERO times in the sites MCP tool descriptions
# (``agent/mcp_servers/sites.py`` + ``sites_create.py``), and ZERO times anywhere
# in ``src/pocketpaw/bundled_skills/``. The publish tool's own result payload
# (``sites.py::_publish_handler``) returns only ``{ok, site:{id, pocket_id, name,
# url, deployed}}``, so not even a successful publish told the agent a bar had
# just been grown onto the pages it deployed.
#
# With no source at all, whether the agent brought up the concierge was decided
# by model sampling and by whatever the user happened to type. That is the whole
# explanation for "sometimes": the gate was in the sampler, not in the code. One
# always-present block replaces a coin flip with a fact.
#
# Every claim below is pinned to a code path, because a preamble that oversells
# trades a blind agent for a confidently wrong one:
#   * embedded on every published page — ``sites/service.py::_embed_concierge_bar``
#     injects the snippet into the built tree between build and deploy;
#   * provisioned at PUBLISH, not at draft — that same function is reached only on
#     a live publish (a preview returns from ``publish_pocket`` well before it),
#     and it funnels into ``paw_bar/agent_provisioning.py::ensure_site_widget`` →
#     ``ensure_site_agent``, the third of the three provisioning triggers;
#   * on by default — ``Site.concierge_enabled`` defaults True, and the embed
#     reads an absent doc as enabled so a FIRST publish still gets its bar;
#   * grounded in the site's own content — ``sites/kb_ingest.py``'s sync is
#     scheduled at publish and at bind, so the concierge answers from the pages;
#   * always able to fetch a human — ``agent/mcp_servers/pawbar.py`` registers the
#     built-in ``pawbar_request_human`` on EVERY concierge run bound to a widget,
#     declared actions or not.
#
# And the honest LIMIT, which is the reason this block names no tool: widget
# ``catalog`` and ``actions`` are owner-authored only. No agent tool declares
# them, on this surface or any other, so the block routes the user to the
# dashboard instead of promising something that would hard-error. The word
# "republish" is deliberately absent — the chat-mode preamble is a no-mutation
# surface and its test forbids that string.
_CONCIERGE_NOTE = (
    "<site-concierge>\n"
    "Every Paw Site we publish ships with a CONCIERGE: a Paw Bar chat widget "
    "embedded on the live page, backed by a dedicated agent that belongs to that "
    "one site. You do not build it and you do not wire it up — it is provisioned "
    "automatically the moment the site goes live (publishing is what creates it, "
    "so a draft does not have one yet), and it is ON by default for every site.\n"
    "What it does for the business: it answers their VISITORS' questions about "
    "the site, grounded in the site's own published content, which is synced into "
    "its knowledge automatically. Every concierge can also hand a conversation to "
    "a real person when a visitor asks for one.\n"
    "KNOW THIS AND SAY IT when it is relevant — when the user asks what happens "
    "after publishing, asks about chat / support / lead handling / answering "
    "visitors, or is deciding whether to go live. It is a real part of the "
    "deliverable, so never tell a user their site has no chat or no way to "
    "capture a visitor's question.\n"
    "WHAT YOU CANNOT DO: the greeting, the product catalog, and the concierge's "
    "actions are owner-authored — the user sets them from the site's settings in "
    "the dashboard. You have no tool for any of that, so point them at the "
    "dashboard rather than offering to configure it from chat, and never claim you "
    "changed a concierge setting.\n"
    "</site-concierge>"
)


# --- Engine resolution, shared by the create fork and the refine fork ---------
#
# ONE tuple and ONE normalizer so the two forks cannot drift: a fifth engine added
# here has to be given a branch in both, and until it is, both fall back visibly
# rather than silently instructing the wrong track.
#
# The two forks take DIFFERENT defaults, and that is the point of the parameter
# rather than a wart. Create is choosing an engine for a page that does not exist
# yet, so an unrecognized hint means "build the default" — html. Refine is
# describing a page that ALREADY exists, so there is no sensible default: guessing
# hands the agent the wrong edit tool. Refine passes what the pocket stored and
# treats anything unrecognized as unknown (see ``_refine_engine``).
_SITE_ENGINES: tuple[str, ...] = ("html", "svelte", "ripple", "react")


def _preamble_engine(raw: str | None, *, default: str) -> str:
    """Normalize an engine value for a preamble fork. Never raises.

    Mirrors ``sites/engines.py::normalize_engine``'s never-raise policy (an
    unusable engine string must not break chat) while keeping the create fork's
    html default, which differs from that module's ripple one.
    """
    engine = (raw or default).lower()
    return engine if engine in _SITE_ENGINES else default


async def _refine_engine(pocket_id: str, user_id: str, workspace_id: str) -> str | None:
    """Resolve which engine authored the site being refined. ``None`` if unknowable.

    Read from the SOURCE POCKET, not from ``meta``: ``meta.engine`` is a create-time
    hint that the /sites gallery's preset picker sets and the refine surface never
    stamps at all, so a fork on it would put every refine on the create default.
    The pocket's own ``engine`` is what publish branches on, so a preamble keyed to
    it describes the page the user is actually looking at.

    Normalized through the canonical ``sites/engines.py::normalize_engine`` rather
    than ``_preamble_engine`` so this agrees with the publish path by construction —
    including its legacy default: a pocket predating the field reads ``"ripple"``,
    which is exactly the branch such a site has always been given.

    Returns ``None`` on any failure, which the caller renders as an explicit
    "identify the engine first" instruction. The catch is broad on purpose (the same
    shape ``handlers/pocket.py::_load_pocket`` uses): NotFound, Forbidden and a
    dropped connection all mean the same thing here — we cannot say, so we must not
    claim. A preamble is never worth breaking a chat turn over.

    Tenancy: ``pockets_service.get`` gates by owner / shared_with / visibility, NOT
    by workspace, so a user in two workspaces could stamp a pocket from B in a chat
    in A. Rejecting a workspace mismatch keeps the engine (and with it the tool the
    prompt names) from being read out of the wrong tenant.
    """
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket = await pockets_service.get(pocket_id, user_id)
    except Exception:  # noqa: BLE001 — see the docstring: never break a turn
        logger.debug("sites_handler: engine lookup for %s failed", pocket_id, exc_info=True)
        return None
    if pocket.get("workspace") != workspace_id:
        logger.warning(
            "sites_handler: workspace mismatch for pocket %s (chat=%s, pocket=%s); "
            "refusing to read its engine",
            pocket_id,
            workspace_id,
            pocket.get("workspace"),
        )
        return None
    try:
        from pocketpaw_ee.sites.engines import normalize_engine

        return normalize_engine(pocket.get("engine"))
    except Exception:  # noqa: BLE001
        logger.debug("sites_handler: normalize_engine unavailable", exc_info=True)
        return None


def _publish_runs_async(engine: str) -> bool:
    """Does publishing this engine QUEUE its build instead of running it inline?

    Delegates to ``sites/service.py::build_runs_async`` — the predicate the publish
    path itself branches on — so this prompt cannot claim a synchronous publish for
    an engine that has since moved to the queue (static svelte is the live
    candidate). Hardcoding ``engine == "react"`` here would be a second copy of a
    fact that is actively moving.

    An import failure answers TRUE, deliberately. The two wordings are not
    symmetric: telling the agent to report a queued build when the publish was
    actually inline costs the user one extra click to see a url, while telling it to
    show a url when the build is still queued makes it announce a change that is not
    live yet — on a first publish there is no url at all, and on a re-publish the url
    serves the PREVIOUS page. Degrade toward the claim that cannot be false.
    """
    try:
        from pocketpaw_ee.sites.service import build_runs_async

        return build_runs_async(engine)
    except Exception:  # noqa: BLE001
        logger.debug("sites_handler: build_runs_async unavailable", exc_info=True)
        return True


@functools.lru_cache(maxsize=1)
def _design_taste_system() -> str:
    """Return the ``pocketpaw-design-taste`` SKILL.md body (frontmatter stripped)
    wrapped as a governing ``<design-system>`` block.

    PERMANENT FIX (2026-07-14): the sites build agent was not reliably INVOKING
    the design-taste skill (skill invocation is model-driven and probabilistic,
    and the SDK backend can auto-select a weaker model that skips it), so sites
    came out generic. On this surface, design quality IS the job — so instead of
    hoping the model calls the skill, we EMBED the full 2026 Creative Director
    system straight into the create preamble. It is read once from the bundled
    skill file (the SKILL.md stays the single source of truth) and cached. A
    missing file degrades to a short inline directive, never a crash.
    """
    try:
        from pocketpaw.bundled_skills import bundled_skills_plugin_dir

        base = bundled_skills_plugin_dir()
        if base is not None:
            md = (base / "skills" / "pocketpaw-design-taste" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            body = md
            if md.startswith("---"):
                parts = md.split("---", 2)
                if len(parts) == 3:
                    body = parts[2].strip()
            return (
                '<design-system name="pocketpaw-design-taste">\n'
                "This 2026 Creative Director system GOVERNS every site you build "
                "on this surface. It is ALREADY LOADED here — apply it in full; "
                "you do NOT need to invoke any skill to use it.\n\n"
                f"{body}\n"
                "</design-system>"
            )
    except Exception:  # noqa: BLE001 — never let a read error break the preamble
        pass
    return (
        '<design-system name="pocketpaw-design-taste">\n'
        "Invoke the `pocketpaw-design-taste` skill and follow it in full.\n"
        "</design-system>"
    )


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the /sites surface preamble.

    Modes, keyed on the meta:

    * **Create** (no ``pocket_id``) — the /sites gallery / describe-to-create
      rail. Build a brand-new marketing site as a reviewable DRAFT; publish only
      when the user explicitly asks (draft-first — see ``_create_preamble``).
    * **Chat** (``pocket_id`` present AND ``mode == "chat"``) — the per-site
      chat with the Build/Chat toggle set to Chat. Answer QUESTIONS about the
      existing site with NO mutation: never edit, republish, or create a pocket.
    * **Refine / Build** (``pocket_id`` present, ``mode`` "build" or unset) —
      the per-site chat at ``/sites/[siteId]``. Refine the EXISTING published
      site by editing its source pocket in place; never rebuild from scratch.

    TWO KEYING RULES, because the modes no longer read the same amount:

    * Create and chat are pure functions of ``meta`` plus a process-cached read
      of the design-taste SKILL.md. Nothing about the SITE is read — chat echoes
      the ``pocket_id`` and never describes the page's contents — so ``meta_key``
      is exact rather than a concession, and editing a site correctly leaves the
      key still: the preamble says the same thing before and after.
    * Refine reads the pocket to learn which ENGINE authored the site, because
      the instructions it renders (which edit tool exists, whether JavaScript
      runs for the visitor, whether a publish returns a usable url) are different
      facts per engine and ``meta`` does not carry the engine on this surface. So
      it answers ``content_key`` — the digest of what was actually rendered,
      which is the honest key the moment a handler reads live state. It moves
      when the rendered instructions move and not otherwise: editing the SITE
      does not shift it (the text describes the track, never the content), while
      a pocket that became unreadable does, which is correct — that turn's
      preamble genuinely says something different.

    The sub-builders keep returning ``str``; only the entry point answers the key.
    """
    if meta.pocket_id:
        if meta.mode == "chat":
            text = _chat_preamble(meta)
        else:
            # The one read on this surface. Its failure mode is a preamble that
            # says "identify the engine first", never a failed turn.
            engine = await _refine_engine(meta.pocket_id, user_id, workspace_id)
            refine_text = _refine_preamble(meta, engine)
            return SurfacePreamble(
                text=refine_text,
                cache_key=content_key("sites", refine_text),
            )
    else:
        text = _create_preamble(meta)
    # The five inputs the two meta-only sub-preambles read, all off ``meta``.
    # ``mode`` and ``engine`` are in there because they pick which preamble was
    # rendered at all — toggling Build/Chat on the same site is a different prompt.
    return SurfacePreamble(
        text=text,
        cache_key=meta_key(
            "sites", meta.route_path, meta.pocket_id, meta.site_id, meta.engine, meta.mode
        ),
    )


def _create_preamble(meta: SurfaceMeta) -> str:
    """The /sites CREATE preamble — the single guided authoring flow for a
    brand-new site, across ALL engines.

    ONE preamble, always on (no feature flag): the clarity gate and the design
    phase are engine-agnostic instructions, so they apply whether the deliverable
    is plain HTML, SvelteKit, or a ripple widget spec. Only the final BUILD step
    forks on ``meta.engine``:

    * ``"html"`` (and the DEFAULT when no engine is set) — a plain static
      HTML/CSS bundle you hand-author → ``create_html_site``. No framework, no
      build step; publishing serves ``index.html`` directly on the edge.
    * ``"svelte"`` — hand-written SvelteKit components → ``create_svelte_site``.
    * ``"react"`` — hand-written React components → ``create_react_site``. A Vite
      SSG: the build prerenders ``<App />`` to static HTML, and the site ships no
      JavaScript unless the create declares ``interactive``.
    * ``"ripple"`` — a ripple widget landing spec via the pocket specialist →
      ``create_landing_site``. The ONE engine that does not author markup by hand.

    Phase 1 assesses the request and, when it is vague, asks ONE round of
    high-value questions via the ``ask_user`` chips (with a "just build it"
    escape hatch); Phase 2 picks a design system, gathers real assets, states a
    one-line brief, then builds via the engine-appropriate tool above.

    DRAFT-FIRST (feat/sites-draft-first-create): the create tools persist a
    reviewable DRAFT and do NOT publish, so the build step stops at the draft.
    The final step points the user at the in-app Preview (/sites) and offers to
    publish; it calls the publish tool IN THE SAME TURN only when the user's
    request already asked to go live ("publish", "make it live", "ship it").
    Publishing deploys to the public edge (and can open a paid checkout), so it
    is the user's call — not an automatic step off a plain "create a site".
    """
    route = meta.route_path or "/sites"
    # RX-2 adds "react". Every engine named here MUST have a create tool the
    # /sites agent can actually reach — the build step below names one, and a
    # preamble that commands an absent tool does not error, it makes the model
    # improvise (pocketpaw/CLAUDE.md, "The prompt may not command a tool the agent
    # doesn't have"). react's tool is ``create_react_site``, registered on the
    # sites_manager server and carried into the surface allow-list by
    # ``SITES_TOOL_IDS``. An unknown engine still falls back to html — shared with
    # the refine fork via ``_preamble_engine`` so the two engine lists are one list.
    engine = _preamble_engine(meta.engine, default="html")
    engine_attr = f' engine="{engine}"'

    if engine == "svelte":
        engine_note = (
            " On this track the page is authored as hand-written SvelteKit "
            "components and PRERENDERED to static HTML (no JavaScript runs for the "
            "visitor on first paint)."
        )
        build_step = (
            "BUILD via the `pocketpaw-create-svelte-site` skill — invoke it by "
            "intent (no slash command). YOU write premium hand-written SvelteKit "
            "components (Hero, Pricing, Faq, …) at the design quality bar, "
            "authoring them per the embedded `pocketpaw-design-taste` design "
            "system for premium, non-generic styling on top of the design "
            "system's tokens, "
            "THEME them with those tokens + your asset URLs, and it persists the "
            'source pocket `type="site"` + `pattern="landing"` + `engine="svelte"` '
            "as a reviewable DRAFT — it does NOT publish (see the DRAFT-FIRST "
            "step). There is NO rippleSpec and NO widget catalog on "
            "this track — do not draft a rippleSpec or call the pocket "
            "specialist. If the skill is unavailable, author the source map "
            "yourself and call `mcp__pocketpaw_sites_manager__create_svelte_site` "
            "(then STOP at the draft — publish only on explicit request)."
        )
    elif engine == "react":
        engine_note = (
            " On this track the page is authored as hand-written React components "
            "and PRERENDERED to static HTML at build time — the copy is in "
            "view-source before any JavaScript runs."
        )
        build_step = (
            "BUILD via the `pocketpaw-create-react-site` skill — invoke it by "
            "intent (no slash command). YOU write premium hand-written React "
            "components (Hero, Pricing, Faq, …) at the design quality bar, "
            "authoring them per the embedded `pocketpaw-design-taste` design "
            "system for premium, non-generic styling on top of the design "
            "system's tokens, THEME them with those tokens + your asset URLs, and "
            "assemble a `source` map rooted at `src/App.tsx` (the composition "
            "root; sections under `src/components/*.tsx`). The build shell is "
            "GENERATED and reserved — your map may NOT write `index.html`, "
            "`package.json`, `vite.config.ts`, `paw-prerender.mjs`, or anything "
            "under `src/paw/`. The project has react, react-dom and vite and "
            "NOTHING else — no router, no CSS framework, no state or animation "
            "library, no way to add dependencies — and it is ONE page. Persist it "
            "with `mcp__pocketpaw_sites_manager__create_react_site`, which stamps "
            'the source pocket `type="site"` + `pattern="landing"` + '
            '`engine="react"` as a reviewable DRAFT — it does NOT publish (see the '
            "DRAFT-FIRST step). PASS `interactive=true` WHENEVER any component "
            "needs the browser — a mobile-menu toggle, tabs, a counter, any "
            "onClick/onChange or useEffect. The site ships ZERO JavaScript "
            "otherwise, so an unflagged interactive component renders correctly "
            "and then does nothing; the failure is silent. Leave it off only for a "
            "purely static page (CSS-only hover/keyframe motion, anchors, a native "
            "form POST — none of those need it). PRERENDER RULE: every component "
            "must render its resting/final state in its RETURNED MARKUP — "
            "`useEffect` does not run at prerender time, so a count-up initialized "
            "to 0 bakes '0'; initialize it to the final value. There is NO "
            "rippleSpec and NO widget catalog on this track — do not draft a "
            "rippleSpec or call the pocket specialist, and there is no "
            "`/api/submit` route (no server runtime), so a lead form is a native "
            "`<form>` with flat named fields.\n"
            "CHANGES GO THROUGH THE EDIT TOOL. Once the site exists, ANY further "
            "change the user asks for in this conversation — 'shorten the hero "
            "headline', 'darker pricing cards', 'add a testimonials section' — is "
            "`mcp__pocketpaw_sites_manager__edit_react_component` with the SAME "
            "pocket_id, NOT a second `create_react_site` call. Calling create again "
            "mints a SECOND site and leaves the one the user is looking at "
            "unchanged. Send a targeted `edits` diff for a small change; to add a "
            "section, call it with `create=true` for the new "
            "`src/components/<Name>.tsx` and then again with `edits` on "
            "`src/App.tsx` to render it. The edit saves to the DRAFT — it does not "
            "publish, so keep offering the Preview rather than announcing a live "
            "change."
        )
    elif engine == "ripple":
        engine_note = " The page is rendered STATICALLY (no JavaScript runs for the visitor)."
        build_step = (
            "BUILD via the `pocketpaw-create-paw-site` skill — invoke it by "
            "intent (no slash command). It composes the page by conversion role "
            'and stamps the source pocket `type="site"` + `pattern="landing"`. '
            "THEME the page with the chosen design system's tokens + your asset "
            "URLs. The lead-capture form must be FLAT native "
            '`input`/`textarea`/`button{type:"submit"}` widgets with real field '
            "names (name, email, phone, message) — NEVER the `form` or "
            "`newsletter` widget, which nests an invalid `<form>` on a static "
            "site and captures zero leads. If the skill is unavailable, fall back "
            "to `mcp__pocketpaw_pocket_specialist__create` (build the "
            "conversion-ordered ripple landing spec, then STOP at the draft — "
            "publish only on explicit request per the DRAFT-FIRST step). This is "
            "the ripple/widget "
            "track — the page is a widget spec, not hand-authored markup."
        )
    else:  # html (default)
        engine_note = (
            " On this track YOU hand-author the page as a premium static HTML/CSS "
            "bundle — no framework and no build step; publishing serves your "
            "`index.html` directly on the edge."
        )
        build_step = (
            "BUILD the page yourself as a PREMIUM, hand-crafted static HTML/CSS "
            "bundle — this is the DEFAULT engine and the deliverable for this "
            "create. Author a root `index.html` (plus `styles.css`, and only the "
            "vanilla JS a static page genuinely needs) at the HIGHEST design "
            "quality bar — treat HTML/CSS as first-class, not a lesser option; a "
            "hand-written static page can look every bit as premium as a "
            "component build. THEME it with the chosen design system's tokens + "
            "your asset URLs, then persist it by calling "
            "`mcp__pocketpaw_sites_manager__create_html_site` with the `source` "
            'map (it stamps the source pocket `type="site"` + `pattern="landing"` '
            '+ `engine="html"`), then STOP at the draft — publish only on '
            "explicit request per the DRAFT-FIRST step. There is NO rippleSpec and NO widget "
            "catalog on this track — do not draft a rippleSpec or call the pocket "
            "specialist; the HTML files ARE the page. The lead-capture form must "
            "be a real `<form>` with FLAT named `input`/`textarea` fields (name, "
            'email, phone, message) and a `button type="submit"`. `index.html` '
            "MUST contain the full resting state in markup (never rendered only "
            "by JS), since the visitor is served static HTML.\n"
            "DO NOT switch engines. You MUST use "
            "`mcp__pocketpaw_sites_manager__create_html_site` — do NOT call "
            "`create_svelte_site`, `create_landing_site`, or the "
            "`pocketpaw-create-svelte-site` / `pocketpaw-create-paw-site` skills, "
            "and do NOT reach for Svelte just because you want a premium result "
            "(build premium HTML instead). The ONLY exceptions, by what the user "
            "literally asked for: the word 'Svelte' → "
            "`mcp__pocketpaw_sites_manager__create_svelte_site`; the word 'React' "
            "→ `mcp__pocketpaw_sites_manager__create_react_site` (and pass "
            "`interactive=true` if any component you write needs the browser); a "
            "live-data / dynamic app (dashboards, per-user data) → "
            "`mcp__pocketpaw_sites_manager__create_dynamic_site`. A described "
            "business, a desire for a 'nice' or 'modern' site, or the design "
            "direction the user picked is NOT such a request — stay on HTML."
        )

    # ASK MECHANISM — used ONLY when the agent genuinely needs a real-world FACT
    # it cannot infer (never for the visual theme, which it decides itself). On
    # the ripple/html engines inline ripple is ON (surface_registry.
    # _sites_profile), so it renders an `ask-user-questions` ripple widget (a
    # ```ui-spec block whose completeActions emit chat.send); on the hand-authored
    # component tracks ripple is OFF, so it uses the `ask_user` MCP tool (chips).
    #
    # This MUST agree with ``_sites_profile``: the branch below tells the agent
    # which mechanism it has, and being wrong is the failure mode
    # pocketpaw/CLAUDE.md describes — a ui-spec block emitted on a ripple-off
    # surface is not an error, it is a fenced code block the user reads as raw
    # JSON. react joins svelte here (RX-2) because it hand-authors markup and its
    # profile drops ripple for the same reason svelte's does.
    ripple_on = engine not in ("svelte", "react")
    if ripple_on:
        ask_mechanism = (
            "render an `ask-user-questions` ripple widget — a ```ui-spec fenced "
            "block using the {version, ui} envelope (NOT plain text, NOT a tool "
            'call): `{"version": 1, "ui": {"type": "ask-user-questions", "props": '
            '{"questions": [{"title": "<your question>", "options": [{"title": '
            '"<option>"}]}], "completeActions": {"action": "emit", "target": '
            '"chat.send"}}}}` — then STOP and wait for the click'
        )
    else:
        ask_mechanism = (
            f"call the `mcp__pocketpaw_ask__ask_user` tool ({engine}-create has "
            "inline ripple OFF) with a one-line `question` and 3–5 short "
            "`options`, then STOP and wait for the click"
        )

    return (
        f'<surface kind="sites" route="{route}"{engine_attr} mode="create" />\n'
        "<sites-orientation>\n"
        "The user is on the SITES surface, building a publishable WEBSITE that "
        "deploys as a standalone static page on the edge — not an in-app pocket "
        "dashboard. It renders as a real marketing landing page read top to "
        "bottom as a conversion funnel: nav, hero, services, social proof, "
        "pricing, a call-to-action, a lead-capture form, footer. Talk about it "
        "as a 'site' or 'page' — never a 'pocket'. The pocket is only the "
        "source spec; you create the site as a reviewable DRAFT the user "
        "previews in-app, then publish to a live URL only when they ask — do "
        f"NOT auto-publish.{engine_note} You are a "
        "SENIOR DESIGNER + engineer: YOU decide the look and build a coherent, "
        "premium, on-brand site — you do not ask the user what theme to use.\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "DECIDE THE DESIGN YOURSELF; ASK ONLY FOR REAL FACTS. Choosing the visual "
        "style, palette, layout, and typography is YOUR expertise — infer it from "
        "the business and NEVER ask the user 'what style / theme / colors do you "
        "want?'. The only things worth asking are real-world FACTS you genuinely "
        "cannot know and cannot sensibly placeholder (a specific offering list, "
        "real contact details, real pricing) — and even then PREFER to proceed "
        f"with a clearly-reasonable placeholder and let the user refine. When you "
        f"truly must ask, {ask_mechanism}. NEVER fabricate specific real-world "
        "facts (invented testimonials, precise stats, prices, addresses, phone "
        "numbers) — use an obviously-generic placeholder and flag it instead.\n"
        "\n"
        "PHASE 1 — CREATIVE DIRECTION (infer, do NOT ask).\n"
        "Run the Creative Direction Engine from the DESIGN SYSTEM embedded at the "
        "end of this message (the 2026 Creative Director system is ALREADY in your "
        "context — you do NOT need to invoke a skill): declare the Vision Ledger "
        "and a one-line Visual DNA Token (the Design Read), then commit to ONE "
        "visual identity from its Trend Engine (Dark Kinetic, Tactile Brutalism, "
        "Immersive WebGL, Aurora Mesh, Liquid Glass, Frosted Editorial). State the "
        "read in one sentence, then go — do NOT ask the user to pick the look. If "
        "the user already named a style or brand, honor it. Rotate the identity "
        "and palette so two similar briefs never look identical.\n"
        "\n"
        "PHASE 2 — DESIGN + BUILD (apply the embedded DESIGN SYSTEM throughout).\n"
        "1. PICK A LOOK. Call "
        "`mcp__pocketpaw_design_systems__list_design_systems` to see the "
        "library, choose the one that fits your Design Read, then "
        "`mcp__pocketpaw_design_systems__get_design_system` with its slug to load "
        "the DESIGN.md + tokens.css as a starting palette. THEME the site with "
        "those tokens while the pocketpaw-design-taste rules GOVERN the build: a "
        "mandatory background architecture (never a plain #fff/#000 page), the "
        "chosen visual identity, diverse section compositions (the default AI "
        "sequence and two consecutive same-layout sections are banned), a bold "
        "typographic pairing, Tier-0 CSS-only motion, and ZERO em-dashes. Do NOT "
        "default to the warm / earthy system just because the business is a cafe, "
        "salon, or shop — rotate to the less-obvious fit and reseed the accent.\n"
        "2. CUSTOM COLORS. If the user gave a brand color, call "
        "`mcp__pocketpaw_palette__scale_from_color` with the hex to get a full "
        "scale and OVERRIDE the design system's primary with it. If they gave "
        "a logo or reference-image URL, call "
        "`mcp__pocketpaw_palette__extract_palette` on it and key the palette "
        "off that.\n"
        "3. REAL ASSETS. Use `mcp__pocketpaw_stock__search_stock_images` for "
        "hero and section photography and `mcp__pocketpaw_icons__search_icons` "
        "for feature icons. Wire the REAL returned URLs into the page — never "
        "leave placeholders or invent image paths.\n"
        "4. BRIEF. State a one-line brief back so the user sees the plan — e.g. "
        "'Building a [design-system vibe] site for [business] with sections "
        "[…], palette [primary].' Then build.\n"
        f"5. {build_step}\n"
        "6. DRAFT-FIRST — STOP at the draft; do NOT publish by default. The create "
        "tool persists a reviewable DRAFT the user previews IN-APP (open /sites → "
        "the site's Preview tab). Publishing deploys the site to the public edge "
        "(and on a paid tier can open a checkout), so it is the user's call, not "
        "an automatic next step. Tell the user the draft is ready, point them at "
        "the Preview, and OFFER to take it live — e.g. 'Your site is ready as a "
        'draft — preview it under /sites, and say publish (or "make it live") '
        "when you're happy with it.' Then STOP. Publish IN THIS SAME TURN only if "
        "the user's request ALREADY asked to go live ('publish', 'make it live', "
        "'ship it', 'put it online'): in that case call "
        "`mcp__pocketpaw_sites_manager__publish` with the pocket_id and SHOW the "
        "returned live `url` plus a link to /sites. Do not publish on a plain "
        "'create'/'build'/'make' request.\n"
        "\n"
        "ROBUSTNESS (this flow must never stall or disappoint in real use):\n"
        "- If ANY tool errors (design-system, stock, palette, or icons), do "
        "NOT retry blindly or stall. Proceed with sensible defaults — a "
        "reasonable built-in look, generic-but-tasteful section imagery — and "
        "briefly note what you fell back on. A tool failure must NEVER block "
        "the build.\n"
        "- ONE round of questions maximum. If the user already gave detail or "
        "says 'just build it', skip straight to Phase 2 with sensible "
        "defaults.\n"
        "- When you DO publish (the user asked), never claim a publish that "
        "didn't happen — relay the real publish error and show the real `url`. "
        "No phantom URLs. On the default draft path there is no `url` yet — point "
        "at the in-app Preview, don't invent a live link.\n"
        "- Keep the 'site' / 'page' vocabulary throughout; never say 'pocket'.\n"
        "</sites-procedure>\n"
        f"{_CONCIERGE_NOTE}\n"
        f"{_design_taste_system()}"
    )


# The rules that transfer to EVERY engine, because they are properties of a
# marketing landing page and not of a rendering track. The five numbered rules the
# ripple branch keeps are the ones that do NOT transfer: they name widget types
# (``pricing-table``'s ``tiers``, the ``accordion`` ban, the ``form`` /
# ``newsletter`` ban) and a react/html/svelte page has no widgets to name.
_REFINE_SHARED_RULES = (
    "PRESERVE the landing funnel (nav → hero → services → proof → pricing → "
    "call-to-action → lead form → footer): a refine changes a section, it does not "
    "reorder or drop the funnel, and it does not turn the page into a dashboard.\n"
    "REAL COPY ONLY. Never invent a testimonial, a statistic, a price, an address, "
    "or a phone number to fill a section you are editing — ask, or keep what is "
    "there.\n"
    "Every CTA stays an anchor `href` (or `tel:` / `mailto:`), never a click "
    "handler that needs JavaScript to navigate.\n"
    "The lead form stays a FLAT native `<form>` — real `name=` on every "
    '`input`/`textarea` and a `button type="submit"`. Never nest a form inside a '
    "form, and never replace it with a widget that does.\n"
)


def _react_write_scope() -> str:
    """The paths a react edit may and may not write — rendered from the constants
    the tool ENFORCES, not retyped as prose beside them.

    ``sites/react_paths.py`` is the shared module ``edit_react_component`` and
    ``create_react_site`` both check against, so reading it here means the day a
    fifth reserved file is added, this instruction gains it in the same commit
    instead of drifting into a stale list the agent trusts. A prompt that lists four
    reserved paths when the tool rejects five does not fail loudly: the agent writes
    the fifth, gets ``site_edit.reserved_path`` back, and spends a turn discovering
    what the prompt was supposed to have told it.

    The normalization note is not decoration — the tool collapses ``.``/``..`` and
    backslashes BEFORE it checks, so ``./package.json`` is rejected too and an agent
    that reads the list as a literal string match would think it found a loophole.
    """
    reserved = ", ".join(f"`{path}`" for path in REACT_RESERVED_FILES)
    authorable = " and ".join(f"`{prefix}`" for prefix in REACT_AUTHORABLE_PREFIXES)
    return (
        f"YOU MAY ONLY WRITE under {authorable}, and never under "
        f"`{REACT_RESERVED_PREFIX}`. {reserved} and everything under "
        f"`{REACT_RESERVED_PREFIX}` are GENERATED and will be rejected — they carry "
        "the prerender contract and the dependency list. Paths are normalized "
        "before that check, so a `./` prefix or a `..` segment does not get around "
        "it. The project has react, react-dom and vite and NOTHING else: no router, "
        "no CSS framework, no state or animation library, and no way to add a "
        "dependency — so build the change with what is there, and if it genuinely "
        "needs a new package, say so instead of importing one.\n"
    )


def _refine_publish_step(engine: str | None, pocket_id: str) -> str:
    """The publish instruction for a refine turn. Honest per engine.

    Three claims in the old text were wrong at once, and each was wrong in the
    direction that makes the agent announce something that did not happen: that the
    site "auto-publishes from its source pocket" (nothing auto-publishes — publish
    is a tool call, and on a paid tier it can open a checkout), that an edit is
    followed by a re-publish at all (the svelte and react edit tools stage a DRAFT
    and deliberately do not build), and that the agent should "show the live `url`"
    (on an ASYNC engine that url is empty on a first publish and points at the
    PREVIOUS deploy on a re-publish, so it serves the pre-change page).

    ``_publish_runs_async`` decides the last part, so the wording follows the
    publish path instead of restating it.

    THE GATE IS ``is_live``, WHICH IS WHY THIS NAMES A FIELD AT ALL (#1920). The
    publish response carries ``build_status`` / ``build_reason`` / ``build_job_id`` /
    ``build_in_progress`` / ``is_live`` beside the original five keys, and ``is_live``
    is the only one that answers the question the agent is about to answer out loud:
    it is true only when there is a non-empty ``url`` AND ``deployed`` AND no build
    in flight. ``deployed`` and a non-empty ``url`` each look like that answer and
    are not — a rebuild deliberately keeps the previous deploy's values so a working
    site is not reported as down mid-build, which is exactly the state where they
    lie. The statuses are deliberately NOT enumerated here: the wire contract treats
    an unrecognized ``build_status`` as in-progress, so a closed list in the prompt
    would go stale in the unsafe direction.
    """
    common = (
        "PUBLISHING IS A SEPARATE STEP AND THE USER'S CALL. Nothing you do here "
        "goes live on its own: the published page keeps serving its last build "
        "until someone publishes, and publishing deploys to the public edge (on a "
        "paid tier it can open a checkout). Do NOT publish off a plain edit "
        "request. Tell the user their change is saved and offer to take it live.\n"
        "When they DO ask ('publish', 'make it live', 'ship it'), call "
        f"`mcp__pocketpaw_sites_manager__publish` with pocket_id `{pocket_id}` and "
        "relay the REAL result — the real error text on a failure. Never claim a "
        "publish that did not happen.\n"
        "GATE 'IT IS LIVE' ON THE `is_live` FIELD AND ON NOTHING ELSE. Show the "
        "`url` only when `is_live` is true. Never report a site as live off "
        "`deployed` or a non-empty `url` alone — a rebuild keeps the PREVIOUS "
        "deploy's url and `deployed:true` on purpose, so both are set while the "
        "page the user just changed is not live. The response also carries a "
        "`message` that already states the correct conclusion: relay it.\n"
    )
    if not _publish_runs_async(engine or ""):
        return common + ("Never invent or guess a url — show only what the tool returned.\n")
    return common + (
        "THE BUILD IS ASYNCHRONOUS ON THIS ENGINE, so publish returns BEFORE the "
        "build starts and its response can never tell you how the build ended. On a "
        "FIRST publish the site is created with no url at all (empty) and "
        "`deployed` false; on a RE-publish the url is the PREVIOUS deploy, which "
        "keeps serving the OLD page for as long as the rebuild takes. Report it as "
        "a build that has STARTED, not as a live page.\n"
        "TO LEARN THE OUTCOME, call "
        "`mcp__pocketpaw_sites_manager__get_site_build_status` with pocket_id "
        f"`{pocket_id}` — that is the ONLY way, and it is the follow-up whenever a "
        "publish came back with `build_in_progress` true. While it is still true, "
        "say the site is still building and show no url. If the build FAILED, relay "
        "`build_reason` instead of a url and offer to fix the page and publish "
        "again. Only once `is_live` is true is the url the thing to show.\n"
    )


def _refine_unknown_engine_step(pocket_id: str) -> str:
    """What to say when we could not read which engine authored the site.

    The read fails on a deleted pocket, a cross-workspace stamp, or a dropped
    connection — and a fifth engine in ``sites/engines.py`` with no branch here
    lands in the same place. Every engine's edit path is a DIFFERENT tool and three
    of the four reject a pocket from another track, so guessing is the one move
    guaranteed to be wrong roughly three times in four. Name no tool; make the
    agent look first.
    """
    return (
        "WHICH ENGINE AUTHORED THIS SITE COULD NOT BE DETERMINED, so do not assume "
        "one. The edit path is a different tool per engine and each rejects a site "
        "from another track, so a guess is a failed tool call at best and a "
        "confident wrong answer at worst.\n"
        f"FIRST call `mcp__pocketpaw_pocket__get_pocket` with pocket_id "
        f"`{pocket_id}` and read its `engine`. A `ripple` pocket carries a "
        "`rippleSpec` (a widget tree) and is edited with "
        "`mcp__pocketpaw_pocket_specialist__edit`. `svelte`, `react` and `html` "
        "pockets carry a `source` map ({path: file contents}) instead, and the "
        "specialist CANNOT edit those — svelte uses "
        "`mcp__pocketpaw_sites_manager__edit_svelte_component`, react uses "
        "`mcp__pocketpaw_sites_manager__edit_react_component`, and html has no "
        "edit tool at all yet (say so plainly rather than reaching for a create "
        "tool).\n"
        "If the read fails too, tell the user you could not load their site rather "
        "than attempting an edit blind.\n"
    )


def _refine_preamble(meta: SurfaceMeta, engine: str | None = None) -> str:
    """The /sites/[siteId] refine preamble — edit an EXISTING published site.

    IT FORKS BY ENGINE because every load-bearing instruction in it is a different
    fact per engine, not a different phrasing of one fact:

    * **which tool can edit the page at all** — ripple's content is a ``rippleSpec``
      the pocket specialist merges into; svelte, react and html carry a ``source``
      map instead, which that tool cannot touch. Each source engine has (or lacks)
      its own file-level edit tool;
    * **whether JavaScript runs for the visitor** — a react site ships a hydrating
      client bundle by default (``sites_keep_client_bundle_default``), so "no
      JavaScript runs" is false there, while the prerender contract that DOES bind
      react has no analogue on a ripple widget page;
    * **whether a publish hands back a url worth showing** — see
      ``_refine_publish_step``.

    Written for ripple and shipped to all four, it asserted the ripple answer to all
    three on every site. ``engine`` is resolved by ``_refine_engine`` from the
    SOURCE POCKET (``meta`` does not carry it on this surface); ``None`` means it
    could not be read and routes to ``_refine_unknown_engine_step``.

    The ripple branch is deliberately unchanged apart from the publish claim: it was
    correct there, and this is a fix for the other three engines rather than a
    rewrite of working behaviour. What is shared across all branches is the
    ASK-DON'T-ASSUME gate, the ``ask-user-questions`` mechanism (refine keeps
    ``ripple_mode="on"`` on every engine, so that widget is real here), the funnel /
    real-copy / anchor-CTA / flat-form rules, and the source ``pocket_id`` — which
    every branch still threads into the tool call it names.
    """
    route = meta.route_path or "/sites"
    pocket_id = meta.pocket_id or ""
    engine_attr = f' engine="{engine}"' if engine else ' engine="unknown"'

    if engine == "ripple":
        render_truth = (
            " The page renders STATICALLY (no JavaScript runs for the visitor), so "
            "every change must still work as plain HTML."
        )
        edit_step = (
            "Treat the user's message as an edit to APPLY to the existing site. "
            f"Apply the change to pocket `{pocket_id}` via "
            "`mcp__pocketpaw_pocket_specialist__edit` (the merge/edit path — it "
            "mutates the existing spec in place). NEVER use the create path and "
            "NEVER rebuild the page from scratch; a refine is a targeted edit on "
            "top of the current landing spec.\n"
        )
        # The five rules are ripple's and stay ripple's: each names a widget type,
        # and the other three engines have no widgets. Byte-identical to the text
        # that shipped before the fork.
        rules = (
            "PRESERVE the landing structure (nav → hero → services → proof → "
            "pricing → flat lead form → footer) and keep the 5 static-site (SSR) "
            "rules intact while you edit:\n"
            "1. Lead capture stays FLAT native `input`/`textarea`/"
            '`button{type:"submit"}` with real field names (name, email, phone, '
            "message) — NEVER the `form` or `newsletter` widget, which nests an "
            "invalid `<form>` inside the site template's outer POST form and "
            "captures zero leads.\n"
            "2. `pricing-table` uses `tiers` (never `plans`/`columns`).\n"
            "3. An FAQ is `heading` + `text` pairs — NEVER the `accordion` widget "
            "(its panels only open with JS, so on a static site the answers never "
            "expand).\n"
            "4. Every CTA is an anchor `href` (or `tel:` / `mailto:`) — never an "
            "`on_click` handler, which is a dead button with no client JS.\n"
            "5. `hero` is the marketing Hero widget — never the dashboard "
            "`hero+grid` (a page-header plus a KPI `stat` grid); no metric grid, "
            "no charts. This is marketing, not analytics.\n"
            "Any animation stays Tier-0 (CSS-only, static-safe) — `aurora`, "
            "`marquee`, `border-beam`, `shimmer`, `text-effect`; never `reveal`, "
            "`parallax`, or `spotlight` (they need client JS and hide content on a "
            "static page).\n"
        )
    elif engine == "svelte":
        render_truth = (
            " The page is hand-written SvelteKit components PRERENDERED to static "
            "HTML, so a section must look finished in the markup it returns — "
            "never set the resting state only in `onMount`."
        )
        edit_step = (
            "Treat the user's message as an edit to ONE component file. This "
            "site's content is a `source` map ({path: file contents}), NOT a "
            "rippleSpec — there is no widget spec here, so do NOT call the pocket "
            "specialist and do NOT draft a rippleSpec.\n"
            "Call `mcp__pocketpaw_sites_manager__edit_svelte_component` with "
            f"pocket_id `{pocket_id}`, the `component_path` of the file to change "
            "(it must already exist in the source map, e.g. "
            "'src/lib/components/Hero.svelte'), and EXACTLY ONE of `edits` (a "
            "list of {old_string, new_string} blocks — prefer this for anything "
            "targeted; each old_string must match the current file verbatim and "
            "exactly once) or `new_source` (the whole new file, for a large "
            "rewrite). Read the file before you diff it. NEVER call a create tool "
            "to apply a change — that mints a second site and leaves this one "
            "untouched.\n"
            "THE EDIT IS A DRAFT PREVIEW, NOT A DEPLOY. The tool returns "
            '`status:"draft"`, `is_live:false` and a `preview_url` that previews '
            "the edit — it is not the live site and the live page is unchanged. "
            "Relay the tool's own `message`, and never tell the user the change is "
            "published or live. On `ok:false` nothing was staged: an old_string "
            "that matched 0 or more than 1 time needs more context, and a "
            "smoke-test failure means fix the component and retry.\n"
        )
        rules = _REFINE_SHARED_RULES
    elif engine == "react":  # RX-3's `_react_refine_preamble`, folded in here
        # The tool id and its argument names are read off ``edit_react_component``'s
        # own schema (RX-3), not guessed.
        render_truth = (
            " The page is hand-written React components PRERENDERED to static HTML "
            "at build time, and it ALSO ships a client bundle by default — so it "
            "is not a no-JavaScript page, and it is not a page you may leave blank "
            "until JavaScript runs."
        )
        edit_step = (
            "Treat the user's message as an edit to ONE component file. This "
            "site's content is a `source` map ({path: file contents}), NOT a "
            "rippleSpec — there is no widget spec here, so do NOT call the pocket "
            "specialist and do NOT draft a rippleSpec.\n"
            "Call `mcp__pocketpaw_sites_manager__edit_react_component` with "
            f"pocket_id `{pocket_id}`, the `component_path` to write (e.g. "
            "'src/components/Hero.tsx'), and EXACTLY ONE of `edits` (a list of "
            "{old_string, new_string} blocks — prefer this for anything targeted; "
            "each old_string must match the current file verbatim and exactly "
            "once) or `new_source` (the whole new file). Read the file before you "
            "diff it. To ADD a section, call it twice: once with `create=true` and "
            "`new_source` for the new `src/components/<Name>.tsx`, then once with "
            "`edits` on `src/App.tsx` to import and render it. NEVER call "
            "`create_react_site` again for a change — that mints a SECOND site "
            "pocket and leaves the one the user is looking at untouched.\n"
            f"{_react_write_scope()}"
            "THE EDIT IS SAVED TO THE DRAFT, NOT PUBLISHED. The tool returns "
            '`status:"draft"` / `is_live:false`, nothing is built and nothing '
            "goes live; the user previews it under /sites. Relay the tool's own "
            "`message` and do not tell them it is live. On `ok:false` NOTHING was "
            "saved — relay the reason (a reserved path, the wrong engine, an "
            "old_string that matched 0 or more than 1 time) rather than reporting "
            "a successful edit.\n"
        )
        rules = _REFINE_SHARED_RULES + (
            "THE PRERENDER RULE governs every component you touch: at build time "
            "`<App />` is rendered to HTML by `react-dom/server`, before any "
            "browser JavaScript runs. `useEffect` does NOT run at prerender time, "
            "and `window` / `document` do not exist during that render. So every "
            "component must render its resting/final state in its RETURNED MARKUP "
            "— a count-up initialized to 0 bakes '0', so initialize it to the "
            "final value; an accordion's open panel is the `useState` initial "
            "value; a scroll-reveal keeps its content in the markup and only adds "
            "a class. Touch `window`/`document` inside `useEffect` (or behind a "
            "`typeof window !== 'undefined'` check), never in a component body or "
            "at module top level. If your edit would leave a section looking "
            "unfinished with JavaScript disabled, move the final state into the "
            "returned markup.\n"
        )
    elif engine == "html":
        render_truth = (
            " The page is a hand-authored static HTML/CSS bundle served straight "
            "from the edge — the files in the source map ARE the page."
        )
        # THE HONEST BRANCH. html is the DEFAULT create engine and has no
        # chat-reachable edit tool: ``create_html_site`` takes no ``pocket_id`` (it
        # mints a new pocket, so calling it here would leave the user with a second,
        # unrelated site), ``edit_svelte_component``'s service guard is svelte-only
        # by design, and the uid-splice path behind the native editor
        # (``sites/service.py::apply_leaf_edits``) is a REST route and svelte-gated
        # too. Naming any of them would be the exact defect this fork fixes, so the
        # branch states the gap instead. Being useful inside a real limit beats
        # improvising outside it.
        edit_step = (
            "YOU CANNOT WRITE THIS SITE'S FILES FROM THIS CHAT. An html site has "
            "no edit tool yet — that is a real gap in the product, not something "
            "to work around:\n"
            "- the pocket specialist edits a rippleSpec. This pocket has a "
            "`source` map instead, so it has nothing to merge into — do NOT call "
            "it and do NOT draft a rippleSpec.\n"
            "- the create tools take no pocket id: each one creates a NEW pocket "
            "and a SECOND site, leaving the one the user is looking at untouched. "
            "Do NOT call a create tool to 'apply' a change.\n"
            "WHAT TO DO INSTEAD, in one turn: call "
            f"`mcp__pocketpaw_pocket__get_pocket` with pocket_id `{pocket_id}` to "
            "read the current files from its `source` map, work out exactly what "
            "the change is, and give the user the finished replacement markup for "
            "the file that changes (in a code block, with the file's path) plus a "
            "one-line description of where it goes. Then say plainly that editing "
            "an html site's files from chat is not wired up yet, so you cannot "
            "apply it for them. Do not imply you saved, published, or previewed "
            "anything — nothing was written.\n"
        )
        rules = _REFINE_SHARED_RULES + (
            "Whatever markup you hand back keeps the page working with no "
            "JavaScript: the full resting state lives in the HTML, never rendered "
            "only by a script.\n"
        )
    else:
        render_truth = ""
        edit_step = _refine_unknown_engine_step(pocket_id)
        rules = _REFINE_SHARED_RULES

    # html has nothing to publish FROM chat (no write landed), and the unknown
    # branch has not established which engine it would be publishing — naming a
    # publish flow in either would invite the agent to publish the LAST build as
    # if it carried the change the user just asked for.
    publish_step = "" if engine in (None, "html") else _refine_publish_step(engine, pocket_id)

    return (
        f'<surface kind="sites" route="{route}" pocket="{pocket_id}"'
        f'{engine_attr} mode="refine" />\n'
        "<sites-orientation>\n"
        f"The user is REFINING an EXISTING published Paw Site (source pocket "
        f"`{pocket_id}`) — a live standalone marketing website already deployed "
        "as a static page on the edge. They are on its per-site chat, asking for "
        "a CHANGE to that page. Do NOT rebuild the site from scratch, do NOT "
        "create a new site or a new pocket, and do NOT treat it as an in-app "
        "dashboard pocket. It is a real marketing landing page that reads top to "
        "bottom as a conversion funnel: nav, hero, services, social proof, "
        "pricing, a call-to-action, a flat lead-capture form, footer. Talk about "
        f"it as a 'site' or 'page' — never a 'pocket'.{render_truth}\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "ASK, DON'T ASSUME: if the requested edit is ambiguous, or applying it "
        "needs a real fact or content you don't have (new copy, a price, a "
        "section's content, or which of several interpretations the user means), "
        "ASK with an `ask-user-questions` ripple widget (include a 'you decide' "
        "option) instead of guessing — and NEVER fabricate real-world facts "
        "(testimonials, stats, prices, addresses, contact details).\n"
        f"{edit_step}"
        f"{rules}"
        f"{publish_step}"
        'Keep `type="site"` + `pattern="landing"` on the pocket. Keep talking '
        "'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>\n"
        f"{_CONCIERGE_NOTE}"
    )


def _render_copy(block: Any) -> str:
    """Flatten one section's copy block into an inline, LLM-readable string.

    ``brief.copy[section_id]`` is free-form: usually a dict of copy fields
    (``{"headline": ..., "subhead": ...}``), sometimes a plain string/list. Join
    dict entries as ``key: value`` so the actual copy text lands in the preamble.
    """
    if isinstance(block, dict):
        return "; ".join(f"{k}: {v}" for k, v in block.items())
    if isinstance(block, (list, tuple)):
        return "; ".join(str(item) for item in block)
    return str(block)


def _frontend_preamble(meta: SurfaceMeta, brief: DesignBrief) -> str:
    """Render the Frontend stage's build instructions FROM a structured brief.

    The crew's baton (``DesignBrief``) in → the build preamble out. This is the
    additive, brief-driven twin of ``_create_preamble``: instead of orienting the
    agent off a raw user message, it walks the brief's ordered ``sitemap``,
    injects the matching ``copy`` blocks and ``asset_manifest`` imagery, threads
    the branding design-system tokens/voice, and ROUTES by ``brief.engine``
    (``"svelte"`` → ``create_svelte_site``; ``"ripple"`` → ``create_landing_site``
    / the pocket specialist). It preserves the same static-site (SSR) rules the
    create/refine preambles enforce so a brief-driven build can't reintroduce a
    static-site trap.

    NOTE: NOT wired into ``build_preamble`` — the live create/refine dispatch is
    byte-for-byte unchanged. Exercised by tests now; the orchestration slice
    (SC-9) wires it in behind a flag (SC-11).
    """
    route = meta.route_path or "/sites"
    engine = brief.engine

    # --- Ordered sitemap → build-in-order instructions, with copy injected. ---
    section_lines: list[str] = []
    for i, section in enumerate(brief.sitemap, start=1):
        head = f"{i}. `{section.role}` section"
        if section.heading:
            head += f' — heading "{section.heading}"'
        section_lines.append(head)
        if section.notes:
            section_lines.append(f"   - notes: {section.notes}")
        copy_block = brief.copy.get(section.id)
        if copy_block:
            section_lines.append(f"   - copy: {_render_copy(copy_block)}")
    sitemap_block = (
        "\n".join(section_lines)
        if section_lines
        else "(the brief carries no explicit sitemap — build a standard "
        "conversion funnel: nav → hero → services → proof → pricing → cta → "
        "flat lead form → footer)"
    )

    # --- Real imagery from the asset manifest (never invent placeholder URLs). ---
    if brief.asset_manifest:
        asset_lines = "\n".join(
            f"- {a.kind}: {a.url}" + (f' (alt: "{a.alt}")' if a.alt else "")
            for a in brief.asset_manifest
        )
        assets_block = (
            "Use these REAL asset URLs from the brief's manifest verbatim — never "
            "invent placeholder image paths:\n" + asset_lines + "\n"
        )
    else:
        assets_block = ""

    # --- Design system + voice from the branding layer. ---
    branding = brief.branding
    design_block = ""
    ds = branding.design_system
    if ds is not None:
        design_block += f"Theme the page with the brief's design system (`{ds.name}`). "
        if ds.tokens_css:
            design_block += (
                "Apply its compiled `tokens_css` (CSS custom properties) as the "
                "single source of truth for color, type, spacing, radius, and "
                "elevation — never hard-code ad-hoc values. "
            )
        if ds.colors:
            design_block += (
                "Use its palette scales (the 50..900 steps per role color) for "
                "every surface, text, and accent. "
            )
        if ds.rationale:
            design_block += (
                "Honor the design rationale — its mood, do's/don'ts, and "
                f"anti-patterns: {ds.rationale} "
            )
    if branding.voice:
        design_block += f'Match this brand voice throughout the copy: "{branding.voice}". '

    # --- Route by engine. svelte → hand-written components; ripple → landing spec. ---
    if engine == "svelte":
        route_block = (
            "BUILD ENGINE: svelte. Author this page as hand-written SvelteKit "
            "components (the svelte track) — one component per section above, at "
            "the premium design quality bar. There is NO rippleSpec and NO widget "
            "catalog on this track: do not draft a rippleSpec, do not call the "
            "pocket specialist. Assemble the components into the source map and "
            "persist via `create_svelte_site` "
            "(`mcp__pocketpaw_sites_manager__create_svelte_site`), which stamps "
            'the source pocket `type="site"` + `pattern="landing"` + '
            '`engine="svelte"`; then `mcp__pocketpaw_sites_manager__publish`. Do '
            "NOT fall back to any ripple/landing create tool on this track."
        )
    else:
        route_block = (
            "BUILD ENGINE: ripple. Compose this page as a conversion-ordered "
            "ripple landing spec via the pocket specialist / `create_landing_site` "
            "(`mcp__pocketpaw_sites_manager__create_landing_site`), which stamps "
            'the source pocket `type="site"` + `pattern="landing"`; then '
            "`mcp__pocketpaw_sites_manager__publish` with the returned pocket_id."
        )
        if brief.pattern == "dynamic":
            route_block += (
                ' This brief is marked `pattern="dynamic"` (a live-data site): the '
                "dynamic create path is `create_dynamic_site` "
                "(`mcp__pocketpaw_sites_manager__create_dynamic_site`), but keep "
                "the static landing build as the primary focus."
            )

    return (
        f'<surface kind="sites" route="{route}" engine="{engine}" mode="frontend" />\n'
        "<sites-orientation>\n"
        "You are the FRONTEND stage of the site-authoring crew. Build a "
        "publishable WEBSITE from the structured brief below — a real marketing "
        "landing page that deploys as a standalone static page on the edge, NOT "
        "an in-app pocket dashboard. It reads top to bottom as a conversion "
        "funnel. Talk about it as a 'site' or 'page' — never a 'pocket'. The "
        "pocket is only the source spec; it auto-publishes to a live URL. The "
        "page renders STATICALLY (no JavaScript runs for the visitor), so every "
        "section must work as plain HTML.\n"
        "</sites-orientation>\n"
        "<sites-brief>\n"
        f"GOAL: {brief.goal}\n"
        + (f"AUDIENCE: {brief.audience}\n" if brief.audience else "")
        + "SITEMAP — build these sections IN THIS ORDER, using the copy provided:\n"
        + sitemap_block
        + "\n"
        + assets_block
        + (design_block + "\n" if design_block else "")
        + "</sites-brief>\n"
        "<sites-procedure>\n" + route_block + "\n"
        "PRESERVE the landing structure and keep the static-site (SSR) rules "
        "intact while you build:\n"
        "1. Lead capture stays FLAT native `input`/`textarea`/"
        '`button{type:"submit"}` with real field names (name, email, phone, '
        "message) — NEVER the `form` or `newsletter` widget, which nests an "
        "invalid `<form>` inside the site template's outer POST form and captures "
        "zero leads.\n"
        "2. `pricing-table` uses `tiers` (never `plans`/`columns`).\n"
        "3. An FAQ is `heading` + `text` pairs — NEVER the `accordion` widget "
        "(its panels only open with JS, so on a static site the answers never "
        "expand).\n"
        "4. Every CTA is an anchor `href` (or `tel:` / `mailto:`) — never an "
        "`on_click` handler, which is a dead button with no client JS.\n"
        "5. `hero` is the marketing Hero widget — never the dashboard `hero+grid` "
        "(a page-header plus a KPI `stat` grid); no metric grid, no charts. This "
        "is marketing, not analytics.\n"
        "Any animation stays Tier-0 (CSS-only, static-safe) — `aurora`, "
        "`marquee`, `border-beam`, `shimmer`, `text-effect`; never `reveal`, "
        "`parallax`, or `spotlight` (they need client JS and hide content on a "
        "static page).\n"
        "After it publishes, relay any publish error — never claim a phantom "
        "publish — and SHOW the live `url` plus a link to /sites. Keep talking "
        "'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>\n"
        f"{_CONCIERGE_NOTE}"
    )


def _chat_preamble(meta: SurfaceMeta) -> str:
    """The /sites/[siteId] CHAT preamble — answer questions, NEVER mutate.

    The Build/Chat toggle is set to Chat: the user is on an existing site's
    chat asking a QUESTION about it (what's on the page, what a section says,
    how it's structured, why it looks a certain way). Answer helpfully, but this
    is a read-only surface — do NOT edit the site, do NOT republish it, and do
    NOT create a pocket. Mirrors the refine preamble's site/page vocabulary and
    orientation, minus every mutation instruction.
    """
    route = meta.route_path or "/sites"
    pocket_id = meta.pocket_id or ""
    return (
        f'<surface kind="sites" route="{route}" pocket="{pocket_id}" mode="chat" />\n'
        "<sites-orientation>\n"
        f"The user is CHATTING about an EXISTING published Paw Site (source pocket "
        f"`{pocket_id}`) — a live standalone marketing website already deployed as "
        "a static page on the edge. They are on its per-site chat with the "
        "Build/Chat toggle set to CHAT, so they are asking a QUESTION about the "
        "site, not requesting a change to it. It is a real marketing landing page "
        "that reads top to bottom as a conversion funnel: nav, hero, services, "
        "social proof, pricing, a call-to-action, a flat lead-capture form, "
        "footer. Talk about it as a 'site' or 'page' — never a 'pocket'.\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "Treat the user's message as a QUESTION to ANSWER about the existing site "
        "— explain what is on the page, what a section says, how it is structured, "
        "or give advice — and answer clearly and concisely.\n"
        "This is a READ-ONLY surface. Do NOT modify or edit the site, do NOT "
        "call the pocket specialist edit/merge tool or any create tool, do NOT "
        "re-publish the site, and do NOT create a new pocket or a new "
        "site. If the user actually wants a CHANGE applied, tell them to switch "
        "the toggle to BUILD — in Chat mode you only answer questions and never "
        "touch the live page. Keep talking 'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>\n"
        f"{_CONCIERGE_NOTE}"
    )


__all__ = [
    "build_preamble",
    "_create_preamble",
    "_frontend_preamble",
    "_chat_preamble",
    "_refine_preamble",
]

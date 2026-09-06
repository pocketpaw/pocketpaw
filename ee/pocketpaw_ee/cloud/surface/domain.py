# domain.py — Surface context value objects.
#
# Changes: 2026-09-06 (BR-1, feat/browser-surface-server) — added
# ``SurfaceKind.BROWSER`` (the /browser agentic-browser surface).
#
# Created: 2026-05-24 — Surface-aware chat preamble entity. The cloud
# chat agent today only sees scope / participants / current-pocket-id
# (three lines of dynamic context). Paw-enterprise is chat-first and
# every route will eventually have a chat bar — the agent should know
# which SURFACE the user is on and what's actually visible there. This
# module owns the value objects ``SurfaceKind`` (enumerates every chat-
# bearing surface), ``SurfaceMeta`` (client-supplied hints) and
# ``SurfaceContext`` (resolved snapshot + rendered preamble). Per the
# 11 entity rules, tenancy is enforced at construction — ``workspace_id``
# and ``user_id`` are required on ``SurfaceContext``.
#
# Updated: 2026-06-04 (feat/sites-refine-surface) — ``SurfaceMeta`` grows a
# ``site_id`` hint. The /sites/[siteId] refine chat stamps the published
# site id (and the underlying pocket_id) so the sites handler can branch onto
# a LANDING-AWARE REFINE preamble instead of the create-a-new-site one.
# Updated: 2026-06-04 (feat/sites-svelte-engine) — ``SurfaceMeta`` grows an
# ``engine`` hint ("ripple" | "svelte"). The /sites create UI's "Use Svelte
# pages" toggle stamps it so the sites handler routes the CREATE preamble to
# the svelte-track authoring skill (engine="svelte") instead of the
# ripple/default marketing brain. Persisted setting is pocket.engine; this is
# only the per-turn routing signal.
# Changes: 2026-06-05 (feat/surface-profile-bias-kill) — added the typed
# ``SurfaceProfile`` descriptor, the per-surface POLICY object (the data
# backbone of the "ripple-default bias" fix). Its ``ripple_mode`` drives
# whether ``build_behavior_instructions`` includes the ~20k-char
# INLINE_RIPPLE_SYSTEM_PROMPT ("default to ui-spec" LAW); ``off`` omits it
# so the /sites svelte-create surface stops defaulting to a ripple ui-spec
# instead of hand-authored Svelte.
# Changes: 2026-06-05 (feat/sites-svelte-engine) — ``deny_mcp_tool_ids`` is now
# ENFORCED end-to-end: the resolver's set is threaded to the OSS backend's
# ``run`` as a plain ``frozenset[str]`` and subtracted from the SDK allowlist
# (replacing the deleted prompt-sniffing tool gate in ``claude_sdk.py``).
# ``skill_names`` / ``allowed_sdk_tools`` / ``system_message_override`` remain
# DECLARED-but-inert tested DATA for later passes. The resolver lives in
# ``service.py`` and is META-AWARE on /sites (three modes).
# Changes: 2026-06-07 (feat/entity-pocket-profile-field) — relocated
# ``PocketSurfaceProfile`` here from ``models/pocket.py``. It is the JSON-
# friendly mirror of ``SurfaceProfile`` embedded on a Pocket; living it in the
# leaf domain module lets both ``models.pocket`` (the Beanie doc) and
# ``pockets.dto`` (the wire layer) import it WITHOUT ``pockets.dto`` reaching
# into ``models.*`` — which the OSS-EE boundary contract forbids. The surface
# package is models-free at import time, so ``models.pocket`` can import this
# without a cycle.
# Changes: 2026-06-10 (feat/studio-code-migration) — added two new chat-bearing
# surfaces to ``SurfaceKind``: ``STUDIO`` (/studio — describe→generate media,
# image + video) and ``CODE`` (/code — the agent edits + runs code). Both get
# ``ripple_mode="off"`` profiles in ``service.py`` so the agent generates media /
# edits code instead of defaulting to a ripple ui-spec dashboard.
# Changes: 2026-06-10 (feat/belt-surface, BS-2 Belt & Pulley stations thin
# slice) — added the ``BELT`` surface (/belt — the develop station). Its
# ``ripple_mode="off"`` profile in ``service.py`` scopes the agent to the loom
# orientation tools + the Instinct gate tool so it orients first, develops in a
# station worktree, and proposes the diff through the gate — never applying to the
# user's branches directly.
# Changes: 2026-06-10 (feat/belt-console-backend, SC-1) — ``SurfaceMeta`` grows
# two Belt console hints: ``repo`` (the git repo path the /belt user bound for
# this run) and ``base_branch`` (the branch to base the change off). When the
# /belt page has the user pick a repo + branch up front, the client stamps both
# so the belt handler's ``build_preamble`` injects them into the preamble and
# tells the agent NOT to ask for the repo (and to pass exactly these into
# ``belt_propose_change``). Absent → the handler keeps the ask-first behavior.
# Changes: 2026-07-23 (feat/ship-surface-kind, SHIP-8a) — added the ``SHIP``
# surface (/ship — the managed-deploy control plane). Its ripple-OFF profile in
# ``surface_registry`` scopes the agent to the ``pocketpaw_ship`` MCP verbs so it
# drives managed deploys (provision boxes, deploy apps, route domains, attach
# DBs, read logs/metrics) instead of building a dashboard — and every teardown
# only files a proposal for human approval. SHIP carries no surface-specific
# ``SurfaceMeta`` fields (the ship tools resolve tenancy from the chat session).
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — added
# ``SurfacePreamble``, the value object every ``build_preamble`` handler now
# returns: the rendered text AND the ``cache_key`` that says what the handler
# read to render it. ``SurfaceContext`` carries that key through as
# ``preamble_cache_key``. The preamble became a real prompt LAYER on the OSS
# side (``pocketpaw.prompt.surface``), and a layer's key is what a backend
# caching an agent object folds into its own key — so the question "what does
# this preamble depend on" now has to be answered, per handler, by the handler.
# It is deliberately NOT derived centrally from ``(kind, pocket_id, intent)``,
# which every dispatcher has to hand: a pocket preamble lists the first 12 of N
# widgets under a 1500-char cap, so editing widget 13 leaves all three
# identical while the pocket the agent is being told about has changed.
# ``cache_key`` has NO DEFAULT and rejects ``""`` for the same reason
# ``LayerOutput`` does — ``""`` reads as "stable forever" and is what someone
# types when they mean "nothing".
# Changes: 2026-08-25 (feat/other-hand-surface, Otherhand v1) — added the
# ``OTHER_HAND`` surface (/other-hand — the notebook page the user handwrites on
# and the agent writes/draws back onto). ``SurfaceMeta`` grows two hints the
# page stamps per turn: ``snapshot_path`` (the absolute path the snapshot
# endpoint wrote the page PNG to, which the agent ``Read``s to SEE the page) and
# ``free_y`` (the y below which the page is empty, so the agent never draws over
# the user's ink). Its profile (``surface_registry._OTHER_HAND_*``) is ripple-OFF
# and DENIES the two pocket-creation tool ids: an allow-list cannot strip them
# (``POCKET_CREATION_GRANT`` is unioned back and ``ALWAYS_ALLOWED_MCP_SERVERS``
# keeps the servers alive), so without the deny "draw me a mitosis diagram"
# builds a POCKET instead of drawing. Deny is applied BEFORE the grant union in
# ``claude_sdk._build_options``, so a denied id cannot come back.
# Changes: 2026-07-14 (Paw Bar concierge seam, T2) — added the ``CONCIERGE``
# surface (/paw-bar — the public, origin-bound concierge widget). Its handler
# (``handlers/concierge.build_preamble``) and its ripple-OFF, PUBLIC-SAFE profile
# (``surface_registry._concierge_profile``: deny web + code/write/subagent tools,
# lock the MCP surface) live beside the other rows. The run rides
# ``chat.agent_service.ScopeKind.CONCIERGE``, which locks the KB read to the
# Site's pocket.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class SurfaceKind(StrEnum):
    """Every chat-bearing surface in paw-enterprise.

    The router stamps one of these on every chat send via the client's
    ``surface`` hint. Unknown values fall back to ``GENERIC`` so an older
    client (or a route we haven't classified yet) still gets a usable
    preamble instead of failing the chat send.
    """

    HOME = "home"
    POCKETS_LIST = "pockets"  # /pockets index
    POCKET = "pocket"  # /pockets/[id]
    POCKET_WIDGET = "pocket_widget"  # /pockets/[id] with widget-focus modal open
    MISSION_CONTROL = "mission_control"  # /mission-control
    FILES = "files"
    AUDIT = "audit"
    ACTIVITY = "activity"
    AGENTS = "agents"
    AGENT = "agent"  # /agents/[id]
    KNOWLEDGE = "knowledge"
    CALENDAR = "calendar"
    CHAT = "chat"
    QUICKASK = "quickask"
    SETTINGS = "settings"
    SIDEPANEL = "sidepanel"
    FORESIGHT = "foresight"  # /foresight + /foresight/scenarios/* routes
    SITES = "sites"  # /sites — describe-to-create + manage published Paw Sites
    STUDIO = "studio"  # /studio — describe→generate media (image + video)
    CODE = "code"  # /code — agent edits + runs code in the workspace
    BELT = "belt"  # /belt — the develop station (orient→develop→propose via gate)
    SHIP = "ship"  # /ship — the managed-deploy control plane (drive deploys via ship MCP verbs)
    # Otherhand (/other-hand) — a notebook page the user handwrites on. The agent
    # READS the page as an image (the snapshot path arrives on ``SurfaceMeta``)
    # and writes/draws back onto it as vector primitives in a ``page-ops`` block.
    # Its profile is ripple-OFF and denies the pocket create/plan tool ids — the
    # deliverable is ink on the page, never a pocket or a ui-spec.
    OTHER_HAND = "other_hand"  # /other-hand — the page the agent writes back on

    # /browser — a chat-driven agentic browser. The ONLY surface whose profile
    # allows the ``pocketpaw_browser`` MCP tools; every other surface (the
    # unmapped default included) denies them, so the browser is unreachable from
    # /chat where send-capable connector tools live.
    BROWSER = "browser"
    # A PUBLIC, anonymous Paw Bar concierge chat (T2) — a foreign site's embedded
    # widget, answering visitors grounded in the Site's pocket ONLY. Its profile
    # (``surface_registry._concierge_profile``) is ripple-OFF and PUBLIC-SAFE: it
    # denies the web + code/write/subagent tools and locks the MCP surface, so a
    # prompt-injected anonymous caller can't run code, exfiltrate, or mutate the
    # tenant. The run rides ``ScopeKind.CONCIERGE`` (KB locked to pocket:<id>).
    CONCIERGE = "concierge"  # /paw-bar — public, origin-bound concierge widget
    GENERIC = "generic"  # any unknown surface — agent still gets a usable preamble


@dataclass(frozen=True)
class SurfaceMeta:
    """Client-supplied hints about the current surface.

    Every field optional. Stay small — anything heavy gets fetched
    server-side by the matching handler rather than serialized over the
    wire. ``route_path`` is for debugging only (the raw
    ``$page.route.id`` the client read), not for routing decisions.
    """

    pocket_id: str | None = None
    widget_id: str | None = None
    focus_node_id: str | None = None
    agent_id: str | None = None
    file_id: str | None = None
    route_path: str | None = None
    # Foresight surface hints — set by the paw-enterprise sidebar's
    # surface stamp on /foresight routes. ``run_id`` is the active
    # ScenarioRun being viewed; ``scenario_id`` is the custom scenario
    # being edited; ``panel`` is the active rail tab ("scenarios" |
    # "live" | "results" | "aggregate" | "insights" | "editor"). All
    # optional — the handler degrades gracefully when absent.
    run_id: str | None = None
    scenario_id: str | None = None
    panel: str | None = None
    # Sites surface hint — set by the /sites/[siteId] refine chat. The id of
    # the published Paw Site being refined; paired with ``pocket_id`` (the
    # site's source pocket) so the handler routes to the refine/edit path
    # instead of the create-a-new-site path. Absent on the /sites gallery.
    site_id: str | None = None
    # Sites create hint — which engine authors a brand-new site:
    # "html" (the DEFAULT when absent) | "svelte" | "react" | "ripple". On the
    # create branch the handler forks its BUILD step on this, and
    # ``surface_registry._sites_profile`` forks the ripple mode + authoring skill
    # on it: the hand-authored component engines (svelte, react) drop inline
    # ripple and surface their own skill; html/ripple keep ripple. Unknown values
    # normalize to html, mirroring ``sites/engines.py::normalize_engine``'s
    # never-raise policy on the publish side. Does not affect the refine branch
    # (keyed on ``pocket_id``), which wins over engine.
    engine: str | None = None
    # Sites refine hint — the Build/Chat toggle in the /sites/[siteId] refine chat.
    # ``"chat"`` answers questions about the existing site with NO mutation; ``"build"``
    # / unset refines (edits) the site. Consulted only on the refine branch (pocket_id
    # present); the sites handler reads ``meta.mode`` there. Absent on create/gallery.
    mode: str | None = None
    # Sites import-create hint — the id of a captured ``SiteDesignBrief``. Present
    # only on a REBUILD import: the user gave a URL, the crawl distilled it into a
    # design brief, and the create run authors a native site FROM that brief rather
    # than from a typed description. Paired with ``engine`` and NO ``pocket_id``,
    # because a pocket_id would route the run to refine and the pocket is the
    # agent's own to mint. The handler fetches the brief server-side; only the id
    # rides the wire.
    brief_id: str | None = None
    # Belt console hints — set by the /belt page once the user has bound a repo
    # + branch for the run. ``repo`` is the absolute repo path; ``base_branch``
    # is the branch to base the change off. The belt handler injects both into
    # the preamble so the agent doesn't re-ask for the repo and passes exactly
    # these into ``belt_propose_change``. Both absent → ask-first behavior.
    repo: str | None = None
    base_branch: str | None = None
    # Code surface hints — set by the /code page's SurfaceMetaProvider reading
    # the live cloudProjectDetails / CodeStore.workspacePath.
    #
    # ``current_dir`` is the working directory the IDE is scoped to — either a
    # real filesystem path (local-disk adapter, e.g.
    # ``~/.pocketpaw/uploads/projects/{ws}/{uid}/{name}/``) or the storage key
    # prefix (S3 adapter, e.g. ``projects/{ws}/{uid}/{name}/``). The code
    # handler injects it into the preamble so the agent knows where to work.
    #
    # ``project_name`` is the human-friendly project name (e.g. ``"my-app"``),
    # shown in the preamble so the agent can refer to the project by name.
    # Omitted when working locally (no cloud project).
    #
    # ``storage_root`` is the canonical cloud storage key prefix (set on both
    # local-disk and S3 adapters). Always projects/{ws}/{uid}/{name}/.
    #
    # ``is_cloud_storage`` is ``"true"`` when the project has no local
    # filesystem representation (pure S3 adapter). The agent knows files
    # aren't directly on disk and must use the cloud project REST API or
    # a synced Daytona sandbox.
    #
    # ``workspace_vm`` is ``"true"`` when the project is running inside
    # a shared workspace VM (Daytona sandbox). The agent uses Daytona MCP
    # tools exclusively — all file I/O and command execution routes through
    # the sandbox.
    current_dir: str | None = None
    project_name: str | None = None
    storage_root: str | None = None
    is_cloud_storage: str | None = None
    workspace_vm: str | None = None
    # Concierge action registry hint (C1). A CONCIERGE run whose Paw Bar widget
    # declares actions carries the declarations here (a JSON-shaped list of
    # {verb, policy, args, label}). ``concierge_chat`` stamps it from the widget
    # spec; the concierge PROFILE reads the verbs to allow-list exactly this
    # widget's per-verb tools, the PREAMBLE lists them, and ``run_core`` binds them
    # onto the per-stream ContextVar the pawbar_actions MCP server builds from.
    # Absent on every other surface — the concierge stays deny-all.
    pawbar_actions: list[dict[str, Any]] | None = None
    # Concierge catalog hint (C1). The widget's product catalog (capped, JSON of
    # {id, name, price_cents, currency}) so the preamble can name real products
    # and the agent emits pawbar-card fences with real ids. Only for the preamble;
    # the tools re-load the live widget, so this never feeds an effect.
    pawbar_catalog: list[dict[str, Any]] | None = None
    # Otherhand hints — stamped by the /other-hand page on EVERY turn (the page
    # changes every time the user lifts the pen, so neither hint is stable).
    #
    # ``snapshot_path`` is the absolute path ``POST /other-hand/pages/{id}/snapshot``
    # just wrote the page PNG to. It is the agent's only way to SEE the page:
    # attachments do not carry vision on this pipeline, but ``Read`` is in the
    # agent's default SDK tool set and reads images natively, so the preamble
    # points at the path and the agent reads it off disk. The client never
    # invents this value — it echoes back what the endpoint returned, and the
    # endpoint builds it from the workspace jail root, so a hostile client can
    # only ever name a path inside its own workspace's scratch dir.
    #
    # ``free_y`` is the y coordinate (in the page's fixed 1240x1754 logical
    # space) below which the page is empty. Carried as a STRING to match every
    # other scalar hint on this wire — the handler coerces and drops a
    # non-numeric value rather than raising. The agent is told to put everything
    # it adds at y >= free_y; the frontend's placement guard enforces it anyway.
    snapshot_path: str | None = None
    free_y: str | None = None
    # ``book_path`` is the absolute path of the READ-ONLY source page shown
    # beside the notebook in book mode (added 2026-08-26). The agent Reads it
    # to see what the user circled or underlined, and NEVER draws on it — the
    # page-ops coordinate space still addresses the notebook alone, so the
    # frozen contract is untouched. ``None`` on a plain notebook page.
    book_path: str | None = None
    # ``mark_box`` is "x1,y1,x2,y2" in the page's logical space: exactly where
    # the reader's pen went on the book. The client already knows this, so we
    # TELL the agent rather than make it re-derive a circled region from a
    # rasterised page of dense body text — which it does badly.
    mark_box: str | None = None
    # The marked region re-rendered at high resolution (scans, figures,
    # equations) and the exact words under the mark, read off the PDF's own
    # text layer (born-digital pages). Two channels because they fail in
    # different places: no text layer on a scan, no crop worth reading on a
    # pure-text page.
    mark_image_path: str | None = None
    mark_text: str | None = None
    # Compact JSON of what is already ON the page (text content with exact
    # coordinates, shape/user-ink bounding boxes), measured client-side from
    # the live stroke model AFTER the placement guard. The agent anchors its
    # annotations to these coordinates rather than to its memory of what it
    # emitted — which the guard may have shifted.
    scene: str | None = None


@dataclass(frozen=True)
class SurfacePreamble:
    """What one surface handler produced: the text, and what it read.

    ``cache_key`` is REQUIRED and has no default, so a handler author cannot
    ship without answering "what does this preamble depend on". The answer
    reaches the agent's prompt as the ``surface`` layer's cache key, which a
    backend caching an agent object folds into its own key — a key that holds
    still while the text moves is how a user ends up reading a description of a
    pocket that no longer looks like that.

    What a good answer looks like, in descending order of preference:

      * a REVISION of the mutable thing the handler read (a pocket's
        ``updatedAt``). Strongest, because it moves even when the rendered text
        cannot: the pocket preamble shows the first 12 of N widgets and
        truncates at 1500 chars, so editing widget 13 is invisible in the text
        and visible in ``updatedAt``.
      * a digest of what the handler actually rendered
        (``_helpers.content_key``), for the handlers that read a LIST with no
        single revision to point at. Weaker than a revision — it cannot see
        past truncation — but it cannot claim stability it does not have, and
        what it cannot see is by definition not in the prompt either.
      * the surface kind plus the ``meta`` fields the handler read, for the
        handlers that read nothing mutable at all. Their text is a pure
        function of ``meta``, so this is exact rather than approximate.

    ``None`` means volatile: the layer keeps its text and stays out of the
    digest. Correct for a handler that genuinely cannot say what it depends on;
    NOT a shortcut for one that has not thought about it, because a volatile
    layer gives a caching backend nothing to notice a change with.
    """

    text: str
    cache_key: str | None

    def __post_init__(self) -> None:
        if self.cache_key == "":
            raise ValueError(
                "cache_key must be a non-empty string or None; "
                "None is how a handler declares its preamble volatile"
            )


@dataclass(frozen=True)
class SurfaceContext:
    """Resolved surface state, ready to be embedded in the agent's prompt.

    Multi-tenant — ``workspace_id`` and ``user_id`` are required at
    construction time per the entity rules' tenancy-at-construction
    contract. Constructing one without tenancy info is a type error.

    ``preamble`` is the rendered XML-ish block. Since PA-2 it rides its own
    prompt layer (``pocketpaw.prompt.surface``) rather than being prepended to
    the dynamic context, so it sits above the per-turn material instead of
    inside the "Your Knowledge Base" wrapper. Empty when the handler failed or
    had nothing meaningful to say — the chat path keeps going regardless.

    ``preamble_cache_key`` is the handler's answer to "what did I read"
    (see :class:`SurfacePreamble`), threaded to the OSS prompt layer as a plain
    ``str``. ``None`` covers both "no key claimed" and every fall-back path
    below — an invalid body, an unregistered kind, a handler that raised. Those
    all render an EMPTY preamble, so they contribute the same prompt as having
    no surface at all and should hash alike; the assembler makes the same
    argument for two different exceptions in one layer. It defaults to ``None``
    so a legacy constructor keeps working, but ``resolve_surface_context``
    always passes it explicitly — a produced preamble with a silently dropped
    key is the failure this field exists to prevent.
    """

    workspace_id: str
    user_id: str
    kind: SurfaceKind
    meta: SurfaceMeta
    preamble: str
    preamble_cache_key: str | None = None


@dataclass(frozen=True)
class SurfaceProfile:
    """The behavioral policy a surface imposes on the chat agent.

    Resolved once per request from ``SurfaceKind`` via ``resolve_profile``.
    The single source of truth for surface-specific agent shaping, so
    consumers branch on a typed field rather than re-deriving rules from a
    bare ``kind ==`` check scattered across the codebase.

    Fields:
      * ``ripple_mode`` — whether the ripple LAW applies. ``"on"`` (the default
        — every surface, plus the /sites ripple-create and refine modes) keeps
        INLINE_RIPPLE_SYSTEM_PROMPT; ``"off"`` omits it (the /sites *svelte
        create* mode only — the agent hand-authors SvelteKit, so the "default to
        ui-spec" LAW is actively wrong; the ripple-create and refine /sites modes
        still author/edit a ripple spec and KEEP it); ``"trim"`` is reserved for
        a future slimmed variant (declared, not yet used).
      * ``allowed_sdk_tools`` — optional SDK-tool allowlist (``None`` = no
        surface restriction). Declared for PR 2; not consumed in PR 1.
      * ``deny_mcp_tool_ids`` — MCP tool ids this surface forbids. ENFORCED: the
        resolved set is threaded to the OSS backend's ``run`` as a plain
        ``frozenset[str]`` (``deny_mcp_tool_ids``) and subtracted from
        ``allowed_tools`` before the SDK launches. Non-empty only on the /sites
        svelte-create row.
      * ``skill_names`` — skills this surface surfaces to the agent. Tested DATA;
        skill-surfacing consumption lands in a later pass.
      * ``system_message_override`` — the surface's own system prompt, replacing
        the pocket-shaped DELIVERABLE stack. CONSUMED since 2026-07-22
        (fix/code-surface-denies-pocket-authoring); set on the CODE and
        OTHER_HAND rows, ``None`` everywhere else. When set,
        ``build_behavior_instructions`` appends it INSTEAD of the ripple LAW,
        the pocket-delegation rule, the per-backend pocket prompts, the home
        widget prompt, and the artifact-delivery rule.
        It does NOT displace the runtime-identity rule or the Composio rules —
        those describe the ENVIRONMENT (and the Composio ones are gated on
        Composio being enabled, so prompt and tool list agree); this field
        describes the WORK. Prompt text lives in ``surface/system_prompts.py``.

        Pairs with, and does not replace, the deny set: the prompt says what the
        surface DOES build, the deny set makes the alternatives unreachable.
        /code needed both — ``ripple_mode="off"`` plus a preamble forbidding
        pockets still lost to a request whose vocabulary matched the
        create-pocket skill, because a prohibition does not create a default.

    ``ripple_mode``, ``deny_mcp_tool_ids``, ``allow_mcp_tool_ids`` and
    ``system_message_override`` are CONSUMED today; ``allowed_sdk_tools`` and
    ``skill_names`` remain populated-but-inert so the descriptor's shape is
    locked now and later passes add enforcement without re-designing the
    primitive.
    """

    ripple_mode: Literal["on", "off", "trim"]
    allowed_sdk_tools: frozenset[str] | None = None
    # Restrictive per-MODE MCP allow-list (distinct from the additive
    # ``allowed_sdk_tools``). ``None`` = no restriction (keep every MCP tool).
    # When set, the OSS backend keeps only the MCP tools in this set PLUS the
    # universal pocket-creation grant + ripple widgets, and anything from an
    # always-allowed server (connectors / pocket lifecycle) — dropping the rest
    # so a mode's agent context stays lean. Applied AFTER ``deny_mcp_tool_ids``.
    allow_mcp_tool_ids: frozenset[str] | None = None
    deny_mcp_tool_ids: frozenset[str] = field(default_factory=frozenset)
    skill_names: frozenset[str] = field(default_factory=frozenset)
    system_message_override: str | None = None


class PocketSurfaceProfile(BaseModel):
    """Per-entity surface-profile override embedded on a Pocket.

    MIRRORS the surface-domain ``SurfaceProfile`` field-for-field, but with
    JSON-friendly types — plain ``list``s instead of ``frozenset``s — so it
    round-trips cleanly through Mongo and the wire. ALL fields are optional: a
    populated override may set only the dimensions an entity cares about and
    leave the rest ``None`` / empty.

    The entity-aware ``resolve_profile`` (entity-rooms chunk ①) hydrates a real
    ``SurfaceProfile`` from this, coercing the lists back to frozensets
    (roughly ``SurfaceProfile(**pocket.surface_profile)`` with the set fields
    wrapped). ``ripple_mode=None`` means "no opinion — fall back to the
    surface-kind default."

    Lives in the leaf domain module (not ``models/pocket.py``) so both the
    Beanie ``Pocket`` document and ``pockets.dto`` can import it without
    ``pockets.dto`` reaching into ``models.*`` (OSS-EE boundary contract).
    """

    ripple_mode: Literal["on", "off", "trim"] | None = None
    allowed_sdk_tools: list[str] | None = None
    allow_mcp_tool_ids: list[str] | None = None
    deny_mcp_tool_ids: list[str] = Field(default_factory=list)
    skill_names: list[str] = Field(default_factory=list)
    system_message_override: str | None = None

    model_config = {"populate_by_name": True}


__all__ = [
    "SurfaceKind",
    "SurfaceMeta",
    "SurfaceContext",
    "SurfacePreamble",
    "SurfaceProfile",
    "PocketSurfaceProfile",
]

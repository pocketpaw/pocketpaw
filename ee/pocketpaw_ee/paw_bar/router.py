# ee/paw_bar/router.py — HTTP surface for the Paw Bar widget layer.
# Updated: 2026-08-21 (fix/paw-bar-preview-frame-ancestors) — the bar renders in the
#   builder's site preview. GET /paw-bar/frame gated the embedder on the Site's
#   ``allowed_origins`` alone, but the builder previews a site by framing its real
#   published page, so the bar's iframe sits TWO deep — dashboard → site page → bar —
#   and ``frame-ancestors`` is matched against EVERY ancestor, not just the immediate
#   parent. Nothing in the publish path knows the dashboard exists, so no Site
#   allowlist ever named it and every preview logged "Framing '<backend>' violates
#   ... frame-ancestors" and showed an empty box. New ``_public_frame_ancestors``
#   appends the dashboard origin — ``PAWBAR_DASHBOARD_ORIGIN`` or, unset (the state
#   every deploy we ship is in), the already-declared
#   ``POCKETPAW_API_CORS_ALLOWED_ORIGINS`` — through the SAME sanitizer, and new
#   ``_ancestor_sources`` dedupes so a dashboard that is also an allowlist entry
#   lists once. Deliberately NOT a widening of ``allowed_origins``: that list also
#   gates chat and lead capture via ``origin_allowed``, so this widens the render
#   gate and nothing else. Fail-closed still reads the Site's allowlist alone, so a
#   Site with no embedders stays unrenderable. Neither var set → the header is
#   byte-identical to before. The session-authed owner preview
#   (``/paw-bar/admin/site/{id}/preview-frame``) is untouched and still framed by
#   ``_dashboard_origin`` alone.
# Updated: 2026-08-16 (fix/paw-bar-role-gates) — the last nine ``require_scope`` gates
#   in this router become ROLE gates, so the ``require_scope`` import is gone. The
#   two admin site-settings routes (the concierge kill switch) and the seven widget
#   CRUD routes now take ``_require_paw_bar_read`` on the two GETs and
#   ``_require_paw_bar_manage`` on every mutation — the same pair the D2 reads and
#   the knowledge endpoints already used, so the whole admin surface of this router
#   is finally gated one way. ``_require_paw_bar_manage`` moved up beside its read
#   sibling because widget CRUD is now its first caller.
#   ``require_scope("admin")`` is an OSS SINGLE-TENANT primitive: it accepts
#   ``request.state.full_access`` (master token / session cookie / localhost, and in
#   cloud only an ``is_superuser`` platform admin), a file-backed ``pp_`` API key, or
#   a ``ppat_`` OAuth token. A CLOUD workspace admin presents none of those, so
#   PATCH /paw-bar/admin/site/{site_id}/settings answered its intended caller with
#   403 "Missing required scope: admin" — the kill switch was unreachable for the
#   owner it belongs to. The same line failed the opposite way on self-hosted: a
#   session cookie sets ``full_access``, so ANY signed-in dashboard user, member
#   role included, could rotate a widget's token or delete it. One swap closes both.
#   No new actions: ``paw_bar.read`` / ``paw_bar.manage`` already sit at ADMIN in
#   guards/actions.py.
# Updated: 2026-08-01 (AL-2, paw-bar emitters) — three conversation write paths
#   now record their agent-ledger beats through ``paw_bar/ledger.py`` (fail-soft,
#   never raises, ~4 lines each):
#     ~ POST /paw-bar/chat — ``paw.conversation.started``. Fired on every turn
#       and deduped by the ledger on ``widget:customer`` rather than gated on the
#       handler's ``is_new_conversation`` flag: that flag comes from a read this
#       handler is explicitly allowed to lose (the fail-closed mute arm), and a
#       "conversations started" count that drops those is worse than one absorbed
#       insert per turn.
#     ~ PATCH .../conversations/{ref} and POST .../conversations/{ref}/reply —
#       both hand the BEFORE and AFTER rows to
#       ``ledger.emit_conversation_transition``, which records
#       ``paw.conversation.takeover`` when the mute goes on and
#       ``paw.handoff.resolved`` when the thread leaves ``needs_human``. Read as
#       a row diff, not from the request body, so the two endpoints cannot record
#       the same transition differently and a no-op patch records nothing.
#   All three route the ledger FILE by ``workspace_id`` / ``ctx.workspace_id`` —
#   the authenticated tenant these handlers already scope every other store read
#   by, and a store-path-safe token (the widget OWNER label is not).
# Updated: 2026-07-31 (in-thread approvals) — the admin transcript now carries
#   ``pending_actions`` (this visitor's still-PENDING decisions, mapped to the
#   same DecisionItem shape the decisions tab serves) and ``bot_paused``, and
#   DecisionItem gains ``customer_ref``. The dashboard's ConversationThread has
#   had the approval card since slices 2+4, with TWO wire sources — transcript
#   ``pending_actions`` preferred, decisions-list-filtered-by-ref as fallback —
#   and this deployment served NEITHER, so a real approval sat behind the
#   "approve it from Decisions" notice (found live 2026-07-31). Both new reads
#   are failure-soft: a broken decisions read costs the thread its cards, never
#   the transcript. Settled decisions stay absent from pending_actions on
#   purpose — the card list is a to-do, not a log.
# Updated: 2026-07-31 (owner inbox, slice 3) — THE ESCAPE HATCH. A visitor can
#   always reach a person, and the owner is told when it happens:
#     + POST /paw-bar/request-human — {key, w, customer_ref, message?, contact?}
#       → {ok, handoff_id, state, message}. PUBLIC, same ``_front_gate_for_key``
#       chain as chat/action/articles (404 → 429 → 401 → 403 origin, 403 binding,
#       plus the site kill switch inside the key resolver) and the same injection
#       screen on the free-text note. It runs the SHARED producer
#       (``paw_bar.handoff.raise_handoff``) the concierge's own
#       ``pawbar_request_human`` tool runs, so a visitor-raised and an
#       agent-raised handoff are one record. Accepted in EVERY conversation
#       state, including while the bot is answering confidently — the whole point
#       is that reaching a human never depends on the agent offering it.
#     ~ GET .../site/{id}/handoffs — unchanged in shape, no longer always empty.
#     ~ POST /paw-bar/chat — notifies the workspace owner on the FIRST turn of a
#       new conversation (the row's absence before the upsert is the signal), and
#       the muted-bot branch notifies on a visitor reply while a human holds the
#       thread. Both awaited but never-raising (``paw_bar.notify``), so a dead
#       notifier costs the owner a badge, never the visitor an answer.
#   Notification fan-out is the WORKSPACE OWNER ALONE in v1 (design §10 Q4).
# Updated: 2026-07-30 (owner inbox, slice 2) — TYPE-TO-TAKEOVER. The owner types,
#   the bot shuts up, and the visitor sees a human:
#     + POST .../site/{id}/conversations/{customer_ref}/reply — {text} →
#       {ok, message, conversation}. One call does all of it: persists an ``owner``
#       line, mutes the bot (``bot_paused``, stamped so the idle clock starts),
#       stamps ``last_owner_at``, clears the unread counter, and REOPENS a
#       closed/snoozed thread. ``message`` is a TranscriptMessage (the shape the
#       thread already renders); ``conversation`` is the same ConversationRow the
#       PATCH echoes. Gated on ``paw_bar.manage``, workspace-scoped like its
#       siblings. NOT a run: an owner reply never becomes a ChatRunDoc, so the
#       metering sweeper can't bill the owner for typing.
#     ~ POST /paw-bar/chat — the MUTE, checked BEFORE the run is created: when the
#       conversation is paused, the visitor's line is kept (under the same
#       retention toggle), the thread flips to ``needs_human``, and the response is
#       exactly two SSE frames — ``human_replying`` {"message": …} then
#       ``stream_end`` — with NO run dispatched, no metering, no tool surface.
#       Never an empty stream: the glass app reads clean-but-empty as "No reply."
#     + GET /paw-bar/messages/{widget_id}/{customer_ref}?signed_key=&after= — the
#       visitor-side poll, PUBLIC, same ``_front_gate_for_key`` chain as articles
#       (404 → 429 → 401 → 403) and the same ``_request_origin`` same-origin fix.
#       Returns {messages:[{role,content,at}], bot_paused} and NOTHING else — no
#       notes, tags, assignee, contact_email, or queue state ever cross to a
#       visitor, and a visitor's own stored lines are not echoed back.
#     ~ GET .../conversations/{customer_ref} — the transcript now MERGES two
#       sources by timestamp: ChatRunDoc (user/assistant) and the new
#       paw_bar_owner_messages rows (owner/system, plus muted-turn visitor lines
#       presented as "user"). ``TranscriptMessage.role`` widens from
#       user|assistant to user|assistant|owner|system — ADDITIVE.
#   IDLE AUTO-RESUME (§10 Q2 — 4h, hard-coded): a mute with no owner activity for
#   4h ends itself. Computed on READ in the store, so chat and the poll agree, and
#   materialized by both with one system message explaining the hand-back.
# Updated: 2026-07-30 (owner inbox, slice 1) — the concierge LOG becomes a QUEUE.
#   A lifecycle row (``paw_bar_conversations``) is now upserted on every visitor
#   turn from ``concierge_chat`` (failure-soft — inbox bookkeeping never costs a
#   visitor their answer) and joined onto the owner reads:
#     ~ GET  .../site/{id}/conversations — ADDITIVE. Every existing field keeps
#       its name and meaning; each item GAINS state / bot_paused /
#       unread_for_owner / tags / snooze_until / contact_email / display_name /
#       has_pending_action, all with safe defaults so a LEGACY conversation with
#       no state row still lists (no backfill, ever). The response gains
#       ``counts`` (per-state totals, UNFILTERED so the filter chips stay stable)
#       and an optional ``?state=`` filter (unknown value → 422).
#     + PATCH .../site/{id}/conversations/{customer_ref} — {state?, snooze_until?,
#       tags?, note?, bot_paused?} → {ok, conversation}. ``note`` APPENDS a
#       private operator note attributed to the caller; ``tags`` replaces. Gated
#       on ``paw_bar.manage`` (this router's existing write action — there is no
#       ``paw_bar.write``). A conversation with no row yet gets one minted on this
#       first owner action; a ref with no concierge runs on the site 404s.
#     + GET  .../agent/{agent_id}/conversations — the agent-scoped union (D1): the
#       concierge IS a normal agent, so its widgets' sites are unioned into one
#       list, each item carrying site_id + site_name. ``widget_count`` / ``sites``
#       are the positive binding signal (an ordinary agent answers 200 with 0).
#   Snooze expiry is computed on READ in the store, so a snooze always ends on
#   time with no sweeper. ``has_pending_action`` reuses the decision rows the
#   Decisions list already reads — never a second query into Instinct.
# Updated: 2026-07-30 (reply sources + articles) — visible grounding for the
#   concierge. (1) ``concierge_chat`` now emits at most ONE ``event: sources``
#   SSE frame ({"sources": [{title, url}]}, max 3, deduped by url) after the
#   model stream completes and immediately BEFORE the terminal ``stream_end``
#   frame. Attribution is deliberately APPROXIMATE (Crisp-style): a server-side
#   KB search of the visitor's message against the SAME ``pocket:<pocket_id>``
#   scope the concierge run reads, filtered to the articles the site's page sync
#   produced (``Site.kb_article_ids``) and mapped back to public page URLs via
#   the ``site-<slug>`` article-id convention — NOT an exact tool trace. The
#   search runs CONCURRENTLY with the model stream so it adds ~0ms; fail-soft
#   (any error / timeout emits nothing). (2) GET /paw-bar/articles — a public
#   listing of the site's synced KB pages ({title, url, snippet ≤160}), capped
#   at 20, behind the SAME ``_front_gate_for_key`` chain as chat (404 → 429 →
#   401 → 403); no injection screen because there is no free text. The shared
#   front-gate now resolves via ``resolve_site_key_with_site`` and hands back
#   the Site too, so articles (and any future public read) can use owner-set
#   Site fields without a second query.
# Updated: 2026-07-30 (async decision delivery) — added POST
#   /paw-bar/decision-contact: a visitor whose request is still PENDING leaves
#   an optional email before closing the tab; the delivery hook later emails
#   them the same customer-facing reply the poll returns. Same fail-closed
#   armor as chat/action via the shared ``_front_gate_for_key``; email is
#   validated (RFC-ish regex + 254 cap → 422), stamped onto PENDING rows only,
#   and NEVER echoed back on any public read (the poll response omits it).
# Updated: 2026-07-30 (feat/paw-bar-autoembed) — added GET /paw-bar/widget.js, the
#   PUBLIC loader route. The glass bar could only ever be embedded by hand, and the
#   snippet the dashboard printed pointed at ``https://pp.pocketpaw.dev/widget.js``
#   — a placeholder host nobody provisioned, so even a pasted snippet 404'd. There
#   was no way to load the bar from anywhere. This route serves the loader bundle
#   off the SAME origin as the rest of the API, which is what lets ``sites.service``
#   embed it into a published site automatically (see ``paw_bar/embed.py``). The
#   file is resolved by ``paw_bar_widget_file()``: ``PAW_BAR_WIDGET_JS`` when set,
#   else the copy vendored in this package (``static/paw-bar.js``) so the route
#   resolves on any machine rather than depending on a sibling checkout. A missing
#   bundle returns a clean 404 naming the env var, never a stack trace. PUBLIC by
#   design and deliberately tenant-BLIND: it is a world-visible script served
#   byte-identically to every visitor of every site, so it takes no key, reads no
#   Site, and carries nothing tenant-specific — the per-site config rides on the
#   embedding ``<script>`` tag's data attributes, and the credential check happens
#   downstream at /paw-bar/frame.
# Updated: 2026-07-29 (concierge conversation memory) — a concierge turn is no
#   longer answered cold. ``concierge_chat`` built its ``RunSpec`` with
#   ``history=[]``, so the agent forgot the visitor's name between one message and
#   the next: the visitor is anonymous and has no ``Message`` rows, so the authed
#   surfaces' ``load_history_for_scope`` had nothing to read. The stored run docs
#   (``user_text`` + ``partial_text``, from the transcript work below) are now ALSO
#   the memory. (1) ``_concierge_runs_for_visitor`` extracts the (workspace,
#   context_type, scope_id, user_id) query that ``_load_transcript`` already used,
#   so the owner's transcript read and the agent's rehydration share ONE definition
#   of "this visitor's turns" — per-visitor, per-site, per-tenant isolation lives in
#   exactly one place. (2) ``_load_concierge_history`` shapes those rows into
#   ``[{"role","content"}]`` oldest-first (the shape ``load_history_for_scope``
#   returns), bounded by ``_HISTORY_TURN_CAP`` exchanges / ``_HISTORY_MESSAGE_CHARS``
#   per line / ``_HISTORY_TOTAL_CHARS`` overall — fitted newest-first so the budget
#   drops the OLDEST turns and the replay stays contiguous. Failure-soft: a read
#   error answers without memory instead of 500-ing the visitor. (3) The read
#   happens BEFORE ``create_run`` writes this turn's doc, so the current message
#   rides in ``content`` exactly once. (4) Gated on the SAME
#   ``concierge_store_transcripts`` toggle as the write — retention off means no
#   memory, which is the owner's privacy choice working rather than a gap to route
#   around.
# Updated: 2026-07-26 (site knowledge sync) — two owner endpoints over the site's
#   own knowledge: GET /paw-bar/admin/site/{id}/knowledge reports how many articles
#   the concierge can quote and how the last sync went, and POST
#   .../knowledge/sync re-reads the site's pages into pocket:<pocket_id> (the ONE
#   scope a concierge reads) without needing a re-publish. The sync also runs
#   automatically on publish and on agent provisioning; this is the manual handle.
#   The read gates on paw_bar.read, the sync on the new paw_bar.manage — both ADMIN,
#   but a mutation that spends compute does not ride a read gate. The sync is
#   awaited here (the owner clicked a button and wants the result) while the
#   automatic triggers are backgrounded.
# Updated: 2026-07-26 (concierge transcripts) — the visitor half of a conversation
#   is now written down, so an owner's transcript is a dialogue instead of the
#   agent talking to itself. (1) concierge_chat resolves the embed key through
#   ``resolve_site_key_with_site`` (same gates, hands back the Site the gate
#   already loaded) and sets ``RunSpec.persist_user_text`` — the visitor's message,
#   capped at ``_STORED_USER_TEXT_CHARS``, and ONLY when the site's
#   ``concierge_store_transcripts`` is on. The agent always receives the full
#   message; the toggle governs storage, not the answer. (2) ``_load_transcript``
#   emits the user turn (``ChatRunDoc.user_text``, stamped at run creation) before
#   the assistant turn (``partial_text``, stamped at completion); either may be
#   absent and the other still renders, so a retention-off site reads exactly as it
#   did before. (3) the conversations list falls back to the visitor's question
#   when a run produced no reply, instead of rendering a blank row. (4) the
#   settings GET/PATCH carry ``concierge_store_transcripts``. Turning the toggle
#   off stops collection on the next message; it does NOT purge stored lines.
# Updated: 2026-07-23 (feat/site-dedicated-agent) — auto-provision a DEDICATED
#   concierge agent per site. (1) create_widget: when the request carries NO
#   agent_id and the pocket resolves to a published Site, provision + bind one
#   dedicated agent after insert (via agent_provisioning.provision_widget_on_create)
#   and return the widget with agent_id set; a plain (non-site) widget stays unbound;
#   FAILURE-SOFT (a provision error logs + returns the widget unbound, never 500s
#   the create). A manual agent_id is honored and never replaced. (2)
#   update_site_concierge_settings: ANY PATCH that sets concierge_enabled=true
#   provisions the site's widget when it is still unbound (not only a false->true
#   transition — the E2 one-click "create dedicated agent" re-PATCHes enabled=true
#   on an already-enabled site as its provision hook), via
#   provision_on_concierge_enable; idempotent + a no-op on a bound widget; also
#   failure-soft. (3) _pawbar_frame_config now carries ``starters`` (the bound
#   agent's conversation starters, capped 4, empty default) — the owner preview
#   frame threads the bound agent's starters (via _bound_agent_starters); the public
#   frame passes []. (4) GET /paw-bar/admin/site/{id}/overview's widget block now
#   carries ``agent_name`` (the bound agent's display name, resolved via the agents
#   service) so the E2 dashboard card can show the concierge name + detect the
#   "<x> Concierge" dedicated pattern; empty when unbound or the agent no longer
#   resolves (a dangling agent_id degrades to absent, never 500s the overview). The
#   ASG-1 identity fields (welcome_message/conversation_starters) and agent free-form
#   tags are ABSENT on this branch, so their seeding degrades to a graceful no-op
#   (the unbound-chat 409 invariant is unchanged).
# Updated: 2026-07-17 (D5 owner preview frame) — added GET
#   /paw-bar/admin/site/{site_id}/preview-frame → the concierge bar frame HTML for
#   the OWNER to test inside the dashboard. SESSION-authed (paw_bar.read role gate)
#   sibling of the public /paw-bar/frame: REUSES ``_pawbar_bootstrap_html`` + a new
#   shared ``_pawbar_frame_config`` builder (the public frame now calls the same
#   builder — behavior unchanged, just no forked config dict). Two differences from
#   the public frame: (1) CSP ``frame-ancestors`` = the dashboard origin from the new
#   ``PAWBAR_DASHBOARD_ORIGIN`` env (default http://localhost:5173), sanitized to a
#   single host[:port] — never the Site allowlist, never ``*``/Origin/Referer; (2)
#   served REGARDLESS of ``concierge_enabled`` so a paused bar can be previewed
#   (chat/action still obey the kill switch, unchanged). The public visitor frame's
#   security (CSP from allowed_origins, kill-switch 403) is untouched.
# Updated: 2026-07-17 (D2 conversation transcript) — added GET
#   /paw-bar/admin/site/{site_id}/conversations/{customer_ref} → {customer_ref,
#   messages:[{role,content,created_at}], count}. Same role gate (paw_bar.read) +
#   site→widget→pocket resolution as the other reads; the transcript is the
#   concierge ``ChatRunDoc`` runs for (pocket, customer_ref), most-recent 200,
#   oldest-first; 400 on a bad customer_ref, 404 when the ref has no conversation
#   here. (The v1 caveat that every turn was role "assistant" no longer holds —
#   see the 2026-07-26 entry above; visitor lines are stored when the site's
#   retention toggle allows.)
# Updated: 2026-07-16 (D2 security review — role gate + empty-pocket guard) —
#   the four D2 reads now gate on ``require_action("paw_bar.read")`` (ADMIN — the
#   caller's WORKSPACE ROLE), NOT the coarse ``require_scope("admin")`` that
#   admitted any authenticated dashboard user (a member/viewer could read another
#   owner's visitor conversations). The role check binds to the SESSION workspace
#   (``workspace_dep=current_workspace_id``), the same one the reads scope data to,
#   so tenancy stays session-derived. Also ``_resolve_site_and_widget`` now returns
#   no widget on an empty ``Site.pocket_id`` (finding #2 — an empty pocket_id would
#   otherwise widen ``list_widgets`` and resolve a sibling's widget).
# Updated: 2026-07-16 (Paw Bar concierge dashboard reads, D2) — added four OWNER
#   aggregation reads for the per-site Concierge dashboard, all under
#   /paw-bar/admin/site/{site_id}/*, role-gated (``require_action("paw_bar.read")``)
#   + workspace-scoped: (1) GET /overview — {widget, enabled, greeting, counts} with
#   cheap COUNT/distinct counters; (2) GET /conversations — recent concierge
#   ``ChatRunDoc`` runs grouped by customer_ref (LISTABLE via the run model's
#   (workspace, context_type, scope_id, createdAt) index; bounded scan + optional
#   cursor); (3) GET /decisions — the site widget's paw_bar ``DecisionStatus`` rows
#   (``WHERE widget_id = ?``); (4) GET /handoffs — ``_paw_handoffs`` reserved Fabric
#   objects scoped to the widget (no producer yet → empty in v1). Tenancy runs at
#   TWO gates: the Site is loaded workspace-scoped (cross-tenant id → 404), then its
#   paw-bar widget is resolved from ``Site.pocket_id`` ALSO workspace-scoped; the
#   decisions/conversations/handoffs filters then bind to THAT widget/pocket — never
#   pocket-wide or workspace-wide — so a sibling site or a second widget in the same
#   workspace can never appear (the leak surface the security review checks).
#   Decisions read the singleton ``DecisionStatus`` table (the 1:1 mirror of the
#   Instinct proposals) rather than the Instinct store directly, because paw-bar
#   stamps the Instinct row's in-row ``workspace_id`` with the widget OWNER, not the
#   physical workspace, and the Instinct proposal's physical file is
#   ContextVar-dependent — the DecisionStatus table has neither hazard.
# Updated: 2026-07-16 (Paw Bar concierge settings + kill switch, D1 / SS-6) — the
#   owner's on/off toggle + greeting. (a) GET /paw-bar/frame now refuses (403
#   ``concierge_disabled``) when the resolved Site has ``concierge_enabled=False``,
#   mirroring the empty-allowlist 403; chat + action/cart get the SAME 403 via
#   ``resolve_site_key`` (the shared resolver), so all three public entry points fail
#   closed on the kill switch, re-read per request. (b) The frame's ``window.__PAWBAR__``
#   config now carries ``greeting`` (``Site.concierge_greeting``) for the glass app.
#   (c) New admin surface: GET + PATCH /paw-bar/admin/site/{site_id}/settings —
#   ``require_scope("admin")`` + workspace-scoped (cross-tenant id → 404), reads/writes
#   ONLY ``concierge_enabled`` + ``concierge_greeting`` on the Site doc (the natural
#   owner key — the fields live on the Site, not the widget).
# Updated: 2026-07-16 (C1 hardening) — (a) the shared front-gate now validates
#   customer_ref against a charset+length bound (400) as its cheapest check;
#   (b) GET /paw-bar/cart records a cart-read marker so read enumeration counts
#   toward the rate limiter like writes; (c) concierge_chat threads the widget's
#   catalog (capped at _MAX_PREAMBLE_CATALOG) onto surface_meta so the concierge
#   preamble can name real products, not just the action verbs.
# Updated: 2026-07-16 (Paw Bar action registry, C1) — the visitor commerce loop.
#   (1) POST /paw-bar/action {key,w,customer_ref,verb,args} and GET /paw-bar/cart
#   ?key&w&customer_ref — PUBLIC endpoints with the SAME armor as concierge chat,
#   factored into ``_front_gate_for_key``: resolve_site_key fail-closed → dual-mode
#   origin gate → within_rate_limit → widget↔workspace/pocket binding (403). Both
#   call the SHARED ``actions.execute_action`` / cart store — never a parallel path.
#   Args are structured (no free text), so no injection screen; the executor
#   enforces the schema strictly. (2) PATCH /paw-bar/widgets/{id} — admin + owner-
#   token, workspace-scoped (cross-tenant id → 404) partial update of agent_id +
#   name/allowed_domains/rate limits (spec stays on update_spec). The paw-enterprise
#   agent-binding UI drives it.
# Updated: 2026-07-15 (glass frame asset versioning) — the frame HTML now appends
#   ``?v=<newest bundle mtime>`` to the pawbar.js/css URLs (``_asset_version``).
#   The StaticFiles mount sends no Cache-Control, so browsers heuristically cache
#   the bundle and pin embedders to a STALE app after a deploy (bit the first live
#   demo). Versioned URLs bust every embedder's cache on deploy, no restart, no
#   hard-reload. ``pawbar_app_dir()`` is now the shared dir resolver (imported by
#   the cloud mount — same never-drift pattern as PAWBAR_APP_MOUNT).
# Updated: 2026-07-15 (Paw Bar glass frame, A1) — the iframe FRAME endpoint + the
#   CSP-based origin model. (1) GET /paw-bar/frame?key=<signed_key>[&w=&po=] serves
#   the glass app document from OUR origin: it authenticates the embed key
#   (``lookup_site_by_key`` — the SAME chain resolve_site_key runs), FAILS CLOSED on
#   an empty ``allowed_origins`` (403 refuse-to-render), and emits
#   ``Content-Security-Policy: frame-ancestors <Site.allowed_origins>`` so the
#   browser refuses to render the iframe inside any non-allowlisted parent — THIS
#   (not a per-request Origin header) becomes the embedder gate. The body seeds
#   ``window.__PAWBAR__`` (JSON-safe, ``<``-escaped) before loading pawbar.js/css
#   from a configurable StaticFiles mount (``PAWBAR_APP_MOUNT`` / ``PAWBAR_APP_DIR``,
#   wired in cloud/__init__.py). No ``X-Frame-Options`` — it is OBSOLETE beside
#   frame-ancestors and a conflicting XFO:DENY would block the frame. (2) The
#   /paw-bar/chat origin check is now DUAL-MODE: an inline/legacy widget request is
#   still gated against ``Site.allowed_origins`` (fail-closed, via resolve_site_key),
#   while an iframe-mode request (Origin == our ``PAWBAR_FRAME_ORIGIN``) is accepted
#   because the embedder was already gated by the frame CSP at render time. The old
#   step-2 ``_origin_allowed(widget, ...)`` footgun (empty widget.allowed_domains =
#   allow-all) NO LONGER gates chat — the chat path converges on the fail-closed
#   ``Site.allowed_origins`` allowlist. RESIDUAL (unchanged): CSP binds BROWSERS
#   only; the world-visible key + a raw curl POST was always possible — the real
#   controls remain the rate-limit + injection screen + zero-authority CONCIERGE
#   scope. ``_origin_allowed`` stays in use for the spec/ingest/decision endpoints.
# Updated: 2026-07-14 (Paw Bar concierge seam, T2) — added POST /paw-bar/chat, a
#   PUBLIC, anonymous, streaming (SSE) concierge chat endpoint. Front-gate:
#   _origin_allowed (403) → within_rate_limit (429) → injection-screen the
#   free-text message (400 on HIGH, via the new _screen_message_for_injection).
#   Auth: resolve_site_key (401/403 fail-closed) — the embed key is the ONLY
#   credential. Binds the widget to the RESOLVED key's workspace+pocket (403 on
#   mismatch — finding #2, no sibling-pocket reach), requires a bound agent (409),
#   then dispatches a CONCIERGE-scoped RunSpec over the SAME machinery the authed
#   chat uses (create_run + executor.submit + execute_run + transport) and relays
#   its frames as SSE. The tool lockdown + KB pocket-scoping live in the CONCIERGE
#   SurfaceProfile + scope, not here. Refactored the injection screen into the
#   shared _scan_text_is_safe primitive (event ingest + chat both reuse it).
# Updated: 2026-07-14 (concierge connector lockdown) — concierge_chat refuses
#   fail-closed (409) when the pocket exposes any connector (checked via
#   list_pocket_connectors), because _CONCIERGE_DENY cannot strip dynamic
#   per-workspace composio connector tool ids. Pilot posture; the GA fix is an
#   untrusted-mode in claude_sdk (see the guard's TODO(GA-blocker)).
# Updated: 2026-07-14 (Paw Bar concierge seam, T3) — CreateWidgetRequest accepts
#   an optional agent_id; create_widget stamps it onto the PawBarWidget so a
#   concierge widget is bound to the agent that answers its chats. Purely
#   additive — omitting it keeps the existing "" (unbound) behavior.
# Updated: 2026-07-11 (W4a spec revisions) — POST /paw-bar/widgets/{id}/spec/
#   rollback (admin + owner-token, workspace-scoped like update_spec) restores
#   the latest archived spec revision; 409 when no revision exists.
# Updated: 2026-07-11 (W4a tenancy seam) — (1) Admin CRUD (create / list /
#   update-spec / rotate-token / delete) now threads the caller's active
#   workspace via Depends(current_workspace_id): create stamps the row, the
#   rest scope lookups + mutations so a cross-tenant widget id 404s and never
#   mutates. (2) Public-path fix (the cross-tenant Fabric leak): ingest stays
#   token-only but derives the tenant from the widget ROW —
#   _apply_event_mapping now calls get_fabric_store(workspace_id=
#   widget.workspace_id or None) instead of the bare shared store; legacy
#   unstamped rows ('' → None) keep the old single-tenant behavior.
# Updated: 2026-07-08 — Renamed widget "Paw Print" → "Paw Bar" (routes /paw-print→/paw-bar,
#   header X-Paw-Print-Token→X-Paw-Bar-Token, tag PawPrint→PawBar, source_connector
#   "paw_print"→"paw_bar"). Hard-rename — widget has zero deployments. The separate
#   one-word audit feed (past-tense record) is a DIFFERENT feature, unaffected.
# Created: 2026-04-13 (Move 3 PR-B) — Spec serving (public, CORS-gated),
# widget CRUD (owner-authed via access_token), event ingest (rate-limited,
# domain-enforced, injection-screened, Fabric-mapped). The widget.js bundle
# built in PR-C consumes these endpoints.
# Updated: 2026-05-30 — Replaced the always-None Guardian no-op screen
# (getattr(guardian, "check_input") — GuardianAgent never exposed that
# method, so the check was a permanent accept-all) with the real
# InjectionScanner. The stringified event payload is now heuristically
# screened and dropped on a HIGH-or-higher threat. Renamed the helper to
# _screen_event_for_injection and the rejection reason to
# "injection_rejected".
# Updated: 2026-06-10 (W0b security fix) — Closed an unauthenticated
# access-token leak on the widget-management surface. (1) Widget CRUD
# (create / list / update-spec / delete) now requires a fully-authenticated
# dashboard caller via Depends(require_scope("admin")); previously these
# routes had NO route-level auth, and the /api/v1/* mount is auth-OPTIONAL at
# the middleware level, so an unauthenticated caller could reach them. (2) The
# list and read responses now serialize PawBarWidgetPublic, which omits
# access_token — the per-widget owner credential no longer leaves the server
# in a list/read payload. The token is still returned by the explicit,
# authenticated create + rotate-token paths so an owner can capture it once.
# The public spec-serving and event-ingest endpoints stay unauthenticated by
# design (origin/CORS-gated for the embedded widget bundle).
# Updated: 2026-06-11 (gap2 — close the customer decision loop) — An accepted,
# mapped customer event no longer dead-ends at a Fabric object: ingest now also
# raises an Instinct proposal via decision_loop.propose_customer_decision and
# parks a PENDING DecisionStatus row (best-effort — a loop failure never fails
# the ingest response). Added a public, CORS-gated poll endpoint
# (GET /paw-bar/events/{widget_id}/decision/{customer_ref}) so the rendered
# widget can read the owner's decision back out — the back-half of the loop. The
# approve/reject delivery hook lives in the instinct router (it owns the human
# decision); see decision_loop.deliver_customer_decision.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from pocketpaw.paw_bar.appearance import ConciergeAppearance
from pocketpaw.paw_bar.models import (
    MAX_PAYLOAD_BYTES,
    ConversationState,
    DecisionState,
    OwnerMessageRole,
    PawBarEvent,
    PawBarEventMapping,
    PawBarSpec,
    PawBarWidget,
    PawBarWidgetPublic,
)
from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_action
from pocketpaw_ee.paw_bar.handoff import PAW_HANDOFFS_TYPE

logger = logging.getLogger(__name__)

# Role gate for the D2 concierge dashboard reads. ``require_action`` enforces the
# caller's WORKSPACE ROLE against the ``paw_bar.read`` rule (ADMIN — owner/admin
# only, not member); replaces the coarse ``require_scope("admin")`` that admitted
# any authenticated dashboard user. ``workspace_dep=current_workspace_id`` binds
# the role check to the SESSION's active workspace (never a path/query value — the
# D2 routes carry ``{site_id}``, not ``{workspace_id}``, so the default
# path-sourced dep would read an attacker-suppliable query param), the SAME
# workspace the reads scope their data to.
_require_paw_bar_read = require_action("paw_bar.read", workspace_dep=current_workspace_id)

# The write half of the same pair (ADMIN, same session-workspace binding). Declared
# here beside its read sibling because the widget CRUD routes — the first routes in
# the file — now gate on it; it used to sit next to the knowledge-sync routes that
# introduced it, several hundred lines below its first use.
_require_paw_bar_manage = require_action("paw_bar.manage", workspace_dep=current_workspace_id)

router = APIRouter(tags=["PawBar"])

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

# Cap the catalog threaded into the concierge preamble so a large catalog can't
# bloat the prompt (C1). The full catalog is still enforced by the spec cap.
_MAX_PREAMBLE_CATALOG = 50


def _store():
    from pocketpaw_ee.api import get_paw_bar_store

    return get_paw_bar_store()


def _require_owner_token(widget: PawBarWidget, header_token: str | None) -> None:
    if not header_token or header_token != widget.access_token:
        raise HTTPException(status_code=401, detail="Invalid or missing access token")


def _origin_allowed(widget: PawBarWidget, origin: str | None) -> bool:
    """Match an inbound Origin header against the widget's allowed_domains.

    Empty `allowed_domains` disables the check — useful for local demos but
    must be set in production. The match is host-only so ports and paths don't
    matter: `https://brewco.com:443/menu` matches `brewco.com`.
    """
    if not widget.allowed_domains:
        return True
    if not origin:
        return False
    host = origin.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    host = host.split(":", 1)[0]
    return host in widget.allowed_domains


# ---------------------------------------------------------------------------
# Glass frame (iframe) — CSP-based origin model (A1)
#
# The vanilla widget runs in the HOST page's origin, so a chat request's Origin
# header IS the embedder and the per-request Origin gate authenticates WHO
# embedded. Served in an iframe from OUR domain, every request carries OUR frame
# origin — identical for all embedders — so that gate degenerates. The frame
# endpoint moves the embedder gate to a CSP ``frame-ancestors`` header (the browser
# refuses to render our iframe inside a non-allowlisted parent), and the chat
# endpoint's origin check becomes dual-mode (see ``concierge_chat``).
# ---------------------------------------------------------------------------

# URL path the glass app bundle (pawbar.js + pawbar.css) is served from. The
# StaticFiles mount lives in ``ee/cloud/__init__.py``; both the mount and the frame
# HTML import THIS constant so the ``<script src>`` and the mount path never drift.
PAWBAR_APP_MOUNT = "/pawbar-app"


def pawbar_app_dir() -> Path:
    """Directory the glass app bundle is served from (PAWBAR_APP_DIR overridable).

    Single source of truth shared with the StaticFiles mount in
    ``ee/cloud/__init__.py`` (same never-drift pattern as ``PAWBAR_APP_MOUNT``).
    """
    return Path(os.environ.get("PAWBAR_APP_DIR", str(Path.home() / ".pocketpaw" / "pawbar-app")))


def _asset_version() -> str:
    """Cache-busting version stamp for the glass app assets.

    The StaticFiles mount serves pawbar.js/css with no ``Cache-Control``, so
    browsers fall back to heuristic freshness and can pin an embedder to a STALE
    bundle after a deploy (bit the first live demo: a sizing fix shipped but the
    browser kept replaying the old JS). The frame HTML appends ``?v=<newest
    mtime>`` to both asset URLs so every deploy mints new URLs and busts every
    embedder's cache with no server restart and no manual hard-reload. Two
    ``stat`` calls per frame render — negligible next to the DB key lookup.
    Returns "0" when the bundle isn't dropped in yet (assets 404 either way).
    """
    newest = 0
    for name in ("pawbar.js", "pawbar.css"):
        try:
            newest = max(newest, int((pawbar_app_dir() / name).stat().st_mtime))
        except OSError:
            continue
    return str(newest)


# ---------------------------------------------------------------------------
# The loader bundle (GET /paw-bar/widget.js)
#
# The one script a foreign page loads to grow a concierge. It reads its config off
# its own <script> tag and mounts the /paw-bar/frame iframe; everything
# tenant-specific lives in those attributes, so this file is the same bytes for
# every site and needs no auth. Before this existed the only advertised URL was an
# unprovisioned CDN placeholder, which is why a published site could carry a
# concierge and still show nothing.
# ---------------------------------------------------------------------------

# How long a browser may reuse the loader without re-asking. Short on purpose: the
# glass app's own assets are cache-busted by ``_asset_version`` in the frame HTML,
# but a <script src> baked into a customer's deployed page has no version stamp we
# control, so a long max-age would pin every embedder to whatever loader shipped on
# the day their site was published. Five minutes keeps the edge useful and keeps a
# fix at most one coffee away.
_WIDGET_JS_MAX_AGE = 300


def paw_bar_widget_file() -> Path:
    """Path of the loader bundle ``GET /paw-bar/widget.js`` serves.

    ``PAW_BAR_WIDGET_JS`` wins when set — that is the seam for serving a freshly
    built bundle (e.g. ``paw-bar/dist/…``) without a redeploy. Otherwise
    the copy vendored beside this module. The default is deliberately IN the
    package rather than an absolute path into a sibling checkout: the publish path
    now bakes this URL into customers' deployed HTML, so it has to resolve on every
    machine that runs the backend, not just a developer's.
    """
    override = os.environ.get("PAW_BAR_WIDGET_JS", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "static" / "paw-bar.js"


@router.get("/paw-bar/widget.js")
async def widget_js() -> Response:
    """Serve the glass-bar loader — PUBLIC, unauthenticated, tenant-blind.

    No key, no Site read, no per-caller variation: this is a world-visible static
    script, and the credential (the embed key) is presented later by the iframe it
    mounts, at ``/paw-bar/frame``. Read from disk per request rather than cached in
    memory so replacing the file takes effect without a restart — the file is a few
    KB and the OS page cache absorbs the repeat reads.

    A missing bundle is a clean 404 naming the env var that fixes it, not a
    FileNotFoundError escaping as an opaque 500: the operator seeing this is
    debugging why a live site shows no bar, and the message is the answer.
    """
    path = paw_bar_widget_file()
    try:
        body = path.read_bytes()
    except OSError:
        logger.warning("paw-bar: loader bundle unavailable at %s", path)
        raise HTTPException(
            status_code=404,
            detail=(
                "Paw Bar loader bundle not found. Set PAW_BAR_WIDGET_JS to the "
                "path of a built widget bundle, or restore the copy shipped at "
                "pocketpaw_ee/paw_bar/static/paw-bar.js."
            ),
        ) from None
    return Response(
        content=body,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": f"public, max-age={_WIDGET_JS_MAX_AGE}"},
    )


# A frame-ancestors host-source is host[:port] with NO scheme, path, or whitespace.
# ``allowed_origins`` is owner-controlled data flowing into a response HEADER, so
# each entry is reduced to this safe shape (or dropped) — a stray space / ``;`` /
# newline would otherwise inject extra CSP directives or split the header.
_SAFE_ANCESTOR_RE = re.compile(r"^[a-z0-9.\-]+(:[0-9]{1,5})?$")


def _sanitize_ancestor(raw: Any) -> str | None:
    """Reduce one ``allowed_origins`` entry to a safe frame-ancestors host-source.

    Returns the bare ``host[:port]`` (scheme + path stripped) or ``None`` if it
    can't be safely represented. Emitting the bare host (no scheme) is deliberate:
    it matches the rest of the origin model (``origin_allowed`` is host-only) AND a
    schemeless frame-ancestors source adapts to the frame's OWN scheme — https-tight
    when the frame is served over https, http-permissive in local dev — instead of
    hard-pinning a scheme that would break one or the other.
    """
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if not v:
        return None
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]  # drop any path
    # SECURITY (ordering is load-bearing): ``v`` was ``.strip()``-ed above, so it
    # carries no trailing newline. ``$`` in this pattern matches just BEFORE a
    # trailing ``\n``, so a regex-before-strip reorder would let ``"host\n"`` pass
    # and ``return v`` would emit that newline into the CSP header (classic header
    # injection / directive split). Keep .strip() ahead of this match.
    if not _SAFE_ANCESTOR_RE.match(v):
        return None
    # PORT (bug found 2026-07-30 by framing a real published site): a CSP
    # host-source with NO port matches only the scheme's DEFAULT port, so a bare
    # ``127.0.0.1`` refuses to be framed by ``http://127.0.0.1:4174`` and the bar
    # renders as an empty grey box. ``allowed_origins`` is normalized to bare HOSTS
    # (``_normalize_origin_hosts``), so in practice NO entry ever carries a port and
    # every site served on a non-default port was unframeable — every local, dev and
    # demo deploy, and any customer site not on 80/443.
    #
    # Emitting ``host:*`` (any port) is the fix, and it is a CONSISTENCY change
    # rather than a loosening: the origin gate this CSP mirrors
    # (``site_keys.origin_allowed``) compares HOSTS and ignores the port entirely,
    # so a request from any port on an allowed host is already accepted for chat.
    # The CSP was the stricter of the two. An entry that DOES carry an explicit port
    # is honored as written.
    if ":" not in v:
        return f"{v}:*"
    return v


def _sanitize_dashboard_ancestor(raw: Any) -> str | None:
    """``_sanitize_ancestor``, but KEEPING an explicit http/https scheme.

    This is the difference between the fix working and not working. A schemeless
    host-source resolves against the FRAME's OWN scheme (see ``_sanitize_ancestor``),
    so on an https backend the bare source ``localhost:5173`` means
    ``https://localhost:5173`` — and a dashboard served over plain http, which is
    every local dev session pointed at a deployed backend, is refused by a policy
    that appears to name it. That is exactly the reported failure: the blocked
    request's header already listed ``localhost:*`` and ``127.0.0.1:*``.

    ``allowed_origins`` cannot carry a scheme (``_normalize_origin_hosts`` reduces it
    to bare hosts upstream) and shouldn't — a customer's site is https in production
    and the schemeless form is the https-tight one there. The dashboard origin is
    different: it is declared BY the operator, complete with the scheme they serve
    it on, so it is honored as declared. A non-browser scheme (``tauri://localhost``)
    has no host-source spelling and is dropped rather than guessed at.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if "://" not in value:
        return _sanitize_ancestor(value)
    scheme, _, rest = value.partition("://")
    if scheme not in ("http", "https"):
        return None
    host = _sanitize_ancestor(rest)
    return f"{scheme}://{host}" if host else None


def _ancestor_sources(origins: list[str], sanitize: Any = _sanitize_ancestor) -> list[str]:
    """Sanitize origins into frame-ancestors host-sources: drop the unusable, collapse
    repeats (first occurrence wins, order otherwise preserved).

    Dedup earns its keep now that the dashboard origin is appended to the Site's
    allowlist — in local dev it IS one of the seeded hosts. A repeated source is
    harmless to a browser but turns the header into a puzzle in a console error,
    which is the only place anyone ever reads one.
    """
    seen: set[str] = set()
    sources: list[str] = []
    for origin in origins or []:
        source = sanitize(origin)
        if source and source not in seen:
            seen.add(source)
            sources.append(source)
    return sources


def _frame_ancestors_csp(allowed_origins: list[str]) -> str | None:
    """Build the ``frame-ancestors`` CSP value from a Site's ``allowed_origins``.

    Returns ``None`` when NO entry survives sanitization (including an empty
    allowlist) — the caller FAILS CLOSED (refuses to render) rather than emit a
    source-less directive. Mirrors ``site_keys.origin_allowed``'s empty=deny model,
    NOT the router's ``_origin_allowed`` empty=allow-all footgun.
    """
    sources = _ancestor_sources(allowed_origins)
    if not sources:
        return None
    return "frame-ancestors " + " ".join(sources)


def _dashboard_preview_ancestors() -> list[str]:
    """Origins allowed to frame the PUBLIC bar because they are our own dashboard.

    The builder previews a site by framing its real published page, so the bar's
    iframe sits TWO deep — dashboard → site page → bar — and ``frame-ancestors`` is
    matched against EVERY ancestor, not just the immediate parent. Nothing in the
    publish path knows the dashboard exists (``_default_allowed_origins`` seeds the
    local hosts, ``_with_deployed_host`` adds the site's own), so no Site allowlist
    ever named it and the bar was refused in every preview.

    Two sources, in order:
      1. ``PAWBAR_DASHBOARD_ORIGIN`` — explicit, comma-separated. The SAME var the
         session-authed owner preview reads (``_dashboard_origin``), minus its
         ``localhost:5173`` default: unset must mean unset here, or every public
         customer bar would name a visitor's own machine as a permitted embedder.
      2. ``POCKETPAW_API_CORS_ALLOWED_ORIGINS`` — the origins the operator has
         ALREADY declared as first-party browsers for this API, i.e. the
         paw-enterprise frontend. Falling back to it is what makes a correctly
         configured deploy work with no new variable, and that matters because no
         deploy we ship sets PAWBAR_DASHBOARD_ORIGIN — which is how this broke.

    The env is read raw rather than through ``Settings.load()``: load() re-reads
    config.json and the credential store on every call, and this is a public,
    per-request path. That means parsing the list here, so both the JSON shape
    pydantic writes and the CSV operators write are accepted — every entry lands in
    ``_sanitize_ancestor`` regardless, so a malformed one is dropped, never injected.
    Never the request's own Origin/Referer, never a wildcard.
    """
    raw = os.environ.get("PAWBAR_DASHBOARD_ORIGIN", "").strip()
    if not raw:
        raw = os.environ.get("POCKETPAW_API_CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return []
    parts = raw.strip("[]").split(",")
    return [p.strip().strip('"').strip("'") for p in parts if p.strip()]


def _public_frame_ancestors(allowed_origins: list[str]) -> str | None:
    """``frame-ancestors`` for the PUBLIC visitor frame: the Site's allowlist, plus
    our own dashboard so an owner can actually see the bar in the builder preview.

    Fail-closed is still decided on the SITE's allowlist ALONE — a Site with no
    embedders stays unrenderable, and a declared dashboard origin never revives it
    (the owner preview has its own session-authed endpoint for that). The dashboard
    entry is only ever ADDITIVE, on top of a policy that already had one source.

    Deliberately NOT done by widening ``Site.allowed_origins``: that list also gates
    chat and lead capture through ``site_keys.origin_allowed``, so writing the
    dashboard into it would hand the dashboard origin authority it has no need for.
    This widens the render gate and nothing else.
    """
    site_sources = _ancestor_sources(allowed_origins)
    if not site_sources:
        return None
    dashboard = _ancestor_sources(_dashboard_preview_ancestors(), _sanitize_dashboard_ancestor)
    extra = [s for s in dashboard if s not in site_sources]
    return "frame-ancestors " + " ".join([*site_sources, *extra])


def _configured_frame_origin(request: Request) -> str:
    """OUR iframe's origin — the trusted parent for the dual-mode chat gate.

    Configurable via ``PAWBAR_FRAME_ORIGIN`` (set it in any multi-origin / proxied
    deploy where the frame is served from a fixed public origin). Defaults to the
    request's own ``scheme://host`` for single-origin / local deploys where the
    frame and the API share an origin.
    """
    configured = os.environ.get("PAWBAR_FRAME_ORIGIN", "").strip()
    if configured:
        return configured
    return f"{request.url.scheme}://{request.url.netloc}"


def _dead_frame_response(po: str, allowed_origins: list[str]) -> HTMLResponse:
    """The invisible shell a declined frame renders: nothing, then self-remove.

    A refused GET /paw-bar/frame lands inside a VISIBLE iframe on the
    customer's site, so the body must render blank — never an error payload.
    The inline script posts ``{pawbar: 'dead'}`` to the (allowlist-validated)
    parent so a loader that understands it removes the iframe entirely; an
    older loader simply keeps an invisible 48px sliver. Status stays 403:
    programmatic callers still see a refusal.
    """
    parent = _safe_parent_origin(po, allowed_origins)
    script = (
        "<script>try{parent.postMessage({type:'pawbar:dead'},"
        + json.dumps(parent)
        + ")}catch(e){}</script>"
        if parent
        else ""
    )
    return HTMLResponse(
        content=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<style>html,body{background:transparent;margin:0}</style>"
            f"</head><body>{script}</body></html>"
        ),
        status_code=403,
        headers={"Cache-Control": "no-store"},
    )


def _request_origin(request: Request) -> str | None:
    """The effective Origin for the public GET gates — same-origin case resolved.

    Browsers OMIT the ``Origin`` header on same-origin GETs, so a fetch from OUR
    OWN frame (the glass app polling its decision, loading articles) arrives
    origin-less and the fail-closed gates 403'd it — found live on the
    2026-07-30 rig: the frame's decision poll and articles fetch were dead while
    every curl with an explicit Origin passed. When the browser-set
    ``Sec-Fetch-Site: same-origin`` header is present, the caller can only be a
    page on our own origin — i.e. the frame — so resolve it AS the frame origin
    and let the dual-mode gate's frame branch do its normal work. Non-browser
    callers can forge any Origin header anyway; the origin gates are
    browser-defense, so this widens nothing. A request with neither header stays
    origin-less and the gates stay fail-closed.
    """
    origin = request.headers.get("origin")
    if origin:
        return origin
    if request.headers.get("sec-fetch-site", "").strip().lower() == "same-origin":
        return _configured_frame_origin(request)
    return None


def _safe_parent_origin(po: str, allowed_origins: list[str]) -> str:
    """Validate the loader-supplied parent origin against the Site's allowlist.

    ``po`` rides in the iframe URL (set by the loader running in the embedder page),
    so it is attacker-influenceable and never trusted verbatim. Returns a clean
    ``scheme://host[:port]`` origin — usable as the glass app's postMessage
    ``targetOrigin`` — ONLY when its host is on the Site's allowlist; otherwise "".
    (The real embedder is allowlisted by definition, else the CSP blocks the frame.)
    """
    if not po:
        return ""
    v = po.strip().lower().rstrip("/")
    if "://" in v:
        scheme, rest = v.split("://", 1)
    else:
        scheme, rest = "https", v
    hostport = rest.split("/", 1)[0]
    hostonly = hostport.split(":", 1)[0]
    allowed_hostonly = {
        h.split(":", 1)[0] for h in (_sanitize_ancestor(o) for o in (allowed_origins or [])) if h
    }
    if hostonly not in allowed_hostonly:
        return ""
    if scheme not in ("http", "https") or not _SAFE_ANCESTOR_RE.match(hostport):
        return ""
    return f"{scheme}://{hostport}"


def _pawbar_bootstrap_html(
    config: dict[str, Any],
    asset_mount: str,
    page_bg: str = "",
    scene_url: str = "",
) -> str:
    """Render the glass frame document: seed ``window.__PAWBAR__`` then load the app.

    The config dict is ``json.dumps``'d with ``<`` escaped to ``\\u003c`` so no
    value (the world-visible key, the attacker-influenceable ``widgetId`` /
    ``parentOrigin`` query params) can break out of the inline ``<script>`` with a
    ``</script>`` sequence or inject markup. Assets load from ``asset_mount`` (the
    root-absolute StaticFiles mount), so the document is valid wherever the API
    router is mounted.

    ``page_bg`` is empty for the PUBLIC embed (the bar body stays transparent so it
    sits over the customer's real page) and set ONLY by the owner preview frame to a
    dark surface: a transparent-body iframe otherwise paints a white canvas backdrop,
    which clashes with the dark dashboard the preview is embedded in. The value is a
    hard-coded CSS color the server controls (never request-derived), so the inline
    ``<style>`` cannot be injected.
    """
    config_json = json.dumps(config).replace("<", "\\u003c")
    v = _asset_version()
    # THE SITE ITSELF, behind the bar (owner preview only). The owner judges a
    # theme on the page it will actually sit on — scrolling, responding, current
    # — rather than on a screenshot of it or on a colour we chose. Composed HERE
    # rather than in the dashboard because this is where the URL already is: the
    # route has the Site document open.
    #
    # ?pawbar=off keeps the page's OWN embedded bar down. A published site
    # auto-embeds the public one, and without this the owner sees two — the public
    # bar on the SAVED look behind the one being edited, and the wrong one is what
    # responds to the controls.
    #
    # Sandboxed. It is the owner's own page, but still a whole third-party
    # document running inside our frame: allow-scripts so it renders as it really
    # does, and deliberately NOT allow-same-origin beside it, which would hand it
    # this document.
    scene_html = (
        f'<iframe class="pawbar-scene" src="{escape(scene_url, quote=True)}" '
        f'title="Your site" '
        f'sandbox="allow-scripts allow-popups allow-forms"></iframe>'
        if scene_url
        else ""
    )
    # The scene fills the document and the bar app draws over it. Fixed rather
    # than absolute so the bar stays put while the framed page scrolls under it,
    # which is how it behaves on a real page.
    scene_style = (
        "<style>html,body{margin:0;height:100%}iframe.pawbar-scene{position:fixed;inset:0;width:100%;height:100%;border:0}</style>"
        + chr(10)
        if scene_url
        else ""
    )
    preview_style = (
        f"<style>html,body{{background:{page_bg};color-scheme:dark}}</style>\n" if page_bg else ""
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Paw Bar</title>\n"
        f'<link rel="stylesheet" href="{asset_mount}/pawbar.css?v={v}">\n'
        f"{scene_style}"
        f"{preview_style}"
        "</head>\n"
        "<body>\n"
        f"{scene_html}"
        '<div id="pawbar-root"></div>\n'
        f"<script>window.__PAWBAR__ = {config_json};</script>\n"
        f'<script src="{asset_mount}/pawbar.js?v={v}"></script>\n'
        "</body>\n"
        "</html>\n"
    )


def _pawbar_frame_config(
    *,
    site_key: str,
    widget_id: str,
    api_base: str,
    parent_origin: str,
    greeting: str,
    starters: list[str] | None = None,
    appearance: ConciergeAppearance | None = None,
    preview: bool = False,
) -> dict[str, Any]:
    """Build the ``window.__PAWBAR__`` bootstrap config shared by the public frame
    and the owner preview frame (D5).

    Single source of truth for the config shape so the two framing paths never
    drift: both seed the SAME glass app with the SAME fields. The callers differ
    only in WHERE the values come from — the public frame reads ``siteKey`` +
    ``parentOrigin`` from the world-visible key + allowlist, the owner preview from
    the resolved Site + the dashboard origin — and in the CSP ancestor they set.

    ``starters`` are the bound agent's conversation starters (feat/site-dedicated-
    agent, E3): additive, capped at 4, defaulting to an empty list so a frame with
    no starters is unchanged. On this branch the Agent model carries no
    ``conversation_starters`` field (the ASG-1 identity fields are absent), so
    callers pass ``[]`` today — the wire is in place for when those fields land.

    ``appearance`` is the owner's white-label settings (2026-08-19). ``None``
    renders the defaults, which reproduce the look every bar had before this
    existed — so a Site nobody has styled is byte-identical to before apart from
    the token map now carrying the base values explicitly.
    """
    look = appearance or ConciergeAppearance()
    return {
        "siteKey": site_key,
        "widgetId": widget_id or "",
        "endpoint": api_base,
        "parentOrigin": parent_origin,
        "mode": "concierge",
        # 2026-08-20 — TRUE only for the owner preview frame (D5), never the
        # public embed. It is what lets the glass app accept live --pawbar-*
        # updates postMessage'd by the appearance editor as the owner drags a
        # slider. A public bar must refuse those: its parent is the customer's
        # own page, and an embed that restyles itself on request from whatever
        # framed it is a wider surface than this feature needs. Origin is checked
        # too — this flag decides whether the listener exists at all.
        "preview": bool(preview),
        # D1 / SS-6 — the owner's opening line; the glass app renders it (D4) and
        # falls back to its own default when "".
        "greeting": greeting or "",
        # E3 — the bound agent's conversation starters (capped 4).
        "starters": (starters or [])[:4],
        # 2026-08-19 — the owner's appearance, rendered to --pawbar-* custom
        # properties. This line answered ``{}`` from the day the glass bar
        # shipped: the widget read the map and injected it, and nothing ever
        # filled it, so the whole white-label path was dead wire. ``theme`` was
        # never emitted at all, which is why every bar was dark regardless.
        "tokens": look.tokens(),
        "theme": look.surface_mode,
        "agentName": look.agent_name,
        "agentSubtitle": look.agent_subtitle,
        "agentAvatar": look.agent_avatar_url,
        "avatars": list(look.team_avatar_urls),
        # The resting pill's own copy. LauncherAppearance.label has been stored
        # and bound-checked since the appearance model landed; it had nowhere to
        # go until the bar rested as a labelled pill rather than a wide input
        # slab. "" means the widget falls back to its own generic wording, so an
        # owner who never set one still gets a finished sentence.
        "launcherLabel": look.launcher.label,
    }


async def _bound_agent_starters(agent_id: str) -> list[str]:
    """Best-effort conversation starters for a widget's BOUND agent (E3).

    Returns the bound agent's ``conversation_starters`` (capped 4) for the frame
    config, or ``[]`` when the widget is unbound, the agent is gone, or the Agent
    model carries no ``conversation_starters`` field. NOTE: the ASG-1 identity
    fields are ABSENT on this branch, so ``getattr`` misses and this returns ``[]``
    today — the read is in place so starters flow the moment the field lands. Any
    lookup failure degrades to ``[]`` (the frame must still render).
    """
    if not agent_id:
        return []
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await agents_service.get(agent_id)
    except Exception:  # noqa: BLE001 — a starter read must never break the frame
        return []
    starters = getattr(agent.config, "conversation_starters", None) or []
    return list(starters)[:4]


async def _bound_agent_name(agent_id: str) -> str:
    """Best-effort display name of a widget's BOUND agent (E2 dashboard card).

    Resolved through the SAME agents-service read path the provisioner uses, so the
    dashboard can show the concierge name and detect the "<x> Concierge" dedicated
    pattern. Returns "" when the widget is unbound OR the agent no longer resolves —
    a dangling agent_id degrades to absent rather than 500-ing the overview.
    """
    if not agent_id:
        return ""
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await agents_service.get(agent_id)
    except Exception:  # noqa: BLE001 — a name read must never break the overview
        return ""
    return agent.name or ""


def _dashboard_origin() -> str:
    """The paw-enterprise dashboard origin allowed to frame the owner preview (D5).

    Read from ``PAWBAR_DASHBOARD_ORIGIN`` (set it to the deployed dashboard origin,
    e.g. ``https://app.example.com``). Defaults to the Vite dev server
    (``http://localhost:5173``) so local dev works with no config. This is the ONLY
    origin the preview frame's CSP ``frame-ancestors`` permits — deliberately NOT
    the Site's public ``allowed_origins`` (that gates the visitor frame) and never
    ``*`` or the request's own Origin/Referer.
    """
    return os.environ.get("PAWBAR_DASHBOARD_ORIGIN", "").strip() or "http://localhost:5173"


@router.get("/paw-bar/frame")
async def frame(
    request: Request,
    key: str = Query("", description="The public Site.signed_key baked into the loader"),
    w: str = Query("", description="Optional Paw Bar widget id"),
    po: str = Query("", description="Parent origin the loader passes for postMessage"),
) -> HTMLResponse:
    """Serve the glass Paw Bar app inside an iframe, gated by CSP frame-ancestors.

    The embedder gate is NOT a per-request Origin check (in an iframe every request
    carries OUR frame origin) — it is the ``Content-Security-Policy: frame-ancestors``
    header the browser enforces at render time: it refuses to display this iframe
    inside any parent whose origin isn't on the Site's ``allowed_origins``.

    Order (fail-closed):
      1. Authenticate the embed key — ``lookup_site_by_key`` (the SAME chain the
         chat path runs): blank / short / unknown / revoked → 401.
      2. Build the ``frame-ancestors`` value from ``Site.allowed_origins``. FAIL
         CLOSED on an empty / unusable allowlist → 403 (refuse to render), mirroring
         ``site_keys.origin_allowed`` — NOT the ``_origin_allowed`` allow-all footgun.
      3. Seed ``window.__PAWBAR__`` (JSON-safe) and load pawbar.js/css.

    No ``X-Frame-Options``: it is obsolete beside ``frame-ancestors`` and a
    conflicting ``XFO: DENY`` would stop the frame from rendering at all.

    Residual: ``frame-ancestors`` binds BROWSERS only. The key is world-visible and
    a raw ``curl`` POST to /paw-bar/chat was always possible and is unchanged here —
    the real controls stay the rate-limit + injection screen + the zero-authority
    CONCIERGE scope. CSP does not close the curl path.
    """
    from pocketpaw_ee.cloud.auth.site_keys import concierge_available, lookup_site_by_key

    # (1) Authenticate the embed key. A missing/blank ``key`` query param is a
    # too-short key → 401 (never a 422), so the refusal is uniform with the chat path.
    site = await lookup_site_by_key(key)

    # (1b) Kill switch (D1 / SS-6): the owner's ``concierge_enabled`` toggle. When
    # off, refuse to RENDER — but this response body lands inside a visible
    # iframe on the customer's site, so a JSON error is a defect, not a refusal
    # (the 2026-07-30 rig showed literal {"detail":"concierge_disabled"} on the
    # page). Return the invisible shell: a blank document that tells the loader
    # to remove the iframe (``pawbar:dead``). Still 403 — curl callers see the
    # status; browsers see nothing. Re-read per request (``lookup_site_by_key``
    # does a fresh find_one), so toggling off silences the frame immediately.
    # Distinct from ``revoked`` (which cuts the KEY at 401 inside
    # lookup_site_by_key — an api-shaped JSON 401 stays correct there: a revoked
    # key means the embed script itself is stale/removed on next publish).
    #
    # (1c) The BILLING gate rides the same branch (feat/sites-concierge-entitlement):
    # a site whose plan does not sell the concierge refuses identically. Deliberately
    # the SAME response, not a distinct one — the visitor must not be able to tell a
    # lapsed subscription from an owner's choice by looking at the page, and the
    # loader already knows how to remove an iframe that says ``pawbar:dead``. The
    # reason is surfaced to the OWNER through the dashboard, and to logs, never here.
    if not concierge_available(site):
        return _dead_frame_response(po, site.allowed_origins)

    # (2) The embedder gate: the CSP frame-ancestors header. Fail closed when no
    # allowlisted origin survives sanitization — refuse to render (same
    # invisible-shell shape: this body also lands in a visible iframe). The Site's
    # allowlist plus OUR dashboard, which frames the site's real page in the builder
    # preview and so becomes a second ancestor the browser matches too.
    csp = _public_frame_ancestors(site.allowed_origins)
    if csp is None:
        return _dead_frame_response(po, site.allowed_origins)

    # (3) Bootstrap config. ``endpoint`` is the API base derived from the request
    # path (mount-agnostic: /api/v1 in prod, "" when the router is mounted bare in
    # tests); the glass app POSTs ``{endpoint}/paw-bar/chat``. ``parentOrigin`` is
    # validated against the allowlist. ``siteKey`` in the page is fine — it is a
    # world-visible embed key by design.
    api_base = request.url.path.rsplit("/paw-bar/frame", 1)[0]
    # E3 — the bound agent's conversation starters ride the config. The public
    # frame is pre-auth (keyed on the world-visible embed key, no session) and does
    # not load the widget here, so it defaults to []; the bound-agent starters are
    # surfaced through the owner preview frame (below), where the widget is already
    # resolved workspace-scoped. On this branch the value is [] regardless (the
    # Agent ``conversation_starters`` field is absent — ASG-1 not merged here).
    config = _pawbar_frame_config(
        site_key=key,
        widget_id=w or "",
        api_base=api_base,
        parent_origin=_safe_parent_origin(po, site.allowed_origins),
        greeting=site.concierge_greeting or "",
        starters=[],
        # Read off the Site every request, never cached, so an owner saving a
        # colour sees it on the next reload rather than after a redeploy.
        appearance=getattr(site, "concierge_appearance", None),
    )
    html = _pawbar_bootstrap_html(config, PAWBAR_APP_MOUNT)
    return HTMLResponse(
        content=html,
        headers={
            "Content-Security-Policy": csp,
            # The embed key is baked into the loader HTML per-embedder; the frame
            # doc itself must not be cached across keys/parents by a shared proxy.
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateWidgetRequest(BaseModel):
    pocket_id: str
    owner: str
    # T3 — bind the widget to the agent that answers its concierge chats. Optional
    # + defaults to "" (unbound), so existing create calls are unaffected.
    agent_id: str = ""
    name: str = ""
    spec: PawBarSpec
    allowed_domains: list[str] = Field(default_factory=list)
    rate_limit_per_min: int = 60
    per_customer_limit_per_min: int = 10
    event_mapping: dict[str, PawBarEventMapping] = Field(default_factory=dict)


class UpdateWidgetRequest(BaseModel):
    """Partial admin update of a widget's mutable, non-spec fields (C1).

    Every field is optional; only the ones the client SENDS are written (tracked
    via ``model_fields_set``). ``agent_id`` may be set to a new agent or ``null``
    to unbind (stored as ""). The spec is NOT editable here — that is
    ``update_spec`` (which archives a revision). The paw-enterprise agent-binding
    UI ships against exactly this shape.
    """

    agent_id: str | None = None
    name: str | None = None
    allowed_domains: list[str] | None = None
    rate_limit_per_min: int | None = None
    per_customer_limit_per_min: int | None = None


class WidgetListResponse(BaseModel):
    # PawBarWidgetPublic (not PawBarWidget) — list payloads must never
    # carry access_token (W0b).
    widgets: list[PawBarWidgetPublic]
    total: int


class EventIngestResponse(BaseModel):
    accepted: bool
    event: PawBarEvent | None = None
    fabric_object_id: str | None = None
    # gap2 — the Instinct proposal raised for this event (when the widget maps
    # the event type). The customer surface can poll the decision endpoint to
    # read the owner's eventual decision; None when no proposal was raised.
    instinct_action_id: str | None = None
    reason: str | None = None


class DecisionStatusResponse(BaseModel):
    """The customer-facing view of a decision (gap2).

    Deliberately omits internal-only fields (the Instinct action id, the
    workspace) — the customer surface only needs the state + the reply.
    ``found`` is False when no decision exists yet for this (widget, customer).
    """

    found: bool
    state: str | None = None
    reply: str | None = None
    decided_by: str | None = None
    updated_at: str | None = None


class EventsListResponse(BaseModel):
    events: list[PawBarEvent]
    total: int


# ---------------------------------------------------------------------------
# Widget management (CRUD)
#
# Auth model (W0b): these routes are mounted under /api/v1, which the
# dashboard AuthMiddleware treats as auth-OPTIONAL — it populates request.state
# but does NOT 401. So management routes MUST gate themselves at the route
# level. They gate on the caller's WORKSPACE ROLE — ``_require_paw_bar_manage``
# (``paw_bar.manage``, ADMIN) for every mutation, ``_require_paw_bar_read``
# (``paw_bar.read``, ADMIN) for the list — bound to the SESSION's active
# workspace, the same one every handler below scopes its store calls to.
# The per-widget access_token (X-Paw-Bar-Token) is a SECOND factor on
# read/mutate of a specific widget — it is not a substitute for being a
# signed-in workspace admin, which is why create/list need this guard.
#
# These used to gate on ``require_scope("admin")``, which is an OSS
# SINGLE-TENANT primitive: it admits a full-access dashboard session, an
# admin-scoped file-backed API key, or a ppat_ OAuth token. A cloud workspace
# admin holds NONE of those, so the gate was unsatisfiable for the caller it
# was meant for (403 on their own site's settings); on self-hosted it was the
# opposite problem — any signed-in dashboard session sets ``full_access``, so
# it admitted members too. The role gate fixes both directions at once, and
# matches what the D2 reads below already did.
# ---------------------------------------------------------------------------


@router.post(
    "/paw-bar/widgets",
    response_model=PawBarWidget,
    status_code=201,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def create_widget(
    req: CreateWidgetRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidget:
    # W4a — the row is stamped with the caller's ACTIVE workspace, never a
    # client-supplied value: tenancy is derived server-side from the session.
    widget = PawBarWidget(
        pocket_id=req.pocket_id,
        owner=req.owner,
        workspace_id=workspace_id,
        agent_id=req.agent_id,
        name=req.name,
        spec=req.spec,
        allowed_domains=req.allowed_domains,
        rate_limit_per_min=req.rate_limit_per_min,
        per_customer_limit_per_min=req.per_customer_limit_per_min,
        event_mapping=req.event_mapping,
    )
    created = await _store().create_widget(widget)
    # Auto-provision a DEDICATED concierge agent (feat/site-dedicated-agent). Only
    # when the caller bound NO agent_id and the pocket resolves to a published Site
    # — a plain (non-site) widget stays unbound. Failure-soft: a provisioning error
    # logs and returns the widget UNBOUND rather than 500-ing the create (chat then
    # 409s and the dashboard offers a manual create). A manual agent_id is honored
    # and never replaced (the trigger returns early on a bound widget).
    if not req.agent_id:
        from pocketpaw_ee.paw_bar.agent_provisioning import provision_widget_on_create

        created = await provision_widget_on_create(created, workspace_id)
    return created


@router.get(
    "/paw-bar/widgets",
    response_model=WidgetListResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def list_widgets(
    pocket_id: str | None = Query(None),
    owner: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    workspace_id: str = Depends(current_workspace_id),
) -> WidgetListResponse:
    widgets = await _store().list_widgets(
        pocket_id=pocket_id, owner=owner, limit=limit, workspace_id=workspace_id
    )
    # Project to the token-free model — a list payload must never carry the
    # per-widget access_token (W0b).
    public = [PawBarWidgetPublic.from_widget(w) for w in widgets]
    return WidgetListResponse(widgets=public, total=len(public))


@router.get("/paw-bar/widgets/{widget_id}", response_model=PawBarWidgetPublic)
async def get_widget(
    widget_id: str,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
) -> PawBarWidgetPublic:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    # Read responses omit access_token — the caller already holds it (they had
    # to present it to pass _require_owner_token), so echoing it back only
    # widens the blast radius if a read response is logged/cached (W0b).
    return PawBarWidgetPublic.from_widget(widget)


@router.patch(
    "/paw-bar/widgets/{widget_id}/spec",
    response_model=PawBarWidgetPublic,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def update_spec(
    widget_id: str,
    spec: PawBarSpec,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidgetPublic:
    # W4a — the lookup is workspace-scoped: another tenant's widget id resolves
    # to None → 404, before the token even gets compared. Never mutates.
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    updated = await _store().update_spec(widget_id, spec, workspace_id=workspace_id)
    if updated is None:
        raise HTTPException(404, "Widget not found")
    return PawBarWidgetPublic.from_widget(updated)


@router.patch(
    "/paw-bar/widgets/{widget_id}",
    response_model=PawBarWidgetPublic,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def update_widget(
    widget_id: str,
    req: UpdateWidgetRequest,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidgetPublic:
    """Partial-update a widget's mutable, non-spec fields (C1 — agent binding).

    Auth mirrors ``update_spec``: an admin dashboard session AND the per-widget
    owner token, with the lookup workspace-scoped so a cross-tenant widget id
    404s before anything is written. Only the fields the client SENT are applied
    (``model_fields_set``); ``agent_id: null`` unbinds (stored as ""). The spec is
    intentionally not editable here."""
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)

    # Only the sent fields become column writes. agent_id null → "" (unbind).
    fields: dict[str, Any] = {}
    for name in req.model_fields_set:
        value = getattr(req, name)
        if name == "agent_id":
            fields[name] = value or ""
        elif value is not None:
            fields[name] = value
    updated = await _store().update_fields(widget_id, fields, workspace_id=workspace_id)
    if updated is None:
        raise HTTPException(404, "Widget not found")
    return PawBarWidgetPublic.from_widget(updated)


@router.post(
    "/paw-bar/widgets/{widget_id}/spec/rollback",
    response_model=PawBarWidgetPublic,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def rollback_spec(
    widget_id: str,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidgetPublic:
    """Restore the latest archived spec revision (W4a).

    Every ``update_spec`` archives the prior spec as a monotonic revision;
    this endpoint restores the most recent one. The restore is itself an
    update that archives the current spec, so a rollback is reversible.
    Auth mirrors ``update_spec``: admin session + per-widget owner token,
    with the lookup workspace-scoped (cross-tenant id → 404).
    """
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    restored = await _store().rollback_spec(widget_id, workspace_id=workspace_id)
    if restored is None:
        raise HTTPException(409, "No spec revision to roll back to")
    return PawBarWidgetPublic.from_widget(restored)


@router.post(
    "/paw-bar/widgets/{widget_id}/rotate-token",
    response_model=PawBarWidget,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def rotate_token(
    widget_id: str,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidget:
    # Returns the FULL widget (with the new access_token) on purpose: this is
    # the explicit, authenticated reveal path so the owner can capture the
    # rotated secret. Still requires the old token AND an admin dashboard
    # session (W0b). W4a — lookup + rotate are workspace-scoped (cross-tenant
    # id → 404, nothing rotates).
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    rotated = await _store().rotate_token(widget_id, workspace_id=workspace_id)
    if rotated is None:
        raise HTTPException(404, "Widget not found")
    return rotated


@router.delete(
    "/paw-bar/widgets/{widget_id}",
    status_code=204,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def delete_widget(
    widget_id: str,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    # W4a — scoped lookup + scoped DELETE: a cross-tenant widget id 404s and
    # the row is never touched.
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    await _store().delete_widget(widget_id, workspace_id=workspace_id)


@router.get("/paw-bar/widgets/{widget_id}/events", response_model=EventsListResponse)
async def list_events(
    widget_id: str,
    limit: int = Query(100, ge=1, le=500),
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
) -> EventsListResponse:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    events = await _store().recent_events(widget_id, limit=limit)
    return EventsListResponse(events=events, total=len(events))


# ---------------------------------------------------------------------------
# Admin: per-Site concierge settings (D1 / SS-6)
#
# The kill switch + greeting live on the SITE doc (not the widget), so the owner
# read/update is keyed on ``site_id``, not a widget id. Auth mirrors the widget
# admin CRUD above: the caller's WORKSPACE ROLE must clear ``paw_bar.read`` on the
# GET and ``paw_bar.manage`` on the PATCH (both ADMIN), bound to their ACTIVE
# workspace via ``current_workspace_id``. The lookup is workspace-scoped, so
# another tenant's site id resolves to 404 and never leaks or
# mutates. Reads/writes touch ONLY the concierge fields (the kill switch, the
# greeting, and the transcript-retention toggle) — the site's publish / billing /
# capture config is out of scope here. Co-located with the paw-bar
# enforcement (the frame/chat/action gates) rather than the heavier sites control
# plane so the owner surface and the switch it drives sit in one place.
# ---------------------------------------------------------------------------


class ConciergeSettingsUpdate(BaseModel):
    """Partial update of a Site's concierge settings (D1).

    Every field is optional; only the ones the client SENDS are written (tracked
    via ``model_fields_set``), so a PATCH that carries just ``concierge_enabled``
    leaves the greeting untouched and vice-versa.
    """

    concierge_enabled: bool | None = None
    concierge_greeting: str | None = None
    # 2026-08-19. Sent WHOLE rather than per-field: the editor round-trips the
    # block it was handed, and every field validates itself into a safe literal
    # (see paw_bar/appearance.py), so a partial merge would only add a way for
    # half a theme to be stored.
    concierge_appearance: ConciergeAppearance | None = None
    # Retention switch for the VISITOR half of a transcript. Off means the
    # concierge keeps working and keeps storing its own replies, but the visitor's
    # words are never written down. Turning it off does NOT purge what is already
    # stored — that is a delete operation, not a settings change.
    concierge_store_transcripts: bool | None = None


class ConciergePreviewTokensRequest(BaseModel):
    """An UNSAVED appearance to render, for the editor's live preview (2026-08-20)."""

    concierge_appearance: ConciergeAppearance = Field(default_factory=ConciergeAppearance)


class ConciergePreviewTokensResponse(BaseModel):
    """The rendered ``--pawbar-*`` map, plus the appearance as VALIDATED.

    Both halves matter. The tokens are what the widget applies; the normalized
    appearance is what the owner actually gets, and returning it means the editor
    can show a clamped radius or a rejected image URL the moment it happens
    rather than at save time, which is the point at which it currently surprises
    people.
    """

    tokens: dict[str, str]
    concierge_appearance: ConciergeAppearance


class ConciergeSettingsResponse(BaseModel):
    """The owner-facing view of a Site's concierge settings (D1)."""

    site_id: str
    concierge_enabled: bool
    concierge_greeting: str
    concierge_store_transcripts: bool
    concierge_appearance: ConciergeAppearance = Field(default_factory=ConciergeAppearance)


async def _load_site_scoped(site_id: str, workspace_id: str) -> Any:
    """Load a Site by id, scoped to the caller's workspace (cross-tenant → 404).

    Mirrors ``sites.service._load`` (the canonical tenant-scoped Site reader) but
    raises the router's ``HTTPException(404)`` instead of the service's domain
    ``NotFound`` — a malformed/foreign/absent id is all one 404 so nothing leaks
    across tenants. Kept local so the paw-bar router doesn't reach into the sites
    service's private helper or its exception-translation layer.
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    from pocketpaw_ee.cloud.models.site import Site

    try:
        oid = ObjectId(site_id)
    except (InvalidId, TypeError):
        raise HTTPException(404, "Site not found")
    site = await Site.find_one({"_id": oid, "workspace": workspace_id})
    if site is None:
        raise HTTPException(404, "Site not found")
    return site


@router.get(
    "/paw-bar/admin/site/{site_id}/settings",
    response_model=ConciergeSettingsResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_concierge_settings(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeSettingsResponse:
    """Read a Site's concierge settings so the dashboard can render the toggle +
    greeting field. Admin-authed, workspace-scoped (cross-tenant id → 404)."""
    site = await _load_site_scoped(site_id, workspace_id)
    return ConciergeSettingsResponse(
        site_id=str(site.id),
        concierge_enabled=site.concierge_enabled,
        concierge_greeting=site.concierge_greeting,
        concierge_store_transcripts=site.concierge_store_transcripts,
        # getattr, not attribute access: a Site document written before this
        # field existed deserializes without it, and the settings page must open
        # for those rather than 500 on the owner who has not saved a theme yet.
        concierge_appearance=getattr(site, "concierge_appearance", None) or ConciergeAppearance(),
    )


@router.patch(
    "/paw-bar/admin/site/{site_id}/settings",
    response_model=ConciergeSettingsResponse,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def update_site_concierge_settings(
    site_id: str,
    req: ConciergeSettingsUpdate,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeSettingsResponse:
    """Toggle the kill switch and/or set the greeting on a Site (D1 / SS-6).

    Only the fields the client SENT are applied (``model_fields_set``). Admin-authed
    and workspace-scoped, so a cross-tenant site id 404s before anything is written.
    The change is read on the NEXT public request (the gates re-``find_one`` the Site
    every time), so toggling ``concierge_enabled`` off silences the bar immediately.
    """
    site = await _load_site_scoped(site_id, workspace_id)
    # Track whether this PATCH SETS the concierge on, so we can auto-provision the
    # dedicated agent for a site whose widget is still unbound (the owner enabling
    # the concierge is a natural provision point, alongside widget-create). We fire
    # on ANY PATCH that sets concierge_enabled=true, NOT only a false->true
    # transition: the E2 dashboard's one-click "create dedicated agent" re-PATCHes
    # {concierge_enabled: true} on an already-enabled site as its provision hook, so
    # a transition guard would leave that path no way in. It stays cheap + correct
    # because provision_on_concierge_enable only acts on an UNBOUND widget and
    # ensure_site_agent is idempotent, so a re-PATCH on a bound site is a no-op.
    enabling = "concierge_enabled" in req.model_fields_set and req.concierge_enabled is True
    for name in req.model_fields_set:
        value = getattr(req, name)
        if value is not None:
            setattr(site, name, value)
    await site.save()

    # Concierge-enable provisioning trigger (feat/site-dedicated-agent). Failure-
    # soft: a provisioning error logs and never fails this settings PATCH.
    if enabling:
        from pocketpaw_ee.paw_bar.agent_provisioning import provision_on_concierge_enable

        await provision_on_concierge_enable(site, workspace_id)

    return ConciergeSettingsResponse(
        site_id=str(site.id),
        concierge_enabled=site.concierge_enabled,
        concierge_greeting=site.concierge_greeting,
        concierge_store_transcripts=site.concierge_store_transcripts,
        # getattr, not attribute access: a Site document written before this
        # field existed deserializes without it, and the settings page must open
        # for those rather than 500 on the owner who has not saved a theme yet.
        concierge_appearance=getattr(site, "concierge_appearance", None) or ConciergeAppearance(),
    )


@router.post(
    "/paw-bar/admin/site/{site_id}/appearance/preview-tokens",
    response_model=ConciergePreviewTokensResponse,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def render_preview_tokens(
    site_id: str,
    req: ConciergePreviewTokensRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergePreviewTokensResponse:
    """Render an unsaved appearance to its ``--pawbar-*`` tokens. WRITES NOTHING.

    The appearance editor's preview is the real widget in a cross-origin iframe,
    so it can only be repainted by telling it what to paint. What it needs is the
    token map — and ``ConciergeAppearance.tokens()`` is the ONE renderer for that.
    Re-expressing it in the client would put a second copy of the mapping on the
    other side of a network boundary, where the two would drift silently and the
    preview would stop being evidence of anything.

    So the draft comes here, gets validated by exactly the model a save would
    validate it with, and goes back as tokens. That the validation is the same is
    the reason this is trustworthy: the preview shows the CLAMPED value, not the
    one the owner typed, so it cannot promise a look that a save would not store.

    Tenancy: the site is resolved workspace-scoped and the result is discarded.
    We do not need the document to render — the appearance is in the body — but
    loading it is what makes a cross-tenant or bogus site id a 404 here exactly
    as it is on the settings PATCH, rather than an open rendering oracle.
    """
    await _load_site_scoped(site_id, workspace_id)
    look = req.concierge_appearance
    return ConciergePreviewTokensResponse(tokens=look.tokens(), concierge_appearance=look)


# ---------------------------------------------------------------------------
# Admin: per-Site concierge KNOWLEDGE (site content → the pocket KB it reads)
#
# A concierge answers from ONE scope, ``pocket:<pocket_id>``, so if nothing put the
# site's own pages there the agent is live but knows nothing about the business it
# fronts. The sync runs automatically on publish and on agent provisioning; these
# two endpoints are the owner's window into it — what the concierge currently
# knows, and a way to re-run the sync without re-publishing.
#
# Gates: the read uses ``paw_bar.read`` and the sync ``paw_bar.manage`` (both
# ADMIN). The sync is a mutation that spends compute, so it does not ride the read.
# ---------------------------------------------------------------------------

# ``_require_paw_bar_manage`` is now declared beside ``_require_paw_bar_read`` near
# the top of the file — the widget CRUD routes above use it too.


class ConciergeKnowledgeResponse(BaseModel):
    """What a site's concierge currently knows, and how the last sync went.

    ``status`` is "" for a clean sync; otherwise a stable machine code the dashboard
    can turn into a sentence: ``never_synced`` (nothing has run yet), ``no_content``
    (the pocket holds no ingestable pages — the case for a foreign site we do not
    host), ``pocket_unavailable`` or ``sync_failed``.
    """

    site_id: str
    article_count: int
    synced_at: str
    status: str
    ingested: int = 0
    removed: int = 0
    skipped: int = 0


def _knowledge_response(site: Any, report: Any = None) -> ConciergeKnowledgeResponse:
    synced_at = getattr(site, "kb_synced_at", None)
    status = getattr(site, "kb_sync_error", "") or ""
    if not synced_at and not status:
        status = "never_synced"
    return ConciergeKnowledgeResponse(
        site_id=str(site.id),
        article_count=len(getattr(site, "kb_article_ids", None) or []),
        synced_at=synced_at.isoformat() if synced_at else "",
        status=status,
        ingested=getattr(report, "ingested", 0) or 0,
        removed=getattr(report, "removed", 0) or 0,
        skipped=getattr(report, "skipped", 0) or 0,
    )


@router.get(
    "/paw-bar/admin/site/{site_id}/knowledge",
    response_model=ConciergeKnowledgeResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_knowledge(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeKnowledgeResponse:
    """How much of this site the concierge can actually quote. Workspace-scoped."""
    site = await _load_site_scoped(site_id, workspace_id)
    return _knowledge_response(site)


@router.post(
    "/paw-bar/admin/site/{site_id}/knowledge/sync",
    response_model=ConciergeKnowledgeResponse,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def sync_site_knowledge_now(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeKnowledgeResponse:
    """Re-read the site's pages into the KB its concierge answers from.

    Awaited rather than backgrounded, unlike the publish and provisioning triggers:
    the owner clicked a button and needs the result, not a promise. It is idempotent
    (re-ingesting a page versions its article rather than duplicating it) and prunes
    the articles pages that no longer exist left behind, so pressing it twice is
    harmless. Never raises on a sync failure — the failure is reported in ``status``
    so the dashboard can show it instead of a stack trace.
    """
    from pocketpaw_ee.sites.kb_ingest import safe_sync_site_knowledge

    site = await _load_site_scoped(site_id, workspace_id)
    report = await safe_sync_site_knowledge(site)
    return _knowledge_response(site, report)


# ---------------------------------------------------------------------------
# Admin: per-Site concierge aggregation reads (D2)
#
# Read-only aggregation over ONE site's paw-bar operational data, for the
# paw-enterprise Concierge dashboard. OWNER endpoints (admin session +
# ``current_workspace_id``), NOT the public visitor surface. Every read is
# scoped to the caller's ACTIVE workspace at TWO gates:
#   1. the Site is loaded workspace-scoped (``_load_site_scoped`` — cross-tenant
#      id → 404, never leaks existence), then
#   2. its paw-bar widget is resolved from ``Site.pocket_id`` ALSO workspace-scoped
#      (``list_widgets(pocket_id=…, workspace_id=…)``), so the widget in hand
#      always belongs to the caller's tenant.
# The decisions / conversations / handoffs reads then filter to THAT widget /
# pocket — never pocket-wide, never workspace-wide — so a sibling site or a
# second widget in the same workspace can never appear in this site's data. This
# widget/site-scoping is the leak surface the security review checks.
#
# Data sources (all reuse existing stores — nothing new is written here):
#   * decisions  — the paw_bar ``DecisionStatus`` table (get_paw_bar_store, a
#       SINGLETON store), filtered ``WHERE widget_id = ?``. This is the 1:1 mirror
#       of the Instinct proposals the decision loop raises (parked by
#       ``instinct_action_id``), but keyed on a real ``widget_id`` column and
#       served from ONE file regardless of deployment mode. It is chosen over
#       querying the Instinct store directly because (a) paw-bar stamps the
#       Instinct row's in-row ``workspace_id`` with the widget OWNER
#       (``decision_loop.resolve_workspace_id`` → ``widget.owner``), NOT the
#       physical dashboard workspace, so a workspace-scoped Instinct read would
#       hide every row, and (b) the Instinct proposal's physical file is
#       ContextVar-dependent (per-workspace on the run path, shared on the ingest
#       path) — the singleton DecisionStatus table has neither hazard, giving an
#       airtight ``WHERE widget_id = ?`` isolation seam.
#   * conversations — concierge runs are persisted as ``ChatRunDoc`` (context_type
#       "concierge", scope_id = the site's pocket). LISTABLE: the model's compound
#       (workspace, context_type, scope_id, createdAt DESC) index backs an
#       efficient per-site query; a Site is 1:1 with (workspace, pocket_id), so a
#       pocket-scoped read IS site-scoped. Runs are grouped by ``user_id``
#       (customer_ref) into one conversation each. No full-collection scan — the
#       fetch window is bounded.
#   * handoffs — the ``_paw_handoffs`` reserved Fabric object (SS-6), written by
#       ``paw_bar.handoff.raise_handoff`` since slice 3 (before that there was no
#       producer and this read was always empty). The shape is unchanged — contact
#       / question / transcript_ref / created_at — and the read still queries
#       Fabric for objects of that type carrying this widget's id. The widget-id
#       property filter is the same cross-site isolation guarantee.
# Overview counts are cheap (COUNT / distinct) — never load a full list.
# ---------------------------------------------------------------------------


# The reserved Fabric object type for a human-handoff request (SS-6). Owned by
# the PRODUCER (``paw_bar.handoff``) and imported here so the writer and this
# reader can never name two different types. Each contract field is a Fabric
# object property; ``widget_id`` is the scope key this read filters on.
_PAW_HANDOFFS_TYPE = PAW_HANDOFFS_TYPE

# The concierge run marker on ``ChatRunDoc`` — a concierge dispatch stamps
# ``context_type="concierge"`` and ``scope_id=<pocket_id>`` (see ``concierge_chat``).
_CONCIERGE_CONTEXT_TYPE = "concierge"

# Preview length for a conversation's last message — enough for the dashboard row
# without shipping the whole transcript.
_CONVERSATION_PREVIEW_CHARS = 140

# Upper bound on how many raw runs a single conversations page scans before
# grouping — keeps the read bounded (never a full-collection scan) while leaving
# room to dedupe several runs per customer down to ``limit`` conversations.
_CONVERSATION_SCAN_CAP = 200

# Max turns returned in one conversation transcript (the most recent N, presented
# oldest-first). Bounds the drill-in read; a long-running visitor conversation
# never ships the whole history in one response.
_TRANSCRIPT_CAP = 200

# Owner-supplied conversation metadata bounds (slice 1). The owner is trusted, but
# these fields are stored and echoed on every list read, so they are capped like
# every other persisted string on this surface — a runaway paste can't turn one
# row into the payload of the whole inbox.
_MAX_CONVERSATION_TAGS = 20
_MAX_TAG_CHARS = 40
_MAX_NOTE_CHARS = 2000

# Owner reply bounds (slice 2). The cap matches ``_STORED_USER_TEXT_CHARS`` on the
# visitor's side of the same thread — both halves of a conversation get the same
# room — and an over-long reply is REFUSED rather than silently truncated: the
# owner is talking to a customer, and a sentence that ends mid-word without anyone
# saying so is worse than an error they can act on.
_MAX_OWNER_REPLY_CHARS = 4000

# How many out-of-band lines one visitor poll returns (the most recent N,
# presented oldest-first). A widget polls every few seconds, so it is never behind
# by more than a handful; the cap exists so a thread that sat unpolled for a day
# can't return an unbounded page.
_OWNER_POLL_CAP = 50

# The only roles a PUBLIC read may serve. The visitor's own muted line is stored
# in the same table (it has no run doc) and is deliberately NOT in here: echoing a
# visitor's words back to them is pointless, and a public read that serves stored
# visitor content is one bug away from serving someone else's.
_PUBLIC_MESSAGE_ROLES = ["owner", "system"]

# What the widget is told when it sends into a muted bot. It is NOT an answer and
# must never be rendered as one — the frame's own event name says so. Kept short
# and specific: the visitor needs to know a person has this, not to read a policy.
_HUMAN_REPLYING_MESSAGE = "Someone from the team is replying — hang tight."

# What a visitor is told when they use the escape hatch. States the fact — a
# person has been told — without promising a response time nobody committed to.
_HUMAN_NOTIFIED_MESSAGE = "Someone from the team has been notified and will pick this up."

# How many of a visitor's ref characters survive into the fallback display name.
# Enough to tell two visitors apart at a glance, short enough to read as a label.
_DISPLAY_REF_CHARS = 6

# How many widgets one agent's inbox unions over. An agent normally fronts ONE
# site; the cap keeps a pathological binding from fanning out into an unbounded
# number of per-site scans.
_AGENT_WIDGET_CAP = 20

# Max characters of a visitor's own message persisted on the run doc for the
# transcript. The agent still receives the FULL message — this caps only what we
# write down, so a pasted wall of text (or a deliberate storage-stuffing attempt)
# can't grow the run collection without bound. Generous enough that a real
# question is never clipped.
_STORED_USER_TEXT_CHARS = 4000

# ---------------------------------------------------------------------------
# Concierge conversation memory — the bounds on rehydrated history
#
# Every concierge turn replays the visitor's PRIOR turns into the run so the
# agent remembers what was already said. Unbounded that would be both a cost
# problem (the whole conversation is re-sent, and re-billed, on every turn) and
# an abuse vector (a visitor could grow the prompt indefinitely by pasting).
# Three bounds, each closing a different hole:
# ---------------------------------------------------------------------------

# How many prior EXCHANGES (a run = the visitor's line + the agent's reply) are
# replayed. The most recent N, presented oldest-first. A site conversation is
# short by nature — a dozen exchanges covers a full support or sales back-and-
# forth, and it keeps the per-turn prompt cost flat instead of growing linearly.
_HISTORY_TURN_CAP = 12

# Max characters of any SINGLE replayed line. A long agent reply would otherwise
# eat the whole budget below and evict every other turn; clipping it means the
# newest exchange ALWAYS fits (2 x this < the total below), so memory can never
# degrade to nothing because of one verbose turn. The clip affects the replayed
# copy only — the stored transcript the owner reads is untouched.
_HISTORY_MESSAGE_CHARS = 2000

# Total characters across all replayed lines (~3k tokens). This is the real
# ceiling on what a visitor can force us to re-send every turn. Turns are fitted
# newest-first and the budget cuts off the OLDEST ones, so what survives is
# always a contiguous run of the most recent conversation, never a gappy one.
_HISTORY_TOTAL_CHARS = 12000


class AdminWidgetView(BaseModel):
    """The site's paw-bar widget as the owner dashboard needs it (D2 overview)."""

    id: str
    spec: PawBarSpec
    agent_id: str = ""
    # The bound agent's display name (feat/site-dedicated-agent, E2). Resolved from
    # the agents service when ``agent_id`` is set so the dashboard card can show the
    # concierge name and detect the "<x> Concierge" dedicated pattern. Empty when the
    # widget is unbound OR the agent no longer resolves (a dangling agent_id degrades
    # to "" rather than 500-ing the overview).
    agent_name: str = ""


class OverviewCounts(BaseModel):
    """Cheap per-site counters for the dashboard header (D2)."""

    conversations: int = 0
    pending_decisions: int = 0
    handoffs: int = 0


class SiteOverviewResponse(BaseModel):
    """GET /paw-bar/admin/site/{id}/overview payload (D2).

    ``widget`` is ``None`` when the site has no paw-bar widget yet (a published
    site whose concierge widget was never created) — the toggle + greeting still
    render off the Site, and every count is 0.
    """

    widget: AdminWidgetView | None = None
    enabled: bool
    greeting: str
    # The third owner setting, alongside ``enabled`` and ``greeting``: whether the
    # visitor's own messages are stored. Carried here so the dashboard renders all
    # three from the one call it already makes rather than a second round trip for
    # a single boolean.
    store_transcripts: bool = True
    counts: OverviewCounts


class ConversationItem(BaseModel):
    """One row in the owner's conversation list (D2 + the slice-1 queue fields).

    The first three fields are the frozen D2 contract, derived from the run docs.
    Everything after them comes from the ``paw_bar_conversations`` state row and
    carries a SAFE DEFAULT: a conversation that has no row yet — every one that
    predates the table — still serializes, reading as a plain open conversation
    with nothing pending. That is what makes the queue backfill-free.
    """

    customer_ref: str
    # WHICH conversation this row is (2026-08-19). A visitor may hold several, so
    # ``customer_ref`` names the person and no longer identifies the row. Empty
    # only for a conversation with no state row and no identified run — the
    # legacy shape, kept listable rather than dropped.
    conversation_id: str = ""
    last_message_at: str
    preview: str
    state: str = "open"
    bot_paused: bool = False
    unread_for_owner: int = 0
    tags: list[str] = Field(default_factory=list)
    snooze_until: str = ""
    contact_email: str = ""
    # What the owner should SEE instead of a 32-char hex handle: the visitor's
    # email when they left one, else a short readable stub of the ref.
    display_name: str = ""
    # True when this visitor has an undecided gated action waiting on the owner —
    # the "needs you" chip. Read from the SAME decision rows the Decisions list
    # uses (never a second query against Instinct, whose rows are keyed by the
    # widget owner rather than the tenant).
    has_pending_action: bool = False


class ConversationsResponse(BaseModel):
    """GET /paw-bar/admin/site/{id}/conversations payload (D2 + slice 1).

    ``unsupported`` stays False on this deployment: concierge runs ARE listable
    (the ChatRunDoc compound index backs the per-site query). The field is part of
    the frozen contract so the frontend degrades gracefully if a future backend
    can't serve the list. ``cursor`` is the ISO ``createdAt`` to page older
    conversations from; ``None`` when the scan reached the end.

    ``counts`` is the per-state total for the whole widget — UNFILTERED, so the
    filter chips keep showing all four numbers while one of them is active. It is
    ``{}`` when the site has no paw-bar widget (there is nothing to count), and
    counts only conversations that actually have a state row: a legacy row still
    LISTS with defaults, but it is never invented into a total.
    """

    items: list[ConversationItem] = Field(default_factory=list)
    cursor: str | None = None
    unsupported: bool = False
    counts: dict[str, int] = Field(default_factory=dict)


class AgentConversationItem(ConversationItem):
    """A conversation in the AGENT-scoped inbox — the site lens made explicit.

    One agent can front more than one site (manual widget binds are never
    overwritten by the provisioner), so the union carries which site each
    conversation came from. Identical to :class:`ConversationItem` otherwise.
    """

    site_id: str = ""
    site_name: str = ""


class AgentSiteRef(BaseModel):
    """One site included in an agent-scoped union."""

    site_id: str
    site_name: str = ""


class AgentConversationsResponse(BaseModel):
    """GET /paw-bar/admin/agent/{agent_id}/conversations payload (D1 seed).

    ``sites`` + ``widget_count`` are the POSITIVE binding signal: an agent that
    fronts no paw-bar widget answers 200 with ``widget_count=0`` and empty
    everything, so a client can tell "a concierge with a quiet inbox" from "an
    ordinary agent that should not show a Conversations tab at all" without a
    second call. ``counts`` sums the per-widget state counts across the union and
    is UNFILTERED, like the site read's.
    """

    items: list[AgentConversationItem] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    sites: list[AgentSiteRef] = Field(default_factory=list)
    widget_count: int = 0


class ConversationRow(BaseModel):
    """The full conversation state row, as the PATCH echoes it back.

    ``state`` is the EFFECTIVE state (an expired snooze reads ``open``) while
    ``snooze_until`` keeps the stored timestamp, so the UI can say "was snoozed
    until 9am" on a row that has already come back.

    DELIBERATELY NOT a whole list item. ``preview``, ``last_message_at`` and
    ``has_pending_action`` are derived from the run docs, not from this row, so a
    client re-rendering a list row from this echo must carry those three over from
    the item it already had (or refetch the list). Completing the shape here would
    put a run query on a write path purely to hand back data the caller is already
    displaying — don't add it without a reason that survives that sentence.
    """

    id: str
    widget_id: str
    customer_ref: str
    state: str
    bot_paused: bool
    snooze_until: str
    assignee: str
    tags: list[str] = Field(default_factory=list)
    notes: list[dict[str, str]] = Field(default_factory=list)
    contact_email: str
    display_name: str
    last_visitor_at: str
    last_owner_at: str
    # When the human stepped in (slice 2). ``bot_paused`` is EFFECTIVE, so it goes
    # false on its own once this ages past the idle window; this field is what lets
    # the UI say "hands back at 14:20" instead of just "paused".
    bot_paused_at: str = ""
    unread_for_owner: int
    created_at: str
    updated_at: str


class ConversationPatchRequest(BaseModel):
    """Body of PATCH .../conversations/{customer_ref} — every field optional.

    Only the fields actually SENT are applied (``model_fields_set``), so a client
    can flip one thing without echoing the row back. ``note`` APPENDS a private
    operator note; it never replaces the existing ones. ``tags`` DOES replace (a
    tag set is edited as a whole in the UI).

    ``unread_for_owner`` is deliberately absent: mark-as-read has no UI yet, and
    the store already supports the write, so this is one field away whenever a
    surface wants it. A client must NOT fake it locally — a browser-side read flag
    disagrees with the next poll seconds later, which reads as a counter that
    un-marks itself. Ask for the field instead.
    """

    state: str | None = None
    snooze_until: str | None = None
    tags: list[str] | None = None
    note: str | None = None
    bot_paused: bool | None = None
    # WHICH of the visitor's conversations to file (2026-08-19). Absent means the
    # one in progress, which is what every pre-identity client sends and what the
    # store has always written. It is not a patchable FIELD — it is the address —
    # so it is stripped before the field whitelist ever sees it.
    conversation_id: str | None = None


class ConversationPatchResponse(BaseModel):
    ok: bool = True
    conversation: ConversationRow


class TranscriptMessage(BaseModel):
    """One message in a conversation transcript (D2 drill-in).

    ``role`` is "user", "assistant", "owner" or "system". The first two come from
    the run stream — the agent reply from ``ChatRunDoc.partial_text``, the
    visitor's own line from ``ChatRunDoc.user_text``. The last two come from the
    ``paw_bar_owner_messages`` table: a human on the team typing, and the product
    explaining itself. A visitor line that arrived while the bot was muted has no
    run doc, so it comes from that table too and is presented as "user" — the
    thread cares who spoke, not which storage answered.

    Widening from user|assistant is ADDITIVE: an existing consumer that only knows
    the first two roles still renders every message it used to, in the same shape.

    A site whose owner turned ``concierge_store_transcripts`` off stores no visitor
    lines, so its transcripts are assistant-only (plus whatever the owner typed) —
    the same shape this DTO always had, just missing a role.

    There is deliberately NO message id: run-derived messages have none (they are
    projections of a run doc, two per run), so an id here would exist for half the
    thread. Anything that needs to address a message keys off the RUN.
    """

    role: str
    content: str
    created_at: str


class ConversationReplyRequest(BaseModel):
    """Body of POST .../conversations/{customer_ref}/reply — the owner's own turn.

    ``conversation_id`` names the thread being answered (2026-08-19). Absent means
    the visitor's active one — the pre-identity behaviour, kept so a cached
    dashboard bundle keeps working. Sending it is how an owner answers a question
    from a conversation the visitor has since moved on from without that answer
    materializing inside the conversation they are typing in right now.
    """

    text: str
    conversation_id: str | None = None


class ConversationReplyResponse(BaseModel):
    """What the composer gets back: the line it just sent + the row it changed.

    ``message`` is a :class:`TranscriptMessage` on purpose — the exact shape the
    thread is already rendering — so the composer appends the echo to the list it
    has instead of refetching the transcript to see its own sentence.
    ``conversation`` is the full row in the SAME shape the PATCH echoes, because
    sending a reply moves state (the bot mutes, a closed thread reopens, the unread
    badge clears) and the list row has to re-render.
    """

    ok: bool = True
    message: TranscriptMessage
    conversation: ConversationRow


class VisitorMessage(BaseModel):
    """One owner/system line as the VISITOR's widget sees it.

    A deliberately narrow projection, and the narrowness is the point: this is the
    only public read of a conversation. It carries what was said, who said it
    (owner or system — never an operator's identity), and when. Everything the
    owner side keeps on the same conversation — private notes, tags, assignee,
    captured email, the queue state — is owner-only data that must never cross to
    the visitor. ``at`` is an ISO-8601 UTC timestamp; a poller passes the last one
    it saw back as ``after``.
    """

    role: str
    content: str
    at: str


class VisitorMessagesResponse(BaseModel):
    """GET /paw-bar/messages/{widget_id}/{customer_ref} payload.

    ``bot_paused`` is the EFFECTIVE mute: true only while a human is actually
    holding the conversation, and false again the moment the idle window lapses.
    It is the one piece of conversation STATE a visitor is allowed to know, and
    only because it is about them — it tells the widget whether to say "someone is
    replying" or hand the composer back to the assistant.
    """

    messages: list[VisitorMessage] = Field(default_factory=list)
    bot_paused: bool = False


class ConversationTranscriptResponse(BaseModel):
    customer_ref: str
    messages: list[TranscriptMessage] = Field(default_factory=list)
    count: int
    # The Instinct proposals parked in THIS conversation, still waiting on a
    # human (slice 4 — the in-thread approval card). The dashboard's thread
    # prefers this over the site-wide decisions fallback because it is already
    # scoped to one visitor: with neither source, the UI can only show the
    # "approve it from Decisions" notice a real approval sat behind (found live
    # 2026-07-31). Settled decisions are deliberately absent — an approved
    # booking is history, and the card list is a to-do, not a log.
    pending_actions: list[DecisionItem] = Field(default_factory=list)
    # The conversation row's mute flag, so the thread's takeover banner state
    # arrives with the SAME read that renders the timeline.
    bot_paused: bool | None = None


class DecisionItem(BaseModel):
    id: str
    verb_or_kind: str
    summary: str
    status: str
    created_at: str
    # Which visitor raised it. The site-wide decisions list serves EVERY
    # visitor, so without this the thread's fallback source cannot attribute a
    # card to the open conversation and must refuse to guess (a stranger's
    # booking approved into the wrong thread is the failure mode). "" on a
    # legacy row.
    customer_ref: str = ""


class DecisionsResponse(BaseModel):
    items: list[DecisionItem] = Field(default_factory=list)


class HandoffItem(BaseModel):
    contact: str
    question: str
    transcript_ref: str
    created_at: str


class HandoffsResponse(BaseModel):
    items: list[HandoffItem] = Field(default_factory=list)


async def _resolve_site_and_widget(
    site_id: str, workspace_id: str
) -> tuple[Any, PawBarWidget | None]:
    """Resolve a site id → (Site, its paw-bar widget) both workspace-scoped (D2).

    Step 1 loads the Site scoped to the caller's workspace (cross-tenant / bad id
    → 404, via the shared ``_load_site_scoped``). Step 2 resolves the site's
    paw-bar widget from ``Site.pocket_id``, ALSO workspace-scoped, so the returned
    widget always belongs to the caller's tenant. Returns ``widget=None`` when the
    site has no paw-bar widget yet (the concierge was never wired) — the callers
    degrade to an empty view rather than 404, because the SITE exists and its
    owner-facing settings are still meaningful.
    """
    site = await _load_site_scoped(site_id, workspace_id)
    # Belt-and-suspenders (security review finding #2): never resolve a widget on
    # an EMPTY pocket_id. ``list_widgets`` drops the pocket filter on a falsy
    # pocket_id, so a bad write that ever left ``Site.pocket_id`` blank would
    # otherwise match the workspace's FIRST widget — a sibling site's concierge.
    # A site with no pocket has no concierge widget by definition; return None.
    if not site.pocket_id:
        return site, None
    widgets = await _store().list_widgets(
        pocket_id=site.pocket_id, workspace_id=workspace_id, limit=1
    )
    widget = widgets[0] if widgets else None
    return site, widget


def _decision_verb_or_kind(event_type: str) -> str:
    """Map a parked decision's ``event_type`` to the contract's ``verb_or_kind``.

    A gated concierge action parks ``event_type="paw_bar_action:<verb>"`` (see
    ``decision_loop.propose_customer_action``) → return ``<verb>``. Any other
    event is an ingest customer reply → return ``"customer_reply"``.
    """
    if event_type.startswith("paw_bar_action:"):
        return event_type.split(":", 1)[1] or "action"
    return "customer_reply"


def _decision_summary(decision: Any) -> str:
    """One-line owner-facing summary of a parked decision (D2 decisions list).

    Once a human has answered (delivered / declined), the operator's reply is the
    most useful summary; while pending, describe the request from its
    ``verb_or_kind``. Kept terse — the dashboard row is a glance, not the detail
    view.
    """
    reply = str(getattr(decision, "reply", "") or "").strip()
    if reply:
        return reply
    kind = _decision_verb_or_kind(str(getattr(decision, "event_type", "") or ""))
    if kind == "customer_reply":
        return "Customer request awaiting a reply"
    return f"Visitor '{kind}' action awaiting a decision"


@router.get(
    "/paw-bar/admin/site/{site_id}/overview",
    response_model=SiteOverviewResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_overview(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> SiteOverviewResponse:
    """Aggregate a site's concierge widget, kill-switch state, and cheap counts.

    Admin-authed, workspace-scoped (cross-tenant site id → 404). Counts are cheap
    (COUNT / distinct) and each is scoped to THIS site's widget / pocket — never
    pocket-wide or workspace-wide. Every count degrades to 0 on a missing widget
    or an unavailable backing store rather than failing the whole overview.
    """
    site, widget = await _resolve_site_and_widget(site_id, workspace_id)

    counts = OverviewCounts()
    widget_view: AdminWidgetView | None = None
    if widget is not None:
        widget_view = AdminWidgetView(
            id=widget.id,
            spec=widget.spec,
            agent_id=widget.agent_id,
            agent_name=await _bound_agent_name(widget.agent_id),
        )
        counts.pending_decisions = await _store().count_pending_decisions(widget.id)
        counts.handoffs = await _count_handoffs(widget.id, workspace_id)
    # Conversations are pocket-scoped (a Site is 1:1 with its pocket), so the
    # count stands even when the widget row is absent.
    counts.conversations = await _count_conversations(site.pocket_id, workspace_id)

    return SiteOverviewResponse(
        widget=widget_view,
        enabled=site.concierge_enabled,
        greeting=site.concierge_greeting,
        store_transcripts=site.concierge_store_transcripts,
        counts=counts,
    )


@router.get(
    "/paw-bar/admin/site/{site_id}/conversations",
    response_model=ConversationsResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_conversations(
    site_id: str,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    state: str | None = Query(None, description="open | needs_human | snoozed | closed"),
    workspace_id: str = Depends(current_workspace_id),
) -> ConversationsResponse:
    """Recent concierge conversations for a site, newest first (D2 + slice 1).

    Concierge runs persist as ``ChatRunDoc`` (context_type "concierge",
    scope_id = the site's pocket). The compound (workspace, context_type,
    scope_id, createdAt) index backs an efficient per-site query, so this is
    LISTABLE (``unsupported`` stays False). Runs are grouped by customer_ref into
    one conversation each (most-recent run wins for the preview + timestamp). The
    scan window is bounded (never a full-collection read). Cross-site isolation:
    scope_id is the site's OWN pocket, so a sibling site's runs never match.

    Slice 1 joins each conversation's state row onto its item (queue state, unread
    count, tags, whether a decision is waiting) and returns the widget's unfiltered
    per-state ``counts``. ``?state=`` filters the page on the joined state; an
    unknown value 422s rather than silently returning everything.
    """
    if state is not None and state not in {s.value for s in ConversationState}:
        raise HTTPException(422, "invalid_state")
    site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    return await _list_conversations(
        site.pocket_id, workspace_id, limit=limit, cursor=cursor, widget=widget, state=state
    )


@router.patch(
    "/paw-bar/admin/site/{site_id}/conversations/{customer_ref}",
    response_model=ConversationPatchResponse,
)
async def patch_site_conversation(
    site_id: str,
    customer_ref: str,
    req: ConversationPatchRequest,
    # The gate doubles as the author lookup: ``require_action`` RETURNS the caller
    # once it clears the role check, so a note is attributed without a second dep.
    user: Any = Depends(_require_paw_bar_manage),
    workspace_id: str = Depends(current_workspace_id),
) -> ConversationPatchResponse:
    """File a conversation: change its state, snooze it, tag it, or note it.

    The owner WRITE half of the inbox. Gated on ``paw_bar.manage`` — the paw-bar
    write action (there is no ``paw_bar.write``; ``manage`` is the mutation gate
    this router already uses, and both resolve to ADMIN) — and workspace-scoped at
    the same two seams as the reads: the Site loads scoped (cross-tenant → 404),
    then its widget, so the row being written always belongs to this tenant.

    LAZY ROW CREATION: a conversation that predates the state table has no row.
    Rather than 404 on the owner's first snooze, the row is minted with defaults
    (``ensure_conversation`` — it does NOT touch the unread counter or the visitor
    timestamps, because an owner filing something is not visitor activity) and the
    patch applies to it. A ``customer_ref`` with no concierge runs on this site
    404s: that is not a conversation, it's a guess.

    Only the fields SENT are applied. ``note`` appends; ``tags`` replaces.
    """
    if not _CUSTOMER_REF_RE.match(customer_ref or ""):
        raise HTTPException(400, "invalid_customer_ref")
    # str(): the authenticated caller's id arrives as a PydanticObjectId, and a
    # note carrying it raw fails ConversationNote's string_type validation with a
    # 500. Unit tests hand-build notes with plain strings, so only a live PATCH
    # showed it (rig, 2026-07-30).
    fields = _validated_conversation_fields(req, str(getattr(user, "id", "") or ""))
    site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        raise HTTPException(404, "conversation_not_found")

    store = _store()
    conversation = await _resolve_owner_conversation(
        store, site, widget, customer_ref, workspace_id, req.conversation_id or ""
    )

    if fields:
        before = conversation
        updated = await store.update_conversation(
            widget.id,
            customer_ref,
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            **fields,
        )
        conversation = updated or conversation
        # AL-2 — record whichever ledger beats this patch actually crossed
        # (takeover if the mute went on, handoff.resolved if the thread left
        # ``needs_human``). Read from the before/after ROWS rather than from
        # ``fields``, so a no-op patch records nothing and a patch that crosses
        # both records both. This is the ONLY path in the product that takes a
        # conversation out of ``needs_human``, which is why the resolved half of
        # the handoff vocabulary fires here rather than in handoff.py.
        from pocketpaw_ee.paw_bar import ledger

        await ledger.emit_conversation_transition(
            widget=widget,
            workspace_id=workspace_id,
            customer_ref=customer_ref,
            before=before,
            after=updated,
        )
    # Resolve the display email the same way the LIST does, so a client that
    # re-renders a row from this echo doesn't watch a named visitor turn back into
    # an anonymous handle.
    _pending, emails = await _decision_side_data(widget, [customer_ref])
    return ConversationPatchResponse(
        ok=True,
        conversation=_conversation_row(conversation, emails.get(customer_ref, "")),
    )


@router.post(
    "/paw-bar/admin/site/{site_id}/conversations/{customer_ref}/reply",
    response_model=ConversationReplyResponse,
)
async def post_site_conversation_reply(
    site_id: str,
    customer_ref: str,
    req: ConversationReplyRequest,
    # Same gate-as-author-lookup as the PATCH: ``require_action`` hands back the
    # caller once the role check passes, so the reply is attributed for free.
    user: Any = Depends(_require_paw_bar_manage),
    workspace_id: str = Depends(current_workspace_id),
) -> ConversationReplyResponse:
    """The owner types, and the bot stops talking. THE takeover.

    Typing IS taking over — there is no separate "take over" button to forget to
    press, because the failure this design refuses is a human and a bot answering
    the same customer at the same time. So one call does all of it, in one place,
    and no caller can do half:

      * the reply is persisted as an ``owner`` line on the thread;
      * ``bot_paused`` goes on (and the store stamps WHEN, which starts the
        4h idle clock that eventually hands the conversation back);
      * ``last_owner_at`` is now and ``unread_for_owner`` clears — the owner is
        demonstrably reading this conversation, so it is no longer unread;
      * a ``closed`` or ``snoozed`` thread REOPENS. An owner replying is
        engagement; leaving it filed while a human answers it is how a
        conversation disappears from the queue mid-sentence.

    Auth and tenancy are the PATCH's, exactly: ``paw_bar.manage``, the Site loaded
    workspace-scoped (cross-tenant → 404), then its widget — so the row written
    always belongs to this tenant. A ``customer_ref`` with no concierge runs on
    this site 404s, same rule and for the same reason: that is not a conversation,
    it's a guess.

    D4: an owner's chat reply does NOT go through Instinct. Instinct gates
    agent-proposed ACTIONS, because a machine wants a side effect. A human typing
    a sentence is already the human decision.

    The reply is NOT dispatched as a run. It never touches ``ChatRunDoc``, so it
    is never swept into billing (``metering.sweeper`` bills every unbilled terminal
    run) and never counted as agent compute — the owner is not charged credits for
    typing their own sentence.
    """
    if not _CUSTOMER_REF_RE.match(customer_ref or ""):
        raise HTTPException(400, "invalid_customer_ref")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(422, "empty_reply")
    if len(text) > _MAX_OWNER_REPLY_CHARS:
        raise HTTPException(422, "reply_too_long")

    site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        raise HTTPException(404, "conversation_not_found")

    store = _store()
    conversation = await _resolve_owner_conversation(
        store, site, widget, customer_ref, workspace_id, req.conversation_id or ""
    )

    message = await store.add_owner_message(
        widget.id,
        customer_ref,
        text,
        # The line is said IN a conversation (2026-08-19). Without this it was
        # said to the VISITOR, and surfaced in every thread they owned — including
        # ones they started after it was written.
        conversation_id=conversation.id,
        role=OwnerMessageRole.OWNER,
        author=getattr(user, "id", "") or "",
        workspace_id=workspace_id,
    )

    fields: dict[str, Any] = {
        "bot_paused": True,
        "last_owner_at": datetime.now().isoformat(),
        "unread_for_owner": 0,
    }
    if conversation.state in (ConversationState.CLOSED, ConversationState.SNOOZED):
        fields["state"] = ConversationState.OPEN.value
        # A reopened thread forgets its snooze deadline, the same way a visitor's
        # return does — it is live again, not merely due back later.
        fields["snooze_until"] = ""
    before = conversation
    updated = await store.update_conversation(
        widget.id,
        customer_ref,
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        **fields,
    )
    conversation = updated or conversation
    # AL-2 — typing IS taking over, so this is where ``paw.conversation.takeover``
    # is earned. Same before/after diff as the PATCH path, through the same
    # helper, so the two ways an owner can mute the bot cannot record it
    # differently. Never raises: an owner's reply must not depend on the ledger.
    from pocketpaw_ee.paw_bar import ledger

    await ledger.emit_conversation_transition(
        widget=widget,
        workspace_id=workspace_id,
        customer_ref=customer_ref,
        before=before,
        after=updated,
    )

    _pending, emails = await _decision_side_data(widget, [customer_ref])
    return ConversationReplyResponse(
        ok=True,
        message=_owner_transcript_message(message),
        conversation=_conversation_row(conversation, emails.get(customer_ref, "")),
    )


@router.get(
    "/paw-bar/admin/agent/{agent_id}/conversations",
    response_model=AgentConversationsResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_agent_conversations(
    agent_id: str,
    limit: int = Query(20, ge=1, le=100),
    state: str | None = Query(None, description="open | needs_human | snoozed | closed"),
    workspace_id: str = Depends(current_workspace_id),
) -> AgentConversationsResponse:
    """Every visitor conversation this AGENT is answering, across its sites (D1).

    A site concierge is a normal Agent, and "this site's conversations" and "this
    agent's visitor conversations" are the same list — the site is 1:1 with its
    concierge. This endpoint is the agent-centric side of that identity, and the
    seed of the cross-site inbox: it resolves agent → its bound widget(s) → their
    sites and unions the per-site lists, newest first, each item carrying the site
    it came from.

    Tenancy, three seams deep: the AGENT must live in the caller's workspace (a
    cross-tenant id 404s, never leaking that it exists), the widgets are read
    workspace-scoped, and each widget's Site must resolve in this workspace or the
    widget is skipped — so a legacy widget row with a blank workspace can't drag a
    foreign site's conversations into the union.

    An agent with no bound widget answers 200 with ``widget_count=0`` and empty
    everything: "not a concierge" is a normal answer, not an error, and the
    positive marker lets a client hide the tab rather than guess from emptiness.
    """
    if state is not None and state not in {s.value for s in ConversationState}:
        raise HTTPException(422, "invalid_state")
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.sites.service import _canonical_site_doc

    try:
        agent_workspace = await agents_service.get_workspace(agent_id)
    except Exception:  # noqa: BLE001 — an unresolvable agent is a 404, not a 500
        agent_workspace = None
    if agent_workspace != workspace_id:
        raise HTTPException(404, "agent_not_found")

    widgets = await _store().list_widgets(
        agent_id=agent_id, workspace_id=workspace_id, limit=_AGENT_WIDGET_CAP
    )

    items: list[AgentConversationItem] = []
    sites: list[AgentSiteRef] = []
    counts: dict[str, int] = {}
    for widget in widgets:
        if not widget.pocket_id:
            continue
        # ``_canonical_site_doc`` rather than a bare find_one: a pocket published
        # before stable site identity can still have duplicate Site docs, and the
        # arbitrary one is how a dashboard ends up deep-linking a stale site id.
        # Reused rather than re-derived so there is one answer to "which Site doc
        # is this pocket's site".
        site = await _canonical_site_doc(workspace_id, widget.pocket_id)
        if site is None:
            # The widget's pocket has no published site in this workspace — there
            # is no conversation surface to read, and (belt-and-suspenders) this is
            # where a legacy blank-workspace widget stops.
            continue
        sites.append(AgentSiteRef(site_id=str(site.id), site_name=site.name or ""))
        page = await _list_conversations(
            widget.pocket_id, workspace_id, limit=limit, cursor=None, widget=widget, state=state
        )
        for item in page.items:
            items.append(
                AgentConversationItem(
                    **item.model_dump(), site_id=str(site.id), site_name=site.name or ""
                )
            )
        for key, value in page.counts.items():
            counts[key] = counts.get(key, 0) + value

    # Newest first across the whole union, then cut to the page size. Sorting on
    # the ISO timestamp is a string sort on purpose — the values are all produced
    # by ``datetime.isoformat`` and an empty one sorts last, where a conversation
    # with no timestamp belongs.
    items.sort(key=lambda i: i.last_message_at or "", reverse=True)
    return AgentConversationsResponse(
        items=items[:limit],
        counts=counts,
        sites=sites,
        # The BINDING signal: how many paw-bar widgets this agent answers for,
        # counted before the site resolution. ``sites`` can be shorter (a widget
        # whose site was never published has nothing to list) — but the agent is a
        # concierge either way, and that is the question a client is asking.
        widget_count=len(widgets),
    )


@router.get(
    "/paw-bar/admin/site/{site_id}/conversations/{customer_ref}",
    response_model=ConversationTranscriptResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_conversation_transcript(
    site_id: str,
    customer_ref: str,
    conversation_id: str = Query(
        "", description="Read ONE conversation, not the visitor's whole history"
    ),
    workspace_id: str = Depends(current_workspace_id),
) -> ConversationTranscriptResponse:
    """One visitor's concierge transcript on a site, oldest-first (D2 drill-in).

    Admin/owner-authed (``paw_bar.read``), workspace-scoped. Resolves site → widget
    → pocket exactly like the sibling reads, then returns the messages of the
    concierge conversation for ``customer_ref`` on THIS site's pocket
    (``ChatRunDoc`` with context_type "concierge", scope_id = the pocket, user_id =
    customer_ref). Capped at the most recent ``_TRANSCRIPT_CAP`` (200) turns,
    presented oldest-first. 404 when the ref has no concierge conversation here.

    ROLE COVERAGE: all four roles are here — "user" and "assistant" from the run
    stream, "owner" and "system" from the out-of-band table (slice 2), interleaved
    strictly by timestamp so the thread shows exactly when a human stepped in. The
    visitor half is stored only while the site's ``concierge_store_transcripts``
    toggle is on (it is personal data, so the owner controls it); an owner who
    turns it off keeps getting assistant-only transcripts from that point on, and
    lines already stored are NOT retroactively purged.
    """
    if not _CUSTOMER_REF_RE.match(customer_ref or ""):
        raise HTTPException(400, "invalid_customer_ref")
    site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    # No concierge widget on this site → no conversation exists to read.
    if widget is None:
        raise HTTPException(404, "conversation_not_found")
    messages = await _load_transcript(
        site.pocket_id,
        customer_ref,
        workspace_id,
        widget=widget,
        conversation_id=conversation_id,
    )
    if messages is None:
        # No concierge run for this (pocket, customer_ref) — the ref has no
        # conversation on this site's widget.
        raise HTTPException(404, "conversation_not_found")

    # Slice 4: the approvals parked in THIS conversation ride with the thread.
    # Filtered in-handler off the same widget-scoped read the decisions tab
    # uses (widget_id is the cross-site isolation seam), narrowed to this
    # visitor + still-pending. Failure-soft: a broken decisions read costs the
    # thread its cards, never the transcript.
    pending: list[DecisionItem] = []
    try:
        from pocketpaw.paw_bar.models import DecisionState

        decisions = await _store().list_decisions_for_widget(widget.id, limit=200)
        pending = [
            DecisionItem(
                id=d.instinct_action_id or d.id,
                verb_or_kind=_decision_verb_or_kind(d.event_type),
                summary=_decision_summary(d),
                status=d.state.value,
                created_at=d.created_at.isoformat(),
                customer_ref=d.customer_ref,
            )
            for d in decisions
            if d.customer_ref == customer_ref and d.state == DecisionState.PENDING
        ]
    except Exception:  # noqa: BLE001 — cards degrade, the transcript never 500s
        logger.warning("transcript pending_actions read failed (non-fatal)", exc_info=True)

    bot_paused: bool | None = None
    try:
        conversation = await _store().get_conversation(
            widget.id, customer_ref, workspace_id=workspace_id
        )
        if conversation is not None:
            bot_paused = bool(conversation.bot_paused)
    except Exception:  # noqa: BLE001 — same degrade rule
        logger.warning("transcript conversation read failed (non-fatal)", exc_info=True)

    return ConversationTranscriptResponse(
        customer_ref=customer_ref,
        messages=messages,
        count=len(messages),
        pending_actions=pending,
        bot_paused=bot_paused,
    )


@router.get(
    "/paw-bar/admin/site/{site_id}/decisions",
    response_model=DecisionsResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_decisions(
    site_id: str,
    limit: int = Query(50, ge=1, le=200),
    workspace_id: str = Depends(current_workspace_id),
) -> DecisionsResponse:
    """Recent decisions raised on a site's concierge widget, newest first (D2).

    Reads the paw_bar ``DecisionStatus`` rows filtered ``WHERE widget_id = ?`` —
    the airtight cross-site isolation seam (the widget was resolved
    workspace-scoped, so its id belongs to this tenant, and a sibling widget's id
    never matches). Each row maps to {id (the Instinct action id, the Tray join
    key), verb_or_kind, summary, status, created_at}. No widget → empty list.
    """
    _site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        return DecisionsResponse(items=[])
    decisions = await _store().list_decisions_for_widget(widget.id, limit=limit)
    items = [
        DecisionItem(
            # Prefer the Instinct action id so the dashboard can deep-link the
            # decision into The Tray; fall back to the row id for a pre-loop row.
            id=d.instinct_action_id or d.id,
            verb_or_kind=_decision_verb_or_kind(d.event_type),
            summary=_decision_summary(d),
            status=d.state.value,
            created_at=d.created_at.isoformat(),
            customer_ref=d.customer_ref,
        )
        for d in decisions
    ]
    return DecisionsResponse(items=items)


@router.get(
    "/paw-bar/admin/site/{site_id}/handoffs",
    response_model=HandoffsResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_handoffs(
    site_id: str,
    limit: int = Query(50, ge=1, le=200),
    workspace_id: str = Depends(current_workspace_id),
) -> HandoffsResponse:
    """Human-handoff requests captured for a site's concierge widget (D2).

    Reads ``_paw_handoffs`` Fabric objects scoped to this widget + workspace, each
    object's properties mapping to {contact, question, transcript_ref,
    created_at}. Since slice 3 these are real rows: ``paw_bar.handoff`` writes one
    whenever a visitor asks for a person (their own button, or the concierge's
    ``pawbar_request_human`` tool). Empty is still the correct answer for a site
    nobody has escalated. Cross-site isolation is the ``widget_id`` property
    filter (a sibling widget's handoffs never match) on top of the
    workspace-scoped Fabric store and the workspace-scoped widget resolution.
    """
    _site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        return HandoffsResponse(items=[])
    objects = await _query_handoff_objects(widget.id, workspace_id, limit=limit)
    items = [
        HandoffItem(
            contact=str(o.properties.get("contact", "") or ""),
            question=str(o.properties.get("question", "") or ""),
            transcript_ref=str(o.properties.get("transcript_ref", "") or ""),
            created_at=o.created_at.isoformat() if getattr(o, "created_at", None) else "",
        )
        for o in objects
    ]
    return HandoffsResponse(items=items)


@router.get(
    "/paw-bar/admin/site/{site_id}/preview-frame",
    response_class=HTMLResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_preview_frame(
    site_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
) -> HTMLResponse:
    """Serve the concierge bar frame for the OWNER to preview inside the dashboard (D5).

    This is the SESSION-authed sibling of the public GET /paw-bar/frame: same glass
    app, same ``_pawbar_bootstrap_html`` + config builder, but gated by the admin
    role (``paw_bar.read``) instead of the world-visible embed key, and framed by
    the DASHBOARD origin instead of the Site's public ``allowed_origins``. It exists
    so an owner can test their bar in the dashboard without exposing a permissive
    ancestor to the public.

    Differences from the public frame (everything else is identical):
      * Auth: admin/owner role (route-level ``_require_paw_bar_read``), workspace-
        scoped site resolution (cross-tenant / bad id → 404, no widget → 404).
      * CSP ``frame-ancestors`` = the configured dashboard origin
        (``PAWBAR_DASHBOARD_ORIGIN``), sanitized to a single host[:port] — never the
        Site allowlist, never ``*``, never the request's own Origin/Referer.
      * Kill switch: served REGARDLESS of ``concierge_enabled`` so the owner can
        preview a PAUSED bar. Chat/action are unchanged and still obey the kill
        switch, so a paused bar renders in preview but won't answer — correct and
        consistent. The preview iframe is same-origin with the backend, so its
        chat/action calls already satisfy the existing dual-mode Origin gate.
    """
    site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        raise HTTPException(status_code=404, detail="no_concierge_widget")

    # The ONLY embedder allowed to frame the preview is the dashboard origin.
    # Reuse the SAME sanitizer the public frame uses on allowed_origins, so the
    # ancestor is reduced to a safe host[:port] with no header injection. Fail
    # closed (500 — a misconfiguration, not a client error) if the configured
    # origin can't be represented; NEVER emit a source-less or wildcard directive.
    dash = _dashboard_origin()
    csp = _frame_ancestors_csp([dash])
    if csp is None:
        raise HTTPException(status_code=500, detail="dashboard_origin_invalid")

    api_base = request.url.path.split("/paw-bar/", 1)[0]
    # E3 — thread the BOUND agent's conversation starters (capped 4). The widget was
    # resolved workspace-scoped above, so reading its agent's starters here is safe
    # (no cross-tenant reach). [] on this branch until the ASG-1 identity fields land.
    config = _pawbar_frame_config(
        site_key=site.signed_key,
        widget_id=widget.id,
        api_base=api_base,
        # The dashboard is the trusted parent; validate it against itself so the
        # glass app's postMessage targetOrigin is a clean scheme://host[:port].
        parent_origin=_safe_parent_origin(dash, [dash]),
        greeting=site.concierge_greeting or "",
        starters=await _bound_agent_starters(widget.agent_id),
        appearance=getattr(site, "concierge_appearance", None),
        preview=True,
    )
    # Preview-only dark page so the transparent bar reads as sitting on the dark
    # TRANSPARENT, exactly like the public embed. The dashboard composes the
    # preview now: it frames the site's own published page and lays this frame
    # over it, so the bar sits on the real page rather than on a colour we chose
    # for it. A page background here would paint over that site.
    # The site's own published URL, straight off the document this route already
    # loaded. "" when the site has never deployed, or deployed with no dispatch
    # domain configured — there is genuinely nothing to frame then, and the bar
    # previews on a plain surface rather than in an empty white box.
    scene = getattr(site, "url", "") or ""
    if scene:
        scene += ("&" if "?" in scene else "?") + "pawbar=off"
    html = _pawbar_bootstrap_html(config, PAWBAR_APP_MOUNT, scene_url=scene)
    return HTMLResponse(
        content=html,
        headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"},
    )


# --- D2 aggregation data-source helpers -------------------------------------


async def _resolve_owner_conversation(
    store: Any,
    site: Any,
    widget: Any,
    customer_ref: str,
    workspace_id: str,
    conversation_id: str = "",
) -> Any:
    """The conversation an owner's write addresses. Raises 404 if there isn't one.

    ONE resolver for the PATCH and the reply (2026-08-19), because they were
    resolving separately and both resolved to the visitor's ACTIVE row — so an
    owner acting on the thread they were reading silently acted on a different
    one the moment the visitor started a new conversation.

    With ``conversation_id`` the named row must belong to this widget AND this
    visitor; a mismatch is a 404 rather than a silent fallback. The fallback is
    right on the VISITOR path (telling an anonymous caller that an id was real is
    the only thing a refusal there would leak), and wrong here: this caller is
    authenticated and already workspace-scoped, so answering their explicit
    address with a different conversation's data is a correctness bug, not
    defence.

    Without it, the visitor's conversation in progress — the pre-identity
    behaviour, kept so a cached dashboard bundle keeps working. LAZY ROW
    CREATION is preserved on that path only: a conversation that predates the
    state table is minted on first touch rather than 404ing the owner's first
    snooze.
    """
    if conversation_id:
        named = await store.get_conversation_by_id(conversation_id, workspace_id=workspace_id)
        if named is None or named.widget_id != widget.id or named.customer_ref != customer_ref:
            raise HTTPException(404, "conversation_not_found")
        return named
    conversation = await store.get_conversation(widget.id, customer_ref, workspace_id=workspace_id)
    if conversation is None:
        runs = await _concierge_runs_for_visitor(
            site.pocket_id, customer_ref, workspace_id, limit=1
        )
        if not runs:
            raise HTTPException(404, "conversation_not_found")
        conversation = await store.ensure_conversation(widget.id, customer_ref, workspace_id)
    return conversation


def _validated_conversation_fields(req: ConversationPatchRequest, author: str) -> dict[str, Any]:
    """Turn a validated PATCH body into the store's keyword fields.

    Only the keys the client actually SENT survive (``model_fields_set``), so a
    PATCH carrying one field can't blank the others. Validation is strict and
    up-front — a bad value is a 422 before anything is written, never a row in a
    state no filter can find:

      * ``state`` must be one of the four; anything else 422s.
      * ``snooze_until`` must be an ISO timestamp (the store compares it as a
        string in SQL, so a free-form value would silently never expire). Empty
        string is allowed — it clears the snooze.
      * ``tags`` are trimmed, de-duplicated, and capped in count and length.
      * ``note`` is capped and attributed to the CALLER, never to a client-
        supplied author.
    """
    fields: dict[str, Any] = {}
    sent = req.model_fields_set
    if "state" in sent and req.state is not None:
        if req.state not in {s.value for s in ConversationState}:
            raise HTTPException(422, "invalid_state")
        fields["state"] = req.state
    if "snooze_until" in sent and req.snooze_until is not None:
        stamp = req.snooze_until.strip()
        if stamp:
            try:
                datetime.fromisoformat(stamp)
            except ValueError as exc:
                raise HTTPException(422, "invalid_snooze_until") from exc
        fields["snooze_until"] = stamp
    if "tags" in sent and req.tags is not None:
        cleaned: list[str] = []
        for tag in req.tags:
            value = str(tag).strip()[:_MAX_TAG_CHARS]
            if value and value not in cleaned:
                cleaned.append(value)
        if len(cleaned) > _MAX_CONVERSATION_TAGS:
            raise HTTPException(422, "too_many_tags")
        fields["tags"] = cleaned
    if "note" in sent and req.note is not None:
        text = req.note.strip()[:_MAX_NOTE_CHARS]
        if text:
            fields["note"] = {
                "author": author,
                "text": text,
                "at": datetime.now().isoformat(),
            }
    if "bot_paused" in sent and req.bot_paused is not None:
        fields["bot_paused"] = bool(req.bot_paused)
    return fields


def _conversation_row(conversation: Any, captured_email: str = "") -> ConversationRow:
    """Project a stored :class:`Conversation` into the wire shape.

    ``captured_email`` is the address the visitor left on a decision, resolved by
    the caller (slice 1 never writes it onto the row itself). It fills both
    ``contact_email`` and the display name so this echo and the list agree.
    """
    contact_email = conversation.contact_email or captured_email
    return ConversationRow(
        id=conversation.id,
        widget_id=conversation.widget_id,
        customer_ref=conversation.customer_ref,
        state=conversation.state.value,
        bot_paused=conversation.bot_paused,
        snooze_until=conversation.snooze_until,
        assignee=conversation.assignee,
        tags=list(conversation.tags),
        notes=[note.model_dump() for note in conversation.notes],
        contact_email=contact_email,
        display_name=_display_name(contact_email, conversation.customer_ref),
        last_visitor_at=conversation.last_visitor_at,
        last_owner_at=conversation.last_owner_at,
        bot_paused_at=getattr(conversation, "bot_paused_at", "") or "",
        unread_for_owner=conversation.unread_for_owner,
        created_at=conversation.created_at.isoformat(),
        updated_at=conversation.updated_at.isoformat(),
    )


# How a stored out-of-band role reads in a TRANSCRIPT. Owner and system keep their
# own names — the thread's whole point is that you can see when a human took over.
# A visitor's muted line is presented as "user": it is the same person saying the
# same kind of thing as every other visitor line, and only happens to live in a
# different table because no run was dispatched for it.
_TRANSCRIPT_ROLE_BY_STORED = {
    OwnerMessageRole.OWNER.value: "owner",
    OwnerMessageRole.SYSTEM.value: "system",
    OwnerMessageRole.VISITOR.value: "user",
}


def _owner_transcript_message(message: Any) -> TranscriptMessage:
    """Project one ``paw_bar_owner_messages`` row into the transcript's shape.

    Shared by the reply echo and the transcript merge so the composer's optimistic
    append and the refetched thread can't render the same sentence two ways.
    """
    stored = getattr(message.role, "value", str(message.role))
    return TranscriptMessage(
        role=_TRANSCRIPT_ROLE_BY_STORED.get(stored, "system"),
        content=message.content,
        created_at=message.created_at,
    )


def _display_name(contact_email: str, customer_ref: str) -> str:
    """What to call a visitor in the owner's list.

    Their email when they left one during a decision capture — that is the whole
    point of collecting it — otherwise a short stub of the anonymous handle
    (``visitor-ab12cd``). Never the raw 32-char ref: an inbox of hex strings is
    unreadable, and the stub is enough to tell two live conversations apart.
    """
    if contact_email:
        return contact_email
    return f"visitor-{customer_ref[:_DISPLAY_REF_CHARS]}" if customer_ref else "visitor"


def _conversation_of_run(run: Any, pocket_id: str, widget: PawBarWidget | None) -> str:
    """The conversation token encoded in a run's ``session_key``.

    The key is ``cloud:concierge:{pocket}:{conversation}:{agent}``. Before
    conversation identity the middle slot held the VISITOR's handle, so this
    returns whatever is there and the caller decides what it is by looking for a
    matching conversation row — a token that resolves to no row is a legacy run,
    not an error. Parsed by stripping the known prefix and the last segment
    rather than splitting on ``:``, because a pocket id is not guaranteed to be
    colon-free and a naive split would mis-slice it.

    Returns ``""`` for a key this function does not recognize, which groups that
    run under its visitor exactly as the pre-identity code did.
    """
    key = getattr(run, "session_key", "") or ""
    prefix = f"cloud:concierge:{pocket_id}:"
    if not pocket_id or not key.startswith(prefix):
        return ""
    rest = key[len(prefix) :]
    agent_id = getattr(widget, "agent_id", "") if widget is not None else ""
    if agent_id and rest.endswith(f":{agent_id}"):
        return rest[: -len(agent_id) - 1]
    head, _, _tail = rest.rpartition(":")
    return head


async def _conversation_side_data(
    widget: PawBarWidget | None, workspace_id: str, refs: list[str]
) -> tuple[dict[str, Any], set[str], dict[str, str]]:
    """Load the per-visitor extras a conversation list row needs.

    Returns ``(state rows by CONVERSATION ID, refs with a pending action, contact
    email by ref)``. Two bounded store reads for the WHOLE page, never one per
    row:

      * the ``paw_bar_conversations`` rows for exactly the refs on this page, and
      * this widget's decision rows, which answer both "is something waiting on
        the owner" and "did this visitor ever leave an email".

    The state map is keyed by conversation id (2026-08-19). It was keyed by
    ``customer_ref``, which silently kept ONE row per visitor — the same collapse
    the list itself was making, one layer down, so fixing only the list would
    have joined every one of a visitor's conversations to the same state.

    The email is READ from the decision row rather than copied onto the
    conversation row — it stays in the one place the PII invariant names, and this
    owner-authed list is exactly who it was left for. Best-effort throughout: if
    the store hiccups the list still renders, just without the queue metadata.
    """
    states: dict[str, Any] = {}
    if widget is None or not refs:
        return states, set(), {}
    try:
        rows = await _store().list_conversations(
            widget.id,
            workspace_id=workspace_id,
            # A visitor may hold several conversations, so the page's row budget
            # is no longer one-per-ref. Bounded by the same scan cap the run
            # window uses rather than by len(refs), which would truncate a busy
            # visitor's threads out of their own state join.
            limit=_CONVERSATION_SCAN_CAP,
            customer_refs=refs,
        )
        states = {row.id: row for row in rows}
    except Exception:  # noqa: BLE001 — queue metadata is additive, never fatal
        logger.warning("conversation state read failed for widget %s", widget.id, exc_info=True)
    pending, emails = await _decision_side_data(widget, refs)
    return states, pending, emails


async def _decision_side_data(
    widget: PawBarWidget | None, refs: list[str]
) -> tuple[set[str], dict[str, str]]:
    """What this widget's DECISION rows say about a set of visitors.

    Returns ``(refs with an undecided action, contact email by ref)`` from ONE
    bounded read of the rows the Decisions list already serves — never a second
    query into Instinct, whose rows are stamped with the widget owner rather than
    the tenant and so can't be read workspace-scoped.

    The email is READ here rather than copied onto the conversation row: it stays
    in the one place the PII invariant names, and this owner-authed surface is
    exactly who the visitor left it for. Shared by the list and the PATCH echo so
    a row can't change its display name depending on which one produced it.
    Best-effort — a store hiccup costs the metadata, never the response.
    """
    if widget is None or not refs:
        return set(), {}
    try:
        decisions = await _store().list_decisions_for_widget(
            widget.id, limit=_CONVERSATION_SCAN_CAP
        )
    except Exception:  # noqa: BLE001 — additive metadata, never fatal
        logger.warning("decision read failed for widget %s", widget.id, exc_info=True)
        return set(), {}
    pending: set[str] = set()
    emails: dict[str, str] = {}
    wanted = set(refs)
    for decision in decisions:
        ref = decision.customer_ref
        if ref not in wanted:
            continue
        if decision.state == DecisionState.PENDING:
            pending.add(ref)
        # list_decisions_for_widget is newest-first, so the FIRST email we see for
        # a visitor is their most recent one.
        if decision.contact_email and ref not in emails:
            emails[ref] = decision.contact_email
    return pending, emails


async def _list_conversations(
    pocket_id: str,
    workspace_id: str,
    *,
    limit: int,
    cursor: str | None,
    widget: PawBarWidget | None = None,
    state: str | None = None,
) -> ConversationsResponse:
    """Group concierge ``ChatRunDoc`` runs into per-customer conversations.

    Fetches a BOUNDED window of the site's concierge runs (index-backed, newest
    first, optionally older than ``cursor``), then dedupes by customer_ref keeping
    the most-recent run per customer. Returns up to ``limit`` conversations plus a
    cursor (the oldest scanned run's timestamp) when the window filled — the
    signal that older conversations remain. Best-effort: a store error degrades to
    an empty, well-shaped payload rather than failing the dashboard.

    The RUNS stay the source of the list (a conversation exists because someone
    talked to the concierge, not because a row was written), and the state row is
    joined onto each one where it exists. A conversation with no row keeps every
    default — which is why nothing had to be backfilled when the table shipped.

    ``state`` filters on that joined state, AFTER the dedupe, so an unjoined
    legacy conversation is found under ``open`` where the owner expects it.
    Filtering is a page-level operation: a filtered page can come back shorter
    than ``limit`` while older matches remain behind the cursor.
    """
    from datetime import datetime

    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    try:
        conditions: list[Any] = [
            ChatRunDoc.workspace == workspace_id,
            ChatRunDoc.context_type == _CONCIERGE_CONTEXT_TYPE,
            ChatRunDoc.scope_id == pocket_id,
        ]
        if cursor:
            try:
                conditions.append(ChatRunDoc.createdAt < datetime.fromisoformat(cursor))
            except ValueError:
                pass  # A malformed cursor is ignored — start from the newest run.
        runs = (
            await ChatRunDoc.find(*conditions)
            .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
            .limit(_CONVERSATION_SCAN_CAP)
            .to_list()
        )
    except Exception:  # noqa: BLE001 — a read failure must not 500 the dashboard
        logger.warning("conversations read failed for pocket %s", pocket_id, exc_info=True)
        return ConversationsResponse(items=[], cursor=None, unsupported=False)

    # Dedupe the scanned window first (most-recent run per CONVERSATION wins),
    # THEN join and filter, THEN cut to `limit` — cutting first would hide a
    # matching conversation behind a page of non-matching ones.
    #
    # 2026-08-19: the dedupe key is the conversation, not the visitor. It was
    # ``run.user_id``, which was right exactly while a visitor could hold one
    # conversation; once they could hold several it silently discarded all but
    # their most recent, and the owner's inbox showed one row for a person who
    # had asked four separate questions. The other three were not collapsed
    # behind a disclosure — they were absent, with no way to reach them.
    parsed: list[tuple[str, str, Any]] = []  # (customer_ref, conversation token, run)
    refs: list[str] = []
    for run in runs:
        if run.user_id not in refs:
            refs.append(run.user_id)
        parsed.append((run.user_id, _conversation_of_run(run, pocket_id, widget), run))

    states, pending, emails = await _conversation_side_data(widget, workspace_id, refs)

    # A token is a real conversation id only if a row confirms it. Anything else
    # is a run from before conversations had identity, whose session_key carries
    # the visitor's handle in that slot — those keep the old behaviour and group
    # per visitor, resolving to that visitor's conversation in progress.
    active_by_ref = {
        row.customer_ref: row for row in states.values() if getattr(row, "active", True)
    }

    candidates: list[tuple[str, str, str, str]] = []  # ref, conversation_id, when, preview
    seen: set[str] = set()
    for ref, token, run in parsed:
        if states.get(token) is not None:
            conversation_id = token
        else:
            legacy = active_by_ref.get(ref)
            conversation_id = legacy.id if legacy is not None else ""
        # Group by the CONVERSATION we resolved to, falling back to the visitor
        # only when there is no conversation to name. Grouping by the raw token
        # instead splits one thread in two on the shape this deploy creates: a
        # visitor mid-conversation at the migration has runs of both spellings —
        # older ones carrying their handle in the session_key's conversation
        # slot, newer ones carrying the real id — and both resolve HERE to the
        # same row. Two rows, one id, and a client keying its list on the id has
        # duplicate keys.
        group = conversation_id or ref
        if group in seen:
            continue
        seen.add(group)
        when = run.ended_at or run.createdAt
        # Prefer the agent's reply as the row preview (unchanged). Fall back to the
        # visitor's own question when there is no reply — a run that failed or was
        # cut off used to render a blank row, which told the owner nothing; the
        # question at least says what the visitor wanted.
        preview = (run.partial_text or "") or (getattr(run, "user_text", "") or "")
        candidates.append(
            (
                ref,
                conversation_id,
                when.isoformat() if when else "",
                preview[:_CONVERSATION_PREVIEW_CHARS],
            )
        )

    items: list[ConversationItem] = []
    for ref, conversation_id, last_message_at, preview in candidates:
        row = states.get(conversation_id)
        row_state = row.state.value if row is not None else "open"
        if state and row_state != state:
            continue
        email = (row.contact_email if row is not None else "") or emails.get(ref, "")
        items.append(
            ConversationItem(
                customer_ref=ref,
                conversation_id=conversation_id,
                last_message_at=last_message_at,
                preview=preview,
                state=row_state,
                bot_paused=bool(row.bot_paused) if row is not None else False,
                unread_for_owner=row.unread_for_owner if row is not None else 0,
                tags=list(row.tags) if row is not None else [],
                snooze_until=row.snooze_until if row is not None else "",
                contact_email=email,
                display_name=_display_name(email, ref),
                has_pending_action=ref in pending,
            )
        )
        if len(items) >= limit:
            break

    # A cursor is offered only when the scan hit its cap (older runs may remain);
    # it is the oldest run we looked at, so the next page continues strictly older.
    next_cursor = (
        runs[-1].createdAt.isoformat() if len(runs) >= _CONVERSATION_SCAN_CAP and runs else None
    )
    counts = await _conversation_counts(widget, workspace_id)
    return ConversationsResponse(items=items, cursor=next_cursor, unsupported=False, counts=counts)


async def _conversation_counts(widget: PawBarWidget | None, workspace_id: str) -> dict[str, int]:
    """Per-state totals for a widget's inbox — ``{}`` when there is no widget.

    Deliberately NOT derived from the listed page: the chips report the whole
    queue, so they stay stable while a filter is active. Best-effort → ``{}``.
    """
    if widget is None:
        return {}
    try:
        return await _store().conversation_counts(widget.id, workspace_id=workspace_id)
    except Exception:  # noqa: BLE001 — a badge must not 500 the list
        logger.warning("conversation counts failed for widget %s", widget.id, exc_info=True)
        return {}


async def _concierge_runs_for_visitor(
    pocket_id: str,
    customer_ref: str,
    workspace_id: str,
    *,
    limit: int,
    session_key: str = "",
) -> list[Any]:
    """One visitor's concierge runs on one site, most-recent first.

    THE per-visitor isolation seam, deliberately in ONE place: both the owner's
    transcript read and the agent's conversation-memory rehydration go through
    this query, so there is no second, drifting definition of "this visitor's
    turns". All four predicates are load-bearing:

      * ``workspace``     — tenant isolation; another tenant's runs never match.
      * ``context_type``  — concierge runs only; an authed pocket/session run on
                            the same pocket is a different conversation.
      * ``scope_id``      — the site's OWN pocket; a sibling site never matches.
      * ``user_id``       — the anonymous customer handle; a sibling VISITOR of
                            the same widget never matches.

    ``session_key`` narrows further, to ONE conversation of that visitor
    (2026-08-19). It is optional because the two callers want different things:
    the owner's transcript read wants the visitor's whole history with the site,
    while the agent's memory rehydration must see only the conversation actually
    in progress — replaying an abandoned thread into a fresh one is the bug this
    parameter exists to fix. The key already encodes the conversation id, so this
    needs no new field on the run doc.

    Callers pass ``workspace_id`` / ``pocket_id`` from the authenticated
    authority (the resolved site key or the session's workspace), never from the
    request body. Index-backed and always bounded by ``limit``.
    """
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    predicates = [
        ChatRunDoc.workspace == workspace_id,
        ChatRunDoc.context_type == _CONCIERGE_CONTEXT_TYPE,
        ChatRunDoc.scope_id == pocket_id,
        ChatRunDoc.user_id == customer_ref,
    ]
    if session_key:
        predicates.append(ChatRunDoc.session_key == session_key)

    return (
        await ChatRunDoc.find(*predicates)
        .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
        .to_list()
    )


async def _load_concierge_history(
    pocket_id: str, customer_ref: str, workspace_id: str, session_key: str = ""
) -> list[dict[str, str]]:
    """Rehydrate this CONVERSATION's prior turns as ``RunSpec.history``.

    The concierge visitor is anonymous and has no ``Message`` rows, so the authed
    surfaces' ``load_history_for_scope`` has nothing to read and every turn was
    answered cold: the agent could not recall a name, an order number, or its own
    previous answer. The stored run docs ARE the transcript, so they are also the
    memory — same rows, same query (``_concierge_runs_for_visitor``), just shaped
    for the model instead of for the dashboard.

    Shape matches ``load_history_for_scope``: ``[{"role", "content"}]``, roles
    "user" / "assistant", oldest-first.

    Bounded by ``_HISTORY_TURN_CAP`` exchanges, ``_HISTORY_MESSAGE_CHARS`` per
    line, and ``_HISTORY_TOTAL_CHARS`` overall. Turns are fitted newest-first and
    the budget cuts the oldest off, so the replay is always the most recent
    CONTIGUOUS stretch of the conversation — an older turn never jumps a newer
    one just because it happens to be shorter.

    The CURRENT turn is not in here: the caller reads before ``create_run``
    writes this turn's doc, so the visitor's message rides in ``RunSpec.content``
    exactly once.

    ``session_key`` scopes the replay to ONE conversation (2026-08-19). Before it,
    the read was per-VISITOR, so a visitor who started over still had their
    abandoned thread replayed into the agent — the loudest half of the reported
    "every session is one session" bug, because it is the half the visitor could
    actually feel. Passing "" restores the old visitor-wide behaviour and is what
    a caller with no conversation in hand gets.

    Failure-soft: any read error degrades to no memory and logs. A visitor's chat
    must not 500 because the run collection hiccuped.
    """
    # An empty handle is not a visitor — every anonymous caller that omitted the
    # ref would otherwise share one bucket and read each other's conversation.
    if not customer_ref:
        return []

    try:
        runs = await _concierge_runs_for_visitor(
            pocket_id,
            customer_ref,
            workspace_id,
            limit=_HISTORY_TURN_CAP,
            session_key=session_key,
        )

        history: list[dict[str, str]] = []
        budget = _HISTORY_TOTAL_CHARS
        for run in runs:  # newest-first — the newest turns win the char budget
            turn: list[dict[str, str]] = []
            for role, raw in (
                ("user", getattr(run, "user_text", "") or ""),
                ("assistant", getattr(run, "partial_text", "") or ""),
            ):
                line = raw[:_HISTORY_MESSAGE_CHARS]
                if line:
                    turn.append({"role": role, "content": line})
            if not turn:
                # A run that stored neither side (retention off and no reply yet)
                # contributes nothing, and costs nothing.
                continue
            cost = sum(len(m["content"]) for m in turn)
            if cost > budget:
                # Out of budget. Everything left in ``runs`` is OLDER, so stop
                # rather than skip — that is what keeps the replay contiguous.
                break
            budget -= cost
            history = turn + history  # prepend: the result reads oldest-first
        return history
    except Exception:  # noqa: BLE001 — memory is best-effort, the reply is not
        logger.warning(
            "concierge history load failed for pocket %s; answering without memory",
            pocket_id,
            exc_info=True,
        )
        return []


def _transcript_sort_key(created_at: str) -> datetime:
    """One comparable instant for a transcript line, whatever wrote it.

    The two sources keep time differently and the difference is load-bearing:
    ``ChatRunDoc.createdAt`` is timezone-aware UTC, while an out-of-band line is an
    ISO string this router's store wrote. Sorting the raw strings would interleave
    the thread wrongly by the host's UTC offset on any machine not set to UTC — a
    human's reply landing hours before the question it answers.

    So every stamp is parsed, and a naive one is read as UTC (both writers mean
    UTC; the store writes aware UTC deliberately). An unparseable or empty stamp
    sorts FIRST, which is where a line nobody dated belongs: visible at the top of
    the thread rather than silently dropped.
    """
    if created_at:
        try:
            moment = datetime.fromisoformat(created_at)
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)


async def _load_transcript(
    pocket_id: str,
    customer_ref: str,
    workspace_id: str,
    widget: PawBarWidget | None = None,
    conversation_id: str = "",
) -> list[TranscriptMessage] | None:
    """Build one visitor's full conversation, oldest-first (D2 drill-in + slice 2).

    TWO sources, merged into ONE timeline:

      * the concierge ``ChatRunDoc`` runs (index-backed, most-recent
        ``_TRANSCRIPT_CAP``). Each run contributes up to two messages in
        conversation order: the visitor's line (``user_text``) as "user", then the
        agent reply (``partial_text``) as "assistant". Either can be missing and
        the other still renders — a site with ``concierge_store_transcripts`` off
        has no user lines (assistant-only, exactly the shape this endpoint has
        always returned), and a run that failed before producing text has no
        assistant line.
      * the ``paw_bar_owner_messages`` rows — the lines with no run behind them:
        the owner's own replies, the system's hand-back notices, and any visitor
        message that arrived while the bot was muted.

    Merged strictly by timestamp (see ``_transcript_sort_key``), because the whole
    value of this view is seeing WHEN the human stepped in relative to what the
    bot had been saying. The merge is best-effort on the out-of-band side: if that
    store hiccups the run-derived transcript still renders, which is what it did
    before this slice.

    Returns ``None`` only when the ref has NOTHING here (the caller 404s) —
    distinct from an empty list, which means rows exist but none carried text.

    ``conversation_id`` narrows BOTH sources to one thread (2026-08-19). Narrowing
    only one would be worse than narrowing neither: the owner would read one
    conversation's questions interleaved with every reply a human ever sent that
    visitor, and the timestamps would make it look like a coherent exchange.
    """
    session_key = (
        f"cloud:concierge:{pocket_id}:{conversation_id}:{getattr(widget, 'agent_id', '')}"
        if conversation_id and widget is not None
        else ""
    )
    runs = await _concierge_runs_for_visitor(
        pocket_id,
        customer_ref,
        workspace_id,
        limit=_TRANSCRIPT_CAP,
        session_key=session_key,
    )

    messages: list[TranscriptMessage] = []
    for run in reversed(runs):  # oldest-first
        # The visitor's line is stamped at the moment the run was created; the
        # reply is stamped when it finished. Using each one's own timestamp keeps
        # the pair honest about how long the answer took.
        asked = run.createdAt
        answered = run.ended_at or run.createdAt
        user_text = getattr(run, "user_text", "") or ""
        if user_text:
            messages.append(
                TranscriptMessage(
                    role="user",
                    content=user_text,
                    created_at=asked.isoformat() if asked else "",
                )
            )
        reply = run.partial_text or ""
        if reply:
            messages.append(
                TranscriptMessage(
                    role="assistant",
                    content=reply,
                    created_at=answered.isoformat() if answered else "",
                )
            )

    out_of_band: list[Any] = []
    if widget is not None:
        try:
            out_of_band = await _store().list_owner_messages(
                widget.id,
                customer_ref,
                workspace_id=workspace_id,
                limit=_TRANSCRIPT_CAP,
                conversation_id=conversation_id or None,
            )
        except Exception:  # noqa: BLE001 — the run-derived half must still render
            logger.warning("owner message read failed for widget %s", widget.id, exc_info=True)
    if not runs and not out_of_band:
        return None
    messages.extend(_owner_transcript_message(m) for m in out_of_band if m.content)

    # Stable sort on the parsed instant: two lines stamped identically keep the
    # order they were added, so a run's question still precedes its own answer.
    messages.sort(key=lambda m: _transcript_sort_key(m.created_at))
    # Keep the most recent window. The bound is what the run-derived read could
    # already return on its own (two messages per capped run), so adding a second
    # source widened the transcript's content, never its size.
    return messages[-(_TRANSCRIPT_CAP * 2) :]


async def _count_conversations(pocket_id: str, workspace_id: str) -> int:
    """Count DISTINCT concierge customers for a site (D2 overview).

    Scans a BOUNDED window of the site's concierge runs (index-backed, capped at
    ``_CONVERSATION_SCAN_CAP`` — never a full-collection read) and counts distinct
    customer_refs. Bounded rather than exact so a very busy site can't turn the
    overview into an unbounded scan; the badge is a "recent activity" signal, not
    an audited total. Best-effort: any read error degrades to 0.
    """
    try:
        from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

        runs = (
            await ChatRunDoc.find(
                ChatRunDoc.workspace == workspace_id,
                ChatRunDoc.context_type == _CONCIERGE_CONTEXT_TYPE,
                ChatRunDoc.scope_id == pocket_id,
            )
            .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
            .limit(_CONVERSATION_SCAN_CAP)
            .to_list()
        )
        return len({run.user_id for run in runs})
    except Exception:  # noqa: BLE001 — an overview count is best-effort
        logger.debug("conversations count failed for pocket %s", pocket_id, exc_info=True)
        return 0


async def _query_handoff_objects(widget_id: str, workspace_id: str, *, limit: int) -> list[Any]:
    """Query ``_paw_handoffs`` Fabric objects for one widget (D2 handoffs).

    Scoped to the widget (a ``widget_id`` object property) AND the workspace —
    the same two-key scoping ``handoff._write_handoff_object`` writes through.
    Best-effort: a Fabric error degrades to an empty list.
    """
    try:
        from pocketpaw.fabric.models import FabricQuery
        from pocketpaw_ee.api import get_fabric_store

        fabric = get_fabric_store(workspace_id=workspace_id)
        result = await fabric.query(
            FabricQuery(
                type_name=_PAW_HANDOFFS_TYPE,
                filters={"widget_id": widget_id},
                limit=limit,
            ),
            workspace_id=workspace_id,
        )
        return list(result.objects)
    except Exception:  # noqa: BLE001 — a read failure must not 500 the dashboard
        logger.warning("handoffs read failed for widget %s", widget_id, exc_info=True)
        return []


async def _count_handoffs(widget_id: str, workspace_id: str) -> int:
    """Cheap COUNT of a widget's ``_paw_handoffs`` objects (D2 overview).

    Reuses the scoped Fabric query but reads only its ``total`` (a COUNT(*)) — the
    row payload isn't materialized. Best-effort → 0.
    """
    try:
        from pocketpaw.fabric.models import FabricQuery
        from pocketpaw_ee.api import get_fabric_store

        fabric = get_fabric_store(workspace_id=workspace_id)
        result = await fabric.query(
            FabricQuery(type_name=_PAW_HANDOFFS_TYPE, filters={"widget_id": widget_id}, limit=1),
            workspace_id=workspace_id,
        )
        return result.total
    except Exception:  # noqa: BLE001 — an overview count is best-effort
        logger.debug("handoffs count failed for widget %s", widget_id, exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Public spec serving (CORS-enforced)
# ---------------------------------------------------------------------------


@router.get("/paw-bar/spec/{widget_id}")
async def get_spec(
    widget_id: str,
    request: Request,
) -> JSONResponse:
    """Public spec endpoint consumed by the widget.js bundle.

    CORS is enforced per-widget: the response carries
    `Access-Control-Allow-Origin` set to the inbound Origin only when it
    matches the widget's allowlist. Any other origin gets a 403 — browsers
    would block the fetch anyway, but failing explicitly makes misconfigs
    loud instead of silent.
    """
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    origin = request.headers.get("origin")
    if not _origin_allowed(widget, origin):
        raise HTTPException(403, "Origin not allowed for this widget")

    headers: dict[str, str] = {}
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return JSONResponse(widget.spec.model_dump(), headers=headers)


# ---------------------------------------------------------------------------
# Event ingest
# ---------------------------------------------------------------------------


class IngestPayload(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    customer_ref: str


@router.post("/paw-bar/events/{widget_id}", response_model=EventIngestResponse)
async def ingest_event(
    widget_id: str,
    body: IngestPayload,
    request: Request,
) -> EventIngestResponse:
    """Inbound customer event.

    Enforces (in order):
    1. Widget exists.
    2. Origin is on the widget's allowlist.
    3. Payload size is under MAX_PAYLOAD_BYTES.
    4. Rate limits (overall + per customer_ref).
    5. Injection screening: the stringified payload is run through the
       heuristic InjectionScanner and dropped on a HIGH-or-higher threat
       (degrades cleanly to accept when the security stack is absent).
    After that, the event is persisted and — if the widget has a matching
    `event_mapping` — a Fabric object is created.

    gap2 — when the event maps to a Fabric object, ingest ALSO raises an
    Instinct proposal carrying the event context (best-effort) so a human can
    decide and the decision is delivered back via the poll endpoint. This is the
    open-the-loop half; the human decides on the existing Instinct surface and
    deliver_customer_decision closes it.
    """
    store = _store()
    widget = await store.get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    origin = request.headers.get("origin")
    if not _origin_allowed(widget, origin):
        raise HTTPException(403, "Origin not allowed for this widget")

    event = PawBarEvent(
        widget_id=widget_id,
        type=body.type,
        payload=body.payload,
        customer_ref=body.customer_ref,
    )

    if event.payload_size() > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "Payload exceeds 4KB cap")

    ok = await store.within_rate_limit(
        widget_id,
        overall_per_min=widget.rate_limit_per_min,
        per_customer_per_min=widget.per_customer_limit_per_min,
        customer_ref=event.customer_ref,
    )
    if not ok:
        raise HTTPException(429, "Rate limit exceeded")

    if not await _screen_event_for_injection(event):
        return EventIngestResponse(accepted=False, reason="injection_rejected")

    await store.record_event(event)
    fabric_object_id = await _apply_event_mapping(widget, event)

    # gap2 — open the customer decision loop. Only events the widget actually
    # maps (a real, recognized customer request, not arbitrary telemetry) raise
    # a proposal, so we don't flood The Tray with noise. Best-effort: a loop
    # failure never fails this ingest response — the event + Fabric object have
    # already persisted.
    instinct_action_id: str | None = None
    if widget.event_mapping.get(event.type) is not None:
        instinct_action_id = await _open_decision_loop(widget, event, store)

    return EventIngestResponse(
        accepted=True,
        event=event,
        fabric_object_id=fabric_object_id,
        instinct_action_id=instinct_action_id,
    )


# ---------------------------------------------------------------------------
# Customer decision poll (public, CORS-enforced) — the back-half of the loop
# ---------------------------------------------------------------------------


@router.get("/paw-bar/events/{widget_id}/decision/{customer_ref}")
async def get_decision(
    widget_id: str,
    customer_ref: str,
    request: Request,
) -> JSONResponse:
    """Public endpoint the rendered widget polls to read the owner's decision.

    The widget posted an event (which may have raised an Instinct proposal);
    this returns the latest decision for ``(widget_id, customer_ref)``:
    ``pending`` while a human hasn't decided, then ``delivered`` (with the reply)
    on approval or ``declined`` on rejection.

    Auth model matches the public spec/ingest endpoints: no owner credential —
    the row is scoped to the customer's own ``customer_ref`` on a specific
    widget, which is all the embedded widget knows. CORS is enforced per-widget
    exactly as on the spec endpoint so only allowlisted origins can read it.
    """
    store = _store()
    widget = await store.get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    # Same-origin GETs carry no Origin header — resolve the frame case first
    # (see _request_origin), then apply the widget allowlist for real embedders.
    # A request FROM our frame equals the frame origin and is allowed: the
    # embedder was already gated by the frame CSP at render time (the same
    # dual-mode reasoning as resolve_site_key).
    origin = _request_origin(request)
    if origin != _configured_frame_origin(request) and not _origin_allowed(widget, origin):
        raise HTTPException(403, "Origin not allowed for this widget")

    decision = await store.get_latest_decision(widget_id, customer_ref)
    headers: dict[str, str] = {}
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"

    if decision is None:
        body = DecisionStatusResponse(found=False)
    else:
        body = DecisionStatusResponse(
            found=True,
            state=decision.state.value,
            reply=decision.reply,
            decided_by=decision.decided_by,
            updated_at=decision.updated_at.isoformat(),
        )
    return JSONResponse(body.model_dump(), headers=headers)


# ---------------------------------------------------------------------------
# Public concierge chat (SSE) — T2
#
# A PUBLIC, anonymous, streaming chat endpoint. The visitor's embed key
# (Site.signed_key) is the only credential — there is NO signed-in user. Every
# authority comes from the RESOLVED Site scope (resolve_site_key), and the run is
# bound to the Site's pocket + the widget's agent. It drives the SAME run
# machinery the authenticated dashboard chat uses (create_run + executor.submit
# + execute_run + transport) via a CONCIERGE-scoped RunSpec — no new SSE loop, no
# new executor, no new transport. The grounding guard (deny web/code/write tools,
# lock KB to pocket:<id>) is enforced by the CONCIERGE SurfaceProfile + scope, not
# here; this handler owns the front-gate + auth + dispatch.
# ---------------------------------------------------------------------------


# --- Reply sources (visible grounding) -------------------------------------
#
# APPROXIMATE ATTRIBUTION, ON PURPOSE (Crisp-style "Sources"): the concierge run
# grounds itself with a KB search inside the agent loop, and that trace is not
# surfaced by the transport. Rather than plumb a tool trace through the run
# machinery, we re-run a cheap server-side KB search of the visitor's OWN message
# against the SAME ``pocket:<pocket_id>`` scope the run reads, and show the synced
# site pages that match. The pages shown are therefore "what the KB would ground
# this question on", not "what the model verifiably quoted" — good enough to give
# the visitor a place to read more, and honest about being a heuristic.
#
# Fail-soft and near-free: the search task starts before the stream is relayed and
# runs CONCURRENTLY with the model turn (seconds), so by ``stream_end`` it is
# almost always already done; a short grace wait bounds the added latency, and any
# error or timeout emits nothing at all (never an empty ``sources`` event).

# At most this many entries ride the ``sources`` event (CONTRACT: max 3).
_SOURCES_MAX = 3
# Search a few more than we show — hits are filtered to synced site pages and
# deduped by url, so over-fetching keeps the event full when some hits drop.
_SOURCES_SEARCH_LIMIT = 8
# Grace wait at stream end for a search that somehow outlived the model turn.
# The budget for ADDED latency is ~100ms; past it we drop sources, not delay the
# terminal frame.
_SOURCES_WAIT_S = 0.1


def _humanize_article_title(title: str) -> str:
    """Visitor-facing title for a synced page article — never the raw KB id.

    Slug-sourced articles carry their ``site-<slug>`` id as the title
    ("site-services"), which read as internals in the Sources chips and the
    articles list during the 2026-07-30 rig smoke. Strip the prefix, break the
    slug on dashes, and title-case ("site-home" → "Home"). A title that isn't
    id-shaped (an LLM-compiled article's real title) passes through untouched.
    """
    t = title.strip()
    if t.startswith("site-"):
        slug = t[len("site-") :].strip("-")
        words = [w for w in slug.split("-") if w]
        return " ".join(w.capitalize() for w in words) if words else "Home"
    return t


def _article_page_url(article_id: str, base_url: str) -> str:
    """Best-effort public page URL for a synced KB article — "" when unmappable.

    The page sync ingests each page under a deterministic ``site-<slug>`` source
    (``kb_ingest._path_slug``), and kb-go's compile fallback keeps that as the
    article id. Reversing the slug is LOSSY (slashes and dashes both became "-"),
    and an LLM-compiled article is titled — not sourced — so its id carries no
    slug at all; both cases degrade to the site's base URL. That is the accepted
    cost of approximate attribution: the link always lands on the right SITE,
    usually on the right page.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if article_id.startswith("site-"):
        slug = article_id[len("site-") :]
        if slug and slug != "home":
            return f"{base}/{slug}"
    return f"{base}/"


async def _concierge_sources(pocket_id: str, message: str, site: Any) -> list[dict[str, str]]:
    """Sources for one concierge reply: synced site pages that match the message.

    Searches the site's ``pocket:<pocket_id>`` scope — one of the two
    ``_kb_scopes_for_context`` grants a CONCIERGE run (the other is
    ``agent:<its own id>``; never ``workspace:`` or ``user:``). Only the pocket
    scope is searched here, and deliberately: a "source" has to be a link the
    visitor can open, and only the page sync's articles have a public URL. Then
    keeps only
    hits whose article id is in ``Site.kb_article_ids`` — the articles the page
    sync wrote — so an owner-uploaded private file can never surface as a public
    "source". Entries need BOTH a non-empty title and url; deduped by url; capped
    at ``_SOURCES_MAX``. Fail-soft: any error returns [] and the reply is
    unaffected.
    """
    try:
        base_url = str(getattr(site, "url", "") or "")
        synced = set(getattr(site, "kb_article_ids", None) or [])
        if not pocket_id or not message.strip() or not base_url.strip() or not synced:
            return []
        from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

        hits = await KnowledgeService.search_articles_for_scope(
            f"pocket:{pocket_id}", message, limit=_SOURCES_SEARCH_LIMIT
        )
        sources: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            article_id = str(hit.get("id") or "")
            title = str(hit.get("title") or "").strip()
            if article_id not in synced or not title:
                continue
            url = _article_page_url(article_id, base_url)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append({"title": _humanize_article_title(title), "url": url})
            if len(sources) >= _SOURCES_MAX:
                break
        return sources
    except Exception:  # noqa: BLE001 — sources are best-effort, the reply is not
        logger.debug("concierge sources lookup failed (non-fatal)", exc_info=True)
        return []


class ConciergeChatRequest(BaseModel):
    widget_id: str
    # The public, origin-bound embed key (Site.signed_key) baked into the widget.
    signed_key: str
    # The anonymous, widget-minted customer handle — a session / rate-limit key,
    # NEVER an authenticated principal.
    customer_ref: str
    message: str
    # Which of this visitor's conversations the turn belongs to (2026-08-19).
    #
    # OPTIONAL on purpose, and it must stay optional: widget bundles cached on
    # visitors' devices predate the field, and a required one would 422 every one
    # of them the moment this deploys. Absent means "the conversation in
    # progress", which the store resolves-or-creates — exactly the behaviour
    # before conversations had identities.
    #
    # An id belonging to another visitor or another tenant does not resolve, and
    # the turn falls back to the caller's own active conversation rather than
    # erroring: the value is client-supplied, so it is a hint, never an authority.
    conversation_id: str = ""


def _sse(event: str, data: dict[str, Any], *, entry_id: str | None = None) -> bytes:
    """Encode one SSE frame — the SAME wire shape ``agent_router._sse`` writes so
    the frontend's EventSource parser (and Last-Event-Id resume) is unchanged.
    Mirrored here rather than imported so the public router doesn't reach into a
    private helper of the authed chat module."""
    head = f"id: {entry_id}\n" if entry_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def _human_replying_response(
    store: Any,
    *,
    widget_id: str,
    customer_ref: str,
    workspace_id: str,
    text: str,
) -> StreamingResponse:
    """The muted-bot answer: keep the visitor's line, escalate, say a human is on it.

    Emits exactly TWO frames — ``human_replying`` carrying a short human-facing
    line, then the terminal ``stream_end`` — over the same SSE media type as a real
    turn, so the widget's existing stream reader needs no special casing beyond
    knowing that this event is a non-answer rather than an answer. There is
    deliberately no ``message.persisted`` head: that frame announces a run, and the
    entire point of this path is that no run exists to announce.

    Two writes happen BEFORE the response is returned — not inside the generator —
    because a visitor who closes the tab mid-request would otherwise take their own
    message with them: the body might never be consumed, and the owner would be
    left answering a question nobody can see. Both are failure-soft (a visitor's
    experience must not depend on the owner's bookkeeping succeeding):

      * the visitor's line is stored as a ``visitor`` row — it has no run doc to
        live on, and without it the owner's transcript would stop dead at the
        moment they took over, which is precisely when they need to read it. Empty
        when the site's transcript retention is off; the toggle governs this path
        exactly as it governs the run path.
      * the conversation flips to ``needs_human``. A visitor talking into a muted
        bot is, by definition, waiting on a person.
    """
    try:
        if text:
            await store.add_owner_message(
                widget_id,
                customer_ref,
                text,
                role=OwnerMessageRole.VISITOR,
                workspace_id=workspace_id,
            )
        await store.update_conversation(
            widget_id,
            customer_ref,
            workspace_id=workspace_id,
            state=ConversationState.NEEDS_HUMAN.value,
        )
    except Exception:  # noqa: BLE001 — the visitor still gets told a human is on it
        logger.warning("paused-bot turn bookkeeping failed (non-fatal)", exc_info=True)

    # Owner notification #3 of 3 (slice 3): a visitor wrote while the bot was
    # muted — i.e. straight at the person who took the conversation over, who is
    # by definition not watching the bot's queue. Never-raising by construction.
    from pocketpaw_ee.paw_bar.notify import NOTIFY_VISITOR_REPLY, notify_workspace_owner

    await notify_workspace_owner(
        workspace_id=workspace_id,
        kind=NOTIFY_VISITOR_REPLY,
        title="A visitor replied to you",
        body=text,
        widget_id=widget_id,
        customer_ref=customer_ref,
    )

    async def gen() -> AsyncIterator[bytes]:
        yield _sse("human_replying", {"message": _HUMAN_REPLYING_MESSAGE})
        yield _sse("stream_end", {"assistant_message_id": None, "cancelled": False})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/paw-bar/chat")
async def concierge_chat(body: ConciergeChatRequest, request: Request) -> StreamingResponse:
    """Stream a concierge reply for a public visitor's message.

    Order (fail-closed, cheap gates first):
      1. Widget exists (404).
      2. Resolve our frame origin (dual-mode origin model — no rejection here; the
         authoritative, fail-closed origin gate is folded into step 5).
      3. Rate limit, overall + per-customer (429).
      4. Injection screen the free-text message; drop on HIGH (400).
      5. Authenticate the embed key + dual-mode origin gate (``resolve_site_key`` —
         401 bad key / 403 disallowed origin, fail-closed).
      6. Bind the widget to the RESOLVED key: the widget must belong to the key's
         workspace AND pocket (403) — a key for pocket A must not drive a widget
         for a sibling pocket B (finding #2).
      7. The widget must have a concierge agent bound (409).
      7b. The pocket must expose NO connectors (409) — public-safe lockdown until
          the claude_sdk untrusted-mode GA fix (a static deny can't strip dynamic
          composio connector ids). Fail-closed on a lookup error too.
      8. Dispatch a CONCIERGE-scoped run over the shared machinery and stream its
         frames back as SSE.
    """
    origin = request.headers.get("origin")
    store = _store()

    # (1) Widget lookup — UNSCOPED: we don't have the workspace until the key is
    # resolved. The workspace/pocket binding is enforced at step 6.
    widget = await store.get_widget(body.widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    # (2) Origin model — DUAL-MODE (A1). The chat origin gate now converges on the
    # fail-closed ``Site.allowed_origins`` allowlist, enforced at step 5 by
    # ``resolve_site_key``. It is NOT gated on ``widget.allowed_domains`` anymore —
    # that check's empty=allow-all footgun would (a) silently allow any origin for
    # an unconfigured widget and (b) reject OUR frame origin for a configured one,
    # breaking the iframe path. ``frame_origin`` is our configured iframe origin: a
    # request whose Origin equals it was already gated by the frame CSP at render
    # time (iframe mode); any other Origin must be an allowlisted embedder (inline
    # mode). The authoritative, fail-closed decision is made in ``resolve_site_key``.
    frame_origin = _configured_frame_origin(request)

    # (3) Rate limit (reuse the ingest limiter). Counts prior events for this
    # (widget, customer); a recorded chat marker below feeds subsequent checks.
    ok = await store.within_rate_limit(
        body.widget_id,
        overall_per_min=widget.rate_limit_per_min,
        per_customer_per_min=widget.per_customer_limit_per_min,
        customer_ref=body.customer_ref,
    )
    if not ok:
        raise HTTPException(429, "Rate limit exceeded")

    # (4) Injection-screen the untrusted free-text message; drop on HIGH.
    if not await _screen_message_for_injection(body.message, body.widget_id):
        raise HTTPException(400, "message_rejected")

    # (5) Authenticate the embed key + apply the dual-mode origin gate — fail-closed
    # (401 bad/unknown/revoked key, 403 disallowed/missing origin). This is THE
    # credential; there is no user. ``frame_origin`` makes the origin gate accept an
    # iframe-mode request (Origin == our frame, already gated by the frame CSP) while
    # still requiring an inline-mode request's Origin to be on ``Site.allowed_origins``.
    # ``_with_site`` hands back the Site the gate already loaded, so step (8) can
    # read the owner's transcript-retention toggle without a second query.
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key_with_site

    ctx, site = await resolve_site_key_with_site(
        body.signed_key, origin, body.customer_ref, frame_origin=frame_origin
    )

    # (6) Bind the widget to the RESOLVED key (finding #2). A legacy '' widget
    # workspace matches any; a non-empty mismatch is refused. The pocket MUST
    # match the key's pocket — the run is bound to ``ctx.pocket_id`` (the
    # authenticated authority), so a widget for a sibling pocket is rejected.
    if widget.workspace_id and widget.workspace_id != ctx.workspace_id:
        raise HTTPException(403, "widget_workspace_mismatch")
    if widget.pocket_id != ctx.pocket_id:
        raise HTTPException(403, "widget_pocket_mismatch")

    # (7) The widget must be bound to a concierge agent (T3 sets agent_id).
    if not widget.agent_id:
        raise HTTPException(409, "widget has no concierge agent")

    # (7b) Fail-closed connector lockdown (pilot posture, captain call 2026-07-14).
    # ``_CONCIERGE_DENY`` strips web/code/write/pocket-write, but composio CONNECTOR
    # tool ids are dynamic/per-workspace and survive the always-allowed ``composio``
    # server, so a static deny can't reach them. Until the GA fix lands, a PUBLIC
    # concierge pocket must expose NO connectors: ``list_pocket_connectors`` reports
    # exactly the connectors this pocket's agent can use (pocket-scoped OR
    # workspace-wide), so if it returns anything, refuse rather than let a
    # prompt-injected visitor reach it. Fail CLOSED — a lookup error refuses too.
    # TODO(GA-blocker): replace this refuse-guard with an untrusted/public lockdown
    # mode in claude_sdk — a ``ScopeKind.CONCIERGE`` run skips the universal grant
    # (POCKET_CREATION_GRANT/WIDGET/ATLAS) AND the ``ALWAYS_ALLOWED_MCP_SERVERS``
    # bypass, so connectors are stripped for real and a concierge pocket CAN safely
    # have connectors. Touches shared tool-gating -> full-suite + flag-mode validation.
    from pocketpaw_ee.cloud.connectors.service import list_pocket_connectors

    try:
        _bound_connectors = await list_pocket_connectors(ctx.workspace_id, ctx.pocket_id or "")
    except Exception:
        logger.warning("concierge connector check failed; refusing fail-closed", exc_info=True)
        raise HTTPException(409, "concierge_connector_check_failed")
    if _bound_connectors:
        raise HTTPException(409, "concierge_pocket_has_connectors")

    # Record a minimal chat marker so the rate limiter counts concierge traffic
    # (the message body is NOT stored here — the assistant reply persists via the
    # run). Best-effort: a store hiccup must not fail the reply.
    try:
        await store.record_event(
            PawBarEvent(
                widget_id=body.widget_id,
                type="concierge_message",
                payload={},
                customer_ref=body.customer_ref,
            )
        )
    except Exception:
        logger.debug("concierge chat marker record failed (non-fatal)", exc_info=True)

    # Touch the conversation's state row (owner inbox, slice 1). THIS is what makes
    # the queue backfill-free: the row is minted on a visitor's first message and
    # kept current on every one after — no migration, no sweeper, no separate
    # producer to forget. It also carries the auto-reopen rule, so a visitor coming
    # back to a conversation the owner closed lands back in the inbox.
    #
    # FAILURE-SOFT, deliberately and non-negotiably: inbox bookkeeping is the
    # owner's convenience, and a hiccup writing it must never cost a visitor their
    # answer. Worst case the owner's queue is one message stale until the next turn.
    #
    # The idle auto-resume runs FIRST so a mute that has aged out is already gone
    # before this turn is classified — one cheap conditional UPDATE that no-ops for
    # every conversation nobody has taken over (the overwhelming majority).
    # TWO concerns, deliberately NOT sharing one try/except:
    #
    #   (a) reading whether a human has taken this conversation over. That is the
    #       mute signal the gate below reads, and it is SECURITY-RELEVANT — a bot
    #       talking over a human mid-reply is the single loudest complaint about
    #       every product that shipped this feature.
    #   (b) the bookkeeping write (auto-resume, unread++, last_visitor_at).
    #       Losing it costs the owner a badge, nothing more.
    #
    # They used to share one block, so ANY failure in (b) — a busy database while
    # the owner's own reply writes the same row, a malformed legacy row — threw
    # away the already-successful read from (a), left ``conversation`` None, and
    # the mute gate below fell through and dispatched a full agent run. The
    # failure-soft intent was right for the write and wrong for the read.
    conversation = None
    is_new_conversation = False
    mute_unreadable = False
    try:
        # Read BEFORE the upsert so "is this the first time we've heard from this
        # person" is answered by the absence of a row, not inferred from counters
        # the owner's own reads reset. It is the notification trigger below.
        conversation = await store.get_conversation(
            body.widget_id, body.customer_ref, workspace_id=ctx.workspace_id
        )
        is_new_conversation = conversation is None
    except Exception:
        # We cannot prove a human is NOT handling this, so fail closed and treat
        # it as muted. A visitor briefly told the team is replying is recoverable;
        # a bot arguing with its own operator in front of a customer is not.
        mute_unreadable = True
        logger.exception(
            "paw-bar: could not read conversation state for widget %s — failing "
            "CLOSED (treating the bot as muted) rather than risk answering over "
            "a human",
            body.widget_id,
        )

    try:
        await store.auto_resume_bot_if_idle(body.widget_id, body.customer_ref, ctx.workspace_id)
        refreshed = await store.upsert_conversation_on_visitor_turn(
            body.widget_id, body.customer_ref, ctx.workspace_id
        )
        # ADOPT the upsert's fresher row, but never let its failure clear what the
        # read above established.
        if refreshed is not None:
            conversation = refreshed
    except Exception:
        logger.warning("conversation state upsert failed (non-fatal)", exc_info=True)

    # Which conversation is this turn part of (2026-08-19)? The body's id is a
    # client-supplied HINT and is verified against this visitor's own rows before
    # it is honoured — a caller naming a stranger's (or another tenant's)
    # conversation gets their own active one instead of a refusal, because the
    # only thing a refusal would tell them is that the id was real.
    #
    # Everything downstream keys off ``conversation_key``: the run's session_key,
    # the history replay, and the ledger's conversation id. It is derived HERE,
    # once, so those three can never drift apart again.
    if body.conversation_id and conversation is not None:
        try:
            named = await store.get_conversation_by_id(
                body.conversation_id, workspace_id=ctx.workspace_id
            )
            if (
                named is not None
                and named.widget_id == body.widget_id
                and named.customer_ref == body.customer_ref
            ):
                conversation = named
        except Exception:
            logger.warning("conversation id lookup failed (non-fatal)", exc_info=True)
    conversation_key = conversation.id if conversation is not None else body.customer_ref

    # Owner notification #1 of 3 (slice 3): a NEW conversation started. Not every
    # turn — a bar that pinged on each message would train the owner to ignore the
    # badge, which costs them the two escalations that actually need them. Awaited
    # rather than fired-and-forgotten so a raising notifier is proven harmless by
    # the tests rather than merely unobserved; ``notify_workspace_owner`` never
    # raises, so this cannot cost the visitor their answer.
    if is_new_conversation:
        from pocketpaw_ee.paw_bar.notify import NOTIFY_NEW_CONVERSATION, notify_workspace_owner

        await notify_workspace_owner(
            workspace_id=ctx.workspace_id,
            kind=NOTIFY_NEW_CONVERSATION,
            title="New concierge conversation",
            body=body.message,
            widget_id=body.widget_id,
            customer_ref=body.customer_ref,
            # Passed rather than left to the resolver: the widget is already in
            # hand here, and this await sits inside the visitor's turn.
            agent_id=str(getattr(widget, "agent_id", "") or ""),
        )

    # AL-2 — the conversation's first beat. Fired on EVERY turn and deduped by
    # the ledger's UNIQUE(kind, ref) on ``widget:customer``, deliberately NOT
    # gated on ``is_new_conversation`` above: that flag is derived from a read
    # that is allowed to fail (the fail-closed mute arm), and a "started" count
    # that silently drops the conversations whose state read hiccuped is worse
    # than one extra absorbed insert per turn. Never raises (paw_bar/ledger.py).
    from pocketpaw_ee.paw_bar import ledger

    await ledger.emit_conversation_started(
        widget=widget,
        # ``ctx.workspace_id`` is the REAL tenant (the same token this handler
        # scopes its conversation reads and its run dispatch by) — a store-path
        # -safe value, unlike the widget owner label.
        workspace_id=ctx.workspace_id,
        customer_ref=body.customer_ref,
    )

    # (7c) THE MUTE. A human is holding this conversation, so the bot does not
    # answer over them — double-answering is the single loudest complaint about
    # every product that shipped this feature, and the reason takeover is worth
    # building at all.
    #
    # This sits BEFORE the run is created, deliberately: no ChatRunDoc, so no
    # metering, no run analytics, no tool surface, and nothing for the executor to
    # dispatch. The visitor's line is still kept (under the SAME retention toggle
    # the run path honours — this is not a back door around the owner's privacy
    # choice), the conversation is escalated to ``needs_human`` because someone is
    # now waiting on a person, and the widget is told what happened.
    #
    # It is told with a FRAME, never with silence: a clean-but-empty stream reads
    # as "No reply." in the glass app, so a muted bot would look like a broken one.
    # ``mute_unreadable`` is the fail-closed arm: the state read raised, so we
    # cannot prove a human is not mid-reply and must not gamble a run on it.
    if mute_unreadable or (conversation is not None and conversation.bot_paused):
        return await _human_replying_response(
            store,
            widget_id=body.widget_id,
            customer_ref=body.customer_ref,
            workspace_id=ctx.workspace_id,
            text=body.message[:_STORED_USER_TEXT_CHARS] if site.concierge_store_transcripts else "",
        )

    # (8) Dispatch a CONCIERGE run over the SAME machinery the authed chat uses.
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
    from pocketpaw_ee.cloud.chat.runs.executor import get_executor
    from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport

    run_id = uuid.uuid4().hex
    client_message_id = uuid.uuid4().hex
    # C1 — the widget's declared actions ride surface_meta (JSON-shaped) so the
    # CONCIERGE run allow-lists + surfaces EXACTLY this widget's per-verb tools and
    # binds them onto the per-stream ContextVar the pawbar_actions MCP server builds
    # from. Stamped server-side from the widget spec (never client-supplied). Empty
    # when the widget declares no actions → the concierge stays deny-all as before.
    pawbar_actions = [
        {"verb": a.verb, "policy": a.policy, "args": dict(a.args), "label": a.label}
        for a in (widget.spec.actions or [])
    ]
    # C1 — the catalog also rides surface_meta (capped) so the concierge preamble
    # can name real products, prices, and ids: without it the agent knows the
    # action verbs but not WHAT it sells and declines ("I don't have a list"). Only
    # threaded when actions are declared; the preamble renders a compact block.
    pawbar_catalog = (
        [
            {
                "id": c.id,
                "name": c.name,
                "price_cents": c.price_cents,
                "currency": c.currency,
            }
            for c in (widget.spec.catalog or [])[:_MAX_PREAMBLE_CATALOG]
        ]
        if pawbar_actions
        else []
    )
    # The run is bound to the KEY's pocket (ctx.pocket_id — the authenticated
    # authority), the KEY's workspace, and the widget's agent. ``user_id`` is the
    # anonymous customer handle (session / rate-limit key, never a principal).
    # ``surface="concierge"`` makes execute_run resolve the CONCIERGE
    # SurfaceProfile (tool lockdown); ``context_type="concierge"`` makes it
    # resolve the CONCIERGE scope (KB locked to pocket:<id> + agent:<its own id>,
    # never workspace: or user: — D5, #1821).
    #
    # ``persist_user_text`` is the visitor's own line, written onto the run doc so
    # the owner's transcript is a conversation rather than a monologue. The visitor
    # is anonymous and has no Message row, so ``user_message_id`` stays "" and this
    # is the only place that text can live. Gated on the site owner's
    # ``concierge_store_transcripts`` (re-read every turn, so turning it off stops
    # collection on the next message) and length-capped — the agent still gets the
    # full message either way, this governs only what is stored.
    stored_user_text = (
        body.message[:_STORED_USER_TEXT_CHARS] if site.concierge_store_transcripts else ""
    )
    # The agent session this turn belongs to. It carries ``conversation_key`` —
    # the conversation's own id — where it used to carry ``customer_ref``
    # (2026-08-19). That one substitution is the identity fix at the run layer:
    # with the visitor's handle in this slot, every conversation they ever had
    # was ONE agent session, which is precisely what "multiple sessions are
    # treated as a single session" described. Built here rather than inline in
    # the RunSpec because the history read below must scope to the same value.
    session_key = f"cloud:concierge:{ctx.pocket_id}:{conversation_key}:{widget.agent_id}"
    # ``history`` is THIS CONVERSATION's prior turns (see
    # ``_load_concierge_history``). Read BEFORE ``create_run`` below writes this
    # turn's doc, so the current message rides in ``content`` and appears exactly
    # once. Scoped to (workspace, concierge, pocket, customer_ref, session) — a
    # sibling visitor's, a sibling site's, another tenant's, and now this
    # visitor's OWN earlier conversations can never appear.
    #
    # Gated on the SAME retention toggle as the write: an owner who turned
    # transcript storage off gets no memory, because there is nothing stored to
    # remember from and because replaying the agent's half alone would feed it a
    # conversation with the questions missing. That degradation is the owner's
    # privacy choice working, not a bug to route around.
    prior_history = (
        await _load_concierge_history(
            ctx.pocket_id or "",
            body.customer_ref,
            ctx.workspace_id,
            session_key=session_key,
        )
        if site.concierge_store_transcripts
        else []
    )
    spec = RunSpec(
        run_id=run_id,
        workspace_id=ctx.workspace_id,
        context_type="concierge",
        scope_id=ctx.pocket_id or "",
        session_key=session_key,
        group=None,
        user_id=body.customer_ref,
        agent_id=widget.agent_id,
        client_message_id=client_message_id,
        user_message_id="",
        persist_user_text=stored_user_text,
        content=body.message,
        history=prior_history,
        intent=None,
        attachments=[],
        mentions=[],
        surface="concierge",
        surface_meta={
            "pocket_id": ctx.pocket_id,
            "route_path": "/paw-bar",
            "widget_id": widget.id,
            "pawbar_actions": pawbar_actions,
            "pawbar_catalog": pawbar_catalog,
        },
    )
    run = await run_service.create_run(spec)
    if run.run_id != spec.run_id:
        spec = spec.model_copy(update={"run_id": run.run_id})
    run_id = run.run_id
    await get_executor().submit(spec)

    transport = get_stream_transport()

    # Reply sources (visible grounding): kick the approximate-attribution KB
    # search off NOW so it runs concurrently with the model turn — by the time
    # ``stream_end`` arrives it is effectively always finished, so surfacing it
    # adds ~0ms. Fail-soft by construction (``_concierge_sources`` never raises).
    sources_task = asyncio.create_task(_concierge_sources(ctx.pocket_id or "", body.message, site))

    async def gen() -> AsyncIterator[bytes]:
        # Mirror agent_router.post_agent_chat's tail: announce the run, then relay
        # the transport frames the executor writes, verbatim, until a terminal one.
        # One insertion: immediately BEFORE relaying a terminal ``stream_end``
        # frame, emit at most one ``sources`` event (CONTRACT: the widget renders
        # it only between the model stream completing and stream_end; nothing is
        # emitted when no source qualifies, and never after stream_end).
        try:
            yield _sse(
                "message.persisted",
                {"run_id": run_id, "client_message_id": client_message_id},
            )
            cursor = "0"
            while True:
                saw_terminal = False
                async for ev in transport.read_events(run_id, after=cursor, block_ms=2000):
                    cursor = ev.entry_id
                    if ev.event == "stream_end":
                        # The model stream is complete. Give the concurrent
                        # search a short grace, then either surface it or drop
                        # it — the terminal frame is never delayed past the
                        # ~100ms budget and never blocked by a wedged search.
                        try:
                            sources = await asyncio.wait_for(
                                asyncio.shield(sources_task), timeout=_SOURCES_WAIT_S
                            )
                        except Exception:  # noqa: BLE001 — timeout/err ⇒ no event
                            sources = []
                        if sources:
                            yield _sse("sources", {"sources": sources})
                    yield _sse(ev.event, ev.data, entry_id=ev.entry_id)
                    if ev.is_terminal:
                        saw_terminal = True
                if saw_terminal:
                    return
                if await transport.is_cancelled(run_id):
                    yield _sse("interrupted", {"reason": "cancelled"})
                    return
                yield b": ping\n\n"
        finally:
            # Client gone or stream over — never leave the search task dangling.
            sources_task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Public action / cart endpoints (C1) — the visitor commerce loop
#
# Same armor class as concierge chat, factored into ``_front_gate_for_key`` so the
# two endpoints share ONE fail-closed gate (no parallel path). The args are
# structured (verb + typed args, not free text), so there is no injection screen —
# the executor validates every arg against the widget's declared schema. Neither
# endpoint runs the agent, so the agent-bound + connector-lockdown steps that
# concierge chat carries don't apply here.
# ---------------------------------------------------------------------------

# The visitor handle is client-minted (the glass app uses a 128-bit crypto-random
# hex ref). Bound its charset + length server-side so a malformed / oversized ref
# is refused with the same fail-closed shape as the other gate checks. This is a
# cheap bound, NOT the root fix — a server-issued/HMAC-signed handle is a tracked
# follow-up (the 256-bit client ref makes enumeration impractical today).
_CUSTOMER_REF_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


async def _front_gate_for_key(
    *,
    widget_id: str,
    signed_key: str,
    customer_ref: str,
    origin: str | None,
    request: Request,
) -> tuple[PawBarWidget, Any, Any]:
    """The shared public front-gate: resolve the widget + authenticate the key.

    Mirrors ``concierge_chat`` steps 1-6 (fail-closed, cheap gates first):
      0. ``customer_ref`` matches the charset + length bound (400) — cheapest gate.
      1. Widget exists (404) — UNSCOPED (workspace unknown until the key resolves).
      2. Rate limit, overall + per-customer (429).
      3. Authenticate the embed key + dual-mode origin gate
         (``resolve_site_key_with_site`` — 401 bad/unknown/revoked key, 403
         disallowed/missing origin, fail-closed).
      4. Bind the widget to the RESOLVED key: it must belong to the key's workspace
         AND pocket (403) — a key for pocket A must not drive a widget for pocket B.
    Returns ``(widget, ctx, site)`` where ``ctx.workspace_id`` is the
    authenticated tenant used to scope any gated Instinct proposal and ``site`` is
    the Site the gate already loaded — handed back (same pattern as
    ``resolve_site_key_with_site``) so a caller that needs an owner-set Site field
    (the articles listing reads ``url`` + ``kb_article_ids``) never re-queries."""
    if not _CUSTOMER_REF_RE.match(customer_ref or ""):
        raise HTTPException(400, "invalid_customer_ref")
    store = _store()
    widget = await store.get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    ok = await store.within_rate_limit(
        widget_id,
        overall_per_min=widget.rate_limit_per_min,
        per_customer_per_min=widget.per_customer_limit_per_min,
        customer_ref=customer_ref,
    )
    if not ok:
        raise HTTPException(429, "Rate limit exceeded")

    frame_origin = _configured_frame_origin(request)
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key_with_site

    ctx, site = await resolve_site_key_with_site(
        signed_key, origin, customer_ref, frame_origin=frame_origin
    )

    # Bind the widget to the resolved key (finding #2 — no sibling-pocket reach).
    if widget.workspace_id and widget.workspace_id != ctx.workspace_id:
        raise HTTPException(403, "widget_workspace_mismatch")
    if widget.pocket_id != ctx.pocket_id:
        raise HTTPException(403, "widget_pocket_mismatch")
    return widget, ctx, site


class PawBarActionRequest(BaseModel):
    # The public embed key + the widget id (named ``key`` / ``w`` to match the
    # frame endpoint's query params and the glass app's action fetcher).
    key: str
    w: str
    customer_ref: str
    verb: str
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/paw-bar/action")
async def post_action(body: PawBarActionRequest, request: Request) -> JSONResponse:
    """Execute one declared action for a public visitor → {ok, result, cart}.

    Front-gated by ``_front_gate_for_key`` (auth + origin + rate + binding), then
    routed through the SHARED ``execute_action`` — the same code path the concierge
    agent's per-verb tools use. SS-2: a gated verb never executes; it raises an
    Instinct proposal and returns a pending result the visitor polls on the
    decision endpoint. The executor's status hint becomes the HTTP status on
    failure (422 bad verb/args, 409 empty cart / unavailable)."""
    origin = request.headers.get("origin")
    widget, ctx, _site = await _front_gate_for_key(
        widget_id=body.w,
        signed_key=body.key,
        customer_ref=body.customer_ref,
        origin=origin,
        request=request,
    )
    from pocketpaw_ee.paw_bar.actions import execute_action

    outcome = await execute_action(
        widget,
        ctx.workspace_id,
        body.customer_ref,
        body.verb,
        body.args,
        store=_store(),
    )
    if not outcome.ok:
        raise HTTPException(outcome.http_status, outcome.error)
    return JSONResponse({"ok": True, "result": outcome.result, "cart": outcome.cart})


@router.get("/paw-bar/cart")
async def get_cart(
    request: Request,
    key: str = Query("", description="The public Site.signed_key"),
    w: str = Query("", description="The Paw Bar widget id"),
    customer_ref: str = Query("", description="The anonymous visitor handle"),
) -> JSONResponse:
    """Return the visitor's cart → {items, total_cents, currency, checkout_url}.

    Same front-gate as POST /paw-bar/action. Reads the cart via the shared
    ``cart_wire`` serializer (so the shape never drifts from the executor's cart
    results); no cart yet returns an empty cart with the rendered checkout_url."""
    from pocketpaw_ee.paw_bar.actions import cart_wire

    origin = request.headers.get("origin")
    widget, _ctx, _site = await _front_gate_for_key(
        widget_id=w,
        signed_key=key,
        customer_ref=customer_ref,
        origin=origin,
        request=request,
    )
    store = _store()
    # Record a cart-read marker so reads count toward the shared rate limiter —
    # otherwise a read-only enumeration loop is unbounded (the front-gate only
    # CHECKS the limit, nothing was recording for it). Best-effort; a store hiccup
    # must not fail the read.
    try:
        await store.record_event(
            PawBarEvent(widget_id=w, type="pawbar_cart_read", payload={}, customer_ref=customer_ref)
        )
    except Exception:
        logger.debug("cart-read marker record failed (non-fatal)", exc_info=True)
    cart = await store.get_cart(w, customer_ref)
    return JSONResponse(cart_wire(widget, customer_ref, cart))


# ---------------------------------------------------------------------------
# Public conversation endpoints (2026-08-19) — the visitor's own Messages list
#
# The visitor half of the conversation-identity fix. Before it, a visitor had
# exactly one conversation per widget forever, so there was nothing to list and
# no way to start another; the widget's "New conversation" wiped its own
# localStorage and the backend never heard about it.
#
# Same armor class as chat and the cart, through the shared
# ``_front_gate_for_key`` (404 unknown widget → 429 rate limit → 401 bad key →
# 403 origin/binding). Both endpoints are strictly visitor-scoped: the caller can
# only ever read or write conversations belonging to the ``customer_ref`` the
# gate already bound, so there is no id to enumerate and nothing to reach across.
# ---------------------------------------------------------------------------


class VisitorConversationItem(BaseModel):
    """One row of the widget's Messages list."""

    id: str
    state: str = "open"
    # The last thing said, from the visitor's point of view. "" for a conversation
    # opened but never used — the widget renders its own empty-state copy rather
    # than a blank row.
    preview: str = ""
    last_message_at: str = ""
    # Is this the conversation in progress? The widget resumes into it and sends
    # turns against it by default.
    active: bool = False


class VisitorConversationsResponse(BaseModel):
    conversations: list[VisitorConversationItem] = Field(default_factory=list)


class VisitorTranscriptResponse(BaseModel):
    """One of THIS visitor's conversations, oldest-first (2026-08-21).

    The widget's own history. Same messages the owner drill-in renders, through
    the same loader — a visitor and the site owner reading one thread must not
    be reading two different reconstructions of it.
    """

    conversation_id: str
    messages: list[TranscriptMessage] = Field(default_factory=list)


class OpenConversationRequest(BaseModel):
    key: str
    w: str
    customer_ref: str


async def _visitor_conversation_previews(
    widget: PawBarWidget, pocket_id: str, customer_ref: str, workspace_id: str
) -> dict[str, tuple[str, str]]:
    """Map ``conversation_id -> (preview, last_message_at)`` in ONE query.

    The conversation rows carry lifecycle state but deliberately no messages, so
    the preview comes from the run docs. Rather than a query per conversation
    (which would be N round-trips for a list of N), this reads the visitor's
    recent runs once, bounded by ``_CONVERSATION_SCAN_CAP``, and buckets them by
    the conversation encoded in each run's ``session_key``. Runs arrive
    newest-first, so the FIRST run seen for a conversation is its latest.

    A conversation whose only turns are the owner's own replies (those live in
    ``paw_bar_owner_messages``, never as run docs — the metering sweeper bills
    runs, and an owner reply shaped as one would charge the owner for typing)
    resolves to no preview here and renders from its state instead.

    Failure-soft: the list is worth showing without previews.
    """
    out: dict[str, tuple[str, str]] = {}
    if not pocket_id:
        return out
    try:
        runs = await _concierge_runs_for_visitor(
            pocket_id, customer_ref, workspace_id, limit=_CONVERSATION_SCAN_CAP
        )
    except Exception:
        logger.warning("visitor conversation previews failed (non-fatal)", exc_info=True)
        return out

    prefix = f"cloud:concierge:{pocket_id}:"
    suffix = f":{widget.agent_id}"
    for run in runs:
        key = getattr(run, "session_key", "") or ""
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        conversation_id = key[len(prefix) : len(key) - len(suffix)]
        if not conversation_id or conversation_id in out:
            continue  # newest-first, so the first hit is the latest turn
        text = (run.partial_text or "") or (getattr(run, "user_text", "") or "")
        when = getattr(run, "createdAt", None)
        out[conversation_id] = (
            text[:_CONVERSATION_PREVIEW_CHARS],
            when.isoformat() if when else "",
        )
    return out


@router.get("/paw-bar/conversations", response_model=VisitorConversationsResponse)
async def list_visitor_conversations(
    request: Request,
    w: str,
    key: str,
    customer_ref: str,
) -> VisitorConversationsResponse:
    """This visitor's own conversations on this bar, newest first.

    What the widget's Messages tab reads. Scoped twice over — the store filters to
    one (widget, visitor) pair and the front gate has already bound that visitor
    to the resolved key — so a sibling visitor's or a sibling site's conversations
    can never appear in the answer.
    """
    origin = request.headers.get("origin")
    widget, ctx, _site = await _front_gate_for_key(
        widget_id=w,
        signed_key=key,
        customer_ref=customer_ref,
        origin=origin,
        request=request,
    )
    store = _store()
    rows = await store.list_conversations_for_visitor(
        widget.id, customer_ref, workspace_id=ctx.workspace_id
    )
    previews = await _visitor_conversation_previews(
        widget, ctx.pocket_id or "", customer_ref, ctx.workspace_id
    )
    conversations = []
    for row in rows:
        preview, last_at = previews.get(row.id, ("", ""))
        conversations.append(
            VisitorConversationItem(
                id=row.id,
                state=row.state.value,
                preview=preview,
                last_message_at=last_at or (row.last_visitor_at or ""),
                active=row.active,
            )
        )
    return VisitorConversationsResponse(conversations=conversations)


@router.post("/paw-bar/conversations", response_model=VisitorConversationItem)
async def open_visitor_conversation(
    body: OpenConversationRequest, request: Request
) -> VisitorConversationItem:
    """Start a fresh conversation for this visitor and return it.

    The backend half of the widget's "New conversation". The visitor's current
    conversation is RETIRED rather than deleted — it stays in their Messages list
    and in the owner's inbox — and the new one becomes active, so the next turn
    lands on it and the agent starts cold instead of replaying the thread the
    visitor just walked away from.
    """
    origin = request.headers.get("origin")
    widget, ctx, _site = await _front_gate_for_key(
        widget_id=body.w,
        signed_key=body.key,
        customer_ref=body.customer_ref,
        origin=origin,
        request=request,
    )
    store = _store()
    conversation = await store.open_conversation(
        widget.id, body.customer_ref, workspace_id=ctx.workspace_id
    )
    return VisitorConversationItem(
        id=conversation.id,
        state=conversation.state.value,
        preview="",
        last_message_at="",
        active=True,
    )


@router.get(
    "/paw-bar/conversations/{conversation_id}/messages",
    response_model=VisitorTranscriptResponse,
)
async def get_visitor_conversation_messages(
    conversation_id: str,
    request: Request,
    w: str,
    key: str,
    customer_ref: str,
) -> VisitorTranscriptResponse:
    """This visitor's own messages in one conversation, oldest-first.

    WHY THIS EXISTS. The widget had no way to ask. Its thread lived only in the
    frame's localStorage, so anything that lost that storage lost the history
    outright — and plenty does: the bar is a third-party iframe, which Safari
    blocks storage for and Chrome/Firefox partition per top-level site, and the
    stored row carries a 7-day TTL besides. The server had every message the
    whole time (it is what the owner's inbox reads); nothing could fetch them.

    So a visitor who chatted, navigated, and came back saw an empty panel with
    their conversation id still in localStorage pointing at turns nobody could
    load. This is the read that was missing. localStorage becomes a cache for
    the first paint rather than the record.

    Scoped twice, like the list beside it: the front gate binds this visitor to
    the resolved key, and the conversation is checked against the ones the store
    holds for that (widget, visitor) pair. A conversation id belonging to another
    visitor or another site 404s rather than returning an empty thread — the
    loader would answer empty anyway (it filters on customer_ref), but a 404 says
    the honest thing instead of implying the conversation exists and is silent.
    """
    origin = request.headers.get("origin")
    widget, ctx, _site = await _front_gate_for_key(
        widget_id=w,
        signed_key=key,
        customer_ref=customer_ref,
        origin=origin,
        request=request,
    )
    if not conversation_id:
        raise HTTPException(status_code=404, detail="conversation_not_found")

    store = _store()
    rows = await store.list_conversations_for_visitor(
        widget.id, customer_ref, workspace_id=ctx.workspace_id
    )
    if conversation_id not in {row.id for row in rows}:
        raise HTTPException(status_code=404, detail="conversation_not_found")

    messages = await _load_transcript(
        ctx.pocket_id or "",
        customer_ref,
        ctx.workspace_id,
        widget=widget,
        conversation_id=conversation_id,
    )
    # None means the ref has nothing stored at all; for a conversation the store
    # DOES know about, that is an empty thread rather than a missing one — a bot
    # muted the whole time, or a site with transcripts off and no owner replies.
    return VisitorTranscriptResponse(conversation_id=conversation_id, messages=messages or [])


# ---------------------------------------------------------------------------
# Public articles listing (2026-07-30) — the concierge's visible library
#
# The reply-side "sources" event shows WHICH pages grounded one answer; this is
# the browsable other half: everything the concierge can quote, i.e. the site's
# synced KB pages. Same armor class as chat via the shared ``_front_gate_for_key``
# (404 unknown widget → 429 rate limit → 401 bad key → 403 origin/binding). No
# injection screen — there is no free text on this endpoint. The listing is
# filtered to ``Site.kb_article_ids`` (the page sync's own articles), so an
# owner-uploaded private file in the same pocket scope is never listed publicly.
# ---------------------------------------------------------------------------

# Cap on listed articles (CONTRACT: 20) and on the plain-text snippet length
# (CONTRACT: ≤160 chars).
_ARTICLES_MAX = 20
_ARTICLE_SNIPPET_CHARS = 160

# The articles listing has no per-visitor handle in its contract (widget_id +
# signed_key only), but the shared front-gate rate-limits per customer_ref — so
# bare listing calls share ONE fixed, well-formed bucket per widget. A caller MAY
# pass its real visitor handle to get the same per-visitor accounting as chat.
_ARTICLES_DEFAULT_REF = "pawbar-articles"


class ConciergeArticle(BaseModel):
    """One publicly listable synced page: title + public url + short snippet."""

    title: str
    url: str
    snippet: str = ""


class ArticlesResponse(BaseModel):
    articles: list[ConciergeArticle]


@router.get("/paw-bar/articles", response_model=ArticlesResponse)
async def list_public_articles(
    request: Request,
    widget_id: str = Query("", description="The Paw Bar widget id"),
    signed_key: str = Query("", description="The public Site.signed_key"),
    customer_ref: str = Query(
        "",
        description="Optional visitor handle for per-visitor rate accounting",
    ),
) -> ArticlesResponse:
    """List the site's synced KB pages → {"articles": [{title, url, snippet}]}.

    Gates are the SAME front-gate chain as chat, via ``_front_gate_for_key``:
    unknown widget → 404, rate limit → 429, bad/unknown/revoked key → 401,
    disallowed origin / widget∩key binding mismatch → 403. An empty KB (or a
    site whose pages never synced) is a real state, not an error → 200 with
    ``{"articles": []}``. The kb read itself is fail-soft the same way.

    ``_request_origin`` (not the raw header): a same-origin GET from our own
    frame carries no Origin header, and the raw read made the frame's articles
    fetch 403 on the live rig.
    """
    origin = _request_origin(request)
    widget, ctx, site = await _front_gate_for_key(
        widget_id=widget_id,
        signed_key=signed_key,
        customer_ref=customer_ref or _ARTICLES_DEFAULT_REF,
        origin=origin,
        request=request,
    )

    # Record a marker so listing reads count toward the shared rate limiter (the
    # front gate only CHECKS the limit — same reasoning as the cart read).
    # Best-effort; a store hiccup must not fail the read.
    try:
        await _store().record_event(
            PawBarEvent(
                widget_id=widget.id,
                type="pawbar_articles_read",
                payload={},
                customer_ref=customer_ref or _ARTICLES_DEFAULT_REF,
            )
        )
    except Exception:
        logger.debug("articles-read marker record failed (non-fatal)", exc_info=True)

    base_url = str(getattr(site, "url", "") or "")
    synced = set(getattr(site, "kb_article_ids", None) or [])
    pocket_id = ctx.pocket_id or ""
    if not pocket_id or not base_url.strip() or not synced:
        return ArticlesResponse(articles=[])

    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    try:
        raw = await KnowledgeService.list_articles_for_scope(f"pocket:{pocket_id}")
    except Exception:  # noqa: BLE001 — a kb hiccup lists nothing, never 500s
        logger.warning("paw-bar articles: kb list failed for pocket %s", pocket_id, exc_info=True)
        return ArticlesResponse(articles=[])

    articles: list[ConciergeArticle] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        article_id = str(entry.get("id") or "")
        title = str(entry.get("title") or "").strip()
        # Only the page sync's own articles are listable — an owner-uploaded
        # file sharing the pocket scope stays private.
        if article_id not in synced or not title:
            continue
        url = _article_page_url(article_id, base_url)
        if not url:
            continue
        snippet = " ".join(str(entry.get("summary") or "").split())[:_ARTICLE_SNIPPET_CHARS]
        articles.append(
            ConciergeArticle(title=_humanize_article_title(title), url=url, snippet=snippet)
        )
        if len(articles) >= _ARTICLES_MAX:
            break
    return ArticlesResponse(articles=articles)


# ---------------------------------------------------------------------------
# Visitor-side message poll (2026-07-30, owner inbox slice 2) — the other half
# of takeover. The owner's reply is written on the dashboard; this is how it
# reaches the person waiting on the page. Sibling of the decision poll, same
# armor class as chat via the shared ``_front_gate_for_key``, and deliberately
# the NARROWEST read on this router: a conversation carries private notes, tags,
# an assignee, a captured email and a queue state, and none of them belong to
# the visitor. What crosses is what was said to them, and whether a human is
# holding the thread.
# ---------------------------------------------------------------------------


@router.get(
    "/paw-bar/messages/{widget_id}/{customer_ref}",
    response_model=VisitorMessagesResponse,
)
async def get_visitor_messages(
    widget_id: str,
    customer_ref: str,
    request: Request,
    signed_key: str = Query("", description="The public Site.signed_key"),
    after: str = Query("", description="Only messages stamped strictly later"),
    conversation_id: str = Query("", description="Only lines said in this conversation"),
) -> VisitorMessagesResponse:
    """Poll for owner/system messages on this visitor's thread.

    Gates are the SAME front-gate chain as chat, via ``_front_gate_for_key``:
    malformed handle → 400, unknown widget → 404, rate limit → 429,
    bad/unknown/revoked key → 401, disallowed origin or a widget∩key binding
    mismatch → 403. ``_request_origin`` rather than the raw header, because a
    same-origin GET from our own frame carries no Origin and the raw read made
    exactly this kind of poll 403 on the live rig.

    WHAT CROSSES, exhaustively: ``role`` (owner | system), ``content``, ``at``,
    plus the thread's ``bot_paused``. Nothing else — not the private notes, not
    the tags, not the assignee, not the captured email, not the queue state, not
    even the visitor's own stored lines. This is the only public read of a
    conversation, so its shape is the boundary.

    ``after`` is a strict cursor (the last ``at`` the widget rendered), the page is
    capped at ``_OWNER_POLL_CAP`` and comes back oldest-first. A malformed
    ``after`` is IGNORED rather than refused, the same way the conversation list
    treats a malformed cursor: a poll that hard-failed on a bad cursor would leave
    the visitor staring at a thread that silently stopped updating.

    The idle auto-resume is applied here too, so the poll and the chat endpoint can
    never disagree about whether the bot is muted — the visitor learns the
    assistant is back on the same tick the assistant starts answering again.
    """
    origin = _request_origin(request)
    widget, ctx, _site = await _front_gate_for_key(
        widget_id=widget_id,
        signed_key=signed_key,
        customer_ref=customer_ref,
        origin=origin,
        request=request,
    )

    store = _store()
    try:
        await store.auto_resume_bot_if_idle(widget.id, customer_ref, ctx.workspace_id)
        # ``bot_paused`` belongs to the conversation on screen, not to the
        # visitor's most recent one — otherwise a visitor reading an old thread a
        # human had taken over is told the assistant is muted in the thread they
        # are actually typing into. A named conversation must still be THEIRS;
        # a stranger's id reads as no conversation rather than as its state.
        conversation = None
        if conversation_id:
            named = await store.get_conversation_by_id(
                conversation_id, workspace_id=ctx.workspace_id
            )
            if (
                named is not None
                and named.widget_id == widget.id
                and named.customer_ref == customer_ref
            ):
                conversation = named
        else:
            conversation = await store.get_conversation(
                widget.id, customer_ref, workspace_id=ctx.workspace_id
            )
        messages = await store.list_owner_messages(
            widget.id,
            customer_ref,
            workspace_id=ctx.workspace_id,
            after=_normalized_after(after),
            roles=_PUBLIC_MESSAGE_ROLES,
            limit=_OWNER_POLL_CAP,
            # Scoped to the thread on screen (2026-08-19). Omitted by a cached
            # widget bundle, which then keeps the pre-identity behaviour of
            # receiving every owner line on the visitor's whole thread — degraded
            # but never silent, which is the right way round for a poll.
            conversation_id=conversation_id or None,
        )
    except Exception:  # noqa: BLE001 — a poll that 500s stalls the visitor's thread
        logger.warning("visitor message poll failed for widget %s", widget.id, exc_info=True)
        return VisitorMessagesResponse(messages=[], bot_paused=False)

    return VisitorMessagesResponse(
        messages=[
            VisitorMessage(role=m.role.value, content=m.content, at=m.created_at)
            for m in messages
            if m.content
        ],
        bot_paused=bool(conversation.bot_paused) if conversation is not None else False,
    )


def _normalized_after(after: str) -> str:
    """Re-render a client ``after`` cursor in the format the rows are stored in.

    The cursor is compared as a string in SQL, which only works while every value
    shares one format. Rows are written by ``_utc_stamp`` (aware UTC), but a
    browser round-tripping the value through ``Date.toISOString()`` hands back a
    ``…Z`` spelling of the same instant — and ``'Z'`` sorts AFTER the ``'.'`` of a
    fractional second, so the same moment would compare as later and the poll
    would skip messages it should have delivered. Parse, convert to UTC, re-render.
    An unparseable cursor yields "" (no filter) rather than an error: a bad cursor
    should cost a duplicate render, never a stalled thread.
    """
    stamp = (after or "").strip()
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Async decision delivery (2026-07-30) — the visitor who leaves the page
#
# The decision loop's on-page half is the poll endpoint above; this is the
# async half. A visitor whose request is still PENDING can leave an email
# before closing the tab; when the owner decides, the delivery hook emails
# them the SAME customer-facing reply the poll would have shown. Same armor
# class as /paw-bar/chat via the shared ``_front_gate_for_key``. The email is
# row-only PII (see models.DecisionStatus.contact_email) and is NEVER echoed
# back by any public read — the decision poll response omits it by shape.
# ---------------------------------------------------------------------------

# Simple RFC-ish shape check — one local part, one @, a dotted domain. The
# 254-char cap is the SMTP path limit; anything longer is refused outright.
_CONTACT_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_CONTACT_EMAIL_CHARS = 254


class DecisionContactRequest(BaseModel):
    widget_id: str
    # The public, origin-bound embed key (Site.signed_key) — same credential
    # model as ConciergeChatRequest; there is no signed-in user.
    signed_key: str
    customer_ref: str
    email: str


@router.post("/paw-bar/decision-contact")
async def post_decision_contact(body: DecisionContactRequest, request: Request) -> JSONResponse:
    """Attach a contact email to this visitor's PENDING decision rows.

    Gates mirror ``concierge_chat`` via the shared ``_front_gate_for_key``
    (fail-closed, cheap gates first): widget exists (404) → rate limit (429) →
    embed-key auth + dual-mode origin gate (401/403) → widget∩key binding
    (403). Then the email is validated (422) and stamped onto the visitor's
    pending rows only — a decided row was already answered on-page. Returns
    ``{ok: true, attached: N}``; the address itself is never echoed back here
    or on any other public read.
    """
    origin = request.headers.get("origin")
    widget, _ctx, _site = await _front_gate_for_key(
        widget_id=body.widget_id,
        signed_key=body.signed_key,
        customer_ref=body.customer_ref,
        origin=origin,
        request=request,
    )

    email = body.email.strip()
    if len(email) > _MAX_CONTACT_EMAIL_CHARS or not _CONTACT_EMAIL_RE.match(email):
        raise HTTPException(422, "invalid_email")

    store = _store()
    # Record a marker so contact posts count toward the shared rate limiter
    # (the front gate only CHECKS the limit). Payload stays EMPTY — the email
    # must never land in the event log (PII invariant). Best-effort.
    try:
        await store.record_event(
            PawBarEvent(
                widget_id=widget.id,
                type="pawbar_decision_contact",
                payload={},
                customer_ref=body.customer_ref,
            )
        )
    except Exception:
        logger.debug("decision-contact marker record failed (non-fatal)", exc_info=True)

    # Scope the write with the same workspace value the decision rows were
    # stamped with at propose time (decision_loop.resolve_workspace_id — the
    # widget's real workspace, or the owner label for a legacy row).
    from pocketpaw_ee.paw_bar.decision_loop import resolve_workspace_id

    attached = await store.attach_contact_email(
        widget.id,
        body.customer_ref,
        email,
        workspace_id=resolve_workspace_id(widget) or None,
    )
    return JSONResponse({"ok": True, "attached": attached})


# ---------------------------------------------------------------------------
# The escape hatch (2026-07-31, owner inbox slice 3) — "talk to a human"
#
# The concierge agent has a ``pawbar_request_human`` tool and a prompt telling it
# a request for a person is always honored. This endpoint is the half that does
# not depend on the agent AGREEING: the bar can offer "talk to a human" as a
# permanent affordance, and it works while the bot is answering confidently,
# while it is muted, and while it is refusing to admit it can't help. Same armor
# class as chat/action/articles via the shared ``_front_gate_for_key`` (which
# also carries the owner's concierge kill switch), plus the same injection screen
# chat runs on free text, because the note lands on a surface a human reads.
# ---------------------------------------------------------------------------


class RequestHumanRequest(BaseModel):
    # Same public credential + widget naming as the action endpoint (``key`` /
    # ``w``), so the glass app's existing fetcher shape is reused verbatim.
    key: str
    w: str
    customer_ref: str
    # Why they want a person. Optional: a visitor who taps the button without
    # typing anything has still asked, and refusing that for want of a sentence
    # would defeat the point.
    message: str = ""
    # An address they can be reached on, typed HERE and for this purpose. Never
    # inherited from the decision-capture row, whose PII invariant keeps that
    # address where it was left.
    contact: str = ""


@router.post("/paw-bar/request-human")
async def post_request_human(body: RequestHumanRequest, request: Request) -> JSONResponse:
    """Raise a human handoff for this visitor → ``{ok, handoff_id, state}``.

    Front-gated by ``_front_gate_for_key`` (404 → 429 → 401 → 403 origin → 403
    binding, plus the site's ``concierge_enabled`` kill switch inside the key
    resolver), then the free-text note is injection-screened exactly as a chat
    message is, then the SHARED producer runs — the same
    ``handoff.raise_handoff`` the agent's tool calls, so a visitor-raised and an
    agent-raised handoff are the same record.

    Available in EVERY conversation state. A paused bot, an open thread, a
    conversation that has never been escalated — all of them accept this, because
    "I want a person" is not a fallback for when the bot fails, it is a thing
    customers are entitled to ask for at any moment.
    """
    origin = request.headers.get("origin")
    widget, ctx, _site = await _front_gate_for_key(
        widget_id=body.w,
        signed_key=body.key,
        customer_ref=body.customer_ref,
        origin=origin,
        request=request,
    )
    if body.message and not await _screen_message_for_injection(body.message, widget.id):
        raise HTTPException(400, "message_rejected")
    if body.contact and (
        len(body.contact) > _MAX_CONTACT_EMAIL_CHARS or not _CONTACT_EMAIL_RE.match(body.contact)
    ):
        raise HTTPException(422, "invalid_email")

    from pocketpaw_ee.paw_bar.handoff import raise_handoff

    outcome = await raise_handoff(
        widget=widget,
        workspace_id=ctx.workspace_id,
        customer_ref=body.customer_ref,
        question=body.message,
        contact=body.contact,
        source="visitor",
        store=_store(),
    )
    if not outcome.ok:
        raise HTTPException(outcome.http_status, outcome.error)
    return JSONResponse(
        {
            "ok": True,
            "handoff_id": outcome.handoff_id,
            # The resulting queue state, reported honestly: on the rare partial
            # failure where only the handoff record landed, the conversation was
            # not moved and this must not claim that it was.
            "state": ConversationState.NEEDS_HUMAN.value if outcome.escalated else "",
            "message": _HUMAN_NOTIFIED_MESSAGE,
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _scan_text_is_safe(text: str, *, source: str) -> bool:
    """Shared InjectionScanner gate — the single screening primitive both the
    structured-event ingest and the free-text concierge-chat paths reuse.

    Runs the heuristic :class:`InjectionScanner` (regex-based, no API key
    required) over ``text`` and returns ``False`` — DROP — when the scan reports
    a ``HIGH`` threat. The HIGH threshold is deliberate: ``MEDIUM`` covers softer
    persona/roleplay phrasing that legitimate input ("act as my travel guide")
    could trip, so screening only the unambiguous HIGH patterns (instruction
    overrides, delimiter attacks, jailbreaks, exfiltration) avoids false-dropping
    real input.

    Degrades cleanly: if the security module can't be imported or the scan
    raises, the input is ACCEPTED (availability over a hard fail on a public
    endpoint). This is the same logic that replaced the old permanent-no-op
    ``getattr(guardian, "check_input")`` Guardian screen.
    """
    try:
        from pocketpaw.security.injection_scanner import (
            ThreatLevel,
            get_injection_scanner,
        )
    except Exception:
        return True

    try:
        scan = get_injection_scanner().scan(text, source=source)
    except Exception:
        logger.debug("Injection scan raised; accepting by default")
        return True

    if scan.threat_level == ThreatLevel.HIGH:
        logger.warning(
            "Dropping paw-bar input from %s — injection threat %s (patterns: %s)",
            source,
            scan.threat_level.value,
            ", ".join(scan.matched_patterns),
        )
        return False
    return True


async def _screen_event_for_injection(event: PawBarEvent) -> bool:
    """Screen the stringified event payload for prompt-injection content.

    Thin wrapper over :func:`_scan_text_is_safe` (the shared scanner gate) on the
    JSON-serialized payload. Behavior is unchanged from before the shared helper
    existed: drop on HIGH, accept otherwise, degrade to accept on any failure.
    """
    payload = json.dumps(event.payload, default=str)
    return await _scan_text_is_safe(payload, source=f"paw_bar:{event.widget_id}")


async def _screen_message_for_injection(message: str, widget_id: str) -> bool:
    """Screen a free-text concierge-chat message for prompt-injection content (T2).

    The concierge chat path is public + unauthenticated-by-user, and the message
    is untrusted free text (not a ≤4KB structured event), so it runs through the
    SAME :func:`_scan_text_is_safe` gate the event ingest uses — dropped on a HIGH
    threat. This is one layer of the concierge guard; the hard controls are the
    tool-denying surface profile and the pocket-locked KB scope.
    """
    return await _scan_text_is_safe(message, source=f"paw_bar_chat:{widget_id}")


async def _open_decision_loop(
    widget: PawBarWidget,
    event: PawBarEvent,
    store: Any,
) -> str | None:
    """Raise an Instinct proposal for a mapped customer event (gap2).

    Thin wrapper over ``decision_loop.propose_customer_decision`` — keeps the
    import lazy (the OSS paw_bar store never reaches into the EE decision-loop
    module) and the failure best-effort: any error is swallowed by the called
    function, and a defensive guard here ensures even an import failure can't
    break the ingest response. Returns the proposed Instinct action id, or None.
    """
    try:
        from pocketpaw_ee.paw_bar.decision_loop import propose_customer_decision

        return await propose_customer_decision(
            widget=widget,
            event=event,
            paw_bar_store=store,
        )
    except Exception:
        logger.warning(
            "decision-loop proposal failed for widget %s (non-fatal)",
            widget.id,
            exc_info=True,
        )
        return None


async def _apply_event_mapping(widget: PawBarWidget, event: PawBarEvent) -> str | None:
    """Turn a PawBarEvent into a Fabric object when a mapping exists."""
    mapping = widget.event_mapping.get(event.type)
    if mapping is None:
        return None

    try:
        from pocketpaw.fabric.models import FabricObject
        from pocketpaw_ee.api import get_fabric_store
    except ImportError:
        return None

    # W4a tenancy — the public ingest path is token-only (no session), so the
    # tenant is derived from the widget ROW: the workspace_id stamped at
    # create time by the admin route. That is a REAL workspace id (unlike the
    # logical, possibly colon-qualified ``owner`` — ``user:maya`` — which fails
    # the store factory's path allowlist and must never be used as a store
    # key). ``or None`` preserves legacy/single-tenant behavior: an unstamped
    # ('' ) row keeps writing to the shared default store exactly as before.
    fabric = get_fabric_store(workspace_id=widget.workspace_id or None)
    if fabric is None:
        return None

    context = {"payload": event.payload, "customer_ref": event.customer_ref}
    properties = {k: _interpolate(v, context) for k, v in mapping.fields.items()}
    try:
        obj = FabricObject(
            type_name=mapping.creates,
            properties=properties,
            source_connector="paw_bar",
            source_id=widget.id,
        )
        created = await fabric.create_object(obj)
        return getattr(created, "id", None)
    except Exception:
        logger.exception("Failed to create Fabric object from paw-bar event")
        return None


def _interpolate(template: str, context: dict[str, Any]) -> Any:
    """Resolve `{{ a.b }}` placeholders against the context dict.

    If the entire template is a single placeholder (`{{ payload.item }}`), the
    raw value is returned (preserving non-string types). Mixed strings fall back
    to stringified substitution.
    """
    full_match = re.fullmatch(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}", template)
    if full_match:
        return _lookup(full_match.group(1), context)

    def _replace(m: re.Match[str]) -> str:
        val = _lookup(m.group(1), context)
        return "" if val is None else str(val)

    return _PLACEHOLDER_RE.sub(_replace, template)


def _lookup(path: str, context: dict[str, Any]) -> Any:
    cur: Any = context
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur

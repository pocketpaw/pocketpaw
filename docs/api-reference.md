<!--
docs/api-reference.md — Hand-maintained reference for cloud REST endpoints
that are not covered by the per-endpoint Mintlify pages under docs/api/.

Updated: 2026-10-07 (feat/sites-project-tools) — "Project sites": the twelve agent
  tools for `engine: "project"` (templates, recipes, generic file tools, run_site_build /
  get_site_build_log), their path rules, caps and plan gate.
Updated: 2026-10-07 (feat/sites-project-tools) — "Project files": list / read /
  write / patch / delete routes under `/sites/by-pocket/{pocket_id}/files`.
Updated: 2026-10-07 (perf/sites-fast-edits) — "Draft verification": edit tools
  return the static check only (`status: "pending"`, `static`, `build`, `job_id`)
  and the build + browser verdict arrives on the next tool result as
  `previous_verification`; only a static failure rolls a svelte edit back; an
  unreferenced `create=true` is `skipped`; html edits skip the browser layer;
  `preview_site` returns at most 3 tiles. Sandbox image setup:
  `docs/deployment/sites-verify-image.md`.
Updated: 2026-10-06 (fix/site-source-per-site-tier) — a site's source is
  visible (`sourceVisible` on the pocket) only when the SITE is on `site` or
  `staff` with an active subscription. The workspace plan no longer grants it;
  `site_source_visible` on `GET /entitlements` is now the operator override only
  (`null` = each site decides). Download and template-sharing notes updated.
Updated: 2026-10-03 (fix/paw-key-scopes) — "Workspace API key scopes": which
  route families each `paw_` key scope unlocks; everything else is a 403.
Updated: 2026-10-02 (feat/studio-templates) — "Studio templates" (publish a
  Studio generation as a template: POST / GET /studio-templates, PATCH / DELETE
  /studio-templates/{id}). Discover gains a second source, `studio_template`,
  and two public listing fields, `media_kind` and `media_url`.
Updated: 2026-10-02 (feat/partners-tiers, PH-15) — Paw Partners volume tiers and
  milestone rewards: GET /partners/me adds tier standing and benefits, offers and
  sales are priced at the tier discount, commissions use the tier rate, summary /
  earnings add reward credits and lifetime sites sold, new GET /partners/rewards.
Updated: 2026-10-02 (feat/partners-commissions, PH-13 re-check) — partial refunds
  that add up to the full amount lapse the site; a partial refund before the
  payment is processed no longer cancels the link; a pay link whose reservation
  went stale while the checkout was created is a 409 partners.link_in_progress.
Updated: 2026-10-02 (feat/partners-commissions, PH-13 review) — pay-link needs
  sites.buy_plan; one open link per site (409 partners.link_open /
  partners.link_in_progress); partial refunds take a pro-rata share of the
  commission; clawback survives a deleted site.
Updated: 2026-10-02 (feat/partners-commissions, PH-13) — Paw Partners: new
  POST /partners/pay-link (the partner's client pays a site's year through a
  one-time link; the partner earns a commission in credits). Offers carry the
  client's list price, sold sites carry billing_mode, summary and earnings carry
  commission credits.
Updated: 2026-10-02 (feat/partners-earnings, PH-11) — Paw Partners: POST /partners/sell
  takes an optional price_minor + currency (booked as a paid receipt on the site's
  client record) and returns invoice_id; new GET /partners/summary and
  GET /partners/earnings. Review fixes: a receipt only when the sale recorded a new
  debit, idempotent under a double submit, spend/sales semantics spelled out.
Updated: 2026-10-02 (feat/partners-cobrand, PH-5) — "Hide the PocketPaw badge":
  partner-sold sites carry the partner co-brand mark instead of the badge.
Updated: 2026-10-02 (feat/partners-whatsapp-leads, PH-6) — "Partner leads on
  WhatsApp" under Owner notifications: a lead on a partner-sold site goes to the
  opted-in shop owner on WhatsApp through the platform MSG91 account
  (POCKETPAW_MSG91_PLATFORM_*), capped at 30 a day per number; a client PATCH
  that changes the number clears the opt-in.
Updated: 2026-10-02 (feat/partners-sell, PH-2) — Paw Partners: GET /partners/offers,
  POST /partners/sell, GET /partners/sites (yearly partner plans paid from the
  partner's credit wallet).
Updated: 2026-10-02 (feat/partners-foundation, PH-1) — added "Paw Partners —
  profile and clients": GET /partners/me, client CRUD under /partners/clients,
  and the operator PUT/DELETE /platform/workspaces/{workspace_id}/partner. An active
  partner profile turns site billing on for that workspace.
Updated: 2026-10-02 (feat/discover-index, review) — Discover reindexes once at
  startup (and then every 30 minutes with the cloud scheduler on); the owner
  using their own listing doesn't raise `remix_count`.
Updated: 2026-10-02 (feat/discover-moderation) — "Platform — Discover
  Moderation": staff list (SUPPORT) and feature / unfeature / hide / unhide /
  reindex (OPERATOR) under /api/v1/platform/discover, each audited.
Updated: 2026-10-02 (feat/discover-index, hardening) — Discover reports are
  limited to 10 an hour per user (`429 discover.report_rate_limited`); a
  Discover hide also hides the source template (so re-publishing it doesn't
  bring it back) and keeps a hidden listing; staff unhide ignores that
  listing's earlier reporters; a 30-minute reindex refreshes `live_url`.
Updated: 2026-10-01 (feat/discover-index) — Site templates gain `kind`,
  `audiences` (accepted on save and PATCH) and `live_url` (the source site's
  deployed URL) on every response; public, unhidden templates are mirrored into
  the Discover index. Added "Discover — Public Index" (GET /discover,
  GET /discover/{id} public and rate-limited; POST /discover/{id}/use and
  /report signed in).
Updated: 2026-10-01 (CN-3, fix/canon-daily-caps) — the guest-cap note names the
shared daily counter (`metering.service.try_spend`) instead of the removed
`guest_budget.try_spend_turn`.

Updated: 2026-10-01 (feat/atlas-canonical) — added "Atlas — Surfaces, Verbs and
  Search" (GET /api/v1/atlas/{surfaces,verbs,search}), and `open_surface`'s
  route list now comes from atlas (`agent_openable` surfaces). Review pass: the
  atlas routes need an active user and resolve the caller's role.
Updated: 2026-10-01 (feat/rooms-read-tool) — added "Agent — Read Chat Rooms
  (`list_rooms` / `read_room`)": the read-only in-process MCP tools over the
  workspace's own chat rooms, their authz path, caps and scoping.
Updated: 2026-09-30 (feat/open-surface-tool) — added "Agent — Open an App Surface
  (`open_surface`)": the in-process MCP tool, the `open_surface` chat stream event
  it produces, and the checks between the two.
Updated: 2026-09-29 (feat/growth-prospect-actions) — Growth — Prospects: added
  POST /growth/prospects/{id}/research (one-prospect research run that fills
  gaps and stores a `research` profile + `researched_at` on the envelope) and
  POST /growth/prospects/{id}/draft (first-touch drafts from the tool-less
  growth-writer agent, per eligible channel, with `skipped` reasons).
Updated: 2026-09-28 (feat/concierge-knowledge-sources) — Paw Bar admin table: added
  the knowledge-source routes under /paw-bar/admin/site/{site_id}/knowledge/sources.
Updated: 2026-09-28 (feat/concierge-pinned-faqs) — Paw Bar admin table: added
  the pinned-FAQ routes under /paw-bar/admin/site/{site_id}/knowledge/faqs.
Updated: 2026-09-28 (feat/concierge-page-aware, CR-3) — POST /paw-bar/chat takes an
  optional `page: {url, title}` (validated server-side, read by v2 only), and the
  v2 `sources` frame is `{items: [{id, title, url}]}` (mirrored under `sources`),
  equal to the knowledge the model was given.
Updated: 2026-09-28 (feat/concierge-guided-fields, CR-4) — Paw Bar admin table:
  the settings GET/PATCH carry the owner's guided concierge fields, with their
  caps and PATCH rules, in a table under "Admin — the owner surface".
Updated: 2026-09-28 (feat/concierge-manual-create, CR-12) — Paw Bar admin table:
  added POST/DELETE /paw-bar/admin/site/{site_id}/concierge, the only way a
  concierge is created or removed; `concierge_exists` on the settings and overview
  responses; `embed_snippet` now depends on the concierge existing, not on a bound
  agent; widget create and the enable PATCH no longer provision an agent; the
  foreign-concierge rebind needs an `agent_id`.
Updated: 2026-09-27 (feat/sites-visual-research) — added the `preview_site` agent
  tool under "Draft verification": a full-page screenshot of the draft, returned
  to the agent as images so it can look at the page before calling it ready.
Updated: 2026-09-26 (fix/pawbar-frame-sandbox-header) — Paw Bar: the public frame
  and the owner preview frame send a CSP `sandbox` directive on every frame
  document, the dead shell included.
Updated: 2026-09-27 (feat/bulk-grants-conversations) — added "Batch reads for the
  chat sidebar": POST /uploads/grants, POST /paw-bar/admin/sites/conversations and
  POST /sessions/by-agents, the one-call forms of three per-item GETs.
Updated: 2026-09-26 (fix/pawbar-public-route-gates) — Paw Bar public table: the
  per-IP limit, the key rule on events/decision, the chat input bounds, the
  events rate bucket, the author-less visitor transcript and the one error code.
Updated: 2026-09-26 (feat/pawbar-admin-widget-spec-route) — Paw Bar admin table:
  added PATCH /paw-bar/admin/site/{site_id}/widget/spec, and noted `embed_snippet`
  on the settings GET/PATCH; the route pins spec.widget_id / spec.pocket_id.
Updated: 2026-09-23 (VS-4, feat/sites-rename) — added "Sites — Addresses and renaming":
`GET /sites/slug-available`, `PUT /sites/{site_id}/slug` and
`DELETE /sites/{site_id}/slug/pending`, plus `slug` / `slug_pending` on the site
response. Written around the thing a client cannot infer: a rename is a reservation
that goes live on the next publish, so the response keeps showing the old `slug`.

Updated: 2026-09-24 (PP-2, feat/sites-verify-pipeline) — added "Draft verification":
the `verification` verdict every site create/edit tool now returns, the `verify_site`
agent tool, and the counts-only `verification` field on
`GET /sites/by-pocket/{pocket_id}/status`. Rewrote the PP-1 note on
`edit_svelte_component`, which now verifies instead of building locally.

Updated: 2026-10-07 (fix/sites-open-dependencies) — `set_site_dependencies` follows
the "open everything" policy: any public npm package, version, range or dist-tag;
advisories and deprecation come back as `warnings`; no age, size, downloads, script
or count gate. react / svelte authors may write `package.json`, `vite.config.*`,
`svelte.config.js`, `bunfig.toml` and `.npmrc`.

Updated: 2026-09-24 (PP-1, feat/sites-author-dependencies) — added the
`set_site_dependencies` agent tool and the `dependencies` argument on the three
source-engine create tools, under "Sites — Agent Editing Tools".

Updated: 2026-09-23 (feat/sites-badge-switch, VS-3) — added "Sites — Hide the PocketPaw
badge": `PATCH /sites/{site_id}/branding` and `badge_hidden` on the site response. Written
around the two things a client cannot infer: the flag is a preference that only takes
effect on a site whose plan removes the badge, and on a live site the change lands at the
next publish rather than immediately.

Updated: 2026-09-21 (feat/site-project-download-endpoint) — added "Sites — Download the
project". Written around the three things a client cannot infer: the 402 covers two
different customers (a floor site and a lapsed paid one) whose remedies differ, a Ripple
site is a 400 rather than an empty archive, and the pre-check field is the PER-SITE
`project_download` and not the workspace's `site_source_visible` — a paid site in a free
workspace may download a project whose Code tab is hidden.

Updated: 2026-09-19 (SF-13) — added "Sites — the foreign-origin concierge": the
four endpoints that finally reach the bind. Written around the three things a
client cannot infer from the field names. The bind SPENDS MONEY and is
idempotent, so it needs `sites.buy_plan` on top of `fabric.write` and a repeat is
a 200 rather than a second $19 — and it carries no `created` flag, because the
route cannot honestly say which of two concurrent first binds charged. The two
verification refusals are different codes (never proved vs proved over 30 days
ago) and must stay that way, since they are different instructions to the owner.
And the grounding read is NOT here: `/paw-bar/admin/site/{site_id}/knowledge`
already serves it for any Site in the workspace, so this response carries the
`site_id` to call it with instead of a second copy of those fields.

Updated: 2026-09-11 (feat/otherhand-tools-toggle) — added the "Agent chat — the
`tools` switch" section for the new per-send request field. Written around the
two things a client cannot infer from a `bool | None`: the field is subtractive
only, so `true` is a no-op and cannot re-enable anything the server withdrew,
and it governs one send rather than being a setting that sticks.

Updated: 2026-09-11 (feat/byok-image-key) — added the "BYOK key management"
section. The whole `/byok` prefix was undocumented here: five routes, of which
two are new (`PUT` and `DELETE /byok/image-key`), plus the four `image_*`
columns `ByokStatus` now carries. Two things a reader cannot get from the field
names and both are entitlement changes rather than plumbing: the image key is
stored WITHOUT a validation round trip, so the first refused generation is where
a bad credential becomes visible, and a workspace with its own image key is
neither refused as a guest nor counted against the platform's daily cap. Also
stated that the ceiling it removes is a SPEND ceiling and not a rate one.

Updated: 2026-09-02 (SA-7) — finished the Visitor Analytics section with the two
things a reader could not get from the endpoint's own fields. First, what the
`analytics` grant actually buys: which tiers carry it, that it needs an active
subscription and not only a tier, and that UPGRADING DOES NOT BACKFILL — the
consequence customers hit, and one the `never_counted` row alone does not explain,
since it reads as a temporary state rather than as a permanent hole in the history.
Also recorded how a site counts and the one case that still cannot, which is
otherwise indistinguishable from a site nobody has republished. WRITTEN FOR THE STATE
AFTER #2049, which merges first: every engine counts, in one of two shapes chosen by
the BUILD rather than the engine name, and the row carries a device class. The
`devices: null` example this section used to show was retired for the same reason —
it is a real response, but only for a site that has not republished since, so leading
with it taught clients that the field is always null.
Second, `GET /sites/{site_id}/entitlements`, which was undocumented in this file
entirely — it is the pre-check that lets a panel disable itself before the call, and
the section says plainly that it does NOT supersede the analytics `status`, because
entitlement cannot tell "your plan excludes this" from "you have not republished".
The `retention_days` bullet stopped naming the number, which invited clients to copy
it; the field is the source of truth and the note now says so.

Updated: 2026-09-05 (feat/files-links) — new "Files — Links and Graph" section:
GET /files/{file_id}/links and GET /files/graph, the `link_names` field the
FileReady listener now writes for text notes, and the note that POST
/files/write and PUT /files/{id} now emit FileReady so editor notes are
indexed, tagged and linked like uploads.

Updated: 2026-08-18 (fix/sites-html-refine-names-the-edit-tool) — documented
`edit_html_file`, the html track's chat edit tool. It shipped in db083bfc without
reaching this file, so the section below still said html had no edit tool and was
"edited by uid splice via the leaf-edits route" — that route is the NATIVE
editor's path, not the agent's, and the two are different entry points. Recorded
with the same emphasis the react entry gets on the things a field list cannot
show: why the argument is `file_path` rather than `component_path` (html has no
component model, and its paths are root-relative), and why this tool does not
republish for a DIFFERENT reason than react's — html runs no build at all, so
there is no gate that could catch a bad edit before it went live.

Updated: 2026-09-11 (feat/sites-svelte-edit-create, SC-1) — added the
`edit_svelte_component` section, which had no section of its own even though the
table above has always listed it. That gap mirrored a gap in the tool: svelte was
the first edit lane to ship and the last to be able to CREATE a file, so "add an
about page" to a live svelte site was unanswerable and the agent's remaining move
was a second `create_svelte_site` (a second pocket at a second url). The section
documents the new `create` arg and the two things about this track that do not
transfer from the react docs a reader would otherwise reach for: a page here is TWO
files, and an unlinked route still exists (SvelteKit prerenders it) where an
unimported react component does not.

Updated: 2026-08-11 (feat/sites-react-edit-lane, RX-4) — documented the build-lane
fields now on the `publish` tool response and the new read-only
`get_site_build_status` tool, in the same MCP section. Both are recorded here
because the *reason* they exist is not visible from their field lists: `url` and
`deployed` are individually insufficient to answer "is this site live", and on
react they actively mislead (a first publish returns `url: ""`, a re-publish
returns the previous deploy's url). Anyone reading only the field names would
reasonably use `url` directly, which is the defect.

Updated: 2026-08-11 (feat/sites-react-edit-lane, RX-3) — added the "Sites —
Agent Editing Tools (in-process MCP)" section documenting `edit_react_component`
and the per-engine split that decides which editing tool a site gets. This is
the first MCP tool documented in this file, which is otherwise REST-only, and it
belongs here for a specific reason: the react edit lane has no REST route at all
(it is chat-only), so a reader who checks the reference for "how do I change a
react site" would otherwise find the svelte native-editing endpoints above and
reasonably conclude nothing exists. The section also records WHY this tool does
not publish, because the missing republish looks like an omission next to
`edit_svelte_component` and is not one.

Created: 2026-05-21 (RFC 04 alpha) — documents the per-pocket backend
binding + read-only source-run endpoints. The rest of the cloud pockets
API is described in the auto-generated wiki article
`ee/docs/wiki/pockets-router-*.md`.

Updated: 2026-05-21 (PR #1177 security pass) — documented the new
DELETE /pockets/{id}/backend endpoint and the edit-access requirement on
GET /pockets/{id}/backend.

Updated: 2026-05-22 (RFC 05 M2a) — documented the write-action endpoints
(POST /pockets/{id}/actions/run, PUT /pockets/{id}/backend/write-policy)
and the per-pocket write allowlist now carried on the backend summary.

Updated: 2026-05-22 (feat/api-skills, Increment 2b) — documented
POST /skills/api-doc, the per-backend API-skill install endpoint that
turns a pocket backend's OpenAPI document into a loadable SKILL.md so
the authoring agent stops hallucinating endpoints.

Updated: 2026-05-22 (feat/catalog-allowlist, Increment 5) — documented
the catalog-as-allowlist ingest gate, the two escape-hatch widgets
(`model-viewer` + `embed`), and the `embed` URL/host policy.

Updated: 2026-06-11 (gap-3 outcome VALUE metering) — documented the
Outcome Metering section: the binding-level `outcome` / `outcome_value` /
`outcome_unit` declaration, the existing `GET /outcomes` count surface,
and the new `GET /outcomes/meter` aggregation surface that sums billable
value by unit per workspace over a since/until window. Invoicing /
payment / pricing-rules / clawback remain deferred.

Updated: 2026-06-15 (feat/invoke-tool-v1) — documented the now-live
POST /pockets/{id}/tools/run (was a fail-closed stub) and the new
owner-only PUT /pockets/{id}/backend/tool-policy. The backend summary now
carries `allowed_tools` (the per-pocket tool allowlist) alongside
`allowed_writes`.
Updated: 2026-06-15 (feat/invoke-tool-v1, v2) — the WRITE path is now live.
A connector READ tool still fires immediately; a WRITE tool is no longer
refused with `code: blocked` — it is PROPOSED for human approval through the
Instinct gate and returns `code: instinct_pending` with a `proposed_action_id`
(the write fires only when a human approves it in The Tray).

Updated: 2026-06-20 (feat/workspace-jobs, pp#1459) — documented the
workspace jobs primitive: the `kind: "job"` variant of
POST /pockets/{id}/actions/run that enqueues a server-side async job
instead of firing an HTTP write, and the new
GET /workspaces/{ws}/jobs/{job_id} status poll. Jobs run on the shared ARQ
worker under the synthetic `system:workspace_job` identity and merge their
results back into the pocket's `state` over the live update bridge.

Updated: 2026-06-26 (ART-1) — documented the Files — Versioned Writes
section: POST /files/write, PUT /files/{id}, and the two version-history
reads (GET /files/{id}/versions[/{vid}]). The write path archives each
prior blob as a FileVersionDoc and bumps a per-file content_version counter;
every read is workspace-scoped.
Updated: 2026-06-26 (ART-4) — documented the Agent Artifact Delivery
(deliver_artifact) in-process MCP tool: routes a built file/dir through the
workspace upload pipeline (file as-is, dir zipped) and returns a presigned
download URL; jail-scoped path safety; POCKETPAW_DELIVER_MAX_MB cap.
Updated: 2026-07-02 (NE-4b / NE-5b) — documented the Sites — Native Editing
section: POST /sites/by-pocket/{id}/leaf-edits (splice editor edits into the
svelte source via the apply-leaf-edit CLI and persist as a Branch draft, no
rebuild — dynamic-source split + input-keyspace confinement) and GET
/sites/by-pocket/{id}/native-artifact (serve the armed build's body_html + css
for shadow render — per-GET arm-build cost, path-traversal-guarded CSS reader).

Updated: 2026-09-02 (SA-4) — documented the Sites — Visitor Analytics section:
GET /sites/{site_id}/analytics, the read half of the pageview counter SA-1/SA-2
deploy. The response leads with a status because three different customer
situations otherwise render as the same panel of zeros (not on a plan that buys
analytics, on one but not republished since, and genuinely no traffic), and a
FAILED read is an error response rather than a fourth status so an outage cannot
arrive looking like a quiet week. Every metric is null unless the status is ok.
Updated: 2026-08-24 (SP-2) — GET /sites/by-pocket/{id}/native-artifact has two
response shapes now. A cold miss no longer builds in the API container (there is
no bun there, so it 5xx'd as sites.generator_failed on every cold preview); it
queues the armed build in an ephemeral Daytona sandbox and returns
build_status / build_reason / build_job_id with body_html and css empty. A cache
hit is unchanged and still costs nothing. An enqueue that fails is a 503
(sites.preview_build_unavailable), never a job id for a job nobody will run.

Updated: 2026-07-27 (feat/growth-g1) — documented the Growth — Prospects
section: workspace-scoped prospect store under /growth/prospects (create /
get / list with tier|status|source filters / update), domain-deduped per
workspace, cross-tenant ids 404. First slice of the /growth outbound engine.

Updated: 2026-07-27 (feat/growth-g2) — added POST /growth/prospects/bulk to
the Growth — Prospects section: batch ingestion (max 500 rows) via the
upsert-by-domain seam, per-row errors, idempotent re-runs.

Updated: 2026-07-27 (feat/growth-g3) — added the Growth — Drafts section:
per-channel outreach drafts on a prospect (POST /growth/prospects/{id}/drafts,
GET /growth/drafts with prospect|channel|status filters, POST
/growth/drafts/{id}/status) with the enforced lifecycle
draft→proposed→approved→sent, sent→replied, non-terminal→rejected; illegal
moves 422 draft.illegal_transition.

Updated: 2026-07-27 (feat/growth-g5) — documented email dispatch: the
growth.dispatch job's email branch now sends through the per-workspace
Mailtrap connector, re-checks that the draft is still approved before any
provider call, writes a MessageLog audit row per attempt, and flips the draft
to sent through the existing gate seam. Also documented the retryable failure
path (failed row, draft stays approved, nothing raises) and the required
GROWTH_SENDING_DOMAIN config plus why outreach never rides the apex.

Updated: 2026-07-27 (feat/growth-g6) — documented the Growth — WhatsApp
dispatch section: the growth.dispatch job's channel="whatsapp" branch sends via
MSG91 behind a HARD prospect.opted_in guard (not opted in ⇒ no provider call at
all, typed error, blocked send-log row, draft left approved), the guard order,
the per-attempt WhatsAppSendLog compliance record, connector-state credential
resolution (no env fallback for the authkey), the fail-closed inbound webhook
POST /growth/webhooks/msg91, and the GROWTH_WHATSAPP_MAX_PER_HOUR /
GROWTH_MSG91_WEBHOOK_SECRET environment variables.

Updated: 2026-07-27 (feat/growth-g4) — documented the Instinct send gate:
POST /growth/drafts/{id}/propose files a gated _growth_send proposal and
flips the draft to proposed; the status route now refuses the gate-owned
approved/sent targets with 403 draft.gate_required. Approve (single or bulk)
flips the draft to approved and enqueues growth.dispatch on the growth arq
queue; reject flips it to rejected. Nothing sends without an approval.
Security review follow-up: documented per-route growth RBAC
(growth.read / growth.write MEMBER, growth.manage ADMIN on the propose verb)
and the fact that a _growth_send blob can only be minted by this route —
the generic POST /instinct/actions refuses reserved gated parameter keys.

Updated: 2026-07-28 (feat/growth-mcp) — added the Growth — the agent surface
section: the nine pocketpaw_growth in-process MCP tools the chat agent on the
/growth rail drives, the table of how that surface is narrower than the HTTP
one, and why the agent's reach ends at proposed (no send tool, no status
argument, no route to gate_transition). Also added PATCH /growth/drafts/{id}
— edit a draft's copy while it is still `draft`; anything past that is
403 draft.not_editable, because from proposed on the stored body is what the
Tray shows and what the worker sends.

Updated: 2026-07-28 (feat/growth-api-scale) — the prospect list grew a scale
surface. BREAKING: GET /growth/prospects now returns
{items, next_cursor, total} instead of a bare array. Added q search across
name/company/domain/research_brief, four sort modes (tier ordering is the
declared rank a-b-c-unqualified, not lexicographic), keyset cursor
pagination, GET /growth/prospects/facets (per-tier/status/source counts,
each block excluding its own filter), and POST /growth/drafts/propose-batch
(<=100 ids, each proposed through the existing Instinct gate, per-draft
error entries, growth.manage).

Updated: 2026-07-28 (feat/growth-projects) — a prospect can now be just a
domain: name and company are optional on create and on a bulk row, defaulting
to "" (not yet known), and nothing renders an empty value as "unknown".
domain stays required and still normalises. Added project_id — the client
container from cloud/projects — on create, on PATCH (three-valued: omit to
leave alone, an id to reassign, "" to clear) and as an optional filter on the
list, the facets and the search; a foreign project is 404 project.not_found.
The email dispatcher resolves a per-project sender identity (from-name /
from-address / reply-to, per-field fallback to the workspace default) via the
MAILTRAP_PROJECT_SENDERS and MAILTRAP_REPLY_TO connector keys, and the daily
follow-up sweep works one client's threads at a time so their nudges go out
under that identity.

Updated: 2026-07-27 (feat/growth-g8) — added the Growth — LinkedIn Queue
section: GET /growth/linkedin/queue (proposed/approved linkedin drafts joined
with prospect context, ?format=md for a paste-ready markdown export) and
POST /growth/linkedin/{draft_id}/mark-sent (record a manual send via the G-3
machine). Deliberately manual — no LinkedIn API. Integration note: mark-sent
rides the gate seam (sent is gate-owned since G-4) and takes growth.manage.

Updated: 2026-07-27 (feat/growth-g7) — added the Growth — Follow-ups section:
the daily `growth.followup_sweep` arq cron on the `growth` queue turns a send
that went quiet into a `variant: "follow_up"` draft filed back through the
same `_growth_send` gate (proposed, never approved or sent), capped at
GROWTH_FOLLOWUP_MAX per prospect+channel after which the prospect is retired
to `dead`. Documented both env knobs (GROWTH_FOLLOWUP_DELAY_DAYS,
GROWTH_FOLLOWUP_MAX).

Updated: 2026-07-11 (feat/real-pipeline-s1) — documented the Fabric — Transform
Mappings section: GET/POST/DELETE /fabric/ingest/mappings (author the
workspace's source→Fabric mappings, now with a "connector" source_kind that
dispatches through the OSS FABRIC_INGESTORS registry — gcalendar first) and
POST /fabric/ingest/run (run one mapping immediately; misconfiguration reports
status="error" in the body, never a 5xx).

Updated: 2026-08-01 (AM-6 desktop) — documented POST /auth/social/link/complete
and the desktop link handoff. Worth knowing before touching it: a Tauri webview
carries no cookie for our origin, so the callback cannot authenticate the
acting user and does NOT attach on flow=desktop. It parks the identity behind
a one-time code and the app redeems it under its bearer, where the account can
actually be proved. Also records the /oauth-callback contract that separates a
desktop LINK (link=) from a desktop SIGN-IN (xc=).

Updated: 2026-08-01 (AM-2..AM-6, feat/auth-social-providers) — documented the
Social Sign-In & Connected Accounts section: the four sign-in endpoints
(providers / login / callback / exchange) and the three connected-accounts ones
(GET identities, POST {provider}/link, DELETE identities/{provider}), the nine
refusal codes the frontend maps to copy, and the security model — why a
provider-VERIFIED email is the only join key on sign-in, and why the link path
deliberately does not use email as a join key at all. Also records two things
that are easy to get wrong and cost real time here: cloud routes authenticate
at the route level, because the global AuthMiddleware does not gate /api/v1/,
so a new cloud route needs its own guard; and localhost_auth_bypass defaults to
TRUE, so verifying an auth change with curl from your own machine cannot tell
you whether the guard is there.

Updated: 2026-07-22 (SHIP-4, feat/ship-4-agent-surface) — the two DELETE routes
now file REAL Instinct proposals (kind `_ship_action`), executed on approval by
``ship.executor`` with an execute-time `ship.manage` re-check; documented the
`pocketpaw_ship` MCP agent surface and how it is narrower than the HTTP one (a
prod deploy proposes rather than deploys).

Updated: 2026-07-22 (SHIP-3, feat/ship-3-cloud-entity) — documented the
Ship — Managed Deploys section: the workspace-scoped /ship surface for
provisioning a box, registering and deploying an app, routing a domain,
creating a linked database, and reading logs + box health. The two DELETE
routes PARK a teardown for human approval and never destroy anything.
Updated: 2026-08-04 (feat/knowledge-wiki-api) — documented the Knowledge —
Living Wiki API section: the enriched GET /knowledge/articles rows, the new
GET /knowledge/articles/{id}, GET /knowledge/stats, GET /knowledge/uploads,
and the two reingest routes (POST /knowledge/reingest,
POST /knowledge/reingest-upload) that re-run content through the hardened
KnowledgeService ingest funnel. Design doc:
docs/design/drafts/2026-08-04-knowledge-wiki-redesign.md (workspace repo).
-->

# Cloud REST API Reference

This file documents cloud (`pocketpaw-ee`) REST endpoints that do not yet
have a dedicated page under `docs/api/`. All cloud endpoints require a
valid enterprise license and an authenticated workspace context.

That second requirement is enforced **per route, not by the global
middleware**, which does not gate `/api/v1/`. If you are adding or reviewing a
cloud route, read "Cloud routes authenticate at the route level" in the Social
Sign-In section below — the route's own guard is what carries it.

## Pockets — Backend Binding & Live Data Sources

RFC 04 alpha. A pocket can be bound to **one** external backend (base URL +
auth credential). Its `rippleSpec.sources` declares read-only `GET`
bindings; a server-side executor runs them and returns the JSON results.

The credential is stored in a **separate, encrypted collection**
(`pocket_backend_credentials`) — never inside the `Pocket` document and
never inside `rippleSpec`, so the spec stays shareable and secret-free.

### `PUT /pockets/{pocket_id}/backend`

Bind a pocket to one backend. Requires pocket **edit** access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `base_url` | string | Required. Must be `https://` and point to an external host (no loopback / RFC1918 / link-local). |
| `auth_type` | string | One of `bearer`, `api_key`, `basic`, `none`. |
| `auth_token` | string | The secret. Encrypted at rest; never returned. Required unless `auth_type` is `none`. |
| `auth_header` | string \| null | Custom header name for `api_key` auth. Defaults to `X-Api-Key`. |

Response `200`:

```json
{
  "base_url": "https://api.example.com",
  "auth_type": "bearer",
  "configured": true,
  "allowed_writes": [],
  "allowed_tools": []
}
```

The token is never echoed back. A non-https or internal `base_url` yields
a `400`. `allowed_writes` is the per-pocket write allowlist (RFC 05 M2a) —
empty by default, so no write action can fire until an owner sets a policy
via `PUT /pockets/{id}/backend/write-policy`. `allowed_tools` is the
per-pocket tool allowlist (feat/invoke-tool-v1) — also empty by default, so
no `invoke_tool` can fire until an owner sets a policy via
`PUT /pockets/{id}/backend/tool-policy`.

For `basic` auth, send `auth_token` as the raw `user:pass` credential —
the server base64-encodes it into the `Authorization: Basic` header. Do
not pre-encode it yourself.

### `GET /pockets/{pocket_id}/backend`

Read the pocket's backend binding summary. Requires pocket **edit** access
(owner or editor) — backend config metadata is owner/editor-facing,
consistent with the `PUT` route. Viewers receive a `403`.

Response `200`:

```json
{
  "base_url": "https://api.example.com",
  "auth_type": "bearer",
  "configured": true,
  "allowed_writes": [{ "method": "POST", "path_pattern": "/leases/*/renew" }],
  "allowed_tools": [{ "tool": "connector:github:list_issues" }]
}
```

Returns `404` when the pocket has no backend configured. The token is
never included in the response. `allowed_writes` carries the current
write allowlist (RFC 05 M2a); `allowed_tools` carries the current tool
allowlist (feat/invoke-tool-v1).

### `DELETE /pockets/{pocket_id}/backend`

Revoke the pocket's backend binding — deletes the stored (encrypted)
credential. Requires pocket **owner** access.

Returns `204 No Content`. Idempotent: deleting when no backend is
configured still returns `204`. The removal is written to the audit log.

### `POST /pockets/{pocket_id}/sources/run`

Run the pocket's read-only `rippleSpec.sources` (GET bindings) against its
configured backend. Read access mirrors `GET /pockets/{pocket_id}` —
deliberately **not** gated on edit access. Any pocket reader may run the
already-authored sources: a viewer of a shared live pocket triggering the
`pocket_open` refresh is the core shared-dashboard UX. A viewer cannot
change the backend or the source paths (both are edit-only), so the SSRF
hardening plus the immutable, edit-authored source list bound the risk.

Request body (all fields optional):

| Field | Type | Notes |
|-------|------|-------|
| `trigger` | `pocket_open` \| `manual` \| null | Run only sources whose `refresh` list contains this trigger. |
| `source` | string \| null | Run a single named source regardless of refresh policy. |

When both are omitted, every source in the spec runs.

Response `200`:

```json
{
  "ran": [
    { "source": "prs", "bind": "prs", "value": [ { "id": 1, "title": "PR one" } ] }
  ],
  "errors": [
    { "source": "issues", "error": "backend returned status 503", "code": "http_error" }
  ]
}
```

`bind` is the dotted state path the value should be written to, with a
leading `state.` stripped. The hydrated state is delivered **in this
response body** — there is no `pocket_mutation` SSE emit, because the run
endpoint is a standalone REST call outside any SSE-stream context. The
caller applies the results to the pocket's ripple state.

Returns `400` when the pocket has no backend configured.

**Security.** This endpoint is an SSRF boundary. The executor re-validates
the base URL, rejects absolute-URL paths / `..` traversal / cross-host
joins, runs a DNS check against internal IPs, disables redirects, applies
tight timeouts, caps response bodies at 512 KB, sanitizes error messages,
and rate-limits to 10 runs per `(pocket, user)` pair per minute. Every run
is written to the audit log (actor, pocket, status, query-stripped base
URL) — the credential token is never logged.

## Pockets — Write Actions

RFC 05 M2a. A pocket's `rippleSpec.actions` declares **write** bindings
(`POST` / `PUT` / `PATCH` / `DELETE`) — the write half of the data layer.
A write has blast radius a read does not, so two controls sit on top of the
SSRF guards the read executor already enforces:

- **The per-pocket write allowlist** (`allowed_writes` on the backend
  config). A write whose `(method, path)` does not match an allowlist entry
  is rejected server-side before any call leaves PocketPaw. The allowlist
  lives **outside** `rippleSpec`, in the same human-configured store as the
  credential — the agent authors bindings, a human authorizes the *class*
  of writes. The allowlist is **empty by default**: fail-closed, no write
  fires until an owner sets a policy.
- **Instinct-reject (fail-closed).** An action whose declaration carries a
  truthy `requires_instinct` is rejected with `code: instinct_required` and
  makes no call — M2a has no Instinct approval surface, so it refuses
  rather than silently honor-then-ignore the flag. M2b wires the approval
  routing.

### `PUT /pockets/{pocket_id}/backend/write-policy`

Set the pocket's write allowlist. Requires pocket **owner** access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `allowed_writes` | array | List of `{method, path_pattern}` rules. Replaces the whole list. An empty list is valid — it revokes every write. |

Each rule: `method` is one of `POST` / `PUT` / `PATCH` / `DELETE`;
`path_pattern` is a glob (`/leases/*/renew` allows `POST /leases/42/renew`).
Omitting a verb means no action with that verb can ever fire.

Response `200`: the backend summary, including the updated `allowed_writes`.

Returns `400` when the pocket has no backend configured — a write policy
with no backend to apply it to is meaningless. The change is audit-logged.

### `POST /pockets/{pocket_id}/actions/run`

Run one declared `rippleSpec.actions` write action against the pocket's
configured backend. Access is **owner or explicit `shared_with` only** —
deliberately narrower than the source-run route: a write has blast radius,
so a workspace-visible pocket does **not** grant run access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `action` | string | Required. The action's name (its key in `rippleSpec.actions`). |
| `path` | string | Required. The resolved path — Ripple's `{...}` expression resolver runs client-side at click time. |
| `params` | object | Optional. The resolved request body. |
| `idempotency_key` | string \| null | Optional. When omitted the server generates one so a write retried after a timeout cannot double-submit. |

The HTTP `method` is **read server-side** from the persisted action entry —
the client never picks the verb. The write fires only if the owner
allow-listed the `(method, path)`.

Response `200` (success):

```json
{
  "ok": true,
  "action": "mark_renewed",
  "status": 201,
  "response": { "id": 42, "status": "renewed" },
  "on_success": [{ "action": "run_source", "source": "leases" }],
  "on_error": []
}
```

Response `200` (rejected): `ok` is `false`, with an `error` message and a
`code`. Codes: `action_not_found`, `bad_binding`, `instinct_required`,
`rate_limited`, `bad_base_url`, `bad_path`, `bad_host`, `not_allowed`,
`redirect`, `http_error`, `too_large`, `timeout`, `request_failed`,
`error`. The result is delivered **in this response body** — there is no
`pocket_mutation` SSE emit; the client applies the `on_success` /
`on_error` reconcile handlers.

Returns `400` when the pocket has no backend configured; `403` when the
caller is neither the owner nor in `shared_with`.

**Security.** The write executor inherits every SSRF / timeout / size /
redirect guard from the shared `_http_guard` module (the same code the
read executor uses), then layers the write allowlist check, the
fail-closed instinct-reject, an `Idempotency-Key` header on every call,
and a write-specific rate limit — 20 writes per `(pocket, user)` per
minute, a **separate** counter from the read budget. Every run (including
every rejection) is written to the audit log; the credential token is
never logged.

## Pockets — Tool Invocations (`invoke_tool`)

feat/invoke-tool-v1. `invoke_tool` is the click-driven tool verb for pocket
FLOW-BUTTONs. A button fires `{action: "invoke_tool", tool, args}`; Ripple
resolves the `args` client-side and the host POSTs to the route below. Like
write actions, it carries a per-pocket allowlist that lives **outside**
`rippleSpec` — a human authorizes which tools a pocket may run, so a
compromised or hallucinated spec cannot grant itself a tool.

A grant's `tool` is one of:

- a connector action, `connector:<name>:<action>` (e.g.
  `connector:github:list_issues`), or
- a built-in tool name (e.g. `web_fetch`) — reserved; the built-in registry
  dispatch is a v1.x follow-up, so a built-in grant currently returns
  `code: unknown_tool`.

**Read/write split.** A connector grant is dispatched through the shared
connector executor (`connectors.service.execute`). A **read** action
(`trust=auto`) fires immediately and returns its data. A **write** action
(`trust=confirm`/`restricted`) **never** runs inline — it is **proposed for
human approval** through the Instinct gate. The route files a pending Instinct
Action (via `propose_external_action`) and returns `code: instinct_pending`
with a `proposed_action_id`; the connector write fires only when a human
approves the Action in The Tray, at which point the instinct router runs the
existing execute-on-approve path (`execute_approved_external_action` →
`connectors.service.execute`, re-validated for workspace + params + idempotency).
The client's `on_success` handler branches on `code == "instinct_pending"` to
show a "sent for approval" state and can watch the `proposed_action_id`.

### `PUT /pockets/{pocket_id}/backend/tool-policy`

Set the pocket's tool allowlist. Requires pocket **owner** access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `allowed_tools` | array | List of `{tool}` grants. Replaces the whole list. An empty list is valid — it revokes every tool (fail-closed). |

Each grant: `tool` is a built-in tool name or a connector action
`connector:<name>:<action>` (`min_length=1`). Omitting a tool means that
tool can never fire.

Response `200`: the backend summary, including the updated `allowed_tools`.

Returns `400` when the pocket has no backend configured — a tool policy with
no backend to apply it to is meaningless. The change is audit-logged
(`pocket.backend.tool_policy`).

### `POST /pockets/{pocket_id}/tools/run`

Invoke a named tool with the resolved args. Access is **owner or explicit
`shared_with` only** — a tool invocation has the same blast radius as a write
binding, so a workspace-visible pocket does **not** grant run access.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `tool` | string | Required (`min_length=1`). The tool name — a built-in name or `connector:<name>:<action>`. |
| `args` | object | Optional. The resolved tool arguments (Ripple's `{...}` resolver runs client-side at click time). |

The allowlist is read server-side off the backend-credential row, never from
the spec. A pocket with no backend, no grants, or a tool not on the list
returns `code: not_allowed` — fail-closed.

Response `200` (connector read fired):

```json
{
  "ok": true,
  "tool": "connector:github:list_issues",
  "status": 200,
  "response": [{ "number": 1, "title": "first issue" }],
  "on_success": [],
  "on_error": []
}
```

Response `202` (write proposed for approval):

```json
{
  "ok": true,
  "tool": "connector:github:create_issue",
  "status": 202,
  "code": "instinct_pending",
  "proposed_action_id": "act-7f3c…",
  "response": {
    "action_id": "act-7f3c…",
    "proposed_action_id": "act-7f3c…",
    "status": "pending_approval",
    "connector": "github",
    "action": "create_issue"
  }
}
```

The write does **not** run at this point. The pending Action appears in The
Tray; on approve, the instinct router fires the connector write through the
existing execute-on-approve path. On reject, the write never runs.

Other rejection codes (`ok: false`): `not_allowed` (tool not on the
allowlist / no backend), `not_reachable` (connector not bound to this
pocket), `unknown_tool` (the connector has no such action, or a built-in
grant has no registry implementation yet), `bad_grant` (malformed
`connector:` grant), `propose_failed` (the write could not be filed for
approval — e.g. the Instinct store was unavailable; the write is **not** run
inline as a fallback), plus any connector-side `CloudError` code. The result
is delivered **in this response body** — there is no `pocket_mutation` SSE
emit; the client applies the `on_success` / `on_error` reconcile handlers.

Returns `403` when the caller is neither the owner nor in `shared_with`;
`404` when the pocket is not in the caller's scope.

**Security.** A connector grant re-checks the pocket/workspace bind
(`is_connector_bound_to_pocket`, the tenant boundary) and the action's trust
level before any call leaves PocketPaw; the connector path's outbound URL is
bounded by the connector definition, not by a spec-supplied URL. A
URL-taking built-in tool, when that path lands, must route through the same
`_http_guard` SSRF boundary the read/write executors use.

## Pockets — Jobs

pp#1459. A read (a source) fetches data into the canvas and a write (an
action) sends one HTTP call. A **job** is the third kind: a named,
server-side async unit of work that runs for minutes, computes a result,
and merges it back into the pocket's `state` so an open canvas updates
live. Because a job runs on the shared ARQ worker rather than in the
request, it survives the user closing the browser, and it emits its update
over the same cross-process bridge the resumable chat runs use.

A job is declared as an action with `kind: "job"` in `rippleSpec.actions`:

```json
{
  "actions": {
    "score_applications": {
      "kind": "job",
      "job": "score_applications",
      "params": { "batch_size": 20, "connector": "snctm-api" },
      "label": "Score Next Batch",
      "requires_instinct": false
    }
  }
}
```

The `job` value is the name of a callable registered in the workspace job
registry. An action with no `kind` keeps the existing write-action
behavior, so jobs are additive.

### Trigger: `POST /pockets/{pocket_id}/actions/run` with `kind: "job"`

The same endpoint and the same owner-or-`shared_with` access as a write
action. When the named action's `kind` is `"job"`, the route enqueues the
job instead of making an HTTP call.

Behavior:

- An unknown job name returns `400 job.unknown`.
- The server reads the params from the **persisted action declaration**, not
  from the request. A non-empty client `params` is rejected with
  `400 job.params_not_accepted` so a click can never widen a job's scope.
- A param key that looks credential-bearing (it contains `token`, `api_key`,
  `secret`, and the like, at any nesting depth) is rejected with
  `400 job.params_forbidden`. Jobs read workspace credentials server-side and
  never accept tokens through params.
- `requires_instinct: true` is rejected with
  `400 job.instinct_not_yet_supported`. The Instinct approval path for jobs
  lands in a later version; until then a job that asks to be gated refuses
  rather than run ungated.

Response `200` (enqueued):

```json
{ "ok": true, "code": "job_enqueued", "job_id": "665a1f2e9c3b4a0012ab34cd" }
```

The client polls the status endpoint below. The result arrives on the canvas
as a live `state` update when the job finishes; it is not in this response
body.

### `GET /api/v1/workspaces/{workspace_id}/jobs/{job_id}`

Poll a job's status. Requires workspace membership. The job document is
re-fetched by id and its workspace is re-checked, so a job id from another
workspace returns `404` rather than leaking its existence.

Response `200`:

| Field | Type | Notes |
|-------|------|-------|
| `job_id` | string | The job document id. |
| `status` | string | `queued`, `running`, `done`, or `failed`. |
| `error` | string \| null | The failure message when `status` is `failed`. |
| `created_at` / `started_at` / `ended_at` | string \| null | Lifecycle timestamps. |

The computed result is not returned here. On success the worker has already
merged it into the pocket's `state`; on failure it writes a
`{action}_status: "failed"` marker into `state` so the triggering button
stops spinning without a poll.

**Security.** Every job runs under the hardcoded synthetic identity
`system:workspace_job`, never the triggering user, and that identity is not
addressable from any request. A job result may write **only** `state`; a
result that touches `ui`, `actions`, `sources`, or `shape` is rejected and
the job is marked failed, so a job can never rewrite the template it runs
under. The writeback re-asserts that the target pocket belongs to the job's
workspace before it writes (fail-closed), and the worker enforces a timeout
(`POCKETPAW_JOB_TIMEOUT_SECONDS`, default `900`); a timed-out job writes the
same failed-state marker. Enqueue and failure are written to the audit log.

## Pockets — Template Reconcile

A pocket created from a template stores its `template_slug`. Re-running an
install/deploy script re-applies the template and **clobbers instance edits**.
Reconcile fixes that: it re-applies only the **template-owned** regions of the
source template while preserving the **instance-owned** regions.

| Region | Owner | Reconcile behavior |
|--------|-------|--------------------|
| `rippleSpec.ui` | template | overwritten from the template |
| `rippleSpec.actions` | template | overwritten from the template |
| `rippleSpec.sources` | template | overwritten from the template |
| `rippleSpec.shape` | template | overwritten from the template |
| `rippleSpec.state` (rows, `selected_id`, `pending_proposal`, …) | instance | never touched |
| pocket name / owner / team / visibility | instance | never touched |

Both endpoints accept standard cookie / bearer auth, or the loopback
internal-token bypass (the same one `GET /pockets/{id}` and `/spec/merge`
accept) so the `pocketpaw pocket reconcile` CLI can authenticate locally. The
service re-checks read (preview) / edit (apply) access on the resolved
identity.

### `POST /pockets/{pocket_id}/reconcile/preview`

Dry-run a reconcile — report what **would** change, write nothing. No
`PocketUpdated` event is emitted.

Response `200`:

```json
{
  "pocket_id": "663...",
  "template_slug": "applications-triage",
  "template_owned_regions": ["ui", "actions", "sources", "shape"],
  "changed_regions": ["ui"],
  "unchanged_regions": ["actions", "sources", "shape"],
  "preserved_regions": ["state"],
  "has_changes": true
}
```

Returns `422` (`reconcile.no_template`) when the pocket has no `template_slug`,
`422` (`reconcile.template_unresolved`) when the slug no longer resolves on
disk, `403` when the caller can't read the pocket, `404` for a missing /
cross-tenant pocket.

### `POST /pockets/{pocket_id}/reconcile/apply`

Apply the reconcile — re-write the template-owned regions, preserve the
instance-owned regions, persist through the same spec write path as a normal
edit (so the spec is normalized + validated and a `PocketUpdated` event fires).
**Edit access required.**

Response `200`:

```json
{
  "ok": true,
  "skipped": false,
  "diff": { "...": "the same diff shape as preview" },
  "pocket": { "...": "the updated pocket wire dict" }
}
```

When the pocket already matches its template the write is **skipped**
(`"skipped": true`, no `pocket` field, no event). Error codes mirror the
preview route, plus `403` when the caller lacks edit access — enforced even on
the skipped no-write path so a non-editor cannot probe sync state.

## Pockets — Duplicate a Site

### `POST /pockets/{pocket_id}/duplicate`

Copy a site pocket (`type: "site"`) into a new, independent site pocket owned by
the caller. The original is not modified. Read access is enough: the caller must
be able to read the source pocket under the same within-workspace rule as
`GET /pockets/{id}`.

The copy carries exactly the authored site: `engine`, `pattern`, `rippleSpec`,
`source` (the whole map, including `paw.dependencies.json` and a dynamic site's
`objects` / `sources` / `actions` / `auth` keys) and `keepsClientBundle`. Nothing else is copied: no sharing, team,
agents, widgets, tools, connector allowlist (the copy allows none), surface
profile or project, and nothing from the source's Site row (slug, domains, D1
database, deployment).

The copy gets its own fresh DRAFT Site row so it lists in the sites gallery. That
row is never built or deployed and nothing is billed. Visibility: a private
source gives a private copy; any other source (workspace or public) gives a
workspace-visible copy, so a public site is never re-published. Emits
`PocketCreated` and `site.created`.

Source gate: the copy is never less gated than a new pocket. It is gated if the
source is gated or if the source gate is on at copy time, so an older exempt
pocket does not pass its exemption to a copy.

Audit: a successful copy writes one `pocket.duplicated` workspace audit event
(`actorId` = caller, `targetId` = new pocket id, metadata `source_pocket_id` and
`source_visibility`). The write is best-effort; a refused duplicate writes none.

Request body (optional):

```json
{ "name": "Spring launch" }
```

`name` defaults to `"<source name> (copy)"`, with the source name trimmed so the
result fits the 100-character limit.

Response `200`: the new pocket's wire dict, the same shape `POST /pockets`
returns.

Returns `404` for a missing or cross-tenant pocket, `403` when the caller can't
read a private pocket, `422` (`pocket.not_a_site`) when the pocket is not a site,
and `402` (`billing.pocket_limit`) when the workspace is at its plan's pocket cap.
Returns `403` (`plan.feature_denied`) when the workspace's plan does not include Sites.

## Paw Partners — profile and clients

A partner is a workspace that resells sites to local shops. A **client** is a
shop-owner record inside the partner's workspace (not a workspace, not a user).
All paths are under `/api/v1`. Errors use the standard CloudError JSON shape.

### `GET /partners/me`

The caller's workspace partner profile and where it stands on the volume tiers:

```json
{
  "status": "active", "tier": "silver", "footer_name": "Ravi Prints",
  "billing_country": "IN", "founding": false, "joined_at": "2026-10-01T09:00:00Z",
  "active_sites": 12, "lifetime_sites_sold": 15,
  "next_tier": {"name": "gold", "at": 25, "remaining": 13},
  "benefits": {"wholesale_discount_pct": 10.0, "commission_pct": 30.0}
}
```

`status` is `applied` | `active` | `suspended`. `next_tier` is `null` at gold.
`benefits.commission_pct` is the tier rate; a founding partner's site earns
max(40%, that rate) for 24 months after the site's first client payment. Needs
`fabric.read`. **404** when the workspace is not a partner (a suspended partner
can still read it).

### Volume tiers and milestone rewards

The tier is worked out from **active sold sites**: sites with a client
(`partner_client_id`) on an active paid plan, whoever paid (the wallet or the
client). The same count is `active_sites` on `/me` and on the summary.

| Tier | Active sold sites | Wholesale discount | Commission |
|---|---|---|---|
| bronze | 0–9 | 0% | 25% |
| silver | 10–24 | 10% | 30% |
| gold | 25+ | 20% | 35% |

- **Up right away, down once a month.** After every sale and every paid client
  payment the tier is recomputed and only raised. The sale or payment that
  crosses a threshold is priced at the old tier; the next one gets the new
  discount or rate. A monthly review (the first sweep tick of each UTC month)
  recomputes with downgrade, so a lapse never costs a tier mid-month.
- **Discount:** the partner pays floor(price × (1 − discount)) in whole USD. It
  applies wherever the wallet pays for a partner plan: the sale, a plan change and
  the renewal, and the offers show it. IN `site_year` is 1,700 / 1,500 / 1,300
  credits; elsewhere 2,900 / 2,600 / 2,300. `staff_year` IN 5,600 / 5,000 / 4,400;
  elsewhere 8,900 / 8,000 / 7,100.
- **Who owns the tier:** the system. The operator PUT below can still set `tier`
  (a manual promotion); it stands until the next recompute moves it, which is the
  next upgrade or the next monthly review.

**Milestone rewards** are one-time credit grants on **lifetime distinct sites
sold**: 1st site +200, 10th +1,000, 25th +3,000, 50th +7,500 credits. A site
counts once the wallet paid a partner plan for it or its client's payment earned a
commission. Lifetime never goes down: a refund, a lapse or a deleted site does not
lower it, does not re-trigger a milestone and does not take a reward back. Each
milestone is granted once per workspace (ledger cause `partner_reward`, key
`partner_reward:<workspace_id>:<sites>`), checked after every sale, paid client
payment and monthly review.

### `GET /partners/clients` · `POST /partners/clients`

List (newest first) or create clients. Create body: `name`, `whatsapp` (E.164,
`^\+[1-9]\d{7,14}$`), optional `whatsapp_opt_in_at`, `gstin` (upper-cased, then the 15-char GSTIN
pattern), `notes`. Returns
the client (`id`, `workspace_id`, fields, `created_at`, `updated_at`); create is
**201**. **403** `partner.not_active` unless the workspace has an ACTIVE profile. Reads
need the `fabric.read` workspace action and writes `fabric.write` (member+); a
non-member is **403**.

### `PATCH /partners/clients/{client_id}` · `DELETE /partners/clients/{client_id}`

Partial update (only sent fields change; an empty body writes nothing) or delete
(**204**). Same 403 rule; a client from another workspace is **404**.
Changing `whatsapp` clears `whatsapp_opt_in_at` unless the same PATCH sets it:
consent was given for the old number. New-lead WhatsApp messages go only to an
opted-in number (see "Partner leads on WhatsApp").

**Delete archives.** The client disappears from every read, but the org journal
keeps its full history, including the WhatsApp number and GSTIN. There is no
erasure path yet.

### `PUT /platform/workspaces/{workspace_id}/partner` · `DELETE /platform/workspaces/{workspace_id}/partner`

Platform operators only (`platform.partners.write`, OPERATOR rung, interactive
session cookie; bearer tokens are refused). PUT requires a body: `status`,
`footer_name`, `reason` (required, non-blank), optional `tier` (`bronze` |
`silver` | `gold`, default `bronze`; system-owned, see the tier section above),
`billing_country` (ISO-2, default `IN`,
upper-cased), `founding`, `joined_at` (kept from the previous profile when
omitted). A missing body is **422**. DELETE takes `{"reason": "..."}` and clears
the profile. Both return `{workspace_id, partner}` and write a platform audit row.

**Billing effect:** while the profile is `active`, the per-site billing seams
(`billing.enforcement.sites_enforced`) enforce for that workspace even with
`billing_enforced` and `sites_billing_enforced` off.

### `GET /partners/offers`

The partner-only yearly plans at the caller's `billing_country` price:
`[{sku, period_months, price_credits, conversation_allowance, label,
client_price_minor, client_currency}]` (1 credit = $0.01). Today: `site_year`
(1,700 credits in IN, 2,900 elsewhere) and `staff_year` (5,600 / 8,900; 1,200
conversations a year), at bronze. `price_credits` already has the partner's tier
discount applied. `client_price_minor` + `client_currency` are what the
partner's client pays through a pay link: `site_year` ₹3,588 (`358800`, `INR`) in
IN, $84 (`8400`, `USD`) elsewhere; `staff_year` ₹11,988 / $228. Needs `fabric.read`;
**403** `partner.not_active` unless the profile is ACTIVE. These plans are not in
the public plan catalog.

### `POST /partners/sell`

Body `{client_id, site_id, sku, price_minor?, currency?}`. Sells one of the workspace's sites a partner
plan, paid from the workspace credit wallet. The client must be this partner's
(**404** otherwise), the site must belong to this workspace (**404**), and `sku`
must be a partner plan (**422** `partners.unknown_sku`). It runs the ordinary
paid-publish path for the site's pocket — wallet debit, then redeploy — and
stamps the site's `partner_client_id`. Returns `{site_id, name, url, plan_tier,
renewal_date, partner_client_id, subscription_status, invoice_id}`.

- `price_minor` (optional, integer ≥ 0, ISO-4217 minor units, same ceiling as a
  site receipt) is what the partner charged its client. `currency` is a 3-letter
  code, upper-cased; it defaults to `INR` when the profile's `billing_country` is
  `IN`, else `USD`. Both are validated before the wallet is touched (**422**).
  When the sale records a NEW wallet debit, the price is booked as a PAID receipt
  on the site's client record (the same list `GET /sites/{site_id}/client`
  returns, note `Paw Partners sale · <plan label>`) and its id comes back as
  `invoice_id`. It is the partner's private bookkeeping: nothing bills from it and
  the client never sees it. No new debit means no receipt (`invoice_id: null`): a
  refused sale, the no-op re-sell below, moving back to a plan already paid for
  this period, resuming a site that was set to close, and a sale without
  `price_minor`. The receipt id is derived from the debit, so a double-submitted
  sale books one receipt and both responses carry the same `invoice_id`.
- If the receipt cannot be written after the sale went through, the sale still
  stands and `invoice_id` is `null`. Add the receipt yourself with
  `POST /sites/{site_id}/invoices`.

- Needs `sites.buy_plan` (workspace admin): a sale spends the wallet. **403**
  for a member, and **403** `partner.not_active` without an ACTIVE profile.
- Short wallet: **402** `credits.insufficient`, nothing charged, the site keeps
  its plan.
- Selling the sku a site already holds and pays for is a no-op apart from the
  client stamp: no second debit, no redeploy.
- **409** `partners.site_on_plan` when the site is carried by the workspace
  plan; **409** `partners.foreign_site` when it is a concierge-only (foreign)
  site.
- A site already on a monthly paid plan pays the full year price (no credit for
  the rest of the month) and its year starts today. **409**
  `sites.plan_already_bought_today` if that same change was already charged today.
- The same rules apply to `POST /sites/publish`: moving a site whose year is still
  running to a monthly plan is **409** `sites.period_downgrade_refused`.
- Re-buying a partner plan on a lapsed site goes through the same checks as a new
  sale: **403** `sites.partner_plan_only` if the partner is no longer active.
- Renewals happen on their own: the site-renewal sweep debits the partner price
  when `renewal_date` passes and steps it 12 months, or lapses the site to the
  free tier (still published) when the wallet is short. The renewal is priced at
  the partner's tier on the renewal day. If the partner profile has
  been removed, the renewal reuses the price last paid only when that is a real
  price of the plan (any country, any tier discount); otherwise the site lapses to the free tier (still published),
  never charged at a guessed price.
- A plan change after `renewal_date` has passed (before the renewal sweep runs) is
  charged as one fresh period of the new plan, starting now.

### `POST /partners/pay-link`

Body `{client_id, site_id, sku}`, `sku` one of `site_year` | `staff_year`
(**422** otherwise). Opens a one-time payment link the partner sends its client.
The client pays the plan's list price for one year in the partner's billing
currency (see `client_price_minor` on the offers). Returns:

```json
{"checkout_url": "https://...", "site_id": "...", "sku": "staff_year",
 "amount_minor": 1198800, "currency": "INR"}
```

Nothing changes on the site until the payment lands. When Dodo confirms it, the
site goes on the plan for 12 months (`billing_rail` `client`), its
`partner_client_id` is set, and the partner's wallet gets a commission in credits:
the partner's tier rate (25% / 30% / 35%) of the amount paid net of tax, in US
cents (an INR payment converts through Dodo's USD settlement figure, or the
configured FX rate when that is missing or implausible). Founding partners get
max(40%, the tier rate) on payments made within 24 months of that site's first
client payment. The rate is fixed when the payment lands. The partner's wallet is never charged for a
client-paid site, and the renewal sweep does not renew it: at `renewal_date` the
site drops to the free tier and stays published, unless the client has paid a new
link. A refund or lost dispute within 60 days of the payment takes that payment's
commission back (the wallet can go negative); a partial refund takes the same
share of the commission and leaves the site on its plan, while a full refund, a
lost dispute, or partial refunds that add up to the full amount also drop the
site to the free tier. This still happens if the site has been deleted since.
After 60 days nothing is taken back. A full refund that arrives before the
payment was processed cancels the link: the late payment activates nothing and
earns nothing. A partial refund that arrives that early leaves the link alone (the
payment still activates and pays the full commission) and is logged as an error
for someone to settle by hand. If the partner is no longer active when the
payment lands, the site still gets its year but no commission is paid (the
payment is flagged `partner_inactive` for review).

- Needs `sites.buy_plan` (workspace admin, like `/partners/sell`: a paid link
  changes the site's plan) and an ACTIVE profile (**403** `partner.not_active`).
  Another workspace's client or site is **404**.
- **409** `partners.site_already_paid` when the site is already on a paid plan,
  paid by the wallet or by a client. The one exception is the renewal: a
  client-paid site in the last 30 days of its year can take a link for the same
  plan, and that payment adds a year from the current `renewal_date`.
- **409** `partners.site_on_plan` (carried by the workspace plan),
  `partners.foreign_site` (concierge-only site), `partners.site_not_live` (not
  published yet).
- One open link per site. Asking again for the same plan and price within 7
  days returns the open link instead of making a second one (a double submit
  opens one payment). A link for a different plan while one is open is **409**
  `partners.link_open`; a request racing another one still being created is
  **409** `partners.link_in_progress` (retry).
- The link is charged in exactly its currency (no local-currency conversion at
  checkout).
- Only a payment for a link created here counts, matched on Dodo's payment id.
  If the amount, currency or product Dodo reports differs from the link, a
  discount was applied, the site was bought with the wallet in the meantime, or
  the site is already on a client-paid year of a different plan, the payment is
  flagged: nothing is activated and no commission is paid. Those need a refund
  by hand.
- Changing the plan of a client-paid site through `POST /sites/publish` is
  **409** `sites.client_paid_plan`.

### `GET /partners/sites`

The workspace's sold sites: `[{site_id, name, url, plan_tier, renewal_date,
partner_client_id, client_name, billing_mode}]`, where `billing_mode` is `client`
when the client paid the year through a pay link and `partner` when the wallet
bought it, ordered by `renewal_date` (sites with none come first). Optional
`due_within_days` (0–3660) keeps only sites whose `renewal_date` is within that
many days (a site with no renewal date is never due); without it, every sold
site is returned, including lapsed ones with a null date. Needs `fabric.read` and
an ACTIVE profile.

### `GET /partners/summary`

The partner's earnings at a glance:

```json
{
  "clients": 4, "sites_sold": 6, "active_sites": 5, "renewals_due_30d": 1,
  "spent_credits_30d": 1700, "spent_credits_total": 10200,
  "revenue_30d": [{"currency": "INR", "amount_minor": 299900}],
  "revenue_total": [{"currency": "INR", "amount_minor": 1499500},
                    {"currency": "USD", "amount_minor": 5000}],
  "commission_credits_30d": 3350, "commission_credits_total": 9120,
  "rewards_credits_30d": 200, "rewards_credits_total": 1200,
  "lifetime_sites_sold": 10
}
```

`clients` counts the partner's client records; `sites_sold`, `active_sites`
(`subscription_status` active) and `renewals_due_30d` (same rule as
`GET /partners/sites?due_within_days=30`) count the sold sites. Revenue is the sum
of PAID receipts on the sold sites' client records (sale prices and any receipt
added through `POST /sites/{site_id}/invoices`), one row per currency, never
converted or mixed; `revenue_30d` keeps receipts issued in the last 30 days.
Spend is every `site_plan` wallet debit (purchases, renewals, plan changes) on
the sites that are sold to a client NOW, in credits (1 credit = $0.01). That
includes debits made before the site was sold, for example the partner's own
earlier paid publish of that site. Needs `fabric.read`; **403**
`partner.not_active` without an ACTIVE profile.
`commission_credits_30d` / `commission_credits_total` are the credits earned on
client pay-link payments, minus any taken back after a refund or lost dispute.
`rewards_credits_30d` / `rewards_credits_total` are milestone rewards credited,
and `lifetime_sites_sold` is the milestone count (see the tier section).

### `GET /partners/earnings?months=12`

One row per UTC calendar month, newest first, including months with no
activity: `[{month: "YYYY-MM", sales, revenue: [{currency, amount_minor}],
spent_credits, commission_credits, rewards_credits}]`. `commission_credits` is that month's
commissions minus that month's clawbacks; `rewards_credits` is the milestone
rewards credited that month. `sales` is the number of `site_plan` debits on currently-sold
sites that month: purchases, renewals AND plan changes each count as one, so an
upgrade is a sale. `revenue` and `spent_credits` follow the summary's rules. `months` is 1–24 (default 12; **422** outside it).
Needs `fabric.read` and an ACTIVE profile.

### `GET /partners/rewards`

The milestone ladder with when each reward was credited:

```json
[{"sites": 1, "credits": 200, "reached_at": "2026-10-02T10:15:00Z"},
 {"sites": 10, "credits": 1000, "reached_at": null},
 {"sites": 25, "credits": 3000, "reached_at": null},
 {"sites": 50, "credits": 7500, "reached_at": null}]
```

Needs `fabric.read` and an ACTIVE profile (**403** `partner.not_active`).

### `PATCH /partners/me/profile`

The caller's public partner profile; only the fields sent change. Body
(`extra` forbidden):

```json
{
  "slug": "ravi-prints",
  "display_name": "Ravi Prints",
  "city": "Bengaluru",
  "country": "IN",
  "services": ["print", "design"],
  "bio": "Flex and vinyl since 2009.",
  "contact_url": "https://wa.me/919876543210",
  "public": true
}
```

`display_name` and `city` are stripped of surrounding whitespace before the 1-80
length check, so a blank value is `422`. To clear `services` send `[]`; `null`
is `422` (the other optional fields clear on `null`). `slug` is
`^[a-z0-9-]{3,60}$`, unique across workspaces, and may not be one of
`directory, apply, requests, find, join, request, status` (fixed segments under
`/pros` on the API and on the public site). `services` is a subset of
`print, design, web, marketing, photo` (up to 5). `bio` is at most 600 chars,
`contact_url` must start with `https://`, `country` is ISO-3166 alpha-2 (upper-cased).
`public: true` needs a `slug` and a `display_name`. Returns the same shape as
`GET /partners/me`, which now carries these eight fields too (defaults: nulls,
`services: []`, `public: false`). `fabric.write`. **404** when the workspace is not a
partner (an applied partner may fill its profile in ahead of activation; it is listed
only once active). **422** `partners.invalid_profile` (pattern, reserved slug, unknown
service), `partners.profile_incomplete`; **409** `partners.slug_taken`. An operator
`PUT /platform/workspaces/{id}/partner` leaves these fields as they are. Deleting
the workspace releases its slug (and unlists it), so another partner can take it.
An active partner with `public: true` is listed on Find a Pro (see "Find a Pro
(public)" below).

### `GET /platform/partners/applications` · `PATCH /platform/partners/applications/{id}`

Platform operators only (`platform.partners.write`, OPERATOR rung, interactive
session cookie), the same guard as the partner switch above. The list is newest
first: `?status=new|contacted|rejected|accepted` filters, `cursor` pages (the
`next_cursor` of the previous page; `422 partners.bad_cursor` when invalid),
`limit` 1-200 (default 50). Each row is `{id, name, email, city, country,
services, message, status, note, reviewed_by, reviewed_at, created_at}`; this is
the review queue, so the applicant's contact details are included. Every list
read is recorded as a platform audit read.

PATCH body: `status` (one of the four), optional `note` (up to 2000, kept on the
application), `reason` (required, non-blank; goes to the platform audit row).
Returns the updated row with `reviewed_by` (the operator's user id) and
`reviewed_at`. Marking an application `accepted` records the decision only; the
applicant's workspace becomes a partner through
`PUT /platform/workspaces/{workspace_id}/partner`. An unknown id is `404` with
no audit row. There is no operator UI for this queue yet; it is a paw-enterprise
follow-up.

## Find a Pro (public)

The public face of Paw Partners, under the brand Paw Pros by PocketPaw. Strangers
see "Pro"; the signed-in hub, operator routes and stored data keep the partner
name. These three routes need no sign-in, live under `/pros` and return only
allow-listed fields. Their error codes are `pros.*` (and `pro.not_found`). Nothing
under `/partners` is public: `/partners/directory` and `/partners/{slug}` answer
`404`.

### `GET /pros/directory`

No sign-in. Pros (partners with `status: active` and `public: true`), newest first
(by `joined_at`, then slug). Query params, all optional: `city` (case-insensitive
exact match), `service` (one of the five), `cursor` (the opaque `next_cursor` of
the previous page; it encodes only the card's own `joined_at` and slug, never a
workspace id), `limit` (1-50, default 24). Limited to 60 requests a minute per
IP, shared with `GET /pros/{slug}`; past that `429` `pros.rate_limited`.
A bad cursor is `422` `pros.bad_cursor`.

**Client IP behind the public site.** Every per-IP limit keys on the rightmost
`X-Forwarded-For` hop. Requests relayed by the paw-web Worker all arrive from
Cloudflare's egress, so the Worker sends two headers: `X-Paw-Client-IP` (the
visitor's address) and `X-Paw-Web-Key` (a shared secret). Only the limits on the
routes the Worker fronts read them: these `/pros` reads, `POST /pros/apply`
and the public Discover reads. When the backend has `POCKETPAW_PUBLIC_WEB_KEY`
set and the key header matches it, that IP is the bucket (and the address sent
to Turnstile); a missing or wrong key, an unset env var or an invalid address
falls back to the normal rule. Every other limit (the auth exchange, meeting
lookups and knocks, `POST /tools/ai-check`) ignores both headers, so a leaked
key cannot pick their buckets. `X-Paw-Client-IP` is never read without the key.
Rotation: set the new key on the Worker and in the backend env, redeploy both;
there is no dual-key window, so expect a short gap where Worker traffic shares
one bucket.

Response `200` (`ProDirectoryPage`):
`{"items": [<pro>, ...], "next_cursor": "..." | null}`. A Pro on the wire (`ProPublicOut`) is exactly these fields (never `footer_name`,
`billing_country`, `founding` or `status`):

```json
{
  "slug": "ravi-prints",
  "display_name": "Ravi Prints",
  "city": "Bengaluru",
  "country": "IN",
  "services": ["print", "design"],
  "bio": "Flex and vinyl since 2009.",
  "contact_url": "https://wa.me/919876543210",
  "tier": "bronze",
  "joined_at": "2026-10-01T09:00:00Z",
  "sites": [<Discover listing>, ...]
}
```

`sites` are the partner workspace's public Discover listings, the same card as
`GET /discover` (see Discover below), newest first and capped at the 12 newest;
a Pro with more shows only those 12 here (the full set is on `GET /discover`).

### `GET /pros/{slug}`

No sign-in; same rate limit as the directory. One Pro by slug (`ProPublicOut`),
`404` `pro.not_found` when there is no such slug or the partner is not active or
not public. Registered after every fixed `/pros/<segment>` route, so
`/pros/directory` and `/pros/apply` always win over a slug.

### `POST /pros/apply`

No sign-in. Apply to become a Pro. Body (`ProApplyIn`, `extra` forbidden): `name`
(1-120), `email`, `city` (1-80), `country` (ISO-2), `services` (1-5 of the five), `message` (up to 2000, optional),
`turnstile_token`. Order of checks: the body, then a global cap of 500
applications a day across every address (`429` `pros.apply_daily_limit`, so a
flood from many addresses cannot fill the queue), then the Cloudflare Turnstile
token (`400` `pros.turnstile_failed`; with `POCKETPAW_TURNSTILE_SECRET` unset
the check is skipped with a warning in dev and refused in a production posture,
see the AI check below). Then exactly one application is stored in the platform's
own `partner_applications` collection (never a workspace), with the submitting
address kept only as a sha256 hash. **204**. Limited to 5 applications an hour
per IP (`429` `pros.apply_rate_limited`). Operators review the queue through
`GET /platform/partners/applications` (under Paw Partners above).

## Site templates

Save a site pocket as a template, then start new sites from it. A template is a
frozen copy of the site's authored content (the same five fields a duplicate
copies, plus the source's source-gate stamp), stored on its own. Editing or
deleting the source site does not change the template, and deleting the template
does not touch sites made from it.

### Visibility

| `visibility` | Who can list, read and use it |
|---|---|
| `private` (default) | The owner, in the template's workspace |
| `workspace` | Every member of the template's workspace |
| `public` | Every signed-in user in every workspace, unless reports have hidden it |

Only the owner can change or delete a template. Anyone who can't see a template
gets `404`, never `403`, whatever the operation.

Making a template public (on save or with `PATCH`) runs these checks first:

- **No private files.** Every string in the site's ripple spec and source files is
  scanned for addresses of the workspace's own files: `/api/v1/uploads/...`,
  `/api/v1/files...`, `/api/v1/media/...` (studio output), `/api/v1/auth/avatar/...`,
  `/uploads/...` (relative, or on this deployment's host), bare upload and media
  storage keys, and presigned object-storage links (`X-Amz-Signature`,
  `X-Goog-Signature`, `Signature=`). Any match is `422`
  `site_templates.private_assets`, and the message says how many. External
  images (`https://images.unsplash.com/...`) and the public Sites asset rail are
  allowed.
- **Any site's plan.** The source site's own tier does not matter: a free or
  draft site can be shared publicly. A pocket created from the template later
  follows its own site's tier for source visibility.
- The Sites plan gate and the 2 MB size cap, as on save.

Every response and event carries the template's metadata only, never its
content. `owner` is the owner's user id for the owner and `null` for everyone
else, and nothing names the owner's workspace. `hidden` is only ever `true` for
the owner:

```json
{
  "id": "665f1c...",
  "name": "Bakery",
  "description": "",
  "visibility": "public",
  "version": 1,
  "engine": "svelte",
  "pattern": "landing",
  "owner": null,
  "is_mine": false,
  "hidden": false,
  "preview_image_url": "https://assets.example.com/sites-assets/w1/template-665f1c.../3fa9c1d0e2b4a6f8-preview.png",
  "kind": "site",
  "audiences": ["shop"],
  "live_url": "https://bakery.pawsites.workers.dev",
  "created_at": "2026-10-01T09:00:00Z",
  "updated_at": "2026-10-01T09:00:00Z"
}
```

`preview_image_url` is a screenshot of the source site, or `null`. On save the
source site's current screenshot is copied to the public Sites asset rail under
the template's own prefix (`sites-assets/{workspace}/template-{id}/`), so it
loads for every viewer of a public template and outlives the source site. It is
never the source site's private `/api/v1/uploads/...` link. The copy is
best-effort: no screenshot yet, no public asset bucket on the deployment, or a
file that isn't a PNG, JPEG, GIF or WebP image leaves it `null` and the save
still succeeds. Deleting the template removes the image.

`kind` (`site`, the default, `tool` or `game`) and `audiences` (any of `shop`,
`design`, `everyone`, `fun`; default `[]`) describe the template for the
Discover index. `live_url` is the source site's live URL when that site is
deployed, else `null`; it is re-read on save, on every `PATCH` and on every
Discover reindex (once at startup, then every 30 minutes when the cloud
scheduler is on), so a renamed
or unpublished site's URL catches up within one reindex. A public template is
listed in Discover; making it private or deleting it removes the listing. A
hidden template (reported here or on Discover) keeps a hidden listing, and a
hide from either side hides both: the template leaves the public list and `use`,
and changing its visibility back to `public` keeps it hidden.

Events: `site_template.saved`, `site_template.updated`, `site_template.deleted`
(to the owner) and `site_template.used` (to the user who used it, with the new
`pocket_id`). Nothing fans out to a workspace or to all users. Audit,
best-effort: `site_template.saved`, `.updated`, `.preview_refreshed`, `.deleted` in the owner's
workspace; `site_template.used` and `.reported` in the acting user's workspace
(`used` names the template's workspace only when it is the same one);
`site_template.hidden` in the owner's workspace, with actor `system`.

### `POST /site-templates`

Save a site pocket as a template the caller owns.

```json
{ "pocket_id": "665f...", "name": "Bakery", "description": "Optional, up to 500 chars", "visibility": "private", "kind": "site", "audiences": [] }
```

`name` is 1 to 100 characters; `visibility` defaults to `private`. The caller
needs read access to the pocket, as for a duplicate. Response `200`: the
template's metadata.

Errors: `403` (`plan.feature_denied`) when the plan does not include Sites; `404`
for a missing or cross-tenant pocket; `403` (`pocket.access_denied`) for a
private pocket the caller can't read; `422` for `pocket.not_a_site`,
`site_templates.too_large` (the content is over 2 MB as JSON) and
`site_templates.limit` (the workspace already has 50 templates); and for
`public`, the publish checks above. Nothing is written on any error.

### `GET /site-templates`

Query: `scope` (`mine`, the default: your own templates in this workspace, any
visibility; `workspace`: workspace-visibility templates in this workspace,
yours included; `public`: public, non-hidden templates from every workspace),
`limit` (1 to 50, default 50), `cursor`. Newest first. Response `200`:

```json
{ "templates": [ { "id": "..." } ], "next_cursor": "665f..." }
```

Pass `next_cursor` back as `cursor` for the next page; `null` marks the last one.
A malformed cursor is `422` `site_templates.bad_cursor`.

### `GET /site-templates/{template_id}`

One template's metadata, if you can see it; otherwise `404`.

### `PATCH /site-templates/{template_id}`

Change any of `name`, `description`, `visibility`, `kind`, `audiences`. Owner only (`404` for anyone
else). Setting `visibility` to `public` runs the publish checks. `version` does
not change. Response `200`: the metadata.

### `DELETE /site-templates/{template_id}`

Delete a template you own. Response `200`: `{"id": "...", "deleted": true}`.
`404` for anyone else's template. Sites made from it are unaffected.

### `POST /site-templates/{template_id}/preview-refresh`

Re-copy the source site's current screenshot onto a template you own, after the
site has been republished or re-captured. No body. Response `200`: the metadata
with the new `preview_image_url`; the previous image is deleted. Emits
`site_template.updated`.

Errors: `404` for anyone but the owner; `409`
(`site_templates.no_source_preview`) when the source site no longer exists, has
no screenshot, or the copy fails. The existing image is kept.

### `POST /site-templates/{template_id}/use`

Start a new private site pocket, owned by the caller, in the caller's own
workspace, from a template the caller can see.

```json
{ "name": "Second bakery" }
```

The body is optional; `name` defaults to the template's name. The new pocket
gets the template's content, records `template_id` and `template_version`, and a
fresh draft Site row so it lists in the sites gallery (nothing is built or
deployed). It is source-gated if the template's source was, or if the source
gate is on now. Response `200`: `{"pocket_id": "..."}`.

Errors: `404` for a template you can't see; `403` (`plan.feature_denied`) when
your plan does not include Sites; `402` (`billing.pocket_limit`) when your
workspace is at its plan's pocket cap. Both are checked against the caller's
workspace, not the template's.

### `POST /site-templates/{template_id}/report`

Report a public template.

```json
{ "reason": "Spam, up to 500 chars" }
```

Response `200`: `{"id": "...", "reported": true}`. One report per user counts; a
repeat is accepted and changes nothing. When three different users have
reported a template it is hidden: it leaves the public list, and get and use
return `404` for everyone but the owner, who still sees it with
`"hidden": true`. There is no un-hide endpoint yet.

Errors: `404` for a template you can't see or that is not public; `403`
(`site_templates.own_template`) for the owner reporting their own.

## Studio templates

Publish one asset of a finished Studio generation as a template. The template
is a frozen copy: the asset becomes `cover` and the generation's prompt, model
and settings become `recipe`. Editing or deleting the generation later doesn't
change it. Input images are never published: `recipe.params` drops
`inputImageCount` and any other input-image, reference or upload field, and
`uses_input_images` only says the original run had some. `visibility` is
`private` (default), `workspace` or `public`; a public template is listed on
Discover (`source: "studio_template"`), private or deleted takes the listing
down. `kind` is `image`, `video` or `music` (an `audio` generation publishes as
`music`). Cover URLs stay backend-relative (`/api/v1/media/...`) here.

```json
{
  "id": "6661b2...",
  "owner": "u1",
  "template_type": "generation",
  "source_generation_id": "gen_abc",
  "kind": "image",
  "title": "Red fox",
  "description": "",
  "audiences": ["design"],
  "visibility": "public",
  "cover": {"url": "/api/v1/media/fox.png", "mime": "image/png", "width": 1024, "height": 1024, "poster_url": null},
  "recipe": {"kind": "image", "model": "flux", "prompt": "a red fox", "params": {"aspectRatio": "1:1", "count": 1}},
  "uses_input_images": false,
  "hidden": false,
  "created_at": "2026-10-02T09:00:00Z",
  "updated_at": "2026-10-02T09:00:00Z"
}
```

### `POST /studio-templates`

```json
{ "generation_id": "gen_abc", "asset_id": "a2", "title": "Red fox", "description": "", "audiences": ["design"], "visibility": "public" }
```

`asset_id` is optional (default: the generation's first asset). Response `200`:
the template. Errors: `404` for a generation that isn't in your workspace or an
unknown `asset_id`; `409` (`studio_templates.not_ready`) unless the generation
succeeded.

### `GET /studio-templates`

Your own templates in this workspace, newest first. Query: `limit` (1-50,
default 50), `cursor`. Response `200`:
`{"templates": [<template>, ...], "next_cursor": "..." | null}`.

### `PATCH /studio-templates/{template_id}`

Any of `title`, `description`, `audiences`, `visibility`. Owner only; anyone
else gets `404`. The cover and recipe never change. A template hidden by
Discover reports stays hidden after a private and public round trip.

### `DELETE /studio-templates/{template_id}`

Owner only. Response `200`: `{"id": "...", "deleted": true}`. The generation is
untouched.

## Discover — Public Index

One index of shareable items from every workspace, newest first. Sources are
public site templates (`source: "site_template"`) and public studio templates
(`source: "studio_template"`); a public template has one listing, hidden when
the template is hidden. The two reads need no sign-in and
are limited to 60 requests a minute per IP (shared between them); past that they
return `429` with `discover.rate_limited`. Behind the paw-web Worker the IP comes
from `X-Paw-Client-IP` when `X-Paw-Web-Key` matches (see `GET /pros/directory`). `use` and `report` need a signed-in
user and act in the caller's active workspace.

A listing on the wire is exactly these fields (never the owner, workspace,
reports or the source item's id):

```json
{
  "id": "6660a1...",
  "slug": "bakery",
  "source": "site_template",
  "kind": "site",
  "title": "Bakery",
  "description": "",
  "audiences": ["shop"],
  "featured": false,
  "preview_image_url": "https://assets.example.com/sites-assets/w1/template-665f1c.../3fa9c1d0e2b4a6f8-preview.png",
  "live_url": "https://bakery.pawsites.workers.dev",
  "remix_count": 3,
  "created_at": "2026-10-01T09:00:00Z",
  "media_kind": null,
  "media_url": null
}
```

`slug` is the listing's URL handle: the title lowercased and folded to
`a-z0-9` with `-` between words (`Café Crème & Co!` is `cafe-creme-co`), cut
to 40 characters, set when the listing is first indexed and never changed
afterwards, so a renamed template keeps its link. A title that leaves no ASCII behind (CJK, Devanagari)
falls back to the source item's id. Slugs are unique across every source; a
second `Bakery`, whichever source lists it, gets `bakery-2`, then `bakery-3`. A
listing indexed before slugs existed reports its `id` as `slug` until the next
reindex (at most 30 minutes after a deploy) fills it in; the id works on the
item route too.

`media_kind` (`image`, `video`, `audio`) and `media_url` are set on studio
template listings and `null` on site templates. Studio listings carry absolute
URLs built from `POCKETPAW_PUBLIC_BASE_URL`: `media_url` is the asset,
`preview_image_url` the image itself or a video's poster (`null` for music),
and `live_url` is always `null`.

### `GET /discover` (public)

Query params, all optional: `source`, `kind` (`site`, `tool`, `game`, `image`,
`video`, `music`),
`audience` (matches one of a listing's `audiences`), `q` (case-insensitive
substring of title or description, up to 100 chars), `featured` (`true` /
`false`), `cursor`, `limit` (1-50, default 24).

Response `200`: `{"items": [<listing>, ...], "next_cursor": "6660a0..." | null}`.
Pass `next_cursor` back as `cursor` for the next page; it is `null` on the last
page. A cursor that isn't one we issued returns `422` (`discover.bad_cursor`);
`limit` above 50 returns `422`.

### `GET /discover/{id_or_slug}` (public)

Takes a listing id or its slug. The id is tried first, then the slug among
unhidden listings; slugs are unique across sources, so a bare slug is enough.

Response `200`: one listing. `404` when it doesn't exist or has been hidden.

### `POST /discover/{listing_id}/use` (signed in)

Make your own copy of the listing's item in your workspace. The body is
optional; `name` defaults to the item's name.

```json
{ "name": "My bakery" }
```

Response `200`: `{"source": "site_template", "result": {"pocket_id": "..."}}`.
For a studio template nothing is copied: the result is the recipe to run in
Studio, `{"source": "studio_template", "result": {"recipe": {...}, "uses_input_images": false}}`
(`name` is ignored).
The source's own checks apply (a site template needs a plan with Sites), and
`remix_count` goes up by one only when the copy succeeded, and not when the
listing's owner uses their own listing. `404` for a missing
or hidden listing.

### `POST /discover/{listing_id}/report` (signed in)

```json
{ "reason": "Spam, up to 500 chars" }
```

Response `204`, no body. One report per user counts; a repeat changes nothing.
Each user may send 10 reports an hour across all listings (repeats included);
past that the route returns `429` with `discover.report_rate_limited`.
Three different reporters hide the listing from both public reads, `use` and
`report`, and hide the source item too: a hidden site template also leaves the
/sites public list, and making it private and public again does not relist it.
Staff unhide a listing with `POST /api/v1/platform/discover/{listing_id}/unhide`
(see "Platform — Discover Moderation"). The source item is unhidden with it, the reports are cleared, and the users who reported it are
recorded so their later reports on that listing are ignored. Hiding, unhiding,
featuring and `use` each write an audit row. Errors: `404` for a missing or
hidden listing; `403` (`discover.own_listing`) for the owner reporting their
own; `429` past the report limit.

## AI visibility — Free check

### `POST /tools/ai-check` (public)

No sign-in. Asks ChatGPT (OpenAI Responses API with web search) three local
questions, twice each, and says whether it names the business.

```json
{
  "name": "Joe's Pizza",
  "city": "Austin",
  "website": "joespizzaatx.com",
  "turnstile_token": "<Cloudflare Turnstile token>"
}
```

`name` and `city` are 2 to 100 characters; `website` is optional and is reduced to
its host (a non-web scheme is a `422`). Unknown fields are a `422`.

Response `200`:

```json
{
  "mentioned": false,
  "engine": "ChatGPT",
  "answers_checked": 4,
  "competitors": [],
  "sources": [{ "type": "yelp", "url": "https://www.yelp.com/biz/joes-pizza-austin" }],
  "fix": { "id": "site_content", "text": "AI assistants aren't using your website..." }
}
```

One of the three questions names the business ("Is Joe's Pizza in Austin a good
choice?"). Its answers nearly always repeat the name, so `mentioned`,
`answers_checked` and `fix` come only from the other two questions; the named
one adds to `sources`. `sources` holds up to 10 unique https URLs the engine read,
typed `own_site | gbp | yelp | tripadvisor | reddit | directory | other`.
`competitors` is always `[]` for now: an anonymous check has no competitor list
to match against. The check is stored without a workspace or site; the caller's
IP is not stored.

Errors (`{"error": {"code", "message"}}`):

| Status | Code | When |
|--------|------|------|
| `400` | `tools.ai_check.turnstile_failed` | Turnstile rejected the token, or Cloudflare could not be reached |
| `429` | `tools.ai_check.rate_limited` | More than 5 checks in an hour from one IP |
| `503` | `tools.ai_check.daily_limit` | Today's anonymous checks spent `POCKETPAW_AI_CHECK_DAILY_USD` (default `5.0`) |
| `502` | `tools.ai_check.engine_failed` | No OpenAI key configured, or every engine call failed |

With `POCKETPAW_TURNSTILE_SECRET` unset, Turnstile is skipped with a warning in dev
and refused (`400 tools.ai_check.turnstile_failed`) in a production posture
(`POCKETPAW_ENV=production` or `POCKETPAW_AUTH_COOKIE_SECURE=true`).

## AI visibility — Staff site card

Signed in, gated like the sites router (the `sites` plan feature, `fabric.read` to
read, `fabric.write` to change). Every route is scoped to the caller's workspace;
a missing or cross-tenant site is `404`. `PATCH /sites/{id}/ai-visibility` (the
AI-training opt-in) lives with the sites routes.

### `GET /sites/{site_id}/ai-visibility`

```json
{
  "ai_training_allowed": false,
  "plan_allows_check": true,
  "questions": ["best pizza in Austin"],
  "check": {
    "status": "done",
    "ran_at": "2026-10-03T07:01:12Z",
    "next_run_at": "2026-11-02T07:01:12Z",
    "questions": ["best pizza in Austin"],
    "engines": [{ "label": "ChatGPT", "named": 2, "of": 3, "failed": 0 }],
    "competitors": [{ "name": "Home Slice Pizza", "count": 4 }],
    "sources": [{ "type": "yelp", "count": 3 }],
    "fix": { "id": "gbp", "text": "...", "we_can_apply": false }
  }
}
```

`plan_allows_check` is true when the site's own plan sells the concierge (Staff)
and its subscription is active. `questions` is what the next check asks (the
owner's list); `check.questions` is what the shown check asked. `check` is `null`
until a check is first requested. While a new check is `pending` or `running`,
the numbers are the previous finished check's (empty before the first).
Engine labels: `ChatGPT`, `Perplexity (search)`, `Claude`. `of` counts answers
received, `failed` calls that errored. `sources` counts unique URLs per type.
`competitors` is empty until a site carries a competitor list. `fix` is `null`
before the first finished check. `ai_training_allowed` reads the site's opt-in
(false when the site has none). `next_run_at` is set only on a plan with checks
and only when the monthly check is on (`POCKETPAW_CLOUD_SCHEDULER_ENABLED=true`).
A check that errors or hits the worker's 15-minute timeout ends `failed`.

### `PUT /sites/{site_id}/ai-visibility/questions`

```json
{ "questions": ["best pizza in Austin", "pizza near Zilker park"] }
```

1 to 10 questions, each 3 to 200 characters after trimming. Returns the card.
New sites start with no questions: a site carries no business type or city to
build good ones from, so the owner writes them.

### `POST /sites/{site_id}/ai-visibility/check`

Response `202 {"status": "pending"}`. Asks every configured engine each question
3 times, on the site worker lane. Errors: `403 ai_visibility.plan_required` (not
Staff), `422 ai_visibility.questions` (no questions saved), `429
ai_visibility.too_soon` (a check was requested in the last 24 hours).

A daily arq cron on the site lane queues a check for every Staff site with
questions whose last check was requested 30 or more days ago. It runs only with
`POCKETPAW_CLOUD_SCHEDULER_ENABLED=true` on the worker, and queues nothing when no
engine key is configured.

### `POST /sites/{site_id}/ai-visibility/apply-fix`

```json
{ "fix_id": "ai_access" }
```

Response `202 {"republish": "started"}`. Only `ai_access`, the one fix Paw Sites
applies itself, and only when it is the fix the site's latest finished check
picked; any other id is `400 ai_visibility.fix_not_applicable`. Errors also
include `403 ai_visibility.plan_required` (not Staff). It starts the site's normal
republish (the same path as Publish, with no plan change), which writes the
AI-ready robots.txt. That republish also puts any unpublished edits live.
`site_content` is guidance only (`we_can_apply: false`): the page text is the
owner's edit.

## Skills — Per-Backend API Skills

Increment 2b (the second half of pocket Increment 2, after the built-in
templates of 2a). When a pocket is bound to a backend, the
pocket-authoring agent does better work if it can see the backend's
**real** API instead of guessing endpoints. This endpoint installs a
backend's OpenAPI / Swagger document as a loadable skill: the agent then
authors `rippleSpec.sources` / `rippleSpec.actions` against real relative
paths and real response shapes rather than hallucinating them.

The skill is a `SKILL.md` file written under `~/.pocketpaw/skills/api-<domain-slug>/`
— one of the three roots PocketPaw's `SkillLoader` scans. The
pocket-specialist runtime loads it (keyed by the pocket's backend
hostname) and splices a `<backend-api>` endpoint reference into the
authoring prompt.

### `POST /skills/api-doc`

Install a backend's OpenAPI / Swagger spec as a per-backend API skill.
Requires the `skills.manage` role (**ADMIN**) — installing a skill
changes workspace-wide pocket-authoring behaviour.

Multipart form upload:

| Field | Type | Notes |
|-------|------|-------|
| `file` | file | Required. The OpenAPI 3.x or Swagger 2.x document — `.json`, `.yaml`, or `.yml`, max 2 MB. |
| `name` | string | Optional. The backend display name — used to derive the skill slug when the spec itself names no server. |

The slug is derived from the spec's server hostname (`servers[0].url`
for OpenAPI 3.x, `host` for Swagger 2.x), falling back to `name`. The
generated reference groups operations by tag (or first path segment),
caps at 200 endpoints, and records each operation's method, path,
summary, key request params, and key 200-response fields.

Response `200`:

```json
{ "ok": true, "slug": "api-example-com" }
```

Returns `422` when the file extension is unsupported, the file exceeds
the 2 MB cap, the document is unparseable, or it carries no `paths`
object. Every install is audit-logged with the workspace, the actor, and
the resulting slug — never the spec contents.

## Plugins — Install a `.claude-plugin`'s Skills and MCP Servers

PocketPaw adopts the `.claude-plugin` standard so a whole plugin's skills
and MCP servers install in one step. This endpoint clones a GitHub repo,
reads its `.claude-plugin/plugin.json`, copies each `skills/<name>/SKILL.md`
directory into the skill loader path, reloads the loader, registers and
starts any MCP servers the bundle declares, and records the install in a
registry at `~/.pocketpaw/plugins.json`.

### MCP servers

After the skills step, the installer reads the bundle's MCP config — a
`.mcp.json` file at the plugin root in the standard
`{"mcpServers": {name: spec}}` shape (a manifest `mcp_servers` path
override is honoured if present). When there is no MCP config the step is
recorded as `skipped`; it never fails the install.

Each declared server is mapped to a PocketPaw MCP server config:
`command`, `args`, and `env` carry over directly, and `transport` is
derived from the spec's `type` (`stdio` is the default; `http`, `sse`, and
`streamable-http` map through). To avoid cross-plugin collisions the
registered name is namespaced as `plugin:<plugin_name>:<server_name>`.

Every server is registered and started through the MCP manager — one step
per server:

- **`succeeded`** — the server started, **or** it registered but couldn't
  start because it's missing required env. The latter is non-fatal and
  carries a `needs env: KEY` detail so the operator knows to supply the
  credential; the server is still recorded as installed.
- **`failed`** — the server failed to start for any other reason.

The namespaced server names appear in the registry entry under
`mcp_servers` and on the report's `installed_mcp_servers`.

### `POST /plugins/install`

Install a plugin's skills and MCP servers from a GitHub source. Requires
the **admin** scope — installing a plugin changes workspace-wide agent
behaviour.

Request body:

```json
{ "source": "owner/repo" }
```

`source` accepts `owner/repo`, `owner/repo/subdir` (when the plugin lives
in a subdirectory), or a full GitHub URL (a `/tree/<ref>/<subdir>` path is
honoured). The repo (or subdir) must contain a `.claude-plugin/plugin.json`
manifest and at least one `skills/<name>/SKILL.md`. An MCP `.mcp.json` is
optional.

Response `200` — a step-by-step install report:

```json
{
  "plugin": "my-plugin",
  "installed_at": "2026-06-07T12:00:00",
  "steps": [
    { "name": "read_manifest", "status": "succeeded", "detail": "my-plugin v1.2.3" },
    { "name": "skill:alpha", "status": "succeeded", "detail": "" },
    { "name": "reload_loader", "status": "succeeded", "detail": "" },
    { "name": "mcp:weather", "status": "succeeded", "detail": "" },
    { "name": "mcp:db", "status": "succeeded", "detail": "needs env: DB_URL" },
    { "name": "record_registry", "status": "succeeded", "detail": "" }
  ],
  "installed_skills": ["alpha"],
  "installed_mcp_servers": ["plugin:my-plugin:weather", "plugin:my-plugin:db"]
}
```

Each unit of work is a step with status `succeeded` / `skipped` /
`failed`, so a per-skill copy failure or a single MCP server start failure
surfaces in the report rather than aborting the whole install. When the
bundle declares no MCP servers, a single `mcp` step is recorded as
`skipped`. Up-front failures return clear status codes instead of `500`:

| Status | When |
|--------|------|
| `400` | Missing or malformed `source`, or an invalid `plugin.json`. |
| `404` | No `.claude-plugin/plugin.json`, or no skills found in the plugin. |
| `502` | The git clone failed. |
| `504` | The git clone timed out. |

A malformed `.mcp.json` (parse error, or a wrong `mcpServers` shape) does
**not** fail the request — it surfaces as a single `failed` `mcp` step in
the report, so already-installed skills and the registry entry are
preserved.

Every install is audit-logged with the source, plugin name, version, and
the installed skill names.

### `GET /plugins`

List every installed plugin from the registry
(`~/.pocketpaw/plugins.json`). Requires the **admin** scope.

Response `200` — an array of installed plugins:

```json
[
  {
    "name": "my-plugin",
    "version": "1.2.3",
    "source": "acme/widgets",
    "skills": ["alpha", "beta"],
    "mcp_servers": ["plugin:my-plugin:weather"],
    "installed_at": "2026-06-08T12:00:00"
  }
]
```

### `POST /plugins/remove`

Uninstall a plugin: delete each skill directory it installed, **stop** each
of its namespaced MCP servers and remove their configs from the MCP manager,
reload the skill loader, and drop its registry entry. Stopping the live
server (not just deleting its config) mirrors install, which both registers
the config and starts the server — so remove tears down the running
connection too, rather than leaving it up until the next restart. Requires
the **admin** scope.

Request body:

```json
{ "name": "my-plugin" }
```

Response `200` — a step-by-step remove report (mirrors the install report):

```json
{
  "plugin": "my-plugin",
  "removed_at": "2026-06-08T12:05:00",
  "steps": [
    { "name": "skill:alpha", "status": "succeeded" },
    { "name": "mcp:plugin:my-plugin:weather", "status": "succeeded" },
    { "name": "reload_loader", "status": "succeeded" },
    { "name": "drop_registry", "status": "succeeded" }
  ],
  "removed_skills": ["alpha", "beta"],
  "removed_mcp_servers": ["plugin:my-plugin:weather"]
}
```

Like install, each component is a step with status `succeeded` / `skipped`
/ `failed`. A component that's already gone (a missing skill dir, an MCP
server that's neither running nor registered) is `skipped` — the remove
still completes and the registry entry is **always** dropped, so a
half-removed plugin never lingers in the listing. The only up-front error
is an unknown plugin:

| Status | When |
|--------|------|
| `400` | Missing or invalid `name`. |
| `404` | The named plugin is not installed. |

Every removal is audit-logged (`action="plugin_remove"`) with the plugin
name and the removed skill / MCP server names.

The registry read-modify-write (shared by install and remove) is
serialised by a process-level lock and written via a temp file + atomic
`os.replace`, so concurrent operations can't corrupt `plugins.json` or
clobber each other's entries.

### Per-agent skills (`skill_refs` + `plugins`)

An agent's `config` carries two skill-bearing fields, set on
`POST /agents` (create) or `PATCH /agents/{id}` (update — via either the
nested `config` object or the flat top-level fields):

| Field | Type | Meaning |
|-------|------|---------|
| `skill_refs` | `string[]` | Skill names this agent always materializes. |
| `plugins` | `string[]` | Installed plugin names whose bundled skills this agent always materializes. |

Both default to `[]`. Unlike a surface / entity-room `skill_names` subset
— which only applies inside that room — an agent's `skill_refs` plus the
skills of its enabled `plugins` fold into the per-run skill set on **every**
run the agent does, regardless of surface. At run time the plugin names are
resolved to their skills via the installed-plugin registry
(`~/.pocketpaw/plugins.json`); an unknown plugin name is ignored and a
missing / unreadable registry degrades to no plugin skills (it never fails
the run). The agent set is UNIONed with any surface/entity skill subset, so
both apply together. Per-agent MCP servers are **not** part of this — that
is a separate, deferred slice.

## Pockets — Catalog-as-Allowlist Ingest Gate

Increment 5. The Ripple renderer has a **closed widget registry**: a
node whose `type` is not a known widget renders as a red "Unknown widget
type" box. The catalog gate catches that at ingest time, before the spec
is persisted.

On every pocket write that carries a `rippleSpec`, the service walks the
node tree and flags any node whose `type` is not in the widget manifest
(plus the control-flow types `if` and `each`). The gate runs in one of
two modes:

- **Strict** — the agent-generation path (`create_from_ripple_spec`, the
  pocket-specialist `agent_create` / `agent_update` ops). A violation
  blocks the write; the specialist edit tools return the corrective
  message so the LLM can retry with a real widget type.
- **Logged** — the human / import path (`POST /pockets`,
  `PUT /pockets/{id}`). A violation is recorded as a structured warning
  for triage but does **not** block — an older imported spec may use a
  widget that has since left the catalog.

Each flagged node reports `{path, type, suggestion}`, where `suggestion`
is the nearest catalog widget by edit distance. The gate is best-effort:
when the widget manifest can't be fetched it is skipped.

### Required-prop gate

The widget manifest marks the props a widget cannot render without as
`required: true` (a `chart` with no `data`, a `stat` with no `value`, a
`table` with no `columns`/`rows`). That flag used to live only in the
system prompt — a node like `{"type": "chart", "props": {}}` passed the
catalog gate (its `type` is known) and rendered an empty box. The
required-prop gate runs as a sibling to the catalog walk and closes that
hole: it flags any node missing a manifest-required prop for its `type`.

It is the rippleSpec expression of the constraint-zone model's 🔒 **HARD**
`required_fields` zone — the agent is free to choose which widgets to use
and how to fill the creative props, but the manifest-declared structural
minimum is locked and checked, not merely asked for. Same **strict**
(agent path, blocks + returns a corrective message naming the missing
prop) / **logged** (human / import path, structured warning, never blocks)
posture as the catalog gate, and the same best-effort skip when the
manifest can't be fetched. A prop counts as present when its key exists
with a non-null value — a literal, an empty list / `0` / `false`, or a
bound `{...}` expression all satisfy it; only a missing key or explicit
`null` is a violation. A node-level `bind` satisfies a single-required-prop
input widget (the bound value populates the prop at render time).

Each flagged node reports `{path, type, missing, required}`.

### Escape-hatch widgets

Two catalog widgets cover content the rest of the catalog can't express:

- `model-viewer` — an interactive 3D model (`.glb` / `.gltf`) with
  orbit / zoom / pan controls.
- `embed` — the **sanctioned escape hatch**: a renderer-sandboxed
  iframe for a CodePen, a Figma frame, an Observable notebook, or a
  self-contained visualization. `mode` is required (`url` or `srcdoc`).
  The iframe `sandbox` attribute is renderer-controlled — it is **not**
  author-settable.

### `embed` URL / host policy

An `embed` node in `mode: "url"` points an iframe at a third-party page,
so its `url` is an SSRF / clickjacking boundary. The ingest gate
enforces:

- `url` must be **https** — plain `http` is rejected.
- the host must be on the embed allow-list (`POCKETPAW_RIPPLE_EMBED_ALLOWED_HOSTS`,
  a JSON array — default: `youtube-nocookie.com`, `player.vimeo.com`,
  `codepen.io`, `codesandbox.io`, `observablehq.com`, `www.figma.com`).
- loopback / RFC1918 / link-local / carrier-grade-NAT / cloud-metadata
  hosts are **hard-blocked unconditionally** — this holds even if the
  allow-list is widened to `["*"]`.

Every ingested spec that contains an `embed` node is audit-logged
(category `pocket_embed`) with the embed count and URLs — never the
iframe contents.

## Outcome Metering

RFC 05 M2b.2 + gap-3. When a governed write action succeeds, the pocket's
binding can declare a named `outcome` and an optional billable value/unit.
Each one appends a row to a workspace-scoped, append-only JSONL ledger.
Two read surfaces sit over the ledger; both take tenancy from the auth
context and **reject** a `workspace_id` query param (a caller cannot read
another workspace's ledger).

### Declaring an outcome on a binding

A write binding in `rippleSpec.actions` declares the metering fields:

| Field | Type | Notes |
|-------|------|-------|
| `outcome` | string \| null | The named business event (`meeting_booked`, `ticket_resolved`, …). `null` → the write is not metered. |
| `outcome_value` | number \| null | The billable value the operator assigns this outcome. Requires `outcome_unit` AND a non-null `outcome` — a half-declared pair is rejected at parse time. |
| `outcome_unit` | string \| null | The unit the value is denominated in (`usd`, `ticket_resolved`, …). Requires `outcome_value`. |

Declaring `outcome` with no value/unit is the **count-only** binding (the
prior behaviour). Declaring all three turns the count into a billable
figure.

### `GET /outcomes`

Count this workspace's recorded outcomes, grouped by name and pocket.
Requires the `outcomes.read` action.

Query params: `pocket_id` (narrow to one pocket), `since` (inclusive
ISO-8601 lower bound on `occurred_at`). Both optional.

Response `200`:

```json
{ "total": 12, "by_outcome": { "ticket_resolved": 9, "meeting_booked": 3 },
  "by_pocket": { "p_support": 9, "p_sales": 3 } }
```

### `GET /outcomes/meter`

Aggregate this workspace's **billable** outcomes into a queryable figure —
the "pay for governed outcomes" read primitive. Requires the
`outcomes.read` action.

Query params: `pocket_id`, `since` (inclusive lower bound), `until`
(**exclusive** upper bound, so adjacent periods never double-count a
boundary outcome). All optional.

Response `200`:

```json
{ "total_outcomes": 12, "metered_count": 9,
  "by_unit": {
    "usd": { "unit": "usd", "count": 6, "total_value": 7200.0 },
    "ticket_resolved": { "unit": "ticket_resolved", "count": 3, "total_value": 3.0 }
  } }
```

`total_outcomes` counts every matching row (including count-only ones);
`metered_count` counts only the rows carrying a whole value/unit pair;
`by_unit` sums `outcome_value` per unit over the window.

**Deferred (not in this surface):** invoicing, payment, currency
conversion, a pricing-rules engine, and disputes / clawback. This endpoint
returns a raw sum of declared values — the queryable figure those layers
will build on later (see `outcome-spec.md`).

## Agent chat — the `tools` switch

`POST /cloud/chat/{scope}/{scope_id}/agent` takes an optional `tools` field on
the request body.

| Value | Effect |
|---|---|
| `false` | Run this turn with no tool surface at all. No custom tools and no MCP toolsets are built, so nothing can reach the wire. |
| `true` | Nothing. Identical to omitting the field. |
| omitted / `null` | Today's behaviour, which is what every existing client sends. |

**It can only withdraw tools, never add one.** The backend forwards the flag
only when it is exactly `false`; `true` is dropped on the floor, so a client
cannot use this field to switch on a tool the server did not intend to offer,
and it cannot undo a server-side withdrawal (the surface profile's
`deny_mcp_tool_ids` is a different mechanism that the request body never
reaches). The only direction of travel is subtractive.

**It is per-send, not a setting.** The flag applies to the one turn that
carries it and is not remembered. It is part of the agent cache key, so a
turn asking for no tools never gets served a cached agent that was built with
them.

Two reasons a client reaches for it, both from the Otherhand kiosk: a gateway
profile that refuses the `tools` field outright and 400s the whole turn, and a
weaker model that fixates on a tool instead of doing the work. It is also the
largest token lever on that surface, because the upstream prompt cache does
not cover tool schemas — a run carrying a tool surface reads zero cached
tokens every turn.

## Agent Activity

HR-12a. The workspace-scoped answer to "which of my agents are working
right now". Distinct from the herdr cockpit (`GET /cockpit/*`), which reads
terminal panes on one operator box, is ADMIN-only, and never shows a `/chat`
agent — that agent runs as an in-process SDK client, not a pane.

The board is built from `ChatRunDoc`, the durable per-turn record, so it is
correct whether runs execute in the web process or an arq worker, complete
across multiple workers, and intact after a restart.

### `GET /agent-activity`

One entry per agent in the caller's workspace with at least one run in the
last **24 hours**. Requires the `agent_activity.read` action (MEMBER). Takes
no query params; tenancy comes from the auth context and a `workspace_id`
query param is **rejected** (`400`), not ignored.

This is a **team board**: it covers every member's runs, not just the
caller's. An Agent is a workspace resource, so its aggregate state is shared.
The individual turn is not — the response carries no `user_id`, no run id and
no message content, and `GET /cloud/chat/runs/{run_id}/stream` still returns
`404` for a run belonging to another member.

`agent_id` is the agent's ObjectId hex (`Agent._id`), the same key
`GET /agents` returns — not a display name.

Response `200`:

```json
{ "agents": [
    { "agent_id": "66f1a2b3c4d5e6f708192a3b", "status": "active",
      "active_runs": 2, "last_active": "2026-07-28T11:58:04+00:00" },
    { "agent_id": "66f1a2b3c4d5e6f708192a3c", "status": "blocked",
      "active_runs": 0, "last_active": "2026-07-28T10:12:44+00:00" }
  ],
  "ts": "2026-07-28T12:00:00+00:00" }
```

`status` uses the Mission Control `AgentStatus` vocabulary, the same one
the cockpit's pane dots use:

| Status | Meaning |
|--------|---------|
| `active` | The agent has at least one `queued` or `running` run. Wins over any earlier failure. |
| `blocked` | No live run, and the agent's newest run `failed` or was `interrupted`. |
| `idle` | No live run, and the newest run `completed` or was `cancelled` (a user stopping their own turn does not block the agent). |

Agents with **no run in the window are omitted** rather than returned as
`offline`: this surface reads runs, not the agent roster. A client that
wants every configured agent joins this board against `GET /agents` and
treats the absent ones as offline.

`active_runs` is how many of that agent's runs are live now; `last_active`
is the newest run's end, else its start, else its creation; `last_run_id`
identifies that run. Entries are ordered working-first, then most recently
active. `ts` stamps when the board was built.

v1 is a plain GET for the client to poll. A push stream is the upgrade path
if polling stops being enough — event-driven off the existing run-status
transitions, not a faster poll.

## Files — Accepted Types and Size

`POST /uploads` accepts **any file type** by default. A Blender `.blend`, a
Photoshop `.psd`, an FBX rig, a firmware image, a packet capture — none of them
need to be registered anywhere first.

This was not always true. Until 2026-09-11 the pipeline enforced an exact-match
allowlist of about 35 browser-native document and code mimes, and anything
outside it came back in the response's `failed[]` array with
`code: "unsupported_mime"`. The list could only be widened by editing
`src/pocketpaw/uploads/config.py`.

**Narrowing it again is a deployment choice.**

| Variable | Default | Meaning |
| --- | --- | --- |
| `POCKETPAW_UPLOAD_ALLOWED_MIMES` | `*/*` | Comma-separated policy. Entries may be exact (`application/pdf`), a family (`image/*`), or the `*/*` wildcard. Case and `;charset=…` parameters are ignored on both sides of the comparison. A blank or unparseable value falls back to `*/*` rather than locking uploads out. |
| `POCKETPAW_UPLOAD_MAX_BYTES` | `26214400` (25 MiB) | Per-file ceiling. The ASGI request-body ceiling in `security/body_limit.py` is derived from this × `max_files_per_batch`, so raising this raises that guard with it. A file over the limit fails with `code: "too_large"`. |

Example — an install that only wants images and PDFs:

```bash
POCKETPAW_UPLOAD_ALLOWED_MIMES="image/*,application/pdf"
```

**Accepting a type is not the same as rendering it inline.** This policy governs
what may be *stored*; `INLINE_MIMES` governs what may be *served inline*. Every
download rail serves anything outside that short set as
`Content-Disposition: attachment`, so an uploaded `.html` or `.svg` downloads
rather than executing on the storage origin. That set did not change when the
type policy opened up.

**What a file is recorded as.** The stored `mime` comes from, in order: magic
bytes (which overrule a lying `Content-Type`), the client's `Content-Type` when
it says something useful, and the filename otherwise. A browser sends
`application/octet-stream` for anything it does not recognise, so
`spaceship.blend` records as `application/x-blender` and keeps a `.blend` on its
storage key. Formats the stdlib registry misses live in `_EXT_TO_MIME_EXTRAS`;
extending it is always safe, since it decides what a file is *called*, never
whether it is accepted.

Source: `src/pocketpaw/uploads/config.py`,
`src/pocketpaw/uploads/service.py::UploadService._upload_one` (the single gate
every upload path goes through, OSS and cloud alike).

### Large files — multipart settings

`POCKETPAW_UPLOAD_MAX_BYTES` above governs the single-request `POST /uploads`
path and nothing else. Files past that size go through the storage adapter's
multipart surface, which has its own ceiling because its bytes never arrive as
one request body: in presigned mode they go browser→bucket and never reach the
API at all, and in relay mode each request carries one part.

| Variable | Default | Meaning |
| --- | --- | --- |
| `POCKETPAW_MAX_LARGE_FILE_BYTES` | `5368709120` (5 GiB) | Per-file ceiling on the multipart path. Deliberately separate from `POCKETPAW_UPLOAD_MAX_BYTES` — raising *that* would raise the global ASGI request-body guard with it. |
| `POCKETPAW_MULTIPART_PART_BYTES` | `8388608` (8 MiB) | Baseline part size. Scaled up (and rounded to a whole MiB) for files that would otherwise need more than S3's 10000 parts. Must stay at or above S3's 5 MiB per-part floor. |
| `POCKETPAW_MULTIPART_TTL_HOURS` | `168` (7 days) | How long an incomplete upload stays resumable. Matches the bucket lifecycle rule that expires abandoned parts. |

A malformed or non-positive value warns and falls back to the default rather
than reading as "unlimited".

`S3StorageAdapter` uploads parts natively and can presign them, so the browser
PUTs straight to the bucket. `LocalStorageAdapter` cannot presign, so it relays:
parts land as `<key>.part/<n>`, each beside a `<n>.etag` sidecar, and are
concatenated in part order on completion.

Both implement `list_parts(key, upload_id)`, which answers what storage actually
holds. That is the authoritative manifest the `complete` endpoint uses — see
below for why the client's own list cannot be.

Abandoned S3 parts are billed but never appear in `list_objects`, so
`S3StorageAdapter.ensure_multipart_lifecycle()` installs a bucket rule expiring
incomplete uploads after 7 days. It returns `False` (logged, not raised) on a
bucket it lacks permission to configure, or an S3-compatible endpoint with no
lifecycle API.

Source: `src/pocketpaw/uploads/adapter.py` (the protocol),
`src/pocketpaw/uploads/s3.py`, `src/pocketpaw/uploads/local.py`.

### Large files — the endpoints

Five routes under the existing `/api/v1/uploads` prefix, enterprise only. They
carry the same gates as `POST /uploads`: guest refusal
(`403 {"code": "guest_upload_forbidden"}`), `uploads.write` membership, pocket
ABAC, and folder-chain auto-create. An upload session is workspace-scoped —
another workspace's session answers `404`, never a `403` that would confirm the
id exists.

**`POST /uploads/multipart`** — open a session.

```jsonc
// request
{ "filename": "raw.mov", "size": 2147483648, "mime": "video/quicktime",
  "chat_id": null, "pocket_id": null, "path": "/footage" }

// 201
{ "upload_id": "mpu_01J…",        // ours, not the storage provider's
  "mode": "presigned",             // or "relay" — the client does not choose
  "key": "chat/202609/….mov",
  "part_size": 8388608,
  "part_count": 256,
  "parts": [{ "part_number": 1, "url": "https://…", "expires_at": "…" }],
  "expires_at": "2026-09-21T09:00:00Z" }
```

`parts` is present only in `presigned` mode, at most 256 URLs per response,
each valid for an hour. The status route re-mints the rest as the upload
progresses, so no URL expires before the client reaches it. `size` must be the
file's real size: `part_count` is derived from it, and `complete` requires every
part in that range.

**`PUT /uploads/multipart/{upload_id}/parts/{part_number}`** — relay mode only.
Raw binary body, one part, returns `{"part_number": 3, "etag": "…"}`. A body
larger than the session's `part_size` is refused on its `Content-Length` before
it is read. Re-PUTting a part replaces it, so a retry is safe.

**`POST /uploads/multipart/{upload_id}/complete`** — assemble and land the file.

```jsonc
{ "parts": [{ "part_number": 1, "etag": "\"abc…\"" }] }   // OPTIONAL, advisory
```

Returns exactly the shape `POST /uploads` puts in `uploaded[]` — `id`,
`filename`, `mime`, `size`, `url`, `created`. This is the only place the
`FileUpload` row is written and `FileReady` is emitted, so a multipart upload is
indistinguishable from a simple one downstream.

**The manifest comes from storage, not from the request.** The server asks the
provider which parts exist (S3's `ListParts`, paginated) and completes from
that. A client that reloaded mid-upload has no etags for the parts its previous
session sent, and in `presigned` mode the server never observed those PUTs
either, so a complete that required them was unsatisfiable after exactly the
event resume exists for.

`parts` in the body is therefore optional. Send it when you have it and it is
checked against storage: a part number whose etag disagrees is `409
multipart.invalid`, because the two sides are describing different bytes. A
partial list is fine. An absent one is the normal resumed case. Two entries for
one part number that disagree with *each other* are `400` — the client has
contradicted itself before storage is consulted. Etags compare on content, so
quoted, unquoted and `W/`-prefixed forms all match.

Before completing, storage's part set must cover `1..part_count` with no gaps.
A gap is `409`, not a silently truncated object: S3 would otherwise assemble
what it has and land a short file in the library at a plausible size.

This also means **no bucket CORS change is needed**. A presigned PUT's `ETag`
response header is invisible to the browser unless the bucket sets
`Access-Control-Expose-Headers: ETag`; because the client never reads it under
this design, that requirement does not exist. `ensure_cors` is deliberately
unchanged.

**`GET /uploads/multipart/{upload_id}`** — status / resume. Returns
`{upload_id, mode, key, part_size, part_count, received, parts, expires_at}`,
where `parts` re-mints URLs only for what is still missing. `mode` is repeated
here so a resumed client knows whether to PUT to the bucket or to the relay
route without inferring it from whether URLs came back. Note that `received`
counts only parts that passed through the relay route, so on the `presigned`
path it stays empty — storage, not this field, is what `complete` consults.

**`DELETE /uploads/multipart/{upload_id}`** — abort. Drops the provider upload,
releases the daily-budget claim, marks the session dead. `204`, idempotent.

**Where the ceilings are checked.** `POST /uploads` checks the storage plan cap
and the daily budget *after* the write and rolls back by deleting the blob.
Multipart cannot work that way — the user would transfer 5 GB and be refused at
the end — so both are checked at **init** against the declared size, and again
at **complete** against the size storage actually reports. The declared size is
client-supplied, so a client that under-declares to slip past init is still
caught at complete and its object deleted. The daily-budget claim is reserved at
init and released on abort or expiry, charged to the UTC day it was claimed on.

| Status | Code | When |
| --- | --- | --- |
| 400 | `multipart.invalid` | bad size/mime/filename/part numbering, or a client `parts` list that contradicts itself |
| 402 | `billing.storage_limit` | over the workspace's plan storage cap |
| 403 | `guest_upload_forbidden` | guest account |
| 403 | `files.pocket_forbidden` | no pocket edit access (checked at init *and* complete) |
| 404 | `multipart.not_found` | unknown, already-completed, another workspace's id, or an upload storage has since dropped |
| 409 | `multipart.expired` | session past its TTL |
| 409 | `multipart.invalid` | storage is missing parts, or holds a part whose etag disagrees with the client's |
| 413 | `multipart.too_large` | over `POCKETPAW_MAX_LARGE_FILE_BYTES`, or a part over `part_size` |
| 429 | `uploads.daily_limit` | over the workspace's daily upload budget |

Note the envelope differs from the older routes on this router: these raise
`CloudError`, so the body is `{"error": {"code": …, "message": …}}`, whereas
`POST /uploads` still answers `{"detail": "files.pocket_forbidden"}`. The guest
refusal carries its top-level `code` in both.

Session state lives in the ee Mongo collection `multipart_uploads`, with a TTL
index that reaps a row a day after it expires — the grace exists so an expired
session answers `409` rather than `404`.

Source: `ee/pocketpaw_ee/cloud/uploads/router.py`,
`ee/pocketpaw_ee/cloud/uploads/multipart_service.py`,
`ee/pocketpaw_ee/cloud/uploads/multipart_store.py`.

## Files — Content Search

`POST /files/search` answers "which of my files says this?" — as distinct from
the listing's filename filter, which answers "which of my files is called
this?". It searches the caller's kb-go scopes and resolves each hit back to the
FILE row that was ingested into it, via the `kb_article_id` the FileReady
listener records. Rows come back in the same shape `GET /files` returns, plus a
`match` block, so a client renders a hit with the components it already has.

Why it is not a mode on `POST /kb/search`: that endpoint returns kb *articles*
(compiled derivatives, not files) and accepts a client `scope` override bound to
the caller by an allowlist. This one returns file rows and accepts **no scope at
all** — its scopes are derived from the caller through the same
`_kb_scopes_for_context` precedence the chat path uses (`user:` > `pocket:` >
`agent:` > `workspace:`). The only partition a client may ask for is
`pocket_id`, gated by the same membership check the listing uses.

Requires a valid license and the `kb.read` action — the `match` block carries kb
titles and summaries, so this is a kb read whichever door it comes through.

**Request**

```json
{ "query": "quarterly revenue", "limit": 20, "pocket_id": null }
```

`limit` is 1–50 (default 20). A non-member `pocket_id` returns 403
`files.pocket_forbidden`, exactly as `GET /files?pocket_id=` does.

**Response**

```json
{
  "workspace_id": "w1",
  "pocket_id": null,
  "query": "quarterly revenue",
  "files": [
    { "id": "f1", "source": "chat", "filename": "board-minutes.docx",
      "mime": "…", "size": 1234, "url": "/api/v1/uploads/f1",
      "created": "…", "chat_id": null, "tags": [], "collections": [],
      "summary": "…", "agent_id": null,
      "match": { "article_id": "board-minutes", "scope": "workspace:w1",
                 "title": "Board minutes", "snippet": "…",
                 "verbatim": false } }
  ],
  "scopes": ["user:u1", "workspace:w1"],
  "degraded": null,
  "limit": 20
}
```

Results are in kb-go's BM25 rank order. Files that were never ingested (no
`kb_article_id`), files hidden from AI, soft-deleted files, and — on a
workspace-scoped search — pocket-scoped files are all absent by construction.

**`degraded` — read this before rendering an empty list**

| value | meaning | what a client must show |
| --- | --- | --- |
| `null` | normal search | the results, whatever their number |
| `"kb_unavailable"` | kb-go could not be reached; the search did **not** run | "couldn't search" — never "nothing matched" |
| `"verbatim"` | at least one hit is an uncompiled article (kb-go's keyless fallback), so it was matched on raw text | the results, plus a note that some were matched literally |

The `kb_unavailable` distinction is the point of the field. An empty `files`
array is an answer; a failed search is not one, and rendering them identically
is how a broken feature passes for a working one with nothing to show.

Source: `ee/pocketpaw_ee/cloud/files/content_search.py` (the service and the
argument for the surface), `ee/pocketpaw_ee/cloud/files/router.py` (the route),
`ee/pocketpaw_ee/cloud/uploads/mongo_store.py::list_by_kb_articles` (the join
and its tenancy filters).

## Files — Versioned Writes

The `file_versions` entity layers a versioned write path over the uploads
storage adapter. A file's live (current) content lives in the
StorageAdapter; each edit archives the prior blob as a `FileVersionDoc` row
and bumps a per-file `content_version` counter on the `FileUpload` record.
All four routes share the `/files` prefix with the listing router (`GET
/files`, `/files/tree`, `/files/browse`) without collision, require a valid
license, and are workspace-scoped — every read is filtered to the caller's
workspace.

### `POST /files/write`

Create a file, or overwrite an existing one in place (versioned). Used for
first-save and programmatic writes.

Request body:

```json
{ "path": "<file id or path>", "content": "<full content>", "filename": "<optional display name>" }
```

Response `201`:

```json
{ "fileId": "<id>", "version": 1, "sizeBytes": 42 }
```

When a file already exists for `path`, the content is updated through the
PUT path instead and `version` reflects the bumped counter.

### `PUT /files/{file_id}`

Replace a text file's content inline with optimistic concurrency. Body:

```json
{ "content": "<new text>", "expectedVersion": 3 }
```

`expectedVersion` (or the `If-Match: <version>` header, which takes
precedence) guards against lost updates. Response `200`:

```json
{ "fileId": "<id>", "newVersion": 4, "sizeBytes": 57, "contentHash": "<sha256>" }
```

Returns `404` if the file is missing, `409` on a version conflict, and
`422` if the file's mime type is not editable inline.

### `GET /files/{file_id}/versions`

List archived versions for a file (oldest first, content omitted). Returns
only versions in the caller's workspace:

```json
[ { "id": "<oid>", "fileId": "<id>", "versionNumber": 2, "sizeBytes": 42,
    "editorKind": "human", "editorId": "<user id>", "createdAt": "<iso>" } ]
```

### `GET /files/{file_id}/versions/{version_id}`

Fetch a single archived version with its full content (for revert preview /
diff). Workspace-scoped — a version id from another workspace returns `404`.

## Files — Links and Graph

Text notes (`text/markdown`, `text/plain`) are parsed at ingest for
`[[wikilinks]]`, `#hashtags` and frontmatter `tags:`. The normalized link
targets land on the `FileUpload` row as `link_names` (`list[str]`, empty for
anything that is not a text note); hashtags and frontmatter tags merge into
the row's `tags` ahead of the derived keyword tags, so a tag the author typed
is never the one dropped at the cap. Hidden-from-AI files get neither, same as
they get no auto-tags. `POST /files/write` and `PUT /files/{file_id}` now emit
`FileReady` (a no-op save with unchanged content emits nothing), so a note made
or saved in the editor is extracted, tagged, linked and KB-indexed exactly like
an upload; when a re-index lands under a new kb-go article id the previously
tracked article is removed, so one file tracks one article.

A link name is `normalize_link_name(text)`: trimmed, casefolded, one trailing
`.md` dropped. `[[Name]]`, `[[Name|alias]]`, `[[Name#heading]]` and
`[[Name#heading|alias]]` all resolve to `name`, and a file resolves a name when
`normalize_link_name(filename)` matches. Two live files sharing a stem resolve
to the newest. Both routes require `kb.read` (any workspace member) and a valid
license.

### `GET /files/{file_id}/links`

What this file links to, and what links to it. Resolution runs inside the
file's own scope (its pocket, or workspace-only rows), so a pocket-private
note never appears as another scope's linked mention.

```json
{ "outgoing": [ { "name": "beta", "file_id": "<id>", "filename": "Beta.md" },
                { "name": "ghost", "file_id": null, "filename": null } ],
  "backlinks": [ { "file_id": "<id>", "filename": "A.md", "mime": "text/markdown" } ] }
```

An unresolved name comes back with a null `file_id` so the client can offer to
create that note. `404 file.not_found` for a missing or cross-workspace id.

### `GET /files/graph?pocket_id=`

The library as a link graph. Same pocket rule as `GET /files`: with
`pocket_id` the caller must be a pocket member (`403 files.pocket_forbidden`
otherwise); without it only workspace-scoped rows are read. At most 2000 live
files are considered, newest first; `truncated` is `true` when there were
more.

```json
{ "nodes": [ { "id": "<file id>", "filename": "A.md", "mime": "text/markdown",
               "tags": ["todo"], "collections": [] } ],
  "edges": [ { "source": "<file id>", "target": "<file id>" } ],
  "ghosts": [ "ghost" ],
  "truncated": false }
```

Edges are deduplicated (`[[B]]` twice in one note is one edge). `ghosts` are
the link names no file resolves.

## Agent — Read Chat Rooms (`list_rooms` / `read_room`)

Two read-only in-process MCP tools that let the chat agent read the user's own
PocketPaw chat rooms: channels like #general, groups and DMs. Before these
existed, "catch me up on #general" got an answer about Slack not being
connected. Registered via the `pocketpaw.mcp_servers` entry point (`rooms` →
`pocketpaw_rooms` → `mcp__pocketpaw_rooms__list_rooms` /
`mcp__pocketpaw_rooms__read_room`). Source:
`ee/pocketpaw_ee/agent/mcp_servers/rooms.py`. Nothing here sends, edits or
reacts.

**`list_rooms`** `{ "query"?: "...", "limit"?: n }` returns
`{rooms: [{id, name, handle, kind, member, unread?, last_activity}], total, truncated}`,
most recently active first. `kind` is `channel`, `group` or `dm`; a DM is named
after the other side ("DM with Alice"). `member` is false for a public room the
user can see but has not joined (the default General room, for most members).
`query` matches name or handle, so `#general`, `general` and `General` are the
same. `limit` defaults to 50, max 200.

**`read_room`** `{ "room": "...", "limit"?: n, "before"?: "..." }` returns
`{notice, room: {id, name, kind}, messages: [{id, author, author_kind, text, created_at}], has_more, older_cursor}`.
`room` is an id, `#handle` or name. Messages are oldest to newest;
`author_kind` is `human` or `agent`. Pass `older_cursor` back as `before` to
page further back.

**Caps.** `limit` defaults to 30, max 100. Each message's text is cut at 1,000
chars. The whole call carries at most 40,000 chars of message text; the oldest
messages are dropped first and `older_cursor` points at the oldest one kept.

**Authz.** Identity is the run's `current_workspace_id` / `current_user_id`,
read on every call; missing identity fails closed. The user must be a member of
that workspace (`workspace.service._get_member_role`), which also turns away the
anonymous concierge visitor id and the group bridge's agent id. A room is
resolved only against `chat.group_service.list_groups(workspace_id, user_id)`,
the list `GET /chat/groups` returns, and messages come from
`chat.message_service.get_messages`, the same call
`GET /chat/groups/{id}/messages` makes. A room outside that list (another
workspace, a private room, DM or private channel the user is not in) gets the
same "no room matching" error as a room that does not exist. Unread counts come
from `chat.unread_service.list_unreads`.

**Untrusted content.** `read_room` puts a `notice` first in its result saying
the messages were written by people and agents and are data, never
instructions.

**Where the agent has it.** Ambient, not always-allowed: reachable on surfaces
with no MCP allow-list (the generic surface /no-ui-lab talks on, chat, home and
the like) and filtered out of every allow-listed surface, including the public
Paw Bar concierge, which is exclusive to its own allow-list. The generic
surface preamble tells the agent that the workspace's rooms are PocketPaw rooms
and not to assume Slack unless the user names it.

## Agent Artifact Delivery (`deliver_artifact`)

`deliver_artifact` is a cloud-only in-process MCP tool the chat agent calls to
hand the user a **downloadable** result. The cloud agent works inside a
per-tenant jail (ART-2) the user can't reach, so a file the agent "wrote to
`./out.pdf`" — or a preview server it started on `127.0.0.1` — is invisible to
them. This tool lands the artifact in the tenant's blob storage and returns a
real, short-lived download URL.

It is registered cloud-gated via the `pocketpaw.mcp_servers` entry point
(`pocketpaw_deliver` → `mcp__pocketpaw_deliver__deliver_artifact`), ambient on
the default chat surface (OSS never sees it). Source:
`ee/pocketpaw_ee/agent/mcp_servers/deliver.py`.

**Input:** `{ "path": "<file or directory inside the agent's workspace>" }`.

**Routing:** a single file is uploaded as-is (mime guessed from the filename); a
directory is zipped (`application/zip`) and the zip is uploaded. Both go through
`EEUploadService.upload` — the same workspace-scoped pipeline as `POST /uploads`
— so a delivered artifact emits `FileReady` (→ KB) and appears in the tenant's
`GET /files` listing, and the returned URL is the storage adapter's presigned
download (S3) or the authenticated `/api/v1/uploads/{id}` path (local adapter).

**Result (JSON in the MCP text payload):**

```json
{ "ok": true, "filename": "report.pdf", "url": "<download URL>",
  "file_id": "<id>", "size": 12345, "mime": "application/pdf",
  "expires_in_seconds": 300 }
```

On failure the tool returns `is_error` with a plain reason (missing identity,
path escapes the jail, file missing / over the size cap, or the upload failed) —
the agent surfaces the reason rather than fabricating a link.

**Security:** the path must resolve to inside the caller's own jail
(`~/.pocketpaw/workspaces/<workspace_id>/...`); `..` traversal, absolute paths
out, symlinks pointing out (including symlinks nested inside a delivered
directory), and another tenant's jail are all rejected (reusing ART-2's
path-segment guard). Size is capped by `POCKETPAW_DELIVER_MAX_MB` (default
`100`). Because the upload relaxes the mime allowlist, a delivered artifact
whose mime is not in `INLINE_MIMES` (HTML, SVG, JS, …) is served with
`Content-Disposition: attachment` on the presigned download — it downloads, it
does not render inline on the storage origin. Inline-safe types (images, pdf,
plain text) still embed as before. The whole tool is gated on
`is_multi_tenant_cloud()`.

## Agent — Open an App Surface (`open_surface`)

`open_surface` is an in-process MCP tool that lets the chat agent open an app
surface on the user's screen: the file picker, the clip editor, a chat room,
Pockets or Knowledge. It has no server-side effect. The tool validates its input
and returns an envelope; the run loop turns that into an `open_surface` event on
the chat stream, and the browser does the opening. Registered via the
`pocketpaw.mcp_servers` entry point (`surfaces` → `pocketpaw_surfaces` →
`mcp__pocketpaw_surfaces__open_surface`). Source:
`ee/pocketpaw_ee/agent/mcp_servers/surfaces.py`.

**Input:** `{ "route": "...", "params"?: { "<key>": "<string>" }, "reason"?: "..." }`

- `route` is one of the atlas surfaces marked `agent_openable` (today `/files`,
  `/studio/editor`, `/chat`, `/pockets`, `/knowledge`; see
  `src/pocketpaw/atlas/authored/surfaces.json`). Anything else is rejected.
  Settings pages, `/audit`, `/security`, `/admin*` and malformed routes are
  refused even if atlas flags them. If atlas can't load, every route is refused,
  and with no openable route the tool isn't registered at all.
- `params` is a flat string-to-string map: at most 10 keys, keys up to 64 chars,
  values up to 500. A JSON-string `params` is decoded first.
- For `/studio/editor`, `params` is the clip handoff (`src`, `name`, `mime`,
  `kind`), and `src` must be the file's own backend path,
  `/api/v1/uploads/<file_id>` or `/api/v1/media/<name>` (one segment). External
  and presigned URLs are rejected.
- `reason` is an optional line (max 200 chars) shown to the user.

**Stream event:** `event: open_surface`, `data: {route, params?, reason?}`.
`params` and `reason` are omitted when empty. It is emitted in addition to the
normal `tool_result` frame.

**Checks between the tool and the event.** `run_core` promotes the envelope
only when the tool result's name is exactly
`mcp__pocketpaw_surfaces__open_surface` or `open_surface`; a result with an
unresolved name is dropped. It then re-runs the tool's own validation on the
payload. The reason is prompt injection: a web page or file the agent reads can
contain a well-formed envelope, and promoting on text shape alone would let that
content navigate the user's browser.

**Where the agent has it.** The server is ambient but not always-allowed, so the
tool is reachable on surfaces with no MCP allow-list (the generic chat surface,
which is what the /no-ui-lab talks on) and on `/studio/editor`, whose allow-list
names it so the agent can send the user to `/files` for another clip. Every
other allow-listed surface filters it out, including the public Paw Bar
concierge.

## Atlas — Surfaces, Verbs and Search

Atlas (`src/pocketpaw/atlas/`) is the one place that says which app surfaces and
composer verbs exist, where they open, who can trigger them and how risky they
are. The composer reads it here; the agent reads the same entries through
`atlas_search`. Source: `src/pocketpaw/api/v1/atlas.py`.

All three routes are read-only. They need an active signed-in user (a cloud
session the auth bridge verified, for a user whose account is active) or a
caller holding the `chat` scope; anyone else gets 403. Answers go through the
atlas overlay for the caller's workspace. With a signed-in user and workspace,
the overlay resolves the caller's workspace role, so role-gated entries (`role:*`
in `requires`) show up only for roles that clear them: an owner sees the
owner-only `surface:security` (29 surfaces), an admin or member doesn't (28), and
the admin capability cards follow the same tiers. If the role can't be resolved,
every role-gated entry stays hidden.

### `GET /api/v1/atlas/surfaces`

```json
{ "surfaces": [ { "id": "surface:files", "name": "Files", "summary": "...",
  "route": "/files", "slash": "files", "presentation": "inline",
  "agent_openable": true, "keywords": ["files", "..."] } ] }
```

- `slash` is the composer command: the route without its leading `/`
  (`files`, `deep-work`, `agents/activity`, `studio/editor`), `home` for `/`, or
  `null` (settings sub-pages, `/decisions-graph`). The atlas build refuses a
  slash that doesn't match its route.
- `presentation` is `"inline"` for the views the no-UI shell renders in the
  thread (`/chat`, `/files`, `/deep-work`, `/pockets`, `/sites`, `/knowledge`,
  `/studio`) and `"window"` otherwise.
- `agent_openable` marks the routes the agent's `open_surface` tool may open.
  Settings pages, `/audit`, `/security` and `/admin*` can never be openable: the
  atlas build refuses it and the tool filters them again.

### `GET /api/v1/atlas/verbs`

```json
{ "verbs": [ { "id": "verb:send", "name": "Send to a channel", "summary": "...",
  "slash": "send", "applies_to": ["channel"], "triggers": ["slash"],
  "risk": "risky", "undo": false, "keywords": ["send", "..."] } ] }
```

- `applies_to`: the object types the verb acts on (`channel`, `file`, `task`,
  `room`, `message`, `pocket`, `site`, `article`, `panel`).
- `triggers`: `slash` (a composer command), `verb` (an action on the object),
  `agent` (the agent does the work).
- `risk`: `read` changes nothing; `safe` changes the user's workspace objects in
  a benign or reversible way; `risky` speaks for the user where others read it
  (send, reply, edit a sent message, publish) or deletes with no undo. `undo` is
  true exactly where the composer offers an Undo.
- Navigation is not a verb: the surface `slash` values cover it.

### `GET /api/v1/atlas/search?q=<text>&kinds=surface,verb&limit=5`

```json
{ "query": "rename this file", "results": [ { "id": "verb:file-rename",
  "kind": "verb", "name": "Rename file", "route": null, "slash": null,
  "score": 0.769 } ] }
```

- `q`: 1 to 200 chars. `limit`: default 5, at least 1, values above 20 are
  capped to 20. `kinds`: comma-separated subset of `surface`, `verb`,
  `capability`, `primitive` (default: all four, at most 64 chars); anything else
  is 422. Other
  atlas kinds (widgets, connectors, skills, senses) never come back.
- `route` is the entry's home route, or `null`.
- `score` is 0..1, highest first. Atlas ranks by weighted word overlap (a name
  match counts most, then keywords, summary and narrative). The API divides that
  raw score by the score of a name match on every distinct query word, then
  rounds to 3 places. A name that is the only one in atlas carrying the query
  word scores 1.0 for a primitive and 0.96 for other kinds; a word many names
  share is worth less.
- A verb that matches only on its object noun ("files" for `verb:file-delete`)
  scores at 0.4 of its raw match, so a navigational query ("show me my files")
  lands on the surface with a clear margin; an action word ("delete",
  "download") lifts that. Exact ties are deterministic: verbs last, then the
  entry whose name the query covers more, then kind, then id.

## Sites — Native Editing

NE-4b / NE-5b. The **native site editor** renders a svelte Paw Site directly in
the dashboard — the built page's markup is injected into a shadow root rather
than framed in an iframe — and persists in-place text / prop edits by splicing
them back into the pocket's component source as a **reviewable Branch draft**.
Two endpoints back it: one serves the render, one persists the edits. Source:
`ee/pocketpaw_ee/sites/router.py`.

Both require an authenticated workspace context, the `fabric.write` action, and
the `sites` plan feature (the whole sites router is gated on it). Both operate
on a **svelte** Paw Site — a pocket with `engine: "svelte"` and a `source`
component map; a ripple-engine or non-site pocket is a `422`
(`pocket.not_svelte_site`). A missing or access-denied pocket surfaces as `404`
(`pocket.not_found`) / `403` (`pocket.access_denied`) from the pockets service,
exactly like every other `by-pocket` route. All work is tenant-scoped on the
request context (`workspace_id`, `user_id`).

**HE-9 widened this.** `/leaf-edits` now accepts **html** pockets as well as
svelte, and a third endpoint (`/html-armed-source`) serves the html lane's render.
The engine guard moved from a flat `engine != "svelte"` to a write-back predicate,
so the refusal code is now `pocket.not_editable_site`. Note the two questions are
still separate and neither implies the other: **react** can be selected and
refined through chat (it has an armed build) but has no TSX splice, so it is still
refused here; **html** has a splice but no armed build, so it is accepted here and
served by `/native-artifact` only as a `preview_url` (no body/css).

### `GET /sites/by-pocket/{pocket_id}/html-armed-source`

HE-9. Serve an **html** pocket's source with `data-uid` stamped on every editable
leaf, plus the leaf manifest behind those uids.

This exists because select and write did not share an identity. The builder
previews an html pocket by assembling its **raw** source into a sandboxed
`srcdoc` — no build, which is the whole point of the html track — so nothing in
that document carried a uid. The in-frame select agent resolved a click by walking
the DOM (`<section>:<tag>:<ordinal>`) while `/leaf-edits` resolves by manifest uid
(`<page>:<role>:<ordinal>`): two schemes over two documents, so a pick named
something the splice could never find. This returns the document the builder
should actually render, so both sides agree by construction.

It is **not** the html branch of `/native-artifact`. That one answers an html
pocket with a `preview_url` on the draft preview origin (no body/css to
shadow-render). This is a parse and an offset splice over the source map:
no bun build, no Daytona, no artifact cache — cheap enough to call when the
operator opens Design mode.

A non-html pocket is a `422` (`pocket.not_html_site`). A failed or unavailable
toolchain is `sites.arm_html_failed` rather than an opaque 500.

Response `200`:

```json
{
  "pocket_id": "p_abc123",
  "source": { "index.html": "<h1 data-uid=\"index:headline:0\">Build faster</h1>" },
  "manifest": [
    { "uid": "index:headline:0", "file": "index.html", "editKind": "text" }
  ]
}
```

`source` is byte-identical to the authored files apart from the inserted
attributes — the stamping is an offset splice, never a re-serialize, so quote
style, attribute order and entities all survive.

**Re-arm; do not cache.** uids are *derived* from the current source, not stored
in it, so spans shift the moment an edit lands and a held manifest is stale by
construction. That is a feature: a stale uid fails loudly instead of splicing into
the wrong offsets.

### `POST /sites/by-pocket/{pocket_id}/leaf-edits`

Persist a batch of native-editor leaf edits as a Branch draft. The editor has
already rendered each edit optimistically; this splices them into the pocket's
svelte source and writes the draft — **there is no rebuild**. Skipping the
per-edit iframe rebuild is the UX win over the older `edit_svelte_component`
path; an approved review is what later takes the draft live.

Request body:

| Field | Type | Notes |
|-------|------|-------|
| `edits` | array | Required, non-empty. Each entry is one leaf edit. An empty list is a `422` (`site_leaf_edit.empty_edits`). |

Each edit:

| Field | Type | Notes |
|-------|------|-------|
| `uid` | string | The stable id of the edited leaf, e.g. `"Hero:headline:0"`. |
| `op` | object | The change. One of `{ "kind": "setText", "html": "<new inner HTML>" }` or `{ "kind": "setProp", "name": "<prop>", "value": <any> }`. The `op` shape is validated downstream by the `apply-leaf-edit` CLI, not at the request boundary. |

Response `200`:

```json
{
  "pocket_id": "p_abc123",
  "results": [
    { "uid": "Hero:headline:0", "applied": true, "reason": null },
    { "uid": "Pricing:cta:2", "applied": false, "reason": "uid not found in current source" }
  ]
}
```

One verdict per submitted edit, in submission order. `applied` is whether the
splice landed; `reason` (CLI-produced) explains a rejection and is `null` on
success. The caller keeps the whole-file re-author path for any leaf that comes
back `applied: false`.

**How it persists.** Edits apply **in order** through the paw-sites
`apply-leaf-edit` CLI — a pure source transform, no build or workerd. Only the
files whose contents actually changed are persisted (each write auto-writes the
Branch-draft snapshot), so a rejected edit churns no draft. A **dynamic** svelte
site carries its live-data bindings (`objects` / `sources` / `actions` / `auth`)
as sibling keys of the `{path: contents}` file map; the service splits those out
before the splice (the CLI treats every source key as a file) and **confines the
persist loop to the original file keyspace** — a binding key, or a brand-new
path the CLI might echo, is never written back as a component file.

Errors:

| HTTP | Code | When |
|------|------|------|
| 422 | `site_leaf_edit.empty_edits` | The `edits` list is empty. |
| 422 | `pocket.not_svelte_site` | The pocket is not a svelte Paw Site (no component source map). |
| 404 | `pocket.not_found` | Unknown pocket id. |
| 403 | `pocket.access_denied` | The caller lacks access to the pocket. |
| 500 | `sites.leaf_edit_failed` | The `apply-leaf-edit` CLI exited non-zero, timed out, or returned unparseable output. |

### `GET /sites/by-pocket/{pocket_id}/native-artifact`

Serve a site draft: its `preview_url` on the draft preview origin, plus (svelte and
react) the armed build's body markup and CSS so the native editor can shadow-render
the site.

Available on `svelte`, `react` and `html`. Not `ripple` (no source map). The two
built engines emit a prerendered `index.html`; only the build output directory
differs (`.svelte-kit/cloudflare` or `build` vs `dist`), and the server resolves
that per engine.

**`preview_url` — the full draft on its own origin.** An absolute URL of the form
`https://<token>.<PAW_SITES_PREVIEW_BASE_URL host>/index.html`: the draft's
`index.html` with its `<head>` and module scripts intact, every JS chunk, stylesheet,
image and public file beside it, served with `Access-Control-Allow-Origin: *` and no
cookies. `<token>` is a 32-hex-char capability minted per content hash, so an edit
gets a new URL. `null` while a build is pending or failed; show the build state
then. Also `null` beside a served render (`build_status: "none"`) when the artifact
store refused or failed to keep the draft's files, or the preview base URL is
misconfigured: the server retries that at most once per draft every 10 minutes
rather than rebuilding on every view. Append `?paw_edit=1` (and the usual `paw_nonce`) to arm the edit bridge,
which talks to the builder over `postMessage` exactly like the live lane. Setup:
`docs/deployment/sites-draft-preview-origin.md`.

**html never builds.** An html pocket always answers `build_status: "none"`,
empty `body_html` / `css`, and a `preview_url` that serves its source files with the
declared packages' import map injected.

**Two response shapes, and `build_status` says which.** A warm read returns the
render; a cold one returns a build to poll.

Response `200` — the **render** (cache hit):

```json
{
  "pocket_id": "p_abc123",
  "body_html": "<div data-uid=\"Hero:root:0\">…<script id=\"paw-edit-manifest\">…</script></div>",
  "css": "/* concatenated stylesheets */",
  "build_status": "none",
  "build_reason": null,
  "build_job_id": null,
  "preview_url": "https://3f9c0d5e8a1b4c7d9e2f6a0b1c3d5e7f.paw-preview.example/index.html"
}
```

- `body_html` is the built page's `<body>` **inner** HTML — the `data-uid`-stamped
  editable leaves plus the embedded `<script id="paw-edit-manifest">`. The
  frontend injects it into a shadow root.
- `css` is the built stylesheet(s) — inline `<style>` blocks plus every linked
  stylesheet — concatenated into one string, injected as a single `<style>`.
- `build_status` is `"none"` — a served render is not a build in any state.

Response `200` — **build pending** (cache miss, SP-2):

```json
{
  "pocket_id": "p_abc123",
  "body_html": "",
  "css": "",
  "build_status": "queued",
  "build_reason": null,
  "build_job_id": "site-preview-p_abc123-9f2c…",
  "preview_url": null
}
```

- `body_html` and `css` are empty strings, not null — the field types never change
  between the two shapes.
- `build_status` is `queued` (this call started the build), `building` (one was
  already in flight for this exact render), or `failed` (a build of this exact
  render already finished without producing an artifact). The vocabulary is the
  publish lane's, unchanged, and **an unrecognised value means in-progress**.
- `build_reason` carries a rung name on a failure (`build_failed:…`,
  `scaffold_failed:…`, `sandbox_unavailable:…`, `preview_unreadable:…`) — never the
  build's stderr, which stays in the worker log. `sandbox_unavailable:capacity`
  means the Daytona org's resource limit stayed full for the whole retry window
  (try again in a few minutes); `sandbox_unavailable:no_sandbox` means a sandbox
  could not be created for any other reason. While a build waits for capacity it
  reads `queued` with reason `waiting_for_capacity`.
- `build_job_id` is the handle to poll with. **Re-fetch this endpoint** until
  `build_status` reads `"none"`, which is the render shape above.

**One render is one sandbox.** The job id is derived from the same content hash the
cache is keyed on, so polling during a build addresses the job already running
rather than queueing another. A source change produces a different hash and
therefore a different job.

**Read-through cache — a plain view never builds.** The endpoint hashes the
pocket's render inputs (source map + theme + builder origin + engine + generator
version) and serves a prior render from the on-disk artifact store
(`~/.pocketpaw/site-artifacts/<pocket_id>/<hash>.json`) on a **cache hit** — zero
builds, zero sandboxes. Publishing a site and every source-changing edit **pre-warm**
the store in the background, so a live/clean site is a hit.

**Shared store (opt-in) — `PAW_SITES_ARTIFACT_STORE=s3`.** The on-disk store above is
per-container, so on a multi-replica deploy a view routed to replica B misses what
replica A built, and a redeploy empties the cache entirely. Setting
`PAW_SITES_ARTIFACT_STORE=s3` keeps the same `(pocket_id, content_hash)` key but puts
the artifact in blob storage through the configured `StorageAdapter`, so it is shared
across replicas and survives a redeploy. **Unset (the default) is the on-disk store** —
OSS installs and local dev are unchanged. The adapter itself is chosen by
`POCKETPAW_UPLOAD_ADAPTER`, which must also be `s3` for this to be remote storage; a box
that sets only the first knob gets a local-disk adapter writing the same
`~/.pocketpaw/site-artifacts/` layout. Both sides stay best-effort: a miss, a corrupt
object, a timeout, or a failed write degrades to a rebuild and never fails a preview.
The store also **refuses to persist an artifact whose rendered body or CSS carries a
per-site capture key** — that secret is only acceptable because it lives in a container
that is then destroyed, so such a pocket rebuilds on every view instead of caching. No
eviction runs here (the on-disk store's `PAW_SITES_ARTIFACT_KEEP` does not apply); put a
bucket lifecycle rule on the `site-artifacts/` prefix.

**`project` pockets.** A project pocket (the author owns the whole repo: package.json,
framework config, wrangler config) is also served here, with two extra fields:

```json
{
  "pocket_id": "p_abc123",
  "body_html": "",
  "css": "",
  "build_status": "none",
  "build_reason": null,
  "build_job_id": "site-preview-p_abc123-9f2c…",
  "preview_url": "https://3f9c….paw-preview.example/index.html",
  "preview_mode": "static",
  "capabilities": {"select": false, "text": false, "code": true, "build_log": true}
}
```

- It is never armed: `body_html` / `css` are always empty and the draft is only
  `preview_url`. A cold read queues a Daytona build (`paw-sites-gen project-build`)
  with the same `queued` / `building` / `failed` handle as above.
- `preview_mode`:
  - `"full"`: the preview is the whole site. Either the assets are the whole site,
    or (with `PAW_SITES_DRAFT_WORKERS=1`) a draft Worker runs the build's server
    code behind the preview URL.
  - `"static"`: the build has a worker, but the preview serves its static assets
    only.
  - `"server_only"`: nothing can be previewed and `preview_url` is `null`.
  - `"published"`: the files are exactly what is live and the pocket's drafts were
    deleted on publish. `preview_url` is the live site's URL (`null` when unknown).
    No draft is rebuilt; the next edit builds one.
  - `null` until a build finished.
- `capabilities` are the engine's edit/build flags. A project has no Select or Text
  tool yet (no generator-owned anchors); edits go through files and the agent, and
  every build keeps a log. Other engines answer `capabilities: null` for now.
- A project with no `package.json`, or a path that leaves the project, is a `422`.
- `build_reason` on a failed project build is `build_failed:<code>` with the CLI's
  code (`install_failed`, `build_failed`, `output_missing`, `wrangler_failed`,
  `size_limit`, `unknown_framework`, ...), or one of the rungs above.

### Project builds — `/sites/by-pocket/{pocket_id}/builds`

Two reads for a `project` pocket's sandbox builds. Both take `fabric.write` (owner /
editor) and the pockets service's read check, and are scoped to the caller's
workspace: another workspace's pocket is a `404`. A non-project pocket is a `422`
(`sites.not_a_project`). **No realtime event exists for site builds, so poll.**

#### `GET /sites/by-pocket/{pocket_id}/builds/latest`

The newest build, without its log. `404` when the pocket never built.

```json
{
  "pocket_id": "p_abc123",
  "job_id": "site-preview-p_abc123-9f2c…",
  "status": "building",
  "reason": null,
  "preview_mode": null,
  "framework": "astro",
  "updated_at": "2026-10-07T12:00:00+00:00",
  "current": true
}
```

`status` is `queued` / `building` / `built` / `failed`. `current` is true when this
build is of the pocket's current files (an edit since makes it false).

#### `GET /sites/by-pocket/{pocket_id}/builds/{job_id}/log`

One build's install, build and wrangler dry-run output. A `job_id` that is not this
pocket's is a `404`.

```json
{
  "pocket_id": "p_abc123",
  "job_id": "site-preview-p_abc123-9f2c…",
  "status": "failed",
  "reason": "build_failed:build_failed",
  "log": "$ bun install\n…\nerror at src/pages/index.astro:3 …",
  "log_truncated": false,
  "preview_mode": null,
  "updated_at": "2026-10-07T12:01:10+00:00"
}
```

The log is redacted before it is stored (tokens and keys, sandbox paths made
project-relative, capture keys) and capped to its last 64 KiB; `log_truncated` says
the head was cut. It is empty while the build is still running.

### Project files — `/sites/by-pocket/{pocket_id}/files`

A `project` pocket's repo, for the builder's Code view. The routes call the same
functions as the agent's file tools (`sites/project_tools.py`), so the path rules,
caps, lockfile rule and rebuild behaviour are identical (see "Project sites" under
"Sites — Agent Editing Tools"). Every route takes a session, the `sites` plan
feature and `fabric.write`; the pocket must pass the pockets read rule and belong to
the caller's workspace (another workspace's pocket is a `404`). Writes also need edit
access to the pocket. A non-project pocket is `422 sites.not_a_project`.

| Method + path | Body / query | `200` response |
|---|---|---|
| `GET .../files` | `?prefix=src/` (optional) | `{pocket_id, files: [{path, size}], file_count}` (`size` = UTF-8 bytes, sorted by path) |
| `GET .../files/content` | `?path=src/app.ts` | `{path, size, content}`, the whole file (never truncated) |
| `PUT .../files` | `{files: {path: contents}}` | write response |
| `POST .../files/patch` | `{path, edits: [{old, new}]}` | write response (`path` set) |
| `DELETE .../files` | `{paths: [path, ...]}` | write response (`deleted` set) |

The write response:

```json
{
  "pocket_id": "p_abc123",
  "written": ["src/pages/about.astro"],
  "created": ["src/pages/about.astro"],
  "deleted": [],
  "path": null,
  "lockfile_removed": [],
  "verification": {
    "status": "pending",
    "build": "pending",
    "job_id": "site-preview-p_abc123-9f2c…",
    "layers": [{"name": "build", "status": "pending"}]
  }
}
```

Every write saves the draft as one version, which changes the source's content hash,
and queues that hash's draft build: poll `GET .../builds/latest` (or the
native-artifact preview) with the `job_id`. `verification.status` is `passed` when
that exact source already built, `failed` with a `reason` when the tree cannot build
at all, and `unverified` when the build queue is down; the save stands in every case.

Refusals (nothing is saved): `422 sites.project_bad_path` (absolute, drive letter,
`..`, backslash, NUL, `node_modules/`, `.git/`, `.paw/`, `paw-build.json`, a real
`.env*` / `.dev.vars*` file other than `*.example`), `422
sites.project_file_too_large` (over 1 MiB), `422 sites.project_call_too_large` (over
4 MiB in one call), `422 sites.project_too_many_files` (over 200 paths),
`422 sites.project_too_large` (the project over 5,000 files / 50 MiB),
`422 site_edit.*` (an `old` that matches 0 or more than 1 time),
`422 sites.project_needs_package_json` (deleting package.json), `404
site_file.not_found` (a path to read, patch or delete that is not there).
A change to `package.json`'s dependency lists also removes a stale lockfile and lists
it in `lockfile_removed`.

### Site images — `/sites/by-pocket/{pocket_id}/assets`

Three endpoints for the images a site DISPLAYS. Added 2026-08-31
(feat/sites-public-asset-uploads).

**Why a separate rail exists.** A published site is read by anonymous visitors, so
an image it shows needs an address with no credential and no expiry. Neither URL
the rest of this API mints qualifies: `StorageAdapter.presigned_get` expires (S3
caps a presign at 7 days, and the site outlives it), and `/api/v1/uploads/{id}` —
what the screenshot capture and the `deliver_artifact` tool hand back — is
auth-gated, so it 401s for exactly the visitor the site exists to serve. The bytes
cannot ride the build either: the generator takes a **text-only** `source` map, and
its base64 `assets` sideband is rejected for every engine except `html`.

| Method | Path | Gate |
|---|---|---|
| `POST` | `/sites/by-pocket/{pocket_id}/assets` | `fabric.write` |
| `GET` | `/sites/by-pocket/{pocket_id}/assets` | `fabric.read` |
| `DELETE` | `/sites/by-pocket/{pocket_id}/assets` | `fabric.write` |

`POST` takes `multipart/form-data` with a single `file` part and returns
`{key, url, mime, size, filename}`. `url` is absolute, unsigned and permanent —
embed it verbatim. `GET` returns `{assets: [...]}`. `DELETE` takes `{"key": "..."}`
and answers 204.

**Images only, and that is the security boundary.** PNG, JPEG, GIF and WebP are
accepted; the type is decided by **magic bytes**, never by the part's
`Content-Type`, because on a public origin a caller who can label arbitrary bytes
`image/png` can host arbitrary content under our name. SVG is refused even though
it is an image: it is an XML document that executes script. Cap is 10 MB, and — as
with `/sites/import` — that gates PROCESSING, not ingress; Starlette spools the
whole body first, so bounding the raw request is the fronting proxy's job.

**Documents are NOT this rail.** A PRD or spec is agent *input*, not site content,
and publishing a customer's requirements doc to an unauthenticated bucket is a data
leak. Documents keep going through the private upload rail (`/api/v1/uploads`).

**Keys are `sites-assets/{workspace_id}/{pocket_id}/{sha256[:16]}-{stem}{ext}`** —
tenant-scoped, so no workspace can name another's object, and content-addressed, so
re-uploading the same file is free and the year-long immutable `Cache-Control` on
the object is safe. `DELETE` re-checks that prefix and refuses anything outside it.

**Configuration — `S3_PUBLIC_BUCKET`.** The rail needs
`POCKETPAW_UPLOAD_ADAPTER=s3` **and** `S3_PUBLIC_BUCKET` (a bucket whose objects
are world-readable; uploads carry `ACL=public-read`). `S3_PUBLIC_BASE_URL`
optionally puts a CDN or custom domain in front; unset, the URL is path-style over
`S3_ENDPOINT`. With no public bucket configured every endpoint answers **503** and
says so — there is deliberately no fallback to the private bucket, because a link
that 403s for every visitor fails later and looks like a broken site rather than a
missing setting. There is no local-disk equivalent: serving public assets from the
dashboard's own origin is a strictly worse place for visitor-facing bytes than a
separate bucket origin.

**The agent reads them via `list_site_assets`** (MCP, on the sites-manager server),
so a generated page uses the owner's real logo instead of a stock photo. Workspace
comes from the per-stream identity, never from the tool arguments.

**A cold miss builds in an ephemeral Daytona sandbox, not in this process (SP-2).**
It used to shell out to `bun` here, which works on a laptop and cannot work in the
deployed API container — there is no toolchain, so every cold preview raised and
reached users as `sites.generator_failed`. The build is now handed to the same
ephemeral lane the async publish path has used since SL-3: the API scaffolds nothing
and returns immediately, a worker scaffolds, installs and builds in a throwaway
sandbox, and writes the resulting `{body_html, css}` into the artifact store under
the content hash this call looked up. The per-site capture key is scrubbed out of the
job payload before it reaches Redis or the sandbox.

Because a miss now costs a sandbox rather than a local subprocess, the cache check is
what keeps an editing session affordable — measured in-sandbox build times are 8.70s
(react) and 14.67s (svelte), before sandbox create, upload and teardown.

**Auth is `fabric.write`, not a read scope,** because a cold miss still **queues an
armed build**: the build carries a builder origin so the generator stamps `data-uid`
on the editable leaves and embeds the edit manifest — the endpoint spends a sandbox,
so it is not a pure read. The builder origin is resolved
from the request's `Origin` header, falling back to the configured
`PAW_SITES_BUILDER_ORIGIN` when absent (the same precedence as `/editable` and
`/dev-preview`), so the call works with no header.

**Origin stability — the pre-warm must match the view.** The builder origin is part
of the content hash, so a pre-warm only saves a view a build when it builds with the
**same** origin that view resolves. A browser view resolves its origin from the
request `Origin` header (the dashboard origin), so `POST /sites/publish` and
`POST /sites/by-pocket/{id}/leaf-edits` thread their request `Origin` into the
background pre-warm — otherwise the pre-warm would fall back to
`PAW_SITES_BUILDER_ORIGIN` while the view uses the dashboard origin, the two hashes
would differ, and every view would stay a cold miss. Chat-agent / MCP publishes and
edits have no request origin, so their pre-warm keeps the env fallback: **set
`PAW_SITES_BUILDER_ORIGIN` to the dashboard origin** (e.g.
`https://paw.example.com`, not the `http://localhost:8888` default) in every
deployment so that fallback matches the origin views ask for — a belt-and-braces
default even where the request `Origin` is threaded.

**CSS reader is path-traversal-guarded.** Stylesheet `href`s from the built
`index.html` are resolved against the build tree (relative `./_app/…` and
absolute `/_app/…` hrefs both) and each resolved path is checked to be contained
inside the tree before it is read — a `../` traversal in a hand-authored
component's `<link>` is refused.

Errors:

| HTTP | Code | When |
|------|------|------|
| 422 | `pocket.no_native_edit_lane` | The pocket has no source map to serve (ripple). Renamed from `pocket.not_svelte_site` in RX-2, when react joined the lane. |
| 404 | `pocket.not_found` | Unknown pocket id. |
| 403 | `pocket.access_denied` | The caller lacks access to the pocket. |
| 503 | `sites.preview_build_unavailable` | The armed build could not be QUEUED (the job queue is unreachable). Retryable. A failed enqueue is deliberately an error rather than a pending response — a job id for a job nobody will run makes a client poll forever. A build that queues and then FAILS is not an error here: it comes back `200` with `build_status: "failed"` and a rung in `build_reason`. |

## Sites — Secrets

Runtime secrets for a site's Worker (a Stripe key, a webhook signing secret). The agent
asks for one by name (`request_site_secret`); the pocket owner types the value into the
builder; publishing binds every set secret as a `secret_text` binding, read in the
Worker as `env.<NAME>`. **No endpoint, tool or event ever returns a value.** Values are
encrypted at rest with the deployment Fernet key (`CLOUD_ENCRYPTION_KEY`); without it,
`PUT` answers 422 `cloud.encryption_key_missing`.

Secrets are keyed by pocket, so they exist before the first publish. Names are
`UPPER_SNAKE_CASE`: a letter first, then `A-Z`, `0-9` or `_`, at most 64 characters.
At most 50 secrets (set or pending) per site.

| Method | Path | Workspace gate | Pocket gate |
|---|---|---|---|
| `GET` | `/sites/by-pocket/{pocket_id}/secrets` | `fabric.read` | edit access (owner, team, `shared_with`, or a workspace-visible pocket) |
| `PUT` | `/sites/by-pocket/{pocket_id}/secrets/{name}` | `fabric.write` | pocket owner |
| `DELETE` | `/sites/by-pocket/{pocket_id}/secrets/{name}` | `fabric.write` | pocket owner |

A pocket in another workspace is a 404 for every route, owner or not.

**`GET`** returns:

```json
{
  "pocket_id": "...",
  "can_manage": true,
  "secrets": [
    {"name": "STRIPE_KEY", "status": "set", "description": "", "requested_by": null,
     "requested_at": null, "updated_at": "2026-10-07T10:00:00Z"},
    {"name": "RESEND_KEY", "status": "pending", "description": "Resend API key for the contact form",
     "requested_by": "agent", "requested_at": "2026-10-07T09:58:00Z", "updated_at": null}
  ],
  "pending": [ /* the status == "pending" subset of secrets */ ]
}
```

`can_manage` is true only for the pocket owner; the builder shows editors the list
read-only. `updated_at` is when the value was last set.

**`PUT`** takes `{"value": "..."}` (non-empty, at most 8 KB as UTF-8) and returns one
item in the shape above (`status: "set"`). Setting a pending secret fills the request.
**`DELETE`** removes a secret or a pending request and answers 204.

| HTTP | Code | When |
|------|------|------|
| 422 | `sites.secret_name_invalid` | The name is not `UPPER_SNAKE_CASE` or is over 64 characters. |
| 422 | `sites.secret_value_empty` | `PUT` with an empty or whitespace value. |
| 413 | `sites.secret_too_large` | `PUT` with a value over 8 KB. |
| 422 | `sites.secret_cap` | The site already has 50 secrets or requests. |
| 403 | `sites.secret_not_owner` | `PUT` / `DELETE` by anyone but the pocket owner. |
| 403 | `pocket.access_denied` | `GET` without edit access to the pocket. |
| 404 | `pocket.not_found` | Unknown pocket, or a pocket in another workspace. |
| 404 | `site_secret.not_found` | `DELETE` of a name that has no row. |

**Agent tools** (sites-manager MCP server, on `SITES_TOOL_IDS`):

- `request_site_secret(pocket_id, name, description)` leaves a pending request and
  returns `{ok, pocket_id, secret: {name, status, description, requested_by,
  requested_at, updated_at}, message}`. It never takes a value (the schema has no
  `value` property and forbids extra ones). Asking for a secret that is already set
  keeps the value and answers `status: "set"`.
- `list_site_secrets(pocket_id)` returns `{ok, pocket_id, secrets: [{name, status,
  description}]}`.

**Realtime.** `site.secret_requested` (a pending request was created or refreshed) and
`site.secret_updated` (`status` is `"set"` or `"deleted"`) go over the workspace bus to
the pocket owner and the acting user only. Payload: `{workspace_id, pocket_id, name,
status, owner, user_id}` plus `description` and `requested_by` on a request. The chat
run that asked also gets a per-run SSE `site_secret_requested` with
`{pocket_id, secret}`, so the input card can render inline.

**Publish.** A missing required secret refuses the deploy with 422
`sites.secrets_missing`, naming each one to set; see
[Sites bundle deploys](deployment/sites-bundle-deploys.md#secrets). The site delete
cascade removes every secret and request for the site (best effort, logged).

## Sites — Visitor Analytics

SA-4. `GET /sites/{site_id}/analytics` serves a published site's visitor numbers
for the builder's Analytics panel. The rows come from Cloudflare Workers Analytics
Engine, written by the pageview counter a paid site's publish deploys in front of
it (SA-1/SA-2). Source: `ee/pocketpaw_ee/sites/router.py`.

Authenticated, workspace-scoped, `fabric.read`, and behind the same `sites` plan
feature as the rest of the router. Tenant-scoped on the request context, so a site
in another workspace is a `404` — the same as every sibling per-site read.

### `GET /sites/{site_id}/analytics`

| Query | Type | Default | Notes |
|-------|------|---------|-------|
| `window` | string | `7d` | One of `24h`, `7d`, `30d`, `90d`. Anything else is a `422`. The set is closed because the Analytics Engine SQL endpoint has no parameter binding, so this is a query-safety control rather than only input validation. `90d` is the longest window that can return anything: Cloudflare retains the rows for three months. |

**Read `status` first.** Three different customer situations produce an empty
panel, and they are three different sentences:

| `status` | Means | What fixes it |
|----------|-------|---------------|
| `ok` | A counter is up and the numbers are real. **They may legitimately be zero.** | Nothing — this is a report about the site's traffic. |
| `not_entitled` | The site's plan does not include analytics, so nothing was ever recorded. | Upgrading the site's plan, then republishing. |
| `never_counted` | The plan includes it, but no publish has yet deployed a counter. Nothing is recording. | Republishing the site. Upgrading alone does **not** backfill — history begins at the publish that first carried a counter, because no rows exist before it. |

**A failed read is not a status.** If the Analytics Engine query fails, the
endpoint returns an **error response**, never a `200` carrying zeros. A client that
maps an unknown status to "no data" would otherwise render a Cloudflare outage as a
quiet week, which is the one failure this shape exists to prevent.

**Every metric is `null` unless `status` is `ok`.** Not `0` — a client that renders
the numbers without reading the status shows blanks rather than a confident zero.

Response `200`:

```json
{
  "site_id": "68b6f2c1a4d3e50012ab34cd",
  "window": "7d",
  "status": "ok",
  "counting_since": "2026-08-14T10:22:41+00:00",
  "retention_days": 90,
  "pageviews": 1280,
  "visitors": 431,
  "top_pages":  [{"label": "/",         "pageviews": 800, "visitors": 300}],
  "referrers":  [{"label": "(direct)",  "pageviews": 700, "visitors": 190}],
  "countries":  [{"label": "US",        "pageviews": 900, "visitors": 320}],
  "devices":    [{"label": "desktop",   "pageviews": 760, "visitors": 250}],
  "unrecorded": []
}
```

- `counting_since` is when this site's visitors started being counted, ISO-8601 in
  UTC, and `null` when nothing is counting. It is what makes an honest chart
  possible: the series begins here, not at the site's creation, and a window
  reaching further back is reaching into time nobody recorded.
- `retention_days` is how far back the data goes, and it is on the wire so a UI can
  explain why the earliest date it offers is the one it offers, rather than looking
  like it lost the data. **Read the field; do not copy the number into a client.**
  Cloudflare keeps a row for three months and there is no rollup store behind it, so
  anything older is gone rather than summarised — and a client holding its own `90`
  will keep displaying it after the day that stops being true.
- Each breakdown is a **top-10** list ordered by pageviews. Its `visitors` is a
  distinct count **within that row** and does **not** sum to the response total: one
  visitor who reads three pages is one visitor overall and one visitor on each of
  three rows. Summing the column gives a number that means nothing.
- A blank dimension is **named, never dropped** — `(direct)` for a referrer (a direct
  visit or a same-site link), `(unknown)` for a country Cloudflare could not
  geolocate. Dropping those rows would inflate every remaining share.
- `unrecorded` names the dimensions the stored row cannot answer **at all**, as
  opposed to answered-and-empty. A dimension listed here is `null` rather than `[]`,
  because an empty list reads as "none of these exist" and an omitted field is
  indistinguishable from a version skew. In practice the only dimension that can
  appear here is `devices`, and it means **this site has not published a counter that
  records devices yet** rather than a permanent gap. It clears once the site is
  republished and takes traffic. Render the name, not a chart of one bar called
  unknown.

**A visitor is per-day.** The counter identifies a visitor by a salted one-way hash
that rotates at UTC midnight and never leaves a cookie, so somebody who returns
tomorrow counts as two visitors. That is the privacy design rather than a rounding
error, and no join across days is possible even in principle.

**Sampling is accounted for.** Analytics Engine downsamples a hot index and reports
the rate per row, so the aggregates weight by it. A plain row count would
under-report precisely the busiest sites.

**Cached for 60 seconds per site and window.** Analytics Engine bills read queries
against a small daily account-wide allowance, and this panel's usage pattern is
somebody reloading it. The cache is in-process, so a second API replica keeps its
own; only successful reads are cached, so an outage is not extended past its end.

Errors:

| HTTP | Code | When |
|------|------|------|
| 404 | `site.not_found` | Unknown, malformed, or cross-tenant site id. |
| 422 | `sites.invalid_analytics_window` | `window` is not one of the four accepted values. |
| 422 | `sites.cloudflare_error` | The Analytics Engine query failed — a non-2xx, an unparseable body, or a `200` with no data array. Includes Cloudflare's own message. A `403` here usually means the API token is missing the **Account Analytics Read** permission, which is a different scope from the ones the deploy paths use. |
| 422 | `sites.cloudflare_unconfigured` | `PAW_CF_ACCOUNT_ID` / `PAW_CF_API_TOKEN` / `PAW_CF_ZONE_ID` are not all set. |

### What the `analytics` grant buys

Visitor counting is a **paid grant on the site's own plan**, not a floor one. It rides
the `analytics` member of the plan's Cloudflare feature set, and it needs the tier
**and** an active subscription — a cancelled site keeps its `plan_tier` string, so
reading the tier alone would keep counting for a site that stopped paying.

| Per-site tier | Monthly | Buys analytics |
|---------------|---------|----------------|
| `free` | $0 | no |
| `site` | $7 | yes |
| `staff` | $19 | yes |

The legacy keys resolve to what they always paid for: `basic` reads as `free`, `pro`
as `site`, `business` as `staff`. The org-scoped tiers (`studio`, `agency`) are not
legal values for a site's own `plan_tier`, and one appearing there resolves to the
free floor rather than to its own capabilities.

**Upgrading does not backfill, and this is the thing customers hit.** A site's history
begins at the publish that first deployed a counter, because nothing existed to record
before it. Buying the plan changes what the next publish deploys; it cannot create
rows for the weeks that went uncounted. The sequence that works is upgrade, then
republish — and until that republish the endpoint answers `never_counted`, which is
the panel's cue to ask for a republish rather than to draw a flat line at zero.

The same applies in reverse. A publish that deploys no counter clears
`counting_since`, so a site that lapsed to free and later re-upgraded reports
`never_counted` until it is republished, rather than claiming a start date from an era
that stopped recording months ago.

**Every engine counts, in one of two shapes.** A build with no server entry (`html`,
`react`, a *static* `svelte` site) gets a counter that serves the page through the
`ASSETS` binding. A build that emits its own `_worker.js` (`ripple`, a *dynamic*
`svelte` site) gets a shim that imports that worker, counts, and returns its response
untouched. Which shape a site gets is decided by its **build output**, not by its
engine name, which is why `svelte` appears on both sides.

One case still does not count, and it is not an engine: a dynamic site provisioned
through the durable provision job, which cannot resolve a plan from what it holds. Such
a site is entitled and still answers `never_counted` however often it is republished.
The [engineering note](design/2026-09-02-sites-visitor-analytics.md) has the table and
the tracking issue.

### `GET /sites/{site_id}/entitlements` — the pre-check

Same auth, same tenant scoping, same `404` on a cross-tenant site. It answers what the
site may do, so a surface can disable a control and name the reason instead of
offering a button that 402s.

Response `200`:

```json
{
  "site_id": "68b6f2c1a4d3e50012ab34cd",
  "plan_tier": "site",
  "subscription_active": true,
  "badge_required": false,
  "custom_domain": true,
  "max_domained_sites": null,
  "domained_sites_used": 0,
  "domain_slots_available": true,
  "analytics": true,
  "concierge_entitled": false,
  "concierge_enabled": false
}
```

- `analytics` says whether this site's plan buys visitor counting. It is resolved by
  the same predicate the publish path gates the counter on, so a site whose publish
  counted cannot be a site whose read refuses.
- `subscription_active` separates a lapsed paid site from one that never had the
  capability. The tier stays recorded; only the payment stopped, and the UI should say
  which.
- `max_domained_sites` is `null` for uncapped. It reports what the plan grants, while
  `domain_slots_available` reports what the gate will actually do — they differ when
  enforcement is off.
- The custom-domain allowance is per site. On the free plan every site may carry its
  own domain (apex + `www`), and `domain_slots_available` turns false only once THIS
  site holds both. Another site holding a domain never closes it.
- `domained_sites_used` is kept for older clients and is always `0`.

**`analytics` is a pre-check, not the answer.** The analytics endpoint's `status` stays
authoritative, because entitlement alone cannot separate "your plan does not include
this" from "it does, and you have not republished since you upgraded". Those are two
different sentences and two different buttons. Use this field to disable the panel
before the call; use `status` to decide what the panel says.

## Sites — Transfer to Another Workspace

Wave 3 of the sites lifecycle. A site could be created, published, edited and paid
for, but never handed to anyone else. These four endpoints move one between
workspaces.

**It is an offer and an accept, not one call.** A single endpoint would authorise
only the sender, which would let anyone who owns a site push it — with its leads and
its custom domains — into any workspace whose id they could name. The receiving
tenant consents by accepting.

**Nothing on Cloudflare moves.** The Worker, the D1 database, the custom hostnames
and the Worker routes are all untouched, and the site keeps serving throughout. What
changes is which workspace the records belong to.

| Resource | What happens |
|---|---|
| Site document, pocket | **Move.** `workspace` and `owner` are re-keyed; the pocket's `shared_with` is emptied, because those users belong to the workspace it just left |
| Leads | **Move.** Re-keyed to the destination |
| Site `_id`, Worker script name, public URL | **Preserved, deliberately.** The id IS the script name and the subdomain — see below |
| Cloudflare Worker, D1 database | **Stay put.** The D1 binding is by database id, so ownership changes in our records only |
| Custom hostnames, Worker routes | **Stay.** They live on our zone and point at a script that has not changed |
| Public images (R2) | **Cannot move.** The object key contains the source workspace id and is baked into an immutable, year-cached URL inside the deployed HTML *and* the pocket's stored spec, which nothing rewrites. Copying them to a new prefix would buy nothing and purging the source would blank every image on a live site. The source prefix is recorded on `Site.asset_source_prefixes` so teardown can still reclaim them |
| Concierge transcripts | **Stay with the source.** They live on `ChatRun` rows keyed on workspace and session, and they hold free text a visitor typed. Moving personal data into a tenant that has never held it is the direction that needs consent |
| Analytics | **Cannot move.** Cloudflare Analytics Engine rows were written under the old ids and age out on their own three-month retention |

**Why the site id is preserved.** `_live_object_id` derives the Site `_id` from
`(workspace, pocket_id)`, and that same value is the Worker's script name and the
subdomain the page is served at. Re-deriving it on transfer would rename a live
Worker and move a public URL while every custom-domain route kept resolving to the
old script. So the row keeps the id it was minted with, and `Site.identity_workspace`
records which workspace minted it — without that stamp, the next publish in the new
workspace would derive a different id, insert a second Site document, upload a second
Worker and serve it at a second address, forking the site in two. Rows that have
never been transferred carry `""` there and take the original derivation unchanged.

**A paid site cannot be moved.** A paid site is charged against the *source*
workspace's credit balance, and the renewal sweeper debits whichever workspace the
row names — so re-keying it would start charging the destination for a plan its
members never bought. Drop the site to the free plan first; the new workspace can
upgrade it again from its own balance.

### `POST /sites/{site_id}/transfer`

Offer the site to another workspace. Requires `fabric.write`, and the caller must be
the site's **owner** (not merely a workspace admin — the permission layers here are
known to disagree with one another, and widening this needs its own audit).

```json
{ "destination_workspace_id": "665f1c2a9e4b7d3f8a1c2b04" }
```

Returns the offer. Refusals carry a stable code: `transfer.not_owner` (403),
`transfer.blocked_by_workspace` (403), `transfer.site_is_paid` (409),
`transfer.deleting` (409), `transfer.already_offered` (409),
`transfer.same_workspace` (422). An unknown destination is a 404.

### `DELETE /sites/{site_id}/transfer`

Withdraw an offer that has not been accepted. Owner-only, source side.

### `GET /sites/transfers/incoming`

Sites other workspaces have offered to **this** one. Requires `fabric.read`.

This is the one sites read not anchored on the caller's `workspace` — it cannot be,
since an offered site still belongs to the sender until it is accepted.
`transfer_to_workspace` is the tenant filter instead, and only an owner of the
sending side can write it. The payload is deliberately thin (site id, name, url,
source workspace, who offered it, when) and never carries the signed key, the capture
config, the lead count or the client record.

### `POST /sites/transfers/{site_id}/accept`

Accept an offer and take ownership. Requires `fabric.write` in the destination, and
the caller must be a **member** of it — checked against their own user record, not
against the workspace named in the request header, since a header is a request rather
than a credential. A workspace the offer does not name gets a 404 rather than a 403:
saying "that site was offered elsewhere" would confirm the site exists to a tenant
with no business knowing that it does.

Synchronous, unlike delete — every step is a local write, so there is nothing to
poll. The re-key is still ledgered (`Site.transfer_ledger`), because a crash part-way
would leave ownership split across two tenants; accepting again resumes from the step
that stopped rather than repeating the ones that finished.

### Forbidding transfers out

`WorkspaceSettings.site_transfers_allowed` (default `true`). A site leaving takes its
leads with it, so an admin can forbid the outbound half outright. Only the source
side is gated — the receiving side is governed by consent, not by a setting.

## Sites — the foreign-origin concierge

A concierge on a site **PocketPaw does not host** — a Squarespace page, a
hand-rolled marketing site. There is no Worker to deploy, so instead of a publish
the owner buys a credential: a `Site` row with `foreign_origin: true`, a
world-visible `signed_key`, and the origins the embed is valid from. The snippet
goes on the page they already own. Source: `ee/pocketpaw_ee/sites/router.py`,
`ee/pocketpaw_ee/sites/service.py`.

| Method | Path | Action |
|--------|------|--------|
| `POST` | `/sites/by-pocket/{pocket_id}/foreign-concierge` | `fabric.write` **and** `sites.buy_plan` |
| `GET` | `/sites/by-pocket/{pocket_id}/foreign-concierge` | `fabric.read` |
| `POST` | `/sites/by-pocket/{pocket_id}/foreign-concierge/rotate-key` | `fabric.write` |
| `POST` | `/sites/by-pocket/{pocket_id}/foreign-concierge/rebind` | `fabric.write` |

**Prove the domain first.** `POST /sites/origins/claims` mints a token bound to
(workspace, host); publish it at `/.well-known/paw-verify` or as a
`<meta name="paw-verify">` in the origin's `<head>`; `POST /sites/origins/verify`
reads it back off the live domain. The bind refuses an origin that has not been
through this, and refuses one whose proof is over **30 days** old — the same
window the grounding crawl applies, so a bind can never sell a concierge that the
next crawl will refuse to feed.

### `POST .../foreign-concierge` — buy or resolve

```json
{ "allowed_origins": ["https://brewco.example"], "name": "Brew Co" }
```

**It charges $19/month from the workspace credit wallet, and it is idempotent.**
The first call mints and debits; every call after it returns the same concierge
and debits nothing, so a double-clicked button is a 200 with the same `site_id`
rather than a second purchase. The guarantee is a derived primary key — a
duplicate insert fails before the debit — which is why the endpoint calls the
resolve-or-buy layer and never the always-mints primitive underneath it.

`allowed_origins` and `name` apply to the **first** bind only. An existing
concierge comes back untouched: widening a live allowlist from a call that reads
as "make sure this exists" would be a way around the verified-origin gate, one
bind at a time. There is deliberately no `scopes` field — what a world-visible
embed key may do is the model's baseline, not the caller's to widen.

`sites.buy_plan` sits at ADMIN. A member gets 403 `sites.plan_purchase_forbidden`
on the bind and still gets the `GET`, so "does one already exist" never needs an
admin.

Response (all four endpoints share it):

```json
{
  "exists": true,
  "site_id": "68c1f0...",
  "pocket_id": "pk-brewco",
  "name": "Brew Co",
  "site_key": "site_key_kP3...",
  "embed_snippet": "<!-- Paw Bar concierge (embedded at publish) -->\n<script src=\"https://api.pocketpaw.dev/api/v1/paw-bar/widget.js\" data-paw-bar-embed=\"1\" data-site-key=\"site_key_kP3...\" data-widget-id=\"w_9f2\" data-endpoint=\"https://api.pocketpaw.dev/api/v1\" async></script>",
  "widget_id": "w_9f2",
  "agent_id": "ag_71c",
  "origins": [
    {
      "host": "brewco.example",
      "verified": true,
      "verified_at": "2026-09-01T10:22:04",
      "verification_fresh": true
    }
  ],
  "plan_tier": "staff",
  "subscription_status": "active",
  "renewal_date": "2026-10-19T00:00:00",
  "concierge_available": true,
  "concierge_entitled": true,
  "concierge_enabled": true,
  "concierge_exists": true
}
```

`concierge_available` is the public seams' answer: created **and** switched on
**and** sold by the plan. Because it is an AND it cannot say which half is
missing, so the response also carries each half on its own:

| Field | Means |
|-------|-------|
| `concierge_entitled` | the site's plan sells a concierge (always `true` when sites billing is not enforced) |
| `concierge_enabled` | the owner's switch; a concierge is created switched off |
| `concierge_exists` | the owner has created the concierge |

A panel explaining an empty snippet reads these, not `concierge_available`: a
paid concierge that is created but not switched on is `concierge_entitled: true,
concierge_enabled: false, concierge_available: false`, and it must not be told
its plan does not include one. Servers older than these fields omit them.

**The timestamps are UTC and carry no zone suffix.** Mongo stores UTC and hands
back naive datetimes, so `verified_at` and `renewal_date` have no trailing `Z` —
parse them as UTC, not as local time, or a proof looks like it expires a day
early. `verification_fresh` is already computed against the 30-day rule the bind
enforces, so a panel never has to do that arithmetic itself.

`embed_snippet` is authoritative and `""` is meaningful — it is the answer from
the one definition of the five gates a site must pass to earn a bar (plan,
owner's kill switch, a key, a widget, a bound agent). `widget_id` / `agent_id`
say **why** it is empty: an empty snippet beside a bound agent is the plan or the
kill switch (`concierge_entitled` / `concierge_enabled` say which), an empty snippet beside an empty `agent_id` is provisioning that has
not completed yet — retry the bind, which re-runs the funnel.

`site_key` is not a secret. It ships inside the snippet on a public page and is
origin-bound; `OriginClaimResponse.token` is the secret on this surface and
nothing here carries it.

**There is no `created` or `charged` flag.** Only the call that actually minted
spent money, and the endpoint cannot honestly say whether it was the one — a read
outside the service's lock reports "nothing here" to both of two concurrent first
binds while one of them charges. Call the `GET` before binding; `exists` answers
the same question without lying on a race.

Refusals, all of them before a row exists and before money moves:

| Status | Code | Means |
|--------|------|-------|
| 403 | `pocket.access_denied` | the pocket is not the caller's to ground a concierge in |
| 403 | `sites.origin_unverified` | this workspace never proved it controls that host |
| 403 | `sites.origin_verification_stale` | it did, over 30 days ago — verify again |
| 403 | `sites.plan_purchase_forbidden` | the caller may write here but may not buy |
| 404 | `pocket.not_found` | no such pocket |
| 422 | `sites.origin_required` | no usable origin survived normalization |
| 422 | `sites.foreign_concierge_unsellable` | the catalog rung stopped selling a priced concierge |
| 402 | `credits.insufficient` | the wallet cannot cover the month |

The two 403s on verification are **different codes on purpose**: "re-verify your
domain" and "you never claimed this domain" are different instructions, and a
panel that collapsed them would tell a customer to re-verify a domain they have
never heard of. The 402 deletes the unpaid row it had just inserted, so nothing
survives it either — a paid tier with no money behind it is the state this rail
exists to make impossible.

### `GET .../foreign-concierge` — the panel's read

A pocket that has never been bound is `200` with `exists: false`, not a 404:
that is the normal first state of the setup panel, and a 404 there would be
indistinguishable from a wrong pocket id. The lookup filters on `foreign_origin`,
so a pocket that ALSO has a published Worker site never has that site reported
here — conflating them would put a rotate button in front of the embed key of a
site somebody is actually serving.

For what the concierge can **answer from** — the article count, the last sync
stamp, the sync error, and an owner-triggered re-crawl — use
`GET`/`POST /paw-bar/admin/site/{site_id}/knowledge` with the `site_id` from this
response. That surface already covers every Site in the workspace, a foreign row
included, and is not duplicated here.

### `POST .../foreign-concierge/rotate-key`

Retires the embed key and issues a new one; the old key stops resolving
immediately (401 at the key resolver), so the owner's page is serving a dead
credential until they paste the new snippet in. The response carries it. Same
row, same tier, same renewal date — a rotation is not a repurchase, which is why
it is `fabric.write` and not the bind's admin gate: the member who can see a
leaked key should be able to act on it. `404 site.not_found` when the pocket has
no foreign concierge.

### `POST .../foreign-concierge/rebind`

```json
{ "agent_id": "ag_new", "widget_id": "" }
```

Points the bar at a different agent. Both fields are optional and an empty
`agent_id` is **not** a no-op: it means re-provision — clear the stale bind and
let the funnel resolve-or-mint the canonical agent again, which is the repair for
a bar whose agent was deleted. `widget_id` picks the bar when a pocket carries
more than one.

Nothing about the purchase moves: the embedded `signed_key` keeps resolving, the
tier stays bought, the renewal date stays where it was. An `agent_id` in another
tenant is a 404 from inside the funnel, deliberately indistinguishable from an
agent that does not exist.

### The connected site's gallery card

A connected site never deploys, so the card lanes a publish runs (screenshot,
favicon) never fire for it. Instead one card refresh fetches the verified origin's
homepage **once**, through the SSRF-hardened fetch (DNS pinned to a public
address, every redirect hop re-checked, private targets refused), and records the
page title and icon from that markup before taking the screenshot. Only the host
the grounding crawl would use is ever fetched: verified, and proved within 30
days.

It runs in the background after a first bind, after a rebind, after
`POST /sites/origins/verify` succeeds for a host a connected site serves on, and
as a second chance from the knowledge sync (which reuses the crawl's homepage
HTML and only fills fields that are still empty). None of these can fail the
request that triggered them.

| `SiteResponse` field | Meaning for a connected site |
|----------------------|------------------------------|
| `name` | The owner's own label. Never overwritten by the card refresh. |
| `origin_title` | The homepage's `<title>`, falling back to `og:title`. Control characters dropped, whitespace collapsed, capped at 200 characters. `""` before the first read, when the page has no title, for hosted sites, and for rows that predate the field. |
| `favicon_url` | The homepage's icon as a `data:` URI, fetched through the same safe path. `null` when there is none. |
| `preview_image_url` | The screenshot of the verified origin. |

Clients should show `name || origin_title || host`.

`POST /sites/{site_id}/preview-refresh` on a connected site also refreshes
`origin_title` and `favicon_url` from the same single homepage fetch, before the
screenshot. Its error contract is unchanged: `422 sites.origin_unverified`,
`sites.origin_verification_stale` or `sites.preview_unavailable` when there is no
fresh verified origin, `422 sites.preview_not_serving` when the page is not
answering. A failed title/icon read keeps the stored values and does not turn into
an error.

## Sites — Download the project

The built site handed back to its owner as an archive, rather than only served from our
edge. A **paid per-site capability**. Source: `ee/pocketpaw_ee/sites/router.py`,
`ee/pocketpaw_ee/sites/service.py` (`download_site_project`),
`ee/pocketpaw_ee/sites/project_zip.py`.

**Assembled from the stored pocket, not from a built tree.** The archive carries the
authored source plus the smallest manifest and config set that makes it install and build
elsewhere — no `wrangler.toml`, no `adapter-cloudflare`, no edit bridge, no D1 layer, and
no lockfile. It is **buildable, not byte-identical to what we deploy**. The archive itself
is byte-reproducible (sorted entries, fixed timestamps), so the same pocket always
produces the same bytes.

### `GET /sites/{site_id}/project`

Auth: `fabric.read`. Tenant-scoped, and the tenancy check runs **before** the entitlement
— a site in another workspace is `404`, never `402`, because a `402` would confirm the id
is real and leak which plan it is on.

Response `200`: the zip itself, `Content-Type: application/zip`, with
`Content-Disposition: attachment` and an explicit `Content-Length` (the payload is
assembled whole before anything is sent, so a client can show real progress).

Three refusals, and they are three different situations:

| Status | Code | Means |
|---|---|---|
| `402` | `billing.project_download_not_entitled` | This site's plan does not include the download, **or** it is on a paid tier whose subscription has lapsed. The message distinguishes them — "upgrade" versus "renew" — because the remedies differ and a paying customer must not be told to buy a bigger plan. |
| `400` | `sites.project_not_downloadable` | A Ripple site. It is built from a spec rather than source files, so there is no project. **Never a zero-byte zip**: an empty archive cannot be told apart from a site whose files vanished. |
| `500` | `sites.project_unavailable`, `sites.project_too_large`, `sites.project_unsafe_path` | Our data broke an invariant — a source map larger than BSON can have stored, a stored path that escapes its root, or a source engine holding no source map. Deliberately not `4xx`: none of these is the caller's fault, and blaming the request sends an investigator to the wrong layer. |

**The remedy is in `error.message`, not in `detail`.** These refusals travel through the
standard `CloudError` envelope (`{"error": {"code", "message"}}`), and the 402's message is
the part that differs between "upgrade" and "renew". paw-enterprise's
`friendlyErrorMessage` reads `body.detail` — which this envelope does not carry — so a
client that relies on it will render a generic failure and lose the distinction the two
messages exist to draw. Read `error.code` to decide what to show and `error.message` to
show it.

**Check `project_download` on `GET /sites/{site_id}/entitlements` (documented above as
"the pre-check") before offering the button.** That is what the field is for — discovering the refusal by
provoking it is the failure the per-site entitlements read exists to end.

**Do not gate the button on source visibility instead.** The pocket's `sourceVisible`
(which governs the builder's Code tab) is resolved off the same per-site rule as
`project_download` (`site` or `staff`, active subscription), so the two normally agree.
They differ when a platform operator overrides source visibility for the whole
workspace (`site_source_visible` on `GET /entitlements`, `null` when unset): that
override reaches the Code tab only, never the download. Read `project_download` for
the button.

Self-hosted and OSS deployments have no billing, so the gate is skipped entirely there
(`sites_enforced()`) and the download always works.

## Sites — Addresses and renaming

On the `workers` deploy lane a site's address is its Worker name:
`https://<slug>.<account>.workers.dev`. A new site gets one from its name on its first
publish (see the Cloudflare sites runbook, "Site addresses"). These endpoints let an
owner check a name and move a published site to a different one. Source:
`ee/pocketpaw_ee/sites/router.py`, `ee/pocketpaw_ee/sites/service.py`.

Every site response now carries two fields:

| Field | Meaning |
|---|---|
| `slug` | The address the site serves at (`acme-bakery`), or `null` for a site still on `paw-site-<id>` |
| `slug_pending` | The address a rename reserved, which goes live on the next publish; `null` when none is waiting |

**A rename does not take effect when you make it.** A live build cannot be redeployed
without rebuilding the draft, so the request reserves the name and the site's next
publish moves it: it deploys under the new name, re-points every custom domain, then
deletes the old Worker. Until then `slug` and `url` still show the current address.
Show `slug_pending` as "goes live when you next publish".

### `GET /sites/slug-available?slug=<raw>&site_id=<optional>`

Auth: `fabric.read`. Rate-limited to 30 checks a minute per user
(`429 sites.slug_check_rate_limited`).

`slug` is raw input; it is normalized the same way a rename would be (lowercased,
accents stripped, other runs of characters become one hyphen). With `site_id`, that
site's own current and pending address read as available; a `site_id` from another
workspace is a `404`.

```json
{ "available": false, "normalized": "acme-bakery", "reason": "taken", "suggestion": "acme-bakery-2" }
```

`reason` is `null` when available, otherwise one of:

| `reason` | Meaning |
|---|---|
| `invalid` | Not 3–40 lowercase letters, digits or hyphens with no hyphen at either end (includes an empty result) |
| `reserved` | A platform name (`www`, `api`, `admin`, ...) or anything starting `paw-` |
| `taken` | Another site serves at it or has it pending, or a Worker by that name exists in the Cloudflare account |
| `held` | Another workspace gave it up by renaming within the last 30 days. A name your own workspace released reads as available |

`suggestion` is a free alternative when one is found among the next few candidates,
else `null`.

### `PUT /sites/{site_id}/slug`

Auth: `fabric.write`, tenant-scoped (a site in another workspace is a `404`).

```json
{ "slug": "Acme Cakes" }
```

Response `200`: the site, with `slug_pending` set to the normalized name.

- Asking for the site's **current** `slug` cancels any pending rename (`200`,
  `slug_pending: null`). Asking for the **pending** one again is a no-op `200`. Neither
  counts toward the limit.
- At most **3 accepted requests per site in any 24 hours**. A request that is later
  cancelled still counts.

| Status | `error.code` | When |
|---|---|---|
| 422 | `sites.slug_invalid` | The normalized name is not a valid address |
| 409 | `sites.slug_reserved` | A reserved name or `paw-` prefix |
| 409 | `sites.slug_taken` | Another site has it (live or pending), or a Worker by that name exists |
| 409 | `sites.slug_held` | Another workspace released it within 30 days |
| 409 | `sites.slug_unsupported_lane` | The deployment is not on the `workers` lane |
| 409 | `sites.slug_needs_publish` | The site has never been published; its first publish picks an address from its name |
| 409 | `sites.slug_changed` | The site's pending name changed while this request was saving; retry |
| 429 | `sites.slug_rate_limited` | A 4th rename request within 24 hours |

**The next publish can also refuse.** If a Worker with the pending name has appeared in
the Cloudflare account since the reserve, that publish fails with
`409 sites.slug_taken` and nothing moves; cancel the rename or pick another name. If a
custom domain cannot be re-pointed, the publish fails, every domain stays on the old
Worker and `slug_pending` is kept, so publishing again retries the move.

**The old name is held for 30 days** after the move. No other workspace can take it in
that time; yours can take it back. `paw-site-<id>` names are never held.

### `DELETE /sites/{site_id}/slug/pending`

Auth: `fabric.write`, tenant-scoped. Cancels a waiting rename. Idempotent: `200` with
the site (`slug_pending: null`) whether or not one was waiting.

## Sites — Hide the PocketPaw badge

A free site ships with a "Built with PocketPaw" badge stamped into every page at publish
time. A paid per-site plan may remove it, and `badge_hidden` is the owner's choice about
whether to. Source: `ee/pocketpaw_ee/sites/router.py`, `ee/pocketpaw_ee/sites/service.py`
(`update_site_branding`, `_stamp_free_badge`).

**The badge is dropped only when both are true:** the site's plan grants badge removal
(`badge_required: false` on `GET /sites/{site_id}/entitlements`) **and** `badge_hidden` is
`true`. The flag can only add the badge back; it can never remove one the plan does not
pay for. A lapsed paid site gets its badge back on its next publish whatever the flag says.

Every site response (`GET /sites`, `GET /sites/{site_id}`, the publish response, and so
on) now carries `badge_hidden: bool`. It defaults to `true`, and a site written before the
field existed reads `true`, which is how an entitled site behaved before the switch.

**Partner-sold sites carry a co-brand mark instead.** A site a Paw Partner sold
(`POST /partners/sell`, so `partner_client_id` is set) on an active partner-only plan
(`site_year` / `staff_year`) publishes with "Made by <footer_name> · Paw Sites by
PocketPaw", linking to `https://pocketpaw.xyz/partners`. `footer_name` comes from the
partner's profile. It sits in the same place as the badge with the same lock, so the
shop's own stylesheet cannot hide it, and `badge_hidden` does not remove it. A name over
13 characters is shortened with "…" on screen so the mark fits a phone, and the full name
stays in its accessible label. The sale's
own redeploy already carries it. If the partner plan lapses, the next publish puts the
standard badge back. A partner profile with no `footer_name` falls back to the rules
above.

### `PATCH /sites/{site_id}/branding`

Auth: `fabric.write`, tenant-scoped, same as `PATCH /sites/{site_id}/metadata`. A missing
or cross-tenant site is `404`.

Request:

```json
{ "badge_hidden": false }
```

`badge_hidden` is required; an empty body is `422`.

Response `200`: the full site response, carrying the stored value.

```json
{ "id": "68b6f2c1a4d3e50012ab34cd", "name": "Bright Smile", "badge_hidden": false, "...": "..." }
```

| Status | Code | Means |
|---|---|---|
| `402` | `billing.badge_removal_not_entitled` | `badge_hidden: true` was asked for on a site whose plan does not remove the badge, **or** a paid tier whose subscription is not active. Nothing is written. The message says "upgrade" or "renew" accordingly. |
| `404` | `site.not_found` | No such site in this workspace. |
| `422` | — | Missing or non-boolean `badge_hidden`. |

`badge_hidden: false` is always accepted. Sending the value the site already has is a
no-op.

The 402 uses the standard envelope, `{"error": {"code", "message"}}`. Read `error.code`
to decide what to show and `error.message` to show it (see the note under "Sites —
Download the project" on why `detail` is empty).

**The change takes effect on the next publish.** The preference is stored immediately,
but a live site keeps serving the page it was last published with until it is published
again. There is no redeploy of the current build yet, because nothing stores the live
build's inputs. Tell the owner this when they flip the switch on a published site.

Self-hosted and OSS deployments have no billing: the `402` is skipped (`sites_enforced()`)
and the flag is stored, but the stamper still badges any site whose plan does not remove
the badge.

## Sites — AI visibility

Every site published to workers.dev carries files that let search engines and AI
assistants read it. AI crawlers do not run JavaScript, and workers.dev is Cloudflare's
domain, so zone features like managed robots.txt can't be switched on for it. The files
ship inside the site instead. Each publish writes:

| Path | What it is |
|---|---|
| `/robots.txt` | `User-agent: *` with `Allow: /` and a `Content-Signal: search=yes, ai-input=yes, ai-train=<yes\|no>` line. Unless the owner allows training, it also has a `Disallow: /` group for each training crawler: GPTBot, ClaudeBot, Google-Extended, Applebot-Extended, CCBot. Search and assistant crawlers (OAI-SearchBot, ChatGPT-User, Claude-SearchBot, Claude-User, PerplexityBot, Perplexity-User) are never blocked. Ends with the `Sitemap:` line. |
| `/sitemap.xml` | Every built page, as an absolute URL, with the publish date as `lastmod`. |
| `/llms.txt` | Site name, its description, and a list of pages linking to their markdown copies. |
| `/<page>.md` | A markdown copy of each built page (`/index.md`, `/about.md`), served as `text/markdown`. Each page answers with `Link: </about.md>; rel="alternate"; type="text/markdown"`. |
| `/<key>.txt` | The site's IndexNow key. After a successful deploy the site's URLs are sent to IndexNow (Bing, Yandex and others; Google does not take part). |
| JSON-LD | A `LocalBusiness` block in each page's `<head>` when the page shows a `tel:` link or an `<address>`. Skipped when there is neither, or when the page already has its own JSON-LD. |

Absolute URLs use the site's first live custom domain, else its workers.dev host. Only
pages that exist as HTML at deploy time are covered, so SSR-only routes on a dynamic site
get no markdown copy. A `robots.txt`, `sitemap.xml` or `llms.txt` the site ships itself
(an imported site, say) is kept as it is. The IndexNow ping and the file writes never fail
a publish. Operators can turn the whole thing off with `POCKETPAW_SITES_AI_READY=0`.

### `PATCH /sites/{site_id}/ai-visibility`

Auth: `fabric.write`, tenant-scoped, same as `PATCH /sites/{site_id}/branding`. A missing
or cross-tenant site is `404`.

Request:

```json
{ "ai_training_allowed": true }
```

`ai_training_allowed` is required; an empty body is `422`. Default for every site is
`false`: training crawlers are blocked.

Response `200`: the full site response, carrying `ai_training_allowed`.

It only changes the training lines of `robots.txt`. Like the badge switch, the value is
stored now and **reaches the live site on the next publish**.

## Deleting a site

Wave 1 of the sites lifecycle. Deleting a site is **irreversible**, **owner-only**, and
**not one request** — it is a durable job over an ordered cascade (stop the billing,
revoke the signed key, pull the routes / hostnames / Worker, then the D1, the bucket
prefix and the dependent rows, and the Site document last). Source:
`ee/pocketpaw_ee/sites/router.py`, `ee/pocketpaw_ee/sites/delete_job.py`.

**A data export is forced first.** Before anything destructive runs, the job captures the
site's D1 tables and captured leads to a `SiteExport` row in private storage. An export
that cannot be vouched for is a hard stop: the cascade never starts, the site is left
exactly as it was, and the row settles at `export:<cause>`. That export is the entire
recovery story, which is why it is a precondition rather than a courtesy — see
[the export endpoint](#post-sitessite_idexport) for the standalone version of the same
capture.

**Visitor analytics are not purged and cannot be.** They live in Cloudflare Analytics
Engine on a three-month retention this product does not control. The delete says so
rather than implying a completeness it cannot deliver.

### `DELETE /sites/{site_id}`

Auth: `fabric.write`, plus **owner-only** on top of it — a workspace member who may edit
a site may not destroy it. A non-owner inside the workspace gets `403`
(`site.not_owner`); a site in another workspace gets `404`, because confirming a
stranger's site exists is itself a leak.

Response **`202`** — deliberately not `204`. The site is **still serving** when this
returns:

```json
{
  "site_id": "68b6f2c1a4d3e50012ab34cd",
  "status": "queued",
  "job_id": "site-delete-68b6f2c1a4d3e50012ab34cd-9f2c..."
}
```

A second delete of a site whose teardown is already running does not conflict: it answers
`202` reporting the in-flight attempt, because the site is already being deleted, which
is what the caller asked for. A delete that previously **failed** can be started again —
the cascade resumes from its ledger and skips every step the earlier attempt finished.

### `GET /sites/{site_id}/delete-status`

Auth: `fabric.read`, owner-only for the same reason (`delete_reason` names which teardown
step stopped).

Response `200` while the delete is running or stopped:

```json
{
  "site_id": "68b6f2c1a4d3e50012ab34cd",
  "delete_status": "tearing_down",
  "delete_reason": null,
  "delete_ledger": { "billing": "done", "revoke": "done" },
  "delete_export_id": "68b7a1...",
  "delete_job_id": "site-delete-..."
}
```

**This endpoint `404`s when the delete SUCCEEDS, and that 404 is the success signal.**
There is no terminal `"deleted"` status by construction: the cascade's last step removes
the Site document, and `delete_status` is a field on that document, so a completed delete
has nothing left to report a status on. A client that reads this `404` as an error
reports every successful delete as a broken one.

`delete_status` is one of `none`, `queued`, `exporting`, `tearing_down`, `failed`. Treat
an **unrecognised** value as still running rather than terminal — a client that stops
polling on a status it has not heard of abandons a live teardown and leaves the user
looking at a half-destroyed site that claims it finished.

`delete_reason` is a two-part `"<step>:<cause>"` string reusing `build_reason`'s exact
format. Render the **step** half; the cause half is a fixed machine token written for a
log, never raw provider text. `export` is the step worth its own sentence — it is the one
failure where nothing was destroyed and the user's site is untouched.

### Deleting the pocket instead

You cannot. `DELETE /pockets/{id}` **refuses** with `409 pocket.has_site` while a site is
published from it, rather than cascading — a cascade from there would bypass the forced
export, so the one path that can destroy a site stays the one path that preserves its
data first. Delete the site, then the pocket.

## Leads

A Lead is one way a visitor left their details on a site. `site_id` on these
routes is the site's `script_name`, as on the Lead itself.

| Route | Purpose |
|---|---|
| `GET /sites/{site_id}/leads?limit=` | Newest first, at most 500. |
| `PATCH /sites/{site_id}/leads/{lead_id}` | `{"status"?: "new" \| "contacted" \| "won" \| "lost" \| "booked", "read"?: bool}` → the updated lead. `read: true` keeps the first read time; `false` marks it unread. Unknown status → 422. |
| `POST /sites/{site_id}/leads/read-all` | Marks every unread lead on the site read → `{"updated": n}`. |

All three need the `sites` plan feature. The GET needs `fabric.read`; the PATCH
and read-all need `fabric.write`. All are scoped to the caller's workspace: another workspace's lead (or one on another site) is a 404,
and read-all touches nothing there.

A list item:

```json
{"id": "…", "site_id": "…", "form_type": "concierge",
 "properties": {"name": "Priya", "email": "priya@x.com", "message": "20 jackets"},
 "origin": "", "origin_unrecognized": false,
 "source_kind": "concierge", "conversation_ref": "pp_w1:cust-0001",
 "status": "new", "read_at": null, "created_at": "2026-10-01T09:30:00+00:00"}
```

`source_kind` is `form` (a site form), `concierge` (the visitor tapped Send on the
concierge's lead card), `handoff` (a "talk to a person" request that carried an
email or phone; one lead per conversation) or `booking`. `conversation_ref` is
`<widget_id>:<customer_ref>`, the conversation the lead came from (`""` for a
form). Leads written before these fields existed read as `status: "new"`,
`read_at: null`, `source_kind: "form"`.

A status change emits `lead.updated`, delivered to the site webhook only (when it
is active and `lead_captured` routes to `webhook`), as
`{"id": "evt_…", "type": "lead.updated", "data": {…the lead, with status…}}`. It
rings no bell and sends no mail. Marking read emits nothing. A handoff lead's
`lead.captured` is not routed: the handoff already notified the owner.

### Leads from the concierge

When `concierge_lead_capture` is on, the v2 concierge offers a lead card (a
`form` whose verb is `send_to_team`, fields from `name`, `email`, `phone`,
`message`, each optionally prefilled with `value` of at most 500 characters).
Nothing is stored until the visitor taps Send, which posts:

```http
POST /paw-bar/action
{"key": "<site key>", "w": "<widget id>", "customer_ref": "<visitor>",
 "verb": "send_to_team", "args": {"name": "Priya", "email": "priya@x.com", "message": "…"}}
```

`args` carries only those four names; empty fields are left out. Rules: `name` at
most 120 characters, `message` at most 2000, and an `email` and/or `phone` that
look valid. Limits: 3 per visitor per 10 minutes, 30 per site per hour, taken
atomically before the lead is written. A taken slot is never given back, so an
attempt the injection screen drops still counts. The
text goes through the same HIGH injection screen as site forms. `send_to_team`
is reserved. Saving a spec that declares it (the spec PATCH routes and widget
create) is `422 reserved_verb`. A spec already stored with it loads with that
action dropped and a warning logged.

| Response | Meaning |
|---|---|
| `200 {"ok": true, "result": {"message": "Sent. The team will get back to you."}}` | A Lead was written (`form_type` and `source_kind` `concierge`) and `lead.captured` fired. |
| `422 {"detail": {"code", "field", "message"}}` | `field` names the form field to mark: `too_long` (name, message), `not_text`, `invalid_email`, `invalid_phone`, `contact_required` (field `email`). `field: null` (`unknown_field`, or `rejected` by the injection screen) is a generic retry. |
| `429` | A lead limit. |
| `409 lead_capture_off` | The owner turned lead capture off. |
| `503 lead_unavailable` | The limit or the lead write couldn't be checked; nothing was stored. |

## Owner notifications — email, signed webhooks, per-site recipients

A captured lead, and a concierge handoff, reach the site's owner through three
sinks: the in-app bell plus OS push, email (Cloudflare Email Service), and a
signed webhook. Email and webhooks never run on the request that caused them:
they are queued in the `notification_outbox` collection and a background
sweeper sends them, retrying after 1 m, 5 m, 30 m, 2 h and 6 h before giving up.
Slack deliveries go through the same queue. Each send has a hard 30 s deadline
(an endpoint that doesn't answer in time counts as a failed try), and email is
worked separately from webhooks and Slack, so a slow webhook never holds mail up.

Push lock-screen text stays generic. Email and webhook payloads carry the
lead itself (name, email, phone, message, every captured property, site name,
form type and source), loaded when the delivery is sent.

### Per-site settings

Every route below is workspace-scoped (the caller's active workspace) and needs
`notifications.manage` (workspace owner or admin). A member gets `403`; a site in
another workspace is a `404`.

#### `GET /sites/{site_id}/lead-notifications`

```json
{
  "site_id": "68b6f2c1a4d3e50012ab34cd",
  "configured": false,
  "include_owner": true,
  "owner_email": "owner@acme.com",
  "owner_email_status": "verified",
  "emails": [
    {"email": "team@acme.com", "status": "pending", "added_at": "…", "confirmed_at": null}
  ],
  "webhook_url": null,
  "has_webhook_secret": false,
  "webhook_secret": null,
  "webhook_disabled_at": null,
  "webhook_failure_count": 0,
  "events": {
    "lead_captured": ["email", "push"],
    "handoff": ["email", "push"],
    "booking": ["email", "push"]
  },
  "email_enabled": true
}
```

A site that was never configured reads as the default: the workspace owner's
account email, with `email` + `push` for every event. `owner_email_status` is
`verified` when the account has verified that address (it gets mail with no
extra step), or else `pending_confirm` until the owner clicks the same confirm
link an added recipient gets, then `confirmed`. A lead sends that link
automatically (at most once a day per site), and `POST .../recipients` with the
owner's address re-sends it, under the same rate limits. `status` is `pending`
(waiting for the confirm click), `confirmed`, or `bounced` (the mail provider
reported a permanent bounce; re-add the address to try again). `email_enabled`
is false while the server has no Cloudflare email credentials.

#### `PUT /sites/{site_id}/lead-notifications`

Partial update; omitted fields are kept.

```json
{
  "include_owner": true,
  "events": {"lead_captured": ["email", "push", "webhook"]},
  "webhook_url": "https://hooks.example.com/paw",
  "clear_webhook": false
}
```

Sinks are `email`, `push` and `webhook`. `push` covers the bell row and the OS
push together (every bell row is pushed). A `webhook_url` is checked against
SSRF (https only, any port 1-65535, and every address the host resolves to must
be globally routable, which also rules out 100.64.0.0/10; a failure is `403
notifications.invalid_webhook_url` or `webhooks.private_address`). A NEW URL's
signing secret comes back **once**, in this response's `webhook_secret`; later
reads return `null`. Any save that names a webhook URL, the same one included,
re-arms a webhook that was switched off.

#### `POST /sites/{site_id}/lead-notifications/webhook-secret`

Rotates the site webhook's signing secret and re-arms the webhook. The new
secret comes back once in `webhook_secret`. For 24 hours after a rotation the
old secret also signs (see "Webhook payload and signing"). `404` when the site
has no webhook.

#### `POST /sites/{site_id}/lead-notifications/recipients`

Body `{"email": "team@acme.com"}`. Adds the address unconfirmed and emails it a
confirm link that works for 7 days. Nothing else is sent to it until it is
confirmed. At most 5 extra addresses per site (`422
lead_notifications.too_many_recipients`); `422 lead_notifications.email_disabled`
when the server can't send email; `422 lead_notifications.public_url_unset` in
production when `POCKETPAW_PUBLIC_BASE_URL` is unset. Re-adding a pending
address sends a fresh link and voids the old one. Confirm emails are limited to
one per address per site every 30 minutes (`429
lead_notifications.confirm_rate_limited`; removing and re-adding the address
doesn't reset this) and 50 per workspace per day (`429
lead_notifications.confirm_daily_cap`).

#### `DELETE /sites/{site_id}/lead-notifications/recipients/{email}`

Removes the address. Mail already queued for it is dropped at send time.

#### `POST /sites/{site_id}/lead-notifications/test`

Queues a test email to every address that may receive mail now and a test
delivery (`type: "notification.test"`) to the site webhook. Returns
`{"emails": ["owner@acme.com"], "webhook": true}`.

#### `GET` / `POST /lead-notifications/confirm/{token}` (public)

The link in the confirm email; the token is the credential, so no session is
needed. `GET` only shows a page with a "Confirm this address" button and
changes nothing, so mail scanners and link previews that fetch the link can't
confirm on someone's behalf. The button `POST`s to the same path, which
confirms (repeating it is harmless). The POST needs no session and no CSRF
token: the path token is the credential. Both answer `400` when the link expired,
was replaced by a newer one, or the address was removed, and both send
`Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

### Routing

| Sink | Lead captured | Concierge handoff |
|---|---|---|
| `push` | bell + push to the workspace owner and admins | bell + push to the workspace owner |
| `email` | the full lead; `reply_to` is the visitor's email when valid | a short notice linking to the conversation |
| `webhook` | the site webhook, `type: "lead.captured"` | the site webhook, `type: "concierge.handoff"` |

The workspace config (`/notifications/delivery-config`) stays the fallback: its
Slack sink gets every site event (subject to its `routes`), and its webhook gets
the event when the site has no webhook of its own. Each lead is delivered once
per sink, not once per admin. A site event sent to the WORKSPACE webhook also
carries the deprecated flat fields (`kind` = `lead_captured` /
`paw_bar_needs_human`, `title`, `body`, `workspace_id`, `recipient_id: null`,
`actor_id: null`) so receivers that filter on `kind` keep working. Three things
still differ from a plain `notification.created` delivery: `id` is the event id
(`evt_…`), not a notification id; `recipient_id` is `null`; and there is ONE
delivery per event, where the old webhook received one per notified admin.
Site webhooks get the envelope only.

### Workspace webhook

`GET` / `PUT /notifications/delivery-config` (admin) now sign the generic
webhook the same way. The `PUT` response carries `webhook_secret` once, when a
new URL (or a URL that had no secret) is saved; reads return
`has_webhook_secret` instead. Any `PUT` naming a webhook URL re-arms it.
`POST /notifications/delivery-config/webhook-secret` rotates it (returned once,
old secret co-signs for 24 hours).

A webhook saved before signing existed has no secret. It keeps receiving
deliveries exactly as before, **unsigned**, and the config reports
`"signed": false` so the settings screen can say "unsigned: rotate the secret to
sign it". Saving it or rotating its secret turns signing on.
Plain notifications arrive as `type: "notification.created"`. For
compatibility with receivers built before the envelope, the old flat fields are
also kept at the top level of the body:

```json
{
  "id": "68f0c2…",
  "type": "notification.created",
  "created_at": "2026-10-01T09:30:00+00:00",
  "data": {"id": "68f0c2…", "workspace_id": "…", "recipient_id": "…", "actor_id": null,
           "kind": "mention", "title": "…", "body": "…"},
  "workspace_id": "…", "recipient_id": "…", "actor_id": null,
  "kind": "mention", "title": "…", "body": "…"
}
```

The top-level `workspace_id`, `recipient_id`, `actor_id`, `kind`, `title` and
`body` are **deprecated**: read them from `data`. They will be removed in a
later release. There is no key clash: `id` is the notification id in both
shapes (it is also the event id for this type, since each notification is
delivered once per webhook). Every other event type (`lead.captured`,
`concierge.handoff`, `notification.test`), and everything sent to a site
webhook, uses the envelope only: `{id, type, created_at, data}`.

### Webhook payload and signing

```http
POST /your/endpoint
Content-Type: application/json
X-Paw-Timestamp: 1700000000
X-Paw-Signature: v1=<hex HMAC-SHA256(secret, "1700000000.<raw body>")>
                 (v1=<new>,v1=<old> for 24 hours after a secret rotation)

{"id":"evt_…","type":"lead.captured","created_at":"2026-10-01T09:30:00+00:00",
 "data":{"id":"…","site_id":"…","site_name":"Bright Smile","form_type":"lead",
         "name":"Priya","email":"priya@x.com","phone":"","message":"…",
         "properties":{…},"source":{"kind":"form","form_type":"lead","origin":"…",
         "origin_unrecognized":false,"conversation_ref":""},"created_at":"…"}}
```

`id` is the same on every retry of one delivery, so dedupe on it. Any non-2xx
answer (redirects are not followed), or no complete answer within 30 s, is
retried on the schedule above. Only the status code matters: a reply body is
read up to 1 MB and the rest is ignored. The host is resolved and checked when
each delivery is sent and the connection is pinned to the checked address, on a
fresh connection per delivery (never one reused from another host); if DNS
fails nothing is sent and the delivery is retried. After 10 deliveries in a row
that ran out of retries, the webhook is switched off (`webhook_disabled_at`)
until its URL is saved again or its secret rotated.

To verify, recompute the HMAC over the timestamp header, a `.`, and the raw
request body (before any JSON parsing), compare in constant time, and reject a
timestamp more than 5 minutes old so a captured delivery can't be replayed:

```python
import hashlib, hmac, time

def verify(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body,
                        hashlib.sha256).hexdigest()
    return any(
        part.strip().startswith("v1=") and hmac.compare_digest(part.strip()[3:], expected)
        for part in signature.split(",")
    )
```

Test vector: secret `whsec_test`, timestamp `1700000000`, body
`{"id":"evt_1","type":"lead.captured"}` gives
`v1=35aea954dafaba38bb223bdd493656857236540655f31dc96809ce7e59e0abe9`.

### Email configuration

| Variable | Purpose |
|---|---|
| `POCKETPAW_CF_EMAIL_ACCOUNT_ID` | Cloudflare account that owns the sending domain. |
| `POCKETPAW_CF_EMAIL_API_TOKEN` | API token with permission to send email. Secret, never logged. |
| `POCKETPAW_CF_EMAIL_FROM` | From address on the onboarded domain, e.g. `notifications@example.com`. |
| `POCKETPAW_CF_EMAIL_FROM_NAME` | Display name. Default `PocketPaw`. |
| `POCKETPAW_FRONTEND_BASE_URL` | Links to the lead and to notification settings. |
| `POCKETPAW_PUBLIC_BASE_URL` | The confirm link points at this API origin. |

Email is off, and the server logs that once, until the first three are set.
Mail goes out through `POST /client/v4/accounts/{account_id}/email/sending/send`.
A `429` or `5xx` from Cloudflare is retried. So are `401` and `403` (a bad token,
or sending disabled on the account): those are fixed by an operator, not by
dropping the mail. The server logs them at error level for operators, and the
workspace owner/admins get one notice a day (kind `owner_email_failing`) saying
lead email is delayed, the platform team has been alerted and queued mail will
be retried. `400`/`422` (a bad message) are not retried.

One-time ops step per sending domain, which adds the SPF and DKIM records
(the domain must use Cloudflare DNS):

```bash
npx wrangler email sending enable example.com
npx wrangler email sending dns get example.com   # check the records
```

### Partner leads on WhatsApp

A lead captured on a site a partner sold to a client (`partner_client_id` set by
`POST /partners/sell`) also goes to that client, the shop owner, on WhatsApp. The
shop owner never signs in, so this is how they hear about the lead. It is sent
only when the client has a `whatsapp` number AND `whatsapp_opt_in_at` is set,
and only for `lead.captured` (a handoff lead is not routed, as above). The
site's `events` settings don't control it; the client's consent does, and
`whatsapp` is not accepted as a sink by `PUT /sites/{site_id}/lead-notifications`.

The message goes out from the PLATFORM's MSG91 account (not the workspace's
`msg91` connector used by /growth) as the pre-approved template, with this one
body variable, on one line and at most 900 characters:

```text
New enquiry for {site name} via Paw Sites by PocketPaw: {visitor name} — {message} Contact: {phone or email}
```

The contact is kept whole and the message is cut to fit (the site name is cut at
80 characters, the visitor name at 120). In the visitor's text, WhatsApp
formatting marks (`*`, `_`, `~`, backticks) are removed and links are broken
(`https://` becomes `hxxps://`), so a visitor can't style the message or plant a
tappable link. The lead email already carries the visitor's phone and email, so
the WhatsApp text includes one of them: the shop owner has no other way to reply.

Register the template as `{{1}}` plus a short fixed prefix, and end it with an
opt-out line such as "Reply STOP or ask <partner name> to stop these messages".
Meta's 1024-character limit covers the whole body, so keep the fixed text under
about 100 characters. Inbound STOP is not handled yet; for now the partner clears
the opt-in.

Consent follows the number: a client PATCH that changes `whatsapp` clears
`whatsapp_opt_in_at` unless the same PATCH sets it again.

Delivery uses the same outbox and retry schedule as webhooks (sink `whatsapp`,
worked in the webhook lane). The row holds only the lead id and site id; the text
is built when it is sent. At send time the client must still be opted in, on the
same number, and not archived, or the row is dropped; if that check can't be made
(the lookup fails) the row is retried. An MSG91 `4xx` (other than `429`) or a
rejected send is dropped, and a `429`, `5xx` or network error is retried. Only the
error code and HTTP status are stored. The other sinks never wait on it.

Each message is paid for, so one number gets at most 30 a day per workspace
(rolling 24 hours, counted on the outbox). Past that, leads are still saved and
emailed; each skipped one logs one warning (lead and site ids only).

| Variable | Purpose |
|---|---|
| `POCKETPAW_MSG91_PLATFORM_AUTHKEY` | Authkey of the platform MSG91 account. Secret, never logged. |
| `POCKETPAW_MSG91_PLATFORM_INTEGRATED_NUMBER` | The WhatsApp sender number on that account. |
| `POCKETPAW_MSG91_PLATFORM_LEAD_TEMPLATE` | Name of the approved new-lead template (one body variable). |
| `POCKETPAW_MSG91_PLATFORM_LANGUAGE` | Template language code. Default `en`. |

Until the first three are set, nothing is queued: each skipped lead logs one
warning (lead and site ids only), and the lead, bell, email and webhook go out as
usual.

## Ship — Managed Deploys

SHIP-3. The `/api/v1/ship` surface behind the /ship console: a workspace
provisions a **box** (a VPS running Dokku), registers **apps** on it, and
deploys them. Every route is license-gated and scoped to the caller's active
workspace — the workspace never travels in a body or query param, and an id
belonging to another tenant reads as `404`, never `403` (existence does not
leak).

Long work never blocks the request. `POST /ship/boxes` and
`POST /ship/apps/{id}/deploy` enqueue an ARQ job and return immediately with a
pollable record; the engine-backed routes (domains, database, scale, checks,
resources, volumes, restart, rebuild, logs, metrics) run inline over SSH and
answer `409` with `code: ship.*_failed` when the deploy engine refuses.
pollable record; the engine-backed routes (domains, database, logs, metrics)
run inline over SSH and answer `409` with `code: ship.*_failed` when the deploy
engine refuses.

**Secrets never cross this surface.** A box's SSH key is decrypted only inside
the engine session and shredded with it. App env **names** are accepted and
stored (`env_refs`); env **values** are not. A database's connection string
stays on the box — `POST /ship/apps/{id}/db` returns the NAME of the variable
holding it.

### `POST /ship/boxes`

Provision a box. Body: `{"provider": "hcloud"}`. `server_type` and `region` are
optional; they default to `cx22` / `fsn1` (Hetzner: 2 vCPU / 4 GB / 40 GB in
Falkenstein — the cheapest shape that comfortably runs Dokku plus a couple of
app containers), overridable per deployment via `POCKETPAW_SHIP_SERVER_TYPE` /
`POCKETPAW_SHIP_REGION`.

Returns the box in `provisioning`; poll `GET /ship/boxes` until it is `ready`:

```json
{"id": "…", "provider": "hcloud", "ip": "", "status": "provisioning", "price_monthly": null}
```

`status` is one of `provisioning` | `ready` | `degraded` | `destroyed`.

### `GET /ship/boxes`

The workspace's boxes, newest first — a list of the object above.

### `GET /ship/boxes/{box_id}/metrics`

Live box health, read over SSH. Three percentages, `0.0`–`100.0`:

```json
{"cpu": 21.0, "mem": 37.5, "disk": 23.0}
```

`cpu` is derived from the 1-minute load average over the core count and capped
at 100. A box that is not `ready` answers `409 ship.box_not_ready`.

### `DELETE /ship/boxes/{box_id}`

**Parks** a teardown for human approval. Nothing is destroyed, no engine command
runs, and the box keeps its current `status`:

```json
{"status": "pending_approval", "proposal_id": "<instinct-action-id>"}
```

The `proposal_id` is a real Instinct Action id: the teardown lands in The Tray
for a human to approve or reject. Only on approval does
`ship.executor.execute_approved_ship_action` touch the box — the request path
never calls the engine's destroy verb. Repeating the call returns the same
`proposal_id` rather than filing a duplicate.

The executor re-checks `ship.manage` against the proposer's **current** role
before it runs, so an approval for a since-demoted proposer fails closed, and
re-approving an already-executed action never fires twice.

### `POST /ship/apps`

Register an app on a box. Body: `{"name": "demo", "box_id": "…"}`. Optional:
`image` (the container image reference the deploy ships), `git_ref`,
`build_path` (`dockerfile` | `nixpacks`), `prod`, and `env_refs` (variable
NAMES only). Returns:

```json
{"id": "…", "name": "demo", "box_id": "…", "status": "created", "urls": []}
```

`status` walks `created` → `deploying` → `live` | `failed`. A duplicate name on
the same box is `409 ship.app_exists`; a `box_id` from another workspace is
`404`.

### `GET /ship/apps?box_id=<id>`

The workspace's apps, newest first, optionally narrowed to one box.

### `POST /ship/apps/{app_id}/deploy`

Enqueue a deploy. Takes **no body** — the app already carries its image, and the
attempt pins that image so a later app edit cannot rewrite what is in flight. An
app with no image is `422 ship.app_no_image`; a box that is not `ready` is
`409 ship.box_not_ready`. Returns the attempt immediately:

```json
{"id": "…", "app_id": "…", "status": "queued", "started_at": "2026-07-22T…Z", "finished_at": null}
```

### `GET /ship/apps/{app_id}/deploys`

The app's deploy attempts, newest first. `status` walks
`queued` → `building` → `releasing` → `live`, or lands on `failed`;
`finished_at` is set on a terminal state. Poll this to follow a deploy.

### `POST /ship/apps/{app_id}/domains`

Route a domain to the app and (by default) issue a certificate for it. Body:
`{"domain": "demo.example.com", "enable_tls": true}`. Returns
`{"domain": "…", "tls_enabled": true}` and adds the resulting URL to the app's
`urls`.

### `GET /ship/apps/{app_id}/domains`

`{"domains": [{"domain": "…", "tls_enabled": true}]}` — the domains recorded at
add time.

### `POST /ship/apps/{app_id}/db`

Create a database service and link it to the app. Body is optional; `service`
defaults to `<app-name>-db`. `db_type` picks the engine — `postgres`, `redis`,
or `mongo` (default `mongo`); the box installs all three plugins at provision
time. The injected variable name follows the engine (`DATABASE_URL` for
postgres, `REDIS_URL` for redis, `MONGO_URL` for mongo). Returns:

```json
{"service": "demo-db", "linked_app": "demo", "env_var": "DATABASE_URL"}
defaults to `<app-name>-db`. Returns:

```json
{"service": "demo-db", "linked_app": "demo", "env_var": "MONGO_URL"}
```

`env_var` is the NAME of the variable the link injected. The connection string
is a secret and never crosses the wire.

### `PUT /ship/apps/{app_id}/scale`

Set how many containers run per process type. The body is a `scale` map of
process name → count; a count of `0` stops that process. Process names use the
Procfile grammar (`^[a-z][a-z0-9_-]*$`). Applies on the next deploy. Returns the
app with its new `scale`:

```json
{"scale": {"web": 2, "worker": 1}}
```

### `PUT /ship/apps/{app_id}/checks`

Configure zero-downtime deploys. `zero_downtime` (default `true`) toggles Dokku's
settle-and-drain deploy — the new container must pass its checks before the old
one is retired; `healthcheck_path` is the optional HTTP path the check hits.
Both apply on the next deploy. Returns the app's current settings:

```json
{"zero_downtime": true, "healthcheck_path": "/healthz"}
```

### `PUT /ship/apps/{app_id}/resources`

Set the app's CPU and/or memory ceilings (the cost-control lever, `resource:limit`).
`cpu` is in Dokku's CPU units, `memory_mb` in megabytes; a `0` leaves that
dimension unlimited, but at least one must be non-zero. Applies on the next
container start. Returns the app with its new `cpu_limit` / `memory_limit_mb`:

```json
{"cpu_limit": 1000, "memory_limit_mb": 512}
```

### `POST /ship/apps/{app_id}/volumes`

Create a persistent volume and mount it into the app (`storage:create` +
`storage:mount`). `mount_path` is the absolute container path; `name` is optional
and defaults to `<app-name>-data`. The data survives redeploys (a host bind
mount). Returns the app with its `volumes` list:

```json
{"volumes": [{"name": "demo-data", "mount_path": "/data",
              "host_path": "/var/lib/dokku/data/storage/demo-data"}]}
```

### `POST /ship/apps/{app_id}/restart` · `POST /ship/apps/{app_id}/rebuild`

Restart (`ps:restart`) or rebuild-from-source (`ps:rebuild`) the app. Both are
reversible bounces — the app comes back — so they run inline, not through the
Instinct gate, and they change no persisted config. Each answers a confirmation:

```json
{"app_id": "665…", "action": "restart"}
```

### `GET /ship/apps/{app_id}/logs?num=<n>`

Recent app log lines, newest last (`num` defaults to 100, max 1000). The engine
redacts them before they leave the box:

```json
{"lines": ["2026-07-22T…Z app[web.1]: GET /health 200"]}
```

### `DELETE /ship/apps/{app_id}`

**Parks** an app teardown for human approval, exactly like the box DELETE above.
Nothing is destroyed.

### `GET /ship/apps/{app_id}/metrics`

One app's live health: process state (from Dokku) plus **real per-container
CPU/memory** (from `docker stats` — Dokku's own `ps:report` gives only process
state, not resource usage). `cpu`/`mem`/`disk` are percentages or `null` when the
box could not report them (an old Docker, a down container) — render "—" for a
null, never a misleading 0. Process state always comes back.

```json
{"deployed": true, "running": true, "processes": 1,
 "cpu": 12.3, "mem": 5.6, "disk": 38.0}
```

### Environment variables (SHIP-9)

An app's env vars are stored **Fernet-encrypted at rest** (the same envelope as
the box SSH key) and are **never returned in plaintext** — every response masks
the value to a short hint. Values are decrypted only at deploy time, merged into
the engine's `config:set`, and redacted from every log line. `scope` is one of
`both` (default), `prod`, or `preview`; at deploy only the vars matching the
app's kind (its `prod` flag) plus every `both` var are applied.

#### `GET /ship/apps/{app_id}/env`

Lists the app's env vars, values masked:

```json
{"vars": [{"key": "API_KEY", "masked_value": "sk-…3f9", "scope": "both"}]}
```

#### `PUT /ship/apps/{app_id}/env`

Upserts a batch. Each key is added or overwritten; keys absent from the body are
left untouched. Keys use the POSIX env-name grammar; values are opaque (any
string up to 64 KiB). Returns the full masked list.

```json
{"vars": [{"key": "API_KEY", "value": "sk-live-…", "scope": "prod"}]}
```

#### `POST /ship/apps/{app_id}/env/import`

Bulk-imports a `.env` blob. Blank lines and `#` comments are ignored; each
remaining line is split on the first `=` with surrounding quotes stripped; a line
whose key is not a valid POSIX name is **skipped** (a paste never 422s on one
stray line). Returns the full masked list.

```json
{"dotenv": "API_KEY=sk-live-abc\n# comment\nDEBUG=false"}
```

#### `DELETE /ship/apps/{app_id}/env/{key}`

Removes one variable. Returns the remaining masked list.

### The agent surface (`pocketpaw_ship` MCP)

A chat agent in a room whose pocket has the **Ship connector** bound reaches the
same service layer through sixteen in-process MCP tools — `ship_list_boxes`,
`ship_provision_box`, `ship_list_apps`, `ship_create_app`, `ship_deploy_app`,
`ship_add_domain`, `ship_create_db`, `ship_set_scale`, `ship_set_checks`,
`ship_set_resources`, `ship_create_volume`, `ship_restart`, `ship_rebuild`,
`ship_logs`, `ship_metrics`, and `ship_request_destroy`. Binding the connector
also auto-surfaces the bundled `ship` skill into that room.
### The agent surface (`pocketpaw_ship` MCP)

A chat agent in a room whose pocket has the **Ship connector** bound reaches the
same service layer through ten in-process MCP tools — `ship_list_boxes`,
`ship_provision_box`, `ship_list_apps`, `ship_create_app`, `ship_deploy_app`,
`ship_add_domain`, `ship_create_db`, `ship_logs`, `ship_metrics`, and
`ship_request_destroy`. Binding the connector also auto-surfaces the bundled
`ship` skill into that room.

The agent's surface is deliberately **narrower than the HTTP one**:

| Verb | Operator over HTTP | Agent over MCP |
|------|--------------------|----------------|
| reads, provision, create app, domain, db | runs | runs |
| deploy to a non-prod app | runs | runs |
| deploy to a **prod-flagged** app | runs | **proposes** |
| destroy a box or an app | proposes | proposes |

An operator calling the API with their own credentials is a different actor from
an agent acting on their behalf, which is why the prod deploy splits. Both paths
converge on the same Instinct gate for teardowns: the tool returns
`{"status": "proposed", "proposal_id": "…"}` and the agent is instructed never to
report a destroy as done.
## Sites — Agent Editing Tools (in-process MCP)

Editing a Paw Site from chat does not go over HTTP. The chat agent reaches it
through the in-process MCP server `pocketpaw_sites_manager`
(`ee/pocketpaw_ee/agent/mcp_servers/sites.py`), whose tools are namespaced
`mcp__pocketpaw_sites_manager__<tool>`. Three editing tools live there, one per
hand-authored engine, and they are **not interchangeable** — each rejects the
other's pockets.

| Tool | Engine | Publishes? |
|------|--------|-----------|
| `edit_svelte_component` | `engine: "svelte"` | Builds a draft **preview** (workerd smoke gate; rolls the source back if it fails) |
| `edit_react_component` | `engine: "react"` | **No.** Persists the draft and stops — no build, no deploy |
| `edit_html_file` | `engine: "html"` | **No.** Persists the draft and stops — html runs no build, so there is nothing to gate a deploy on |

A ripple or dynamic site is edited through the pocket specialist's rippleSpec
merge instead. The leaf-edits REST route above is the *native editor's* html path
(uid splice) and is a different entry point from `edit_html_file`, which is the
chat agent's.

### `edit_svelte_component`

Write ONE file of a svelte site's `source` map and build a draft **preview**.

| Arg | Type | Notes |
|-----|------|-------|
| `pocket_id` | string | Required. The svelte site pocket. |
| `component_path` | string | Required. Project-relative, e.g. `src/lib/components/Hero.svelte`, `src/routes/about/+page.svelte`. Must already exist unless `create` is true. |
| `edits` | array | A list of `{old_string, new_string}` blocks applied to the file's current contents. Each `old_string` must match **exactly once**. Exactly one of `edits` / `new_source`. |
| `new_source` | string | The full new file contents (replaces the whole file). Required with `create`. |
| `create` | boolean | Default `false`. Create a NEW file at `component_path`; the path must **not** already exist. |
| `name` | string | Optional site name override on the republish. |

Returns `{ok: true, status: "draft", is_live: false, site: {...}, component_path,
created, unreferenced, message}`.

**Adding a page takes three calls, not two.** A SvelteKit route is two files — the
`+page.svelte` and a `+page.ts` carrying `export const prerender = true`, because
the root page's prerender flag is declared at page level and does not cascade to a
child route — and then an `edits` call on the nav or footer to link it. Adding a
*section* is the familiar two: `create: true` for
`src/lib/components/<Name>.svelte`, then `edits` on `src/routes/+page.svelte` to
import and render it.

**`unreferenced` is the outstanding call's reminder**, and svelte answers it two
ways because a svelte source map holds two kinds of file. A `src/lib/**` module is
reached by an import specifier, resolved rather than pattern-matched (`$lib/...`,
relative and root-absolute forms all resolve to the file they mean). A
`src/routes/**` page is reached by URL, so the question is whether anything links
to it — an import scan would call every legitimate page an orphan. A `+page.ts` or
`+layout.svelte` beside a `+page.svelte` counts as reached through its directory,
since the file-system router claims it with nothing linking to it.

Note the asymmetry with react: an unlinked svelte route still *exists*. SvelteKit's
default prerender `entries` is every non-dynamic route, so the page is generated and
reachable by typing the URL; what it lacks is a way for a visitor to find it. An
unimported react component, by contrast, is absent from the bundle entirely. Either
way `unreferenced` is advisory and never blocking — call 1 of a multi-call add is
unreferenced at the instant it lands, so refusing it would make adding a page
impossible — and it is always `false` for an ordinary edit.

**It publishes a preview, and rolls back.** Unlike its react and html siblings this
tool republishes: the edit is persisted, a preview is built behind the workerd smoke
gate, and a `SmokeGateFailed` restores the pocket to its prior state before
re-raising. The rollback shape differs by mode — an ordinary edit restores the
file's previous contents, while a failed `create` **removes the key**, because a
create has no previous contents and writing `""` back would leave an empty file at a
real route for the next publish to serve.

**Write scope is enforced, not advisory**, and the guard arrived with `create`:
while the tool could only overwrite existing keys, every writable path had already
been vetted when `create_svelte_site` landed the map. The resolved path must sit
under `src/` (there is no `public/` or `static/` on this track), and the
generator-owned paths are rejected: `src/lib/paw/`, the gated-site auth files
(`src/hooks.server.ts`, `src/lib/auth.ts`, `src/app.d.ts`) and the build shell
(`package.json`, `vite.config.ts`, `svelte.config.js`). Paths are normalized
(backslashes, `.`/`..`) before the check. The policy lives in
`ee/pocketpaw_ee/sites/svelte_paths.py`.

The guard is checked *before the pocket is read*, and that ordering is load-bearing:
`svelte-scaffold.ts` throws on a generator-owned path at materialize time, and that
throw is not a `SmokeGateFailed`, so it would escape the rollback above and leave
the pocket permanently carrying source every future publish chokes on.

Errors (relayed to the agent as `is_error` with the code, so it can fix and retry):

| Code | When |
|------|------|
| `site_edit.invalid_args` | Not exactly one of `edits` / `new_source`. |
| `site_edit.create_needs_source` | `create` without `new_source`. |
| `site_edit.reserved_path` | The resolved path is generator-owned. |
| `site_edit.path_outside_source` | The resolved path is outside `src/`. |
| `site_edit.no_match` / `site_edit.ambiguous_match` | An `old_string` matched 0 or >1 times. Make it more specific and retry. |
| `pocket.not_svelte_site` | The pocket is not a svelte Paw Site. |
| `pocket.svelte_component_exists` | `create` on a path that already exists. |
| `site_component.not_found` | `create` is false and the path is not in the source map. |
| `plan.feature_denied` | The workspace's plan lacks the `sites` feature. |

Every write goes through `pockets_service.set_svelte_source_file` (or
`remove_svelte_source_file` on a create rollback), which emits `PocketUpdated` and
records a draft `ArtifactVersion` snapshotting the full edited source map.

### `edit_react_component`

Write ONE file of a react site's `source` map as a reviewable draft.

| Arg | Type | Notes |
|-----|------|-------|
| `pocket_id` | string | Required. The react site pocket. |
| `component_path` | string | Required. Project-relative, e.g. `src/components/Hero.tsx`. Must already exist unless `create` is true. |
| `edits` | array | A list of `{old_string, new_string}` blocks applied to the file's current contents. Each `old_string` must match **exactly once**. Exactly one of `edits` / `new_source`. |
| `new_source` | string | The full new file contents (replaces the whole file). Required with `create`. |
| `create` | boolean | Default `false`. Create a NEW file at `component_path`; the path must **not** already exist. |

Returns `{ok: true, status: "draft", is_live: false, pocket_id, component_path,
created, unreferenced, message}`. To **add a section**, call it twice: once with
`create: true` for `src/components/<Name>.tsx`, then again with `edits` on
`src/App.tsx` to import and render it.

**`unreferenced` is the second call's reminder.** It is `true` when this call
*created* a file that nothing else in the source map reaches — no import specifier
resolves to it, or, under `public/`, no file mentions its URL. Such a file is not
in the bundle: the page renders exactly as it did before. The `message` then leads
with the outstanding step and an explicit instruction not to report the section as
added yet, because a create that stops after call 1 otherwise returns an
unqualified success and the agent tells the user about a component they cannot
find. It is advisory and never blocking — call 1 of two is unreferenced at the
instant it lands, every time, so refusing it would make adding a section
impossible. `unreferenced` is always `false` for an ordinary edit; the scan is
scoped to `create` so the common path stays quiet.

**It does not publish and does not enqueue a build**, and that is a deliberate
divergence from the svelte tool rather than an omission. `build_runs_async("react")`
is true: a react publish enqueues a Daytona build and returns before any build
outcome exists, so there is no synchronous result to gate on and nothing to roll
back from — a rollback fired on enqueue-success would revert a good edit.
Persisting the draft is the whole job (the same shape the leaf-edits route
documents). Publishing stays an explicit `publish` call the user asks for.

**Write scope is enforced, not advisory.** The generator owns the prerender shell,
so `index.html`, `paw-prerender.mjs`, `paw.dependencies.json`, lockfiles and
everything under `src/paw/` are rejected. The resolved path must land under `src/`
or `public/`, or be one of the root build files the author owns (`package.json`,
`vite.config.*`, `bunfig.toml`, `.npmrc`; the generator merges them with its
toolchain). Paths are normalized (backslashes, `.`/`..`) before the check, so
`./index.html` and `src/paw/../paw/entry.tsx` are rejected too. This is the same
policy `create_react_site` applies, shared through
`ee/pocketpaw_ee/sites/react_paths.py`.

Errors (relayed to the agent as `is_error` with the code, so it can fix and retry):

| Code | When |
|------|------|
| `site_edit.invalid_args` | Not exactly one of `edits` / `new_source`. |
| `site_edit.create_needs_source` | `create` without `new_source`. |
| `site_edit.reserved_path` | The resolved path is generator-owned. |
| `site_edit.path_outside_source` | The resolved path is outside `src/` and `public/` and is not a root build file. |
| `site_edit.no_match` / `site_edit.ambiguous_match` | An `old_string` matched 0 or >1 times. Make it more specific and retry. |
| `pocket.not_react_site` | The pocket is not a react Paw Site. |
| `pocket.react_component_exists` | `create` on a path that already exists. |
| `site_component.not_found` | `create` is false and the path is not in the source map. |
| `plan.feature_denied` | The workspace's plan lacks the `sites` feature. |

Every write goes through `pockets_service.set_react_source_file`, which emits
`PocketUpdated` and records a draft `ArtifactVersion` snapshotting the full edited
source map — so an edit is a reviewable Branch draft a later publish promotes.

### `edit_html_file`

Write ONE file of an html site's `source` map as a reviewable draft.

| Arg | Type | Notes |
|-----|------|-------|
| `pocket_id` | string | Required. The html site pocket. |
| `file_path` | string | Required. Project-relative and usually at the site **root** — `index.html`, `styles.css`, `about.html`, `img/logo.svg`. Must already exist unless `create` is true. |
| `edits` | array | A list of `{old_string, new_string}` blocks applied to the file's current contents. Each `old_string` must match **exactly once**. Exactly one of `edits` / `new_source`. |
| `new_source` | string | The full new file contents (replaces the whole file). Required with `create`. |
| `create` | boolean | Default `false`. Create a NEW file at `file_path`; the path must **not** already exist. |

Returns `{ok: true, status: "draft", is_live: false, pocket_id, file_path,
created, unreferenced, message}`. To **add a page**, call it twice: once with
`create: true` for e.g. `about.html`, then again with `edits` on `index.html` to
link to it.

**`unreferenced` is the second call's reminder**, the same signal
`edit_react_component` carries, resolved html's way. It is `true` when this call
*created* a file that no other file in the map points at — no `href`, `src`,
`srcset`, `poster`, CSS `url()` or `@import` resolves to it. The reference is
resolved against the referring file's own directory, a link to a directory
matches that directory's `index.html` (`/about` reaches `about/index.html`, which
is what the preview resolver serves), and off-site schemes are excluded, so a
stale `https://example.com/about/` in the markup is not mistaken for a local link.
The file the call just wrote is skipped, because a page whose only link to itself
comes from its own copied nav is still unreachable.

It matters more here than on the react track rather than less: an unimported react
component is invisible, while an unlinked html page is written and deployed and
simply cannot be navigated to — a state that is easy to describe as finished. The
`message` then leads with the outstanding link and says not to report the page as
added yet. Advisory and never blocking, and always `false` for an ordinary edit.

**The argument is `file_path`, not `component_path`, and the difference is not
cosmetic.** svelte and react have a component model; html does not — the scaffold
writes the author's map verbatim into the directory the edge serves, so what
exists is files. Paths are **root-relative with no `src/` prefix**; passing react's
`src/components/Hero.tsx` shape here creates a file nothing serves.

**It does not publish**, for a different reason than react's. React defers because
its build is async and there is no synchronous outcome to roll back from. Html has
no build *at all* (`needs_node_build` is false), so there is no smoke render and
nothing that could reject a bad edit before it deployed — a republish here would
push unvalidated markup straight to a live customer site. Draft-only is the safer
contract, not merely the convenient one.

**Write scope**: only two rejections, because an html site's files legitimately
live at the project root and react's `src/`-or-`public/` rule would reject the
whole track. Paths are normalized (backslashes, `.`/`..`) before the check.

| Rejected | Why |
|----------|-----|
| the `_paw/` namespace | Generator-owned. `_paw/edit-manifest.json` maps each editable element to a byte range; shadowing it makes the next **native** editor edit splice at wrong offsets and land mid-tag — silently. |
| anything escaping the site directory | `..` and absolute paths. |

Errors (relayed to the agent as `is_error` with the code, so it can fix and retry):

| Code | When |
|------|------|
| `site_edit.invalid_args` | Not exactly one of `edits` / `new_source`. |
| `site_edit.create_needs_source` | `create` without `new_source`. |
| `site_edit.reserved_path` | The resolved path is in `_paw/`. |
| `site_edit.path_outside_source` | The resolved path escapes the site directory. |
| `site_edit.no_match` / `site_edit.ambiguous_match` | An `old_string` matched 0 or >1 times. Make it more specific and retry. |
| `pocket.not_html_site` | The pocket is not an html Paw Site. |
| `pocket.html_file_exists` | `create` on a path that already exists. |
| `site_component.not_found` | `create` is false and the path is not in the source map. |
| `plan.feature_denied` | The workspace's plan lacks the `sites` feature. |

Every write goes through `pockets_service.set_html_source_file`, which emits
`PocketUpdated` and records a draft `ArtifactVersion` — the same chokepoint
contract the react tool uses.

**Keep the form plumbing.** If a file contains a `<form>` posting to
`/capture/form`, its `action` and the hidden `paw_site_id` / `paw_key` /
`paw_redirect` inputs are what deliver leads. A rewrite that drops them leaves a
form that still looks right and captures nothing, with no visible change to the
page.

### `set_site_dependencies`

Declare or drop npm packages on a svelte, react or html site. This is the only
writer of the reserved source-map file `paw.dependencies.json`. Every edit tool
above refuses that path in any spelling, case included.

| Arg | Type | Notes |
|-----|------|-------|
| `pocket_id` | string | Required. A svelte, react or html site pocket. ripple is refused (`site_deps.engine_unsupported`). |
| `add` | array | `[{name, range?}]`. `range` is an exact version, an npm semver range or a dist-tag (`next`, `beta`); omit it for `latest`. `"name@range"` strings are accepted too. |
| `remove` | array | Package names to drop. Dropping the last one removes the file. |

Returns `{ok, pocket_id, packages: {name: {version}}, rejected: [{name, code,
reason}], warnings: [{name, code, message}], changed, message}`. A refused package
is not an error: `ok` stays true, and the others still land.

Each add is resolved from registry metadata only. Nothing is installed. Any public
npm package is allowed (the policy since 2026-10-07): there is no release-age,
size, downloads, install-script, native-addon or package-count gate, because
author packages install only in the Daytona build sandbox, which is the isolation
boundary. `latest` and other dist-tags resolve through the packument's
`dist-tags`; a range picks the highest matching version, preferring one that is
not deprecated. The result is always pinned to an exact version. A package is
refused only when:

| `code` | When |
|--------|------|
| `invalid_name` / `invalid_range` | Not a valid npm name, or not a version, range or dist-tag. |
| `non_registry_spec` | git, url, file, tarball, `npm:` alias or GitHub shorthand. |
| `toolchain_reserved` | The name is reserved for the build toolchain by the vendored paw-sites allowlist. None are today: svelte, vite, react and the rest may be declared, and the declared version wins over the generator's pin. |
| `not_found` / `no_eligible_version` | Not on the registry, or no version matches the range / the dist-tag does not exist. The reason names `latest` (or the known tags). |
| `registry_unavailable` | The registry could not be read and the request was a range or tag, which needs it. Retryable. An exact version is accepted as given instead, with an `unverified` warning. |

`warnings` carry `advisory` (a moderate-or-worse npm advisory affects the chosen
version), `deprecated` and `unverified`. They never block. An advisory-endpoint or
jsDelivr outage is ignored rather than refusing the package.

For html, each entry also carries `esm`
(`https://cdn.jsdelivr.net/npm/<name>@<version>/+esm`), which the generator turns
into an importmap, and `integrity` (sha384 of the bytes jsDelivr serves at that
URL) when jsDelivr answered. `integrity` is optional.

`create_svelte_site`, `create_react_site` and `create_html_site` take the same
requests as an optional `dependencies` argument. They resolve them before the
pocket is saved and return `packages` and `rejected` (and `warnings` when there are
any) in the create body. A refused package never fails the create.

**Author packages install only in the build sandbox.** A static svelte site that
declares packages publishes through the ephemeral build lane even with
`PAW_SITES_SVELTE_ASYNC_BUILD` off. Since PP-2, `edit_svelte_component` never
builds on the API host for any svelte site: it verifies the draft in the sandbox
lane (see "Draft verification" below). A host build that gets author packages
anyway is refused with `sites.author_dependencies_need_sandbox` (422).

**Dynamic svelte sites refuse packages.** A svelte site with live-data bindings
(`pattern: "dynamic"`, or `sources` / `actions` / `auth` on the source envelope) is
rendered by a Worker that the sandbox build lane cannot deploy yet. Declaring a
package on one is refused at declaration time with code `engine_unsupported`,
from `set_site_dependencies` and from the create tools' `dependencies` alike, so no
site can reach the publish-time 422.

### Draft verification (`verification`, `verify_site`)

Every `create_svelte_site`, `create_react_site`, `create_html_site`,
`edit_svelte_component`, `edit_react_component`, `edit_html_file` and
`set_site_dependencies` result carries a `verification` object. Creates and
`verify_site` carry the full verdict below; edits carry the faster edit verdict
described under "Edits: static now, build later". It says whether the draft
actually works:

```json
"verification": {
  "status": "passed | failed | unverified",
  "reason": "sandbox_unavailable",
  "content_hash": "…",
  "layers": [
    {"name": "static",  "status": "passed | failed | skipped | unverified", "reason": "…"},
    {"name": "build",   "status": "…"},
    {"name": "browser", "status": "…"}
  ],
  "errors":   [{"layer": "static | build | browser", "file": "…", "line": 1, "col": 1, "code": "…", "message": "…"}],
  "warnings": [ … ],
  "note": "worker-rendered site: browser layer checked the prerendered shell only",
  "checked_at": "ISO-8601"
}
```

`reason` is present only when `status` is `unverified`. `note` is optional.
When the reason is a sandbox one (`waiting_for_capacity`,
`sandbox_unavailable:capacity`, any other `sandbox_unavailable`), the verdict
also carries `message`: the sentence to show the user as written. A full
Daytona org is reported as a queue (`waiting_for_capacity`, then
`sandbox_unavailable:capacity` once about five minutes of retries are spent),
never as an outage. See `docs/runbooks/2026-10-07-daytona-org-capacity.md`.

The three layers:

| Layer | What runs | Where |
|-------|-----------|-------|
| `static` | `paw-sites-gen check` over the same generator input the build gets: svelte compile, TS parse, import resolution (every bare import must be a toolchain package or a declared one), html links and importmap coverage. Installs nothing. | API host, about a second. |
| `build` | The draft preview build: scaffold, install, build. It is the same arq job the editor's `GET /sites/by-pocket/{id}/native-artifact` queues, keyed on the same content hash, so one sandbox warms the editor and answers the agent. html has no build and reports `skipped`. | Daytona sandbox. |
| `browser` | The paw-sites harness (`browser-check.mjs`) loads every built page in headless Chromium and reports runtime exceptions, `console.error`, failed same-origin requests, blank pages, error pages and hydration errors. svelte / react run it in the build's sandbox after a clean build; html gets its own sandbox job. | Daytona sandbox. |

**Status rules.** `passed` means every applicable layer ran and passed. Any
`failed` layer makes the verdict `failed`. Anything that stopped a layer from
running makes it `unverified`, never `passed`: `checker_unavailable`,
`check_crashed`, `queue_unavailable` (arq/Redis down), `sandbox_unavailable`,
`timeout`, `browser_unavailable`, `harness_unavailable`, `harness_install_failed`,
`no_static_pages`, `engine_not_verifiable` (ripple sites, which have no authored
code to check), `verify_unavailable`. A static failure skips the sandbox layers
(`skipped`, reason `static_check_failed`), and spends no sandbox. A generator-owned
build-shell file (see the legacy build-shell migration) or packages on a dynamic
svelte site are static failures too.

**Diagnostics are agent-only.** `errors` and `warnings` messages are run through
`redact_output`, sandbox paths are made project-relative and `site_key_*` tokens are
scrubbed. The whole `errors` + `warnings` payload is capped at 2 KB, with a trailing
`{"code": "truncated"}` entry when something was cut. They never reach the Site row,
`/status` or any UI payload.

**Caching.** A `passed` or `failed` verdict is cached per content hash, so
re-verifying unchanged source is free (the result then carries `"cached": true`).
Any source change produces a new hash and a fresh verification. `unverified` is
never served from the cache. The build job also stores its build + browser report
under the hash, so an editor pre-warm answers the next verify without a second
sandbox.

**Deadline.** A create or `verify_site` waits at most `PAW_SITES_VERIFY_WAIT_SEC`
(default 90) for the sandbox layers, plus a short slack for the static check. On
expiry the verdict is `unverified` / `timeout` and the build keeps running; the next
`verify_site` attaches to the same job.

#### Edits: static now, build later

An edit tool (`edit_svelte_component`, `edit_react_component`, `edit_html_file`,
`set_site_dependencies`) runs only the `static` layer before it answers, about a
second. For svelte and react it then queues the preview build (build + browser) and
returns without waiting:

```json
"verification": {
  "status": "pending",
  "static": "passed",
  "build": "pending",
  "job_id": "site-preview-<pocket_id>-<content_hash>",
  "content_hash": "…",
  "layers": [
    {"name": "static",  "status": "passed"},
    {"name": "build",   "status": "pending"},
    {"name": "browser", "status": "pending"}
  ],
  "errors": [], "warnings": [],
  "checked_at": "ISO-8601"
}
```

`static` and `build` mirror the two layers' statuses. Other shapes an edit can
return:

| Case | `status` | Notes |
|------|----------|-------|
| Static check failed | `failed` | `build` / `browser` are `skipped` (`static_check_failed`); nothing is queued. |
| This exact source was already built (a pre-warm, an earlier verify) or verified | `passed` / `failed` | The full verdict, read from the store; `cached: true` when it was a cached verdict. |
| html | `unverified`, reason `browser_check_on_demand` | `build` is `skipped`; the browser layer runs only when `verify_site` is called. |
| Queue down | `unverified`, reason `queue_unavailable` | |
| `create: true` that nothing links to or imports yet | `skipped`, reason `create_half_step` | No check at all; the edit that wires the file in verifies the whole site. |

**`previous_verification`.** When the queued job finishes, its verdict is attached
to the NEXT result of an edit tool or `preview_site` for that pocket, once, as
`previous_verification` (the full verdict plus `static`, `build` and `job_id`). On a
success result it is a key in the JSON body; on an error result or the image result
of `preview_site` it is a trailing text block. A verdict that `verify_site` already
returned is not repeated. A job that never reports within 15 minutes comes back as
`unverified` / `no_report`. There is no realtime event for it.

**Superseded builds.** A new preview build for a pocket aborts the pocket's previous
queued or running one (arq abort; the sites worker sets `allow_abort_jobs`). A
`verify_site` that was waiting on the aborted job reads its layers as `unverified` /
`superseded`. If the source returns to an aborted render (an undo), that render is
queued again rather than reported as failed.

**One builder origin.** The editor's native-artifact view, the post-edit pre-warm,
the verify pipeline and `preview_site` all compute the armed content hash with the
same origin (`service.resolve_armed_builder_origin`): the request `Origin` when
there is one (recorded per pocket), else the origin the editor last viewed the draft
with, else the Site row's `builder_origin`, else `PAW_SITES_BUILDER_ORIGIN`. One edit
therefore builds one render.

**The draft preview appears before the browser check.** The preview job stores the
draft artifact (and its preview URL) as soon as the build is clean, then runs the
browser harness.

**Timing logs.** Every step logs elapsed milliseconds, so edit latency can be read
from the logs alone: `sites.edit_tool: tool=… pocket=… result=… verification=…
elapsed_ms=…` (API, one line per edit call), `sites.verify: layer=static …` (API),
`sites.verify: layer=queue_wait|build|browser …` (worker) and
`sites.verify: layer=sandbox_wait …` (API, a waiting `verify_site`).

**`edit_svelte_component` rollback.** Only a `static` failure rolls an edit back,
because it is the only failure known before the edit returns: the file is restored
(a created file is removed) and the tool returns
`{ok: false, status: "rolled_back", verification, message}` as data, not as an MCP
error. A `build` or `browser` failure, found by the background job (or read from
the store for source that was already built), keeps the edit staged and is reported;
the agent fixes it with a follow-up edit. `unverified` keeps the edit staged. react
and html edits stay draft-only and carry the edit verdict. The svelte edit result's
`site.preview_url` is always `null`: no local preview deploy is made; the builder
shows the draft from the preview build.

#### `verify_site`

| Arg | Type | Notes |
|-----|------|-------|
| `pocket_id` | string | Required. |

Returns `{ok, pocket_id, verification}`. `ok` means the check ran and answered;
whether the site works is `verification.status`. A missing or foreign pocket is an
error. It waits for the build and browser layers (it is the waiting verify), so the
agent calls it once at the end of a turn's edits, after a fix, or after an
`unverified` / `timeout` result.

#### `preview_site`

| Arg | Type | Notes |
|-----|------|-------|
| `pocket_id` | string | Required. |
| `device` | `desktop` \| `mobile` | Optional, default `desktop` (1280px); `mobile` is 390px. |

Returns MCP `image` blocks (a full-page JPEG screenshot of the current draft, cut
into at most three tiles, top first; the capture waits for the page's `load` event)
followed by one text block, plus a `previous_verification` text block when an
edit's background verdict is waiting. `verify_site` says
whether the draft builds and loads; this is how the agent sees whether it looks
right. Nothing is stored.

The draft document comes from `draft_markup` for html sites and any pocket whose
on-host build is of its current content (the build dir's content stamp must match;
a stale build is skipped), otherwise from the cached preview render (`get_native_artifact`)
for svelte and react. Errors, none of which mean the site is broken:

- the render is still building: call `verify_site`, then ask again;
- a ripple site with no built draft: no picture;
- Cloudflare Browser Rendering is not configured (`PAW_CF_ACCOUNT_ID` /
  `PAW_CF_API_TOKEN` / `PAW_CF_ZONE_ID`): screenshots are unavailable on this
  deployment.

#### `GET /sites/by-pocket/{pocket_id}/status` — `verification`

```json
"verification": {"status": "passed | failed | unverified | pending | none", "error_count": 0, "checked_at": "ISO-8601", "content_hash": "…"}
```

Counts only; there is no message field (the model forbids extra keys). It
describes the CURRENT source: after an edit it reads `none` until the new source is
verified. `pending` is a verify in flight (a marker older than 15 minutes reads as
`none`).

**Deploy requirements.** The image ships the harness at `/opt/paw-sites/harness`
(`scripts/vendor-paw-sites.sh` and the `Dockerfile.enterprise` paw-sites stage copy
it; override with `PAW_SITES_HARNESS_DIR`). Set `PAW_SITES_VERIFY_IMAGE` to a Daytona
sandbox image carrying bun, node and Playwright 1.62.1's Chromium; without it the
sandbox tries `playwright-core install chromium` and, if the browser still cannot
launch, the browser layer is `unverified` / `browser_unavailable`.

### Build state on the `publish` response

`publish` returns `{ok, message, site: {...}}`. The `site` object carries the five
original keys (`id`, `pocket_id`, `name`, `url`, `deployed`) plus the build lane's
state:

| Key | Notes |
|-----|-------|
| `build_status` | `none` \| `queued` \| `building` \| `built` \| `failed`. Passed through **verbatim** — an unrecognised value is never normalised. |
| `build_reason` | `"<rung>:<cause>"` explaining how the build settled. `null` until one does. A `failed` status without this is unactionable. |
| `build_job_id` | Handle for the queued build. Persisted, so it survives a reload. |
| `build_in_progress` | Derived. `true` while a build is running, **and for any unrecognised `build_status`**. |
| `is_live` | Derived. The only field to gate "show the user the url" on. |

`is_live` requires a non-empty `url` **and** `deployed` **and** no build in flight,
because each is individually insufficient. This matters on **react**, the only engine
where `build_runs_async(engine)` is true:

- On a **first** publish, `_enqueue_static_build` creates the Site doc with `url: ""`
  and `deployed: false`. That is honest — nothing is serving yet, and the worker flips
  both when the deploy succeeds — but it means `url` alone is an empty string.
- On a **re-publish**, `url` and `deployed` deliberately keep the *previous* deploy's
  values so a rebuild never reports a working site as down. Both say "live" while the
  url serves the pre-change page.
- `build_status` alone cannot tell a never-built pocket (`none`) from a finished one.

`build_in_progress` reads an unknown status as in-progress, which is the wire contract
and the deliberate **opposite** of `build_state.should_enqueue`, which treats an
unknown status as terminal. Both are correct on their own axis: a redundant build costs
one sandbox, while a spurious "your site is live" costs the user's trust.

The derivation lives in `sites.service.build_wire_state` and is shared with the status
tool below, so the two surfaces cannot disagree about whether a site is live.

### `get_site_build_status`

Read-only. Takes `pocket_id` and returns `{ok, message, pocket_id, site_id, name,
published, url, deployed, build_status, build_reason, build_job_id, build_in_progress,
is_live}`.

This exists because a react publish is **asynchronous**: the `publish` call returns
before the build starts, so its response can never report how the build ended. Without
a later read, `queued` is a dead end — the agent learns a build was enqueued and has no
way to discover it finished.

A pocket with no Site doc returns `published: false` rather than an error; from the
caller's side "this was never published" is the useful answer, and it is correct whether
the pocket has no site or does not exist. The read resolves the canonical Site doc
through `canonical_site_for_pocket`, which is tenant-scoped on the workspace — that
filter is the access check, and there is no plan gate because nothing is mutated.

### Project sites (`engine: "project"`)

A project site is a whole repo started from a paw-sites base template (Astro,
TanStack Start, Vite + React + Hono, Next, SvelteKit on Cloudflare Workers). Twelve
tools on the same server create and edit it (`ee/pocketpaw_ee/agent/mcp_servers/sites_project.py`);
every one refuses a pocket of any other engine. The bundled skill
`pocketpaw-create-project-site` teaches the loop and the template routing.

| Tool | Args | What it does |
|------|------|--------------|
| `list_site_templates` | none | `{templates: [{slug, name, summary, when_to_use, stack, target, recipes}]}` from `paw-sites-gen starters --json` (cached per process) |
| `start_site_from_template` | `slug`, `brief`, `name?` | `template-copy` into a temp dir, then creates the draft pocket: `type="site"`, `pattern="landing"`, `engine="project"`, `site_meta.project = {template, framework, recipes: []}`. Returns `pocket_id`, `next_steps`, `verification`, and AGENTS.md as a second verbatim text block. A template with a binary file is refused (`sites.template_binary_files`) |
| `list_site_recipes` | `template?` | `{recipes: [{id, name, summary, applies_to, requires, conflicts, plan, bindings, secrets, env}]}` |
| `apply_site_recipe` | `pocket_id`, `recipe_id`, `dry_run?` | Runs `apply-recipe` on the source map in a temp dir and writes the changed files back in one save; records the id in `site_meta.project.recipes`. Returns `written`, `packages_added`, `migrations`, `binding_requests`, `secrets` / `secret_names`, `env_requests`, `glue_tasks`, `verify`. A conflict or error writes nothing (`is_error`, `status: "conflict"` / `"error"`) |
| `list_site_files` | `pocket_id`, `prefix?` | `{files: [{path, size}], file_count, truncated}` |
| `read_site_file` / `read_site_files` | `pocket_id`, `path` / `paths` | A JSON block, then one verbatim `=== FILE: <path> (<n> bytes) ===` block per file. Over 200,000 bytes a file is `truncated`; past 400,000 bytes per call a file is `omitted` |
| `write_site_files` | `pocket_id`, `files: {path: contents}` | Create or overwrite. 1 MiB per file, 4 MiB and 200 files per call; the whole map stays under the build's 5,000 files / 50 MiB |
| `patch_site_file` | `pocket_id`, `path`, `edits: [{old, new}]` | Each `old` must match exactly once (the same rule as the other edit tools); nothing is saved otherwise |
| `delete_site_files` | `pocket_id`, `paths` | Every path must exist; `package.json` cannot be deleted |
| `run_site_build` | `pocket_id` | Queues the draft build of the current files (the preview lane) and waits up to 30 s. `status` is `built` / `failed` / still `queued` or `building`; `preview_url`, `preview_mode`, and the log tail as a text block on a failure |
| `get_site_build_log` | `pocket_id`, `job_id?` | The latest (or named) build's status, `current`, and its redacted log tail (last 12,000 characters) |

**Paths.** Relative to the repo root, forward slashes. Refused
(`sites.project_bad_path`): absolute paths, drive letters, `..`, backslashes, NUL,
anything under `node_modules/`, `.git/` or `.paw/`, `paw-build.json`, and real
`.env` / `.env.*` / `.dev.vars` files (only `*.example` variants). Secret values never
live in the source map.

**Every write** (`write_site_files`, `patch_site_file`, `delete_site_files`, `apply_site_recipe`)
saves the draft as one version and queues the build of the new source:
`verification` is `{status: "pending", build: "pending", job_id}` (or `passed` when
that exact source already built). A change to `package.json`'s dependency lists drops
the stale lockfile (`lockfile_removed`), because the sandbox installs with
`--frozen-lockfile` when one exists.

**Recipe plans.** A recipe's `plan` (`free` / `site` / `staff`) is checked against the
site's own plan with `entitlements.site_paid_backends_entitled`, the predicate the
binding provisioner uses; below it is `sites.recipe_plan_required` and nothing runs.

**Secrets.** Recipes return secret NAMES. The agent requests each one with
`request_site_secret` (the secrets lane); no tool here writes a value.

## Fabric — Transform Mappings (source→Fabric ingest)

The transform surface over the per-workspace `FabricIngestConfig`: which
sources land as typed Fabric objects, and how. A mapping's `source_kind`
picks the pipeline:

- `"firestore"` (default) — the original reader path: mirror a Firestore
  collection, keyed on the doc path, with a real high-water cursor.
- `"connector"` — pull records through the OSS connector→Fabric ingestor
  registry (`pocketpaw.connectors.fabric_ingest.FABRIC_INGESTORS`;
  `gcalendar` is the first registered adopter). By convention `collection`
  holds the connector name (it stays the routing key everywhere);
  `connector_id` overrides it when they differ. The run resolves the
  workspace's **enabled** `WorkspaceConnector` row and calls the ingestor
  with that row's `user_id`, so a user-scoped connector reads with that
  member's OAuth token bucket (`null` = the shared/workspace bucket).

All routes are license + plan-feature `fabric` gated (business tier and up).
Reads require `fabric.read`, mutations (author, delete, run-now) require
`fabric.write`; the workspace is always the caller's active workspace — it
never travels in a request body.

### `GET /fabric/ingest/mappings`

Returns `{"mappings": [...]}` — the caller's workspace's authored mappings
(empty list when nothing is configured yet). Shown regardless of the config's
`enabled` flag so a paused pipeline is still visible.

### `POST /fabric/ingest/mappings`

Author one mapping — create-or-replace, keyed on `collection` (201). Body:

```json
{
  "collection": "gcalendar",
  "object_type_id": "ot-calendar-event",
  "source_kind": "connector",
  "connector_id": null,
  "field_map": {},
  "cursor_field": "",
  "link_rules": []
}
```

A malformed mapping (blank `collection` / `object_type_id`, blank field-map
entries) is rejected with 422 before anything is stored.

### `DELETE /fabric/ingest/mappings?collection=<key>`

Remove the mapping keyed on `collection` (204; 404 when it doesn't exist).
The key rides a query param, not a path segment — Firestore collection paths
can contain `/`.

### `POST /fabric/ingest/run`

Run one mapping's ingest immediately. Body: `{"collection": "<key>"}`.
Returns the ingest result envelope:

```json
{
  "workspace_id": "…", "source_id": "gcalendar", "status": "ok",
  "mode": "backfill", "objects": 3, "cursor": "", "errors": []
}
```

Misconfiguration — no mapping for the key, connector not connected or
disabled, no ingestor registered under the connector id — reports
`status: "error"` with the reason in `errors` (HTTP 200, matching the
background sweep's never-raise, per-source isolation contract). Re-runs are
idempotent: objects upsert by `(source_connector, source_id)`.

---

## Social Sign-In & Connected Accounts

Google and GitHub sign-in, plus the Settings surface where a signed-in user
connects and disconnects those identities. Seven endpoints in two groups, and
the groups differ in what authorises them — which is the thing to get right
before changing any of this.

### Cloud routes authenticate at the route level

**Every cloud route needs its own guard.** The global `AuthMiddleware` does not
gate `/api/v1/`: it builds `is_auth_optional` from
`auth_optional_prefixes = ("/api/v1/",)` and skips its final 401 for every
match, so that ee routes resolve identity through fastapi-users instead. The
cascade still runs and still populates `request.state` — session cookies, API
keys, `full_access` — so routes mounted at the shared prefix can read it; it
simply is not the thing that rejects.

So when you add a cloud route, a session dependency (or an explicit in-handler
check) is **required**, not belt-and-braces. `tests/cloud/auth/test_route_auth_audit.py`
asserts this across every mounted router and keeps an allowlist of the routes
that are public by design, each with its reason.

### Verifying an auth change locally proves nothing by default

`POCKETPAW_LOCALHOST_AUTH_BYPASS` **defaults to true** and grants
`request.state.full_access` to any caller whose address is loopback. On a dev
box you therefore cannot tell "this endpoint requires auth" from "this endpoint
let me in because I am on localhost" — a `curl` from your own machine succeeds
either way.

Set it to false before testing an auth change by hand:

```bash
export POCKETPAW_LOCALHOST_AUTH_BYPASS=false
```

Better, assert it in a test against the ASGI app with no session, the way
`tests/cloud/sessions/test_runtime_route_auth.py` does. The bypass refuses a
spoofed `X-Forwarded-For`, so a remote caller cannot claim loopback — the trap
here is local verification, not a production hole.

### Guest onboarding (BYOK-first, 2026-09-01)

| Endpoint | Notes |
|---|---|
| `POST /auth/guest` | `{api_key, provider?="anthropic", base_url?, model?}`. Rate-limited per IP (429). Validates the key against the provider FIRST (422 `byok.key_rejected` / `byok.key_rate_limited` / `byok.provider_unavailable` / `byok.provider_unsupported`); a dead key mints **nothing**. `provider` is `anthropic` or `openai_compatible`; the latter REQUIRES `base_url` and `model` (422 `byok.base_url_required` / `byok.model_required`) and the URL must be https on a host that is external **after DNS resolution** — the name is resolved and every address it answers with is checked, so a public hostname pointing at a private range is refused like the private address itself (422 `byok.base_url_rejected`). The request that verifies the key is then sent to that exact resolved address, with redirects off. On success mints an anonymous user (`is_guest`) + workspace + default agent, stores the key encrypted (the same per-workspace Fernet store the `/byok` routes use), and answers exactly like `POST /auth/login` (204 + cookies). Public by necessity — a guest has no account yet; on the route-auth-audit allowlist with that reason. |
| `POST /auth/guest/upgrade` | Authenticated guest only. `{email, password}` attaches real credentials to the **same user id** (workspace, sessions, key all stay) and flips `is_guest` off. 409 `auth.email_taken` / `auth.not_a_guest`. The stock `/auth/register` always creates a NEW user, hence the dedicated route. |

Guest limits are server-side and fail-CLOSED: 2 sessions and 40 turns/day by
default (per-user `guest_limits`). Over-limit responses are 402 with top-level
`{"code": "guest_limit_reached", "kind": "sessions"|"turns"}`; uploads answer
403 `{"code": "guest_upload_forbidden"}`; a guest whose stored key is missing
or undecryptable gets 402 `{"code": "guest_key_required"}` — guests never fall
back to platform credentials. `GET /auth/me` carries `is_guest`.

`POCKETPAW_GUEST_SESSIONS` and `POCKETPAW_GUEST_TURNS_PER_DAY` raise those caps
deployment-wide. They are FLOORS, not replacements: the larger of the env value
and the guest's own `guest_limits` wins, so a single guest can still be lifted
by their row. Both are unset in production and both ignore a non-integer, zero
or negative value rather than applying it — there is deliberately no "disable
guest limits" switch, because zero is what an operator types when they mean
unlimited and the guest turn counter (`metering.service.try_spend`) reads a
cap of zero as *refuse every turn*. A dev box turns the caps off by setting them past anything it will reach:

```bash
export POCKETPAW_GUEST_SESSIONS=1000
export POCKETPAW_GUEST_TURNS_PER_DAY=100000
```

Turn billing: every workspace with a stored BYOK key (guest or not) now runs
its chat turns on that key — the executor resolves credentials per turn and
threads them into the agent pool's isolated backend. The turn's model must
belong to the key's provider (402-style `byok.model_provider_mismatch` error
frame on a mismatch, never a silent upstream 401). A gateway key
(`openai_compatible`, 2026-09-09) is exempt from that check: a gateway's model
ids are its own namespace, so there is no name shape the server could check
against, and the model that actually runs is the `pydantic_ai_model` the
credential resolver pins — the pydantic_ai backend, which is the one a gateway
turn runs on, accepts a per-send `model_override` only to ignore it.

**The gateway address is re-checked on every turn** (2026-09-11). A stored
`base_url` is resolved again before the turn runs, and every address it
resolves to must be external. A row written before this check existed, or a
host whose DNS later points inside, gets a terminal
`byok.base_url_rejected` error frame. The turn is refused, not quietly moved
onto platform credentials — a tenant's bad address must not spend the
platform's money.

**A gateway turn does not go through the LiteLLM proxy.** An Anthropic key
rides as a forwarded `x-api-key`, which works because the proxy knows where
Anthropic is; there is no model group pointing at a URL a user typed, so a
gateway turn is sent straight to `base_url` through the runtime's own
`openai_compatible` provider. Those turns therefore produce no spend-log row
and skip the proxy's guardrails. Weigh that before a paid tier rides the same
seam.

### BYOK key management

| Endpoint | Notes |
|---|---|
| `GET /byok/key` | `ByokStatus` — `configured`, `provider`, `base_url`, `model`, `last4`, `key_hint`, `last_verified_at`, `last_error`. Built from display-only columns; answering never decrypts, and no route ever returns the key. |
| `PUT /byok/key` | `{provider?="anthropic", api_key, base_url?, model?}`. Validates against the provider (or the gateway's own `/chat/completions`), then encrypts and upserts. Same shape rules and error codes as `POST /auth/guest`. An `anthropic` body carrying `base_url` or `model` is refused rather than silently ignored. |
| `DELETE /byok/key` | Idempotent. Removing an absent key succeeds. |

### BYOK key management

Workspace-scoped, not user-scoped, matching where a credential is spent. Every
response is a `ByokStatus`; no route on this prefix returns a key, and there is
no echo on save. Once written, a key is write-only from the API's point of view.

| Endpoint | Notes |
|---|---|
| `GET /byok/key` | `ByokStatus` — `configured`, `provider`, `last4`, `key_hint`, `last_verified_at`, `last_error`, plus the image columns below. Built from display-only columns; answering never decrypts. |
| `PUT /byok/key` | `{provider?="anthropic", api_key}`. Validates against the provider, then encrypts and upserts. A key the provider rejects is never written. |
| `DELETE /byok/key` | Idempotent. Removing an absent key succeeds. Clears the LLM columns rather than dropping the row when an image key still lives on it, so rotating one credential never takes the other with it. |
| `PUT /byok/image-key` | `{api_key}` — the workspace's own fal.ai key, for illustrations. Shape-checked at the edge (`<key-id>:<secret>`) and stored encrypted, but **not** validated against fal: fal has no free endpoint that proves a credential without generating an image, so a save-time check would spend money on every paste. A bad key surfaces on the first illustration as `image_last_error`. |
| `DELETE /byok/image-key` | Idempotent, and it never touches the LLM key. Safe to call: the workspace falls back to the platform illustrator under the daily cap (an account) or to the guest refusal (a guest), where a workspace with no LLM key answers 402 on every turn. |

`ByokStatus` carries four image columns alongside the LLM ones. They are
display-only and independent of the LLM key — a workspace may have either
credential, both, or neither:

| Field | Meaning |
|---|---|
| `image_configured` | Whether a fal key is stored. |
| `image_last4` | Last four of the SECRET half, so two keys sharing a key id still read differently. |
| `image_key_hint` | The key id, which is the non-secret half of a fal credential. Never the secret. |
| `image_last_error` | Why the last generation was refused, or `null`. Stamped when fal answers 401/403 and cleared on the next successful save. There is no `image_verified_at`, because this is the whole verification story for the credential. The text comes from the provider and is run through the output redactor before it is stored, so an error body that echoes the submitted key does not land in a field the API hands back. |

**A workspace image key bypasses the guest illustration refusal.** Guests are
normally refused an illustration outright, and accounts are metered against a
daily platform cap. A workspace with its own fal key is neither: it is not
refused for being a guest, and it does not claim the platform budget. The guest
refusal exists because a guest can mint a fresh workspace for a fresh ceiling,
which is an argument about the platform's money and says nothing about someone
spending their own. Note that this removes the spend ceiling but not the request
rate — a BYOK illustration path is not rate-limited today.

### Sign-in endpoints (no session — that is the point)

| Endpoint | Notes |
|---|---|
| `GET /auth/social/providers` | `{providers: [...]}` — only providers whose credentials are set. An unconfigured provider is **absent**, not present-and-broken. |
| `GET /auth/social/{provider}/login` | Begins consent, 302s to the provider. Takes `flow=web` or `flow=desktop`, and `next=<relative path>`. |
| `GET /auth/social/callback` | The provider's redirect. Redeems the code, applies the policy, then signs in **or** links. |
| `POST /auth/social/exchange` | `{xc}` traded for a bearer token. How a desktop client gets its FIRST token. Rate-limited per IP. |

Unauthenticated by necessity: the caller has no session yet. The control is the
single-use `state` from `auth/_oauth_state.py` — server-side, 32 bytes,
GET-then-DEL, 600s TTL, namespaced per flow so an SSO state cannot be spent on
the social callback. Server-side rather than a signed token deliberately: a
self-verifying state token verifies for *anyone* who presents it, which is
CVE-2025-68481 against fastapi-users.

Failures **redirect rather than return JSON**, because these are reached by a
full-page browser navigation and a JSON body would render as raw text in the
address bar. A refusal goes to `<frontend>/?auth=signin&auth_error=<code>` —
the dialog reopened with an explanation, because a refusal is a UI state and
not an error page.

The desktop branch redirects to `<frontend>/oauth-callback?xc=<code>` carrying
a **one-time reference, never a token**: 60-second TTL, single-use. A token in
a URL leaks through browser history, `Referer`, window titles and every proxy
log on the path; a spent reference is worthless.

`flow` and `next` are read from the state payload, never from the callback's
query string — a callback URL is attacker-influenced by definition. `next` is
re-validated server-side to a same-origin relative path (one leading slash, no
backslash), so `//evil.com` and absolute URLs degrade to `/`.

### Connected-accounts endpoints (session required)

| Endpoint | Notes |
|---|---|
| `GET /auth/social/identities` | `{identities: [{provider, account_email, linked_at}]}`. `linked_at` is null for rows linked before that field existed. |
| `POST /auth/social/{provider}/link` | Returns `{authorize_url}` — a URL, **not** a 302. Takes `flow=web` (default) or `flow=desktop`, and `next=<relative path>`. |
| `POST /auth/social/link/complete` | Desktop only. `{code}` → `{provider, identities}`. See below. |
| `DELETE /auth/social/identities/{provider}` | 204 on success. |

All three take `current_active_user`, and the acting account comes from that
dependency only. The provider name is the sole caller-chosen value, so no
request shape acts on somebody else's credentials. **A link endpoint that took
its target user from a body or path parameter would be an account-takeover
primitive, not a settings page** — do not add one.

`POST .../link` returns a URL rather than redirecting because Settings calls it
with `fetch`, which follows a 302 opaquely: the request would succeed against
the provider's HTML and the page would never move. The client assigns the URL
to `window.location`. These three return JSON errors in the shared `CloudError`
envelope, unlike the sign-in routes above, because they are XHR with a caller
waiting on a response.

The link flow reuses the sign-in callback. The two are told apart by a
`link_user_id` pinned into the state at authorize time, and **the callback
re-checks that id against the session cookie**. That check is load-bearing:
state is a bearer secret, so without it a stolen link state lets an attacker
complete the flow with their OWN provider account, attach it to the victim, and
sign in as them afterwards.

Web link outcomes redirect to `<frontend><next>?social_linked=<provider>` on
success and `?social_error=<code>` on refusal — never to the sign-in dialog,
which would prompt an already-signed-in user to sign in.

### Linking on desktop finishes somewhere else entirely

A desktop client is not "the web client in a window", and this is the one place
that difference is load-bearing. It authenticates with a bearer held in
localStorage, and the Tauri webview that completes consent carries **no cookie
for this origin**. So the callback cannot authenticate anyone at all — and
attaching on the strength of the state alone is exactly the theft the web
branch's cookie check exists to prevent.

The proof therefore moves to a request the app can actually authenticate. Pass
`flow=desktop` when starting the link, and the callback attaches nothing:

```
1. POST /auth/social/{provider}/link?flow=desktop     (Authorization: Bearer …)
   -> {"authorize_url": "https://github.com/login/oauth/authorize?…"}

2. app opens a webview at authorize_url; user consents

3. callback parks the identity and redirects the webview to:
      <frontend>/oauth-callback?link=<code>&provider=<provider>
   or on failure:
      <frontend>/oauth-callback?link_error=<code>

4. webview closes; app redeems the code:
   POST /auth/social/link/complete   {"code": "<code>"}   (Authorization: Bearer …)
   -> 200 {"provider": "github", "identities": [...]}
   -> 4xx CloudError envelope, same auth.* codes as everywhere else
```

`link=` is what distinguishes this from a desktop **sign-in**, which uses `xc=`
on the same `/oauth-callback` route. They are not interchangeable: one attaches
an identity, the other mints a bearer, and they live in separate single-use
namespaces so a code from one is refused by the other.

Step 4 is where authorisation happens. The parked record names the account the
link was started for, and `complete` compares it against `current_active_user`.
**A stolen link code is worth nothing without that account's bearer**, which
makes the desktop path stronger than the cookie check rather than a concession
to it. The code is single-use with a 60-second TTL.

The response carries the refreshed identity list because the window that
started the flow has already closed; a follow-up refetch that failed would
leave the panel stale with no way to explain itself.

Policy refusals (`auth.identity_claimed`, `auth.sso_enforced`,
`auth.unverified_link`) surface as JSON from step 4, which the panel renders
inline. Only a provider or network failure still refuses at the callback, and
it redirects to `/oauth-callback?link_error=…` so the webview closes rather
than sitting on a page it cannot use.

The desktop redirect ignores `next` — the webview's job is to close, and the
Settings panel that opened it is still mounted in the main window. Not building
a `next`-derived URL there also means the hostile-value problem cannot reach
that redirect at all.

An unknown `flow` is **refused** (`social.unknown_flow`, 422) rather than
defaulted to `web`. A desktop client that silently got the web branch would
consent successfully and then attach nothing, which reads as a frontend bug for
as long as it takes someone to find this paragraph.

### Refusal codes

The frontend maps each of these to its own copy in
`core/auth/social-errors.ts`, so renaming one silently degrades a specific
message to a generic fallback.

| Code | Means | Path |
|---|---|---|
| `auth.unverified_link` | The provider would not vouch for any email address. | Both |
| `auth.sso_enforced` | The user's workspace mandates SSO. | Both |
| `auth.identity_claimed` | That identity is already attached to a **different** account. | Link |
| `auth.link_session_mismatch` | The callback's session is not the account that started the link. | Link |
| `auth.last_credential` | Unlinking would leave the account with no way to sign in. 409. | Unlink |
| `auth.not_linked` | No identity from that provider is attached. 404. | Unlink |
| `social.invalid_state` | State unknown, already spent, expired, or from another flow. | Both |
| `social.provider_not_configured` | No credentials for that provider on this server. 503. | Both |
| `social.unknown_provider` | Not `google` or `github`. 422. | Both |
| `social.unknown_flow` | `flow` was neither `web` nor `desktop`. 422. | Both |
| `social.invalid_link_code` | A parked desktop link record could not be rebuilt. | Link |

### The security model — read this before adding provider #3

**On sign-in, a provider-verified email is the only join key.** The policy, in
order:

1. This `(provider, account_id)` is already linked → sign in.
2. No verified email from the provider → **REFUSE**.
3. Verified email matches an existing account → link, then sign in.
4. Verified email, no existing account → create, link, sign in.

Step 2 before step 3 is the whole defence. Matching an **unverified** address
against an existing account is how an attacker attaches `victim@corp.com` to
their own provider profile and walks into the victim's account. Not
hypothetical — this is nOAuth (Entra's mutable, unverified `email` claim) and
GHSA-6g38-8j4p-j3pr. So the rule every adapter must hold to: **compute
`email_verified` from the provider's authoritative source, and never infer it
from the mere presence of an address.** GitHub's `/user` payload carries an
`email` field that is *not* proof of verification; the flag comes from
`GET /user/emails`, which is why the `user:email` scope is required.

Where a provider gives no verified address the adapter reports `email=None`
rather than guessing, and the service turns that into a refusal, not a link.

Step 1 sitting *before* step 2 is also deliberate. A returning user whose
provider has since stopped vouching for their address — they removed it, or
declined the scope on a re-consent — is still the same person, because that
match was on the provider's immutable id. Verification only gates the step that
BINDS an identity to an account it was not already bound to.

**On linking, email is deliberately NOT a join key at all.** The session
already establishes who the user is, so the identity's address is not needed to
resolve an account and must not be used to. The link path looks up only
`(provider, account_id)`:

- already attached to the caller → no-op, because clicking "Connect" twice is
  not an error and reporting one would put the panel in a failure state over a
  state it already has;
- attached to a **different** account → refuse `auth.identity_claimed`. Never
  re-point it. That would hand over this account *and* silently strip a
  credential from the account that legitimately holds it;
- attached to nobody → attach.

Unverified identities are still refused on the link path, but for a different
reason than on sign-in: it preserves the invariant that **every row in
`oauth_accounts` was established from a provider-verified identity**, which is
exactly what makes step 1 above safe when it signs a returning user in on a
link alone. Break the invariant here and step 1 loses its foundation.

**Unlinking refuses to remove the last credential**, and the check is
`_has_usable_password`, not `bool(hashed_password)`. Accounts created by the
social path and by SSO JIT provisioning store an *unusable sentinel*
(`!social-only-...`, `!sso-only-...`) rather than an empty string, because an
empty hash can compare-equal in some verifiers. A truthiness check therefore
reports "has a password" for precisely the users who have none, and would let
them delete their only way in. The test is positive — pwdlib and passlib hashes
are Modular Crypt Format and begin with `$` — so a sentinel added later needs
no change here. It over-refuses an SSO member who could still reach their IdP;
that is the intended direction, because a false refusal costs one password
reset and a false allow is a permanent lockout support cannot undo.

**Enforced SSO refuses both sign-in and linking.** A workspace paying for SSO
is buying the guarantee that its members authenticate through the IdP, and
consumer Google must not become the documented way around it. Linking is
guarded even though a link is not itself a bypass — sign-in re-checks every
time, so an identity attached under enforced SSO could not be spent — because
it would be a bypass lying in wait if that check ever regressed, and it stores
exactly the credential the org enabled the control to exclude. The check also
runs at `begin_link`, so Settings shows an explainable error instead of a round
trip through Google that ends in a redirect.

Provider access tokens are never stored. Sign-in needs identity, not ongoing
API access, and a token we never use is avoidable breach surface. Repository
access is codeconnect's job.

### Configuration

| Variable | Purpose |
|---|---|
| `POCKETPAW_GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google sign-in. Unset hides the button. |
| `POCKETPAW_GITHUB_OAUTH_CLIENT_ID` / `_SECRET` | GitHub sign-in. Unset hides the button. |
| `POCKETPAW_PUBLIC_BASE_URL` | Backend origin; the callback URL is derived from it. Default `http://localhost:8888`. |
| `POCKETPAW_SOCIAL_REDIRECT_URI` | Overrides the derived callback outright. Set it when the backend sits behind a proxy whose public origin it cannot infer. |
| `POCKETPAW_FRONTEND_BASE_URL` | Where the SPA lives. Default `http://localhost:1420`. |

Callback URL, registered with both providers:

```
<backend-origin>/api/v1/auth/social/callback
```

**GitHub needs an OAuth App, not a GitHub App.** They are different products
with different consent screens and different token models, and picking the
wrong one costs an hour before anything works. Create it under Settings →
Developer settings → **OAuth Apps**. These credentials are also distinct from
two other Google/GitHub credentials already in this codebase, and reusing
either will not work:

- `POCKETPAW_GITHUB_APP_*` — codeconnect's **GitHub App**, for repository
  access via installation tokens. Sign-in uses its own OAuth App so the
  account-creation consent screen asks for identity only, never repository
  permissions.
- `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` (no `POCKETPAW_` prefix, OSS core) — the
  Drive connector's per-install data integration.

Scopes are requested at runtime and are identity-only: Google gets
`openid email profile`, GitHub gets `read:user user:email`. `user:email` is not
optional — without it `GET /user/emails` returns 403, no address can be treated
as verified, and every GitHub sign-in refuses with `auth.unverified_link`.

`POCKETPAW_FRONTEND_BASE_URL` matters more than it looks. Every redirect out of
these routes is **absolute** against that origin, because a relative redirect
resolves against the API origin — the same host only when both are served from
one domain. In production they usually are; in local dev they are not, and a
successfully signed-in user lands on the API root and sees nothing.
## Paw Bar — the site concierge and its owner inbox

Every published Paw Site can carry a concierge: a per-site agent that answers
visitors on the page. These are its endpoints. They split cleanly in two, and
the split is the security model:

- **Public** routes are called by the widget on the customer's site. The caller
  is an anonymous visitor holding a world-visible embed key, so every one of
  them runs the same fail-closed chain — unknown widget 404, rate limit 429,
  bad/revoked key 401, disallowed origin or a key that doesn't own the widget
  403 — and none of them expose owner-private data. Every public data route
  (everything below except `widget.js`, `actions.js` and the frame document) also sits behind
  a per-(client IP, widget) limit (429; 10/s sustained, 300 burst, the IP taken
  from the rightmost `X-Forwarded-For` hop, held in process memory so each
  replica counts separately). A `customer_ref` must be 8-128 characters of
  `[A-Za-z0-9_-]` or the route answers 400 `invalid_customer_ref`.
- **Admin** routes are called by the site's owner from the dashboard. They are
  workspace-scoped and gated on `paw_bar.read` (reads) or `paw_bar.manage`
  (writes).

### Public — the visitor surface

| Route | What it does |
|---|---|
| `GET /paw-bar/widget.js` | The embed loader a published page includes. `public, max-age=300` with a strong `ETag`; a matching `If-None-Match` gets a 304. |
| `GET /paw-bar/actions.js` | The opt-in page-actions script an owner adds beside the loader so the concierge can scroll to or highlight something on the page. It acts only on `pawbar:act` messages from a `/paw-bar/frame` iframe on its own endpoint's origin. Same caching as `widget.js`; `PAW_BAR_ACTIONS_JS` overrides the vendored copy. |
| `GET /paw-bar/frame` | The concierge iframe document. Gated by a CSP `frame-ancestors` header built from the Site's `allowed_origins`; a disabled concierge returns a blank self-removing shell rather than an error page, because this body renders inside a visible iframe. Every frame document, the shell included, also sends CSP `style-src 'self' 'unsafe-inline' https://fonts.googleapis.com` and `font-src 'self' https://fonts.gstatic.com` (so the bar can load the Google Fonts sheet the host page links) and `sandbox allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads`, so the browser sandboxes it whoever embeds it; no flag permits top navigation. A rendered frame is `private, max-age=60` (never `public`) and the key lookup behind it is memoised for 30 s, so a revoked key, a disabled concierge or an appearance edit reaches an open frame within about 90 s; the dead shell is `no-store`. Its `pawbar.js`/`pawbar.css` URLs carry `?v=<content hash>` and are served `immutable` for that exact version, `max-age=300` otherwise. |
| `GET /paw-bar/spec/{widget_id}` | The widget's render spec. Legacy: only the frozen key-less widget fetches it. `public, max-age=60`, always with `Vary: Origin`. Its `catalog` is filled from the [catalog store](#catalog-store) (the first 200 products in the owner's order), so that client keeps working. |
| `POST /paw-bar/events/{widget_id}` | Ingest a widget event: `{type, payload, customer_ref, signed_key?}`. A widget with a concierge agent requires `signed_key` (401 `signed_key_required` without it); an unbound legacy widget still accepts a key-less event from an allowed origin. Events count against their own per-minute budget, never the one chat uses. |
| `GET /paw-bar/events/{widget_id}/decision/{customer_ref}` | Poll the outcome of a gated action the visitor requested. A widget with a concierge agent requires `?signed_key=`. |
| `POST /paw-bar/chat` | Stream a concierge reply (SSE). When the owner has taken the conversation over this emits a single `human_replying` frame and dispatches no agent run at all. Takes an optional `conversation_id`; omit it and the turn lands on the visitor's conversation in progress, which is what widget bundles built before that field send. Takes an optional `page: {"url", "title"}`, the host page the widget sits on; omit it and the turn is answered as before. A v2 site uses it only when the url is http(s) on the site's allowed origins (query and fragment dropped): an indexed page adds its title, summary and article, any other page only its title, cut to 120 characters and marked unverified. A `page` that isn't an object with a string `url` is ignored, never a 422; legacy sites ignore the field. `page` may also carry `tools: [{"name", "description", "input_schema"}]`, the tools the host page declared to paw-bar. A v2 site with page actions on (`concierge_page_actions`) reads at most 12 and keeps each one that passes on its own: `name` matches `^[a-z][a-z0-9_]{0,39}$`, `description` is 1 to 200 characters, and `input_schema` is a flat `{"type": "object", "properties", "required"}` schema of at most 2,048 characters of JSON whose properties are `string`, `number`, `integer` or `boolean` with optional `description`, `enum`, `minimum`/`maximum` (numbers) and `maxLength` (strings). Anything malformed is no tools, never a 422 and never a failed turn; with page actions off the field is ignored. With page actions on, a v2 reply may suggest one page action, streamed before `stream_end` as `{"type": "action", "action": {...}}`: `{"do": "navigate", "to": <absolute url>, "label"}` to a crawled or catalog page on the site's origin, `{"do": "scroll_to" | "highlight", "target", "label"}`, or `{"do": "tool", "name", "args", "label"}` for one of this turn's declared tools, with `args` matching its schema exactly (required keys present, no other keys, strings at most 200 characters, enum and bounds respected). An action that fails these checks is dropped; the reply text never contains it. On v2 the one `sources` frame is `{"items": [{"id", "title", "url"}], "sources": <same list>}`: exactly the knowledge the model was given, in order, with a title and url only for pages the site sync indexed. `message` is capped at 8000 characters (400 `message_too_long`). An `error` frame always carries `code: "agent.error"` and a generic message; the engine's own code is not relayed. On v2, a turn the concierge cannot answer ends with one `unavailable` frame, `{"type": "unavailable", "reason": "temporary" | "limit"}`, then `stream_end`: `temporary` when the model provider failed (a timeout, 429, 5xx or connection error before any text is retried once first; text that already streamed stays and the frame follows it), `limit` when the site is at its daily spend cap or a new conversation finds the monthly allowance used up. It carries no text and raises no handoff; the widget renders the state, and only the visitor's own request-human raises one. The owner gets one `paw_bar_spend_cap` notification per site per UTC day when the cap is hit. On a v2 site whose `concierge_ui_profile` is `"ripple"` and which is on the ops list (so the ripple profile applies), a ```` ```pawbar-card ```` fence never rides a `chunk`; it streams as its own frames, interleaved with the text `chunk`s in stream order: `card.start` `{"card_id"}` when the fence opens (`card_id` is `c1`, `c2`... within the reply), then `card.delta` `{"card_id", "text"}` with the raw fence body (the card JSON `{"ui", "state"?}`) as it arrives, in order and untransformed, then exactly one of `card.final` `{"card_id", "card"}` (the validated, hydrated `{ui, state?}` object, checked against the ripple profile at the close) or `card.rejected` `{"card_id", "reason"}`, where `reason` is `"invalid"` (the card failed validation, is not a `{ui, state?}` JSON object, or its catalog lookup failed) or `"truncated"` (the reply ended, or the turn failed, with the fence still open; a failed turn sends it before `unavailable`). A closed body that is not JSON only because it lacks closing brackets (at most 4; a `}` missing before the root's `,"state":` goes there, the rest at the end) is repaired at the close, never mid-stream, and then checked like any card. At the close a node's declared props written flat beside its `type` (any depth, flow steps too) are moved under its `props`, never a node, handler or `bind` key, never a key the manifest does not declare for that widget, and never over a key `props` already holds; the moved card is then checked in full and is the one `card.final` carries. Nothing else in a body is ever repaired, and its `card.delta`s stay the raw text. A card can be rejected before its fence closes: each piece of body is checked before it goes out, and once the body passes the ripple size cap (64,000 characters) or a complete string in its `ui` or `state` breaks the ripple string rules (a script or data link, a URL off the allowed hosts, an expression where a URL is read, CSS that loads something), or a key repeats in one object (the close refuses that body whole), or the body does not start with `{` once what JavaScript's `trim()` strips is skipped, `card.rejected` `"invalid"` is sent at once, no further `card.delta` follows for that card, and the rest of its fence is dropped unbuffered. A string still being written is never judged, so deltas are a preview the client should not act on before `card.final`. The run transcript keeps a final card as the same validated fence the `"pawbar"` profile streams, and keeps nothing of a rejected one. `"pawbar"` sites get none of these frames. When such a site also has a `concierge_store_url`, each turn reads that store (`/api/store`, `/api/menu`, `/api/booking/services`, then `/api/booking/slots` for the next 7 days in the store's timezone) through the SSRF-safe pinned fetch (JSON only, public addresses only), in one 2-second budget, cached 60 seconds per site, and the prompt gets a `<store-menu>` block (each product's id, name, price, kind and tags; no photos or options). `card.final` then carries the card filled from the store, while `card.delta` stays the model's raw text: a `menu-order` item with a `product_id` takes the store's name, description, price (a number), image, category, tags, kind and option `groups` (`{id, name, choose, required?, max?, options: [{id, name, price_delta}]}`), replacing whatever the model wrote there; an unknown or repeated `product_id` drops that item, not the card; the card gets `currency`, `fulfilment` and `fee` from the store, and `checkout: true` with `on_checkout: {"action": "emit", "target": "checkout"}` only when every item was filled. With the menu unreachable it is display only (no `checkout`, the model's prices, images and options dropped). A `booking` gets the store's `services` (with `party: {min, max}`), `days` (`{date, date_label, slots: [{start, label, available}]}`), `tz` and `on_book: {"action": "emit", "target": "book"}`; when no slots can be read it gets no days and a `notice` saying times are unavailable. A `comparison-layout` item with a `product_id` takes the store's name, price and image. A filled image is kept only when it is `https://` on the store's own host or `images.unsplash.com`. A model-written handler on a `menu-order` or `booking` refuses the card, `book` is never an event the model may emit (only the server-attached `on_book` carries it), and a model-written `checkout`, `confirmed` or `notice` on those widgets is dropped. |
| `GET /paw-bar/conversations` | The visitor's own conversations on this bar, newest first, with a preview and which one is in progress. Scoped to the `customer_ref` the embed key already bound, so there is nothing to enumerate. |
| `GET /paw-bar/conversations/{conversation_id}/messages` | One of the visitor's own conversations, oldest first. Each message is `{role, content, created_at}` only: the owner's view names which operator typed a line, the visitor's never does. |
| `POST /paw-bar/conversations` | Start a fresh conversation. The current one is retired rather than deleted — it stays in the visitor's list and in the owner's inbox — and the next turn starts the agent cold instead of replaying the thread the visitor walked away from. |
| `POST /paw-bar/action` | Run a verb the widget spec declares, or the built-in `send_to_team` (see [Leads from the concierge](#leads-from-the-concierge)). `auto` verbs touch only the visitor's own cart or a checkout link; `gated` verbs execute nothing and raise an Instinct proposal for a human. A cart holds one currency: `add_to_cart` for a product priced in another currency than a non-empty cart is 409 `cart_currency_mismatch` and the cart is left as it was. |
| `GET /paw-bar/cart` | The visitor's own cart: `{items, total_cents, currency, checkout_url}`, amounts in minor units of `currency` (see Money below). |
| `POST /paw-bar/decision-contact` | Leave an email so a decision reaches the visitor after they close the page. The address is stored on the decision row only — never in agent context, the KB, or transcripts. |
| `GET /paw-bar/messages/{widget_id}/{customer_ref}` | Poll for owner and system messages once a human has joined. Returns `role`, `content`, `at` and `bot_paused` — never notes, tags, assignee or contact address. Pass `conversation_id` to scope the read (and `bot_paused`) to the thread on screen; omitting it answers for the visitor's whole history, which is what a cached widget bundle does. |
| `GET /paw-bar/articles` | The site's own synced pages, for a self-serve reading list. |

### Admin — the owner surface

| Route | What it does |
|---|---|
| `POST /paw-bar/admin/site/{site_id}/concierge` | Create the site's concierge. This is the only way one comes to exist: widget create, the settings PATCH, publishing and a connected-site attach never create one. Behind `paw_bar.manage`; 404 for a site outside your workspace, 409 `concierge_exists` when it already has one. It starts **off** (`concierge_enabled: false`), mints the site's widget if there is none (empty spec, no default actions; an existing widget is kept as it is), sets `concierge_runtime` from the v2 eval gate (`v2` once a passing real-model gate report for the deployment's model is committed, `legacy` until then or when the deployment sets `POCKETPAW_PAWBAR_CONCIERGE_DEFAULT_RUNTIME=legacy`) and, for a legacy concierge, binds a dedicated agent; a v2 concierge gets no agent. Optional body `{"concierge_greeting": "..."}`. Returns 201 with the settings response. A published site shows the bar from its next publish after the concierge is switched on. |
| `DELETE /paw-bar/admin/site/{site_id}/concierge` | Delete it: the marker is cleared and the switch turned off, so every public route treats the site as having none. A legacy agent is unbound from the widget, never deleted. `?delete_conversations=true` also purges the concierge's conversations, owner and visitor lines, visitor requests and carts; without it they are kept. 404 when the site has no concierge. Returns the settings response. |
| `GET /paw-bar/admin/site/{site_id}/overview` | Counts and the bound widget, plus `concierge_exists` and `concierge_runtime`. On a v2 concierge, `answer_model` is the `provider:model` visitors are answered with right now, resolved the way a turn resolves it (`""` on legacy, or when it can't be told). The widget's `spec` comes without its catalog (`spec.catalog` is always `[]`); `widget.catalog_count` says how many products the [catalog store](#catalog-store) holds, and the catalog routes below page through them. |
| `GET /paw-bar/admin/site/{site_id}/stats` | The concierge scoreboard for one site over one window (`?window=24h\|7d\|30d\|2w\|all`, default `30d`): conversations, distinct visitors, runs, messages, token volume broken into input / output / cached, and USD cost. Tokens and cost resolve through the same metering the workspace wallet bills with, so the panel and the invoice cannot disagree. `priced_runs` says how many runs carried usable metering — a backend that reports none reads as unpriced rather than as free. The scan is bounded and `truncated` says when it hit the cap. A malformed window is a 422, never a silently widened answer. |
| `GET/PATCH /paw-bar/admin/site/{site_id}/settings` | The kill switch, greeting, transcript-retention toggle, and `concierge_appearance`, the owner's overrides of the site's own look (see [Appearance tokens](#appearance-tokens)). Sent whole rather than per-field; every value validates into a safe CSS literal, since these become the right-hand side of a custom property in a document the widget serves. Both return `concierge_exists` (whether the owner has created one) and `embed_snippet`, the exact tag the published site carries (built on `PAW_CAPTURE_API_BASE`), or `""` when the site has not earned a bar: no concierge created, no widget, no embed key, the concierge off, or a plan without it. Setting `concierge_enabled` writes the switch and nothing else; on a site with no concierge it has no effect for visitors. Also carries `concierge_runtime`, `concierge_allow_doc_code`, `concierge_lead_capture` (default `true`: the v2 concierge may offer the `send_to_team` lead card and the visitor's Send writes a Lead; `false` turns the card off everywhere), `concierge_ui_profile` (`"pawbar"` by default; `"ripple"`, allowed only for a site whose id is on `POCKETPAW_PAWBAR_OPS_SITE_IDS` (`pawbar_ops_site_ids`, comma-separated, empty by default; any other site gets 403 `ops_only_setting`, and a stored value off the list is ignored at turn time), lets a v2 concierge's cards use the Ripple widget catalog less its page chrome, vendored as `ee/pocketpaw_ee/paw_bar/ripple-manifest.json` (from qbtrix/ripple-iui#182, pending a release), up to 400 nodes, 16 levels and 64,000 characters, with paw-bar's actions plus `flow`, `branch`, `validate` and `toast` (every step inside a flow or branch held to the same set) and the same host events; the server walks the whole `ui` and `state`: a node kept in a prop is held to every node rule, any action anywhere must be an allowed one (an `audit-log`'s own `entries[].action` is data only in `ui`; `state` may hold no action at all), a handler slot the widget resolves before it fires (`*Actions`, `actions`, `learn_more`, `onRowClick`, and those inside rows such as comparison `items[]`) holds action objects, never a string, and neither those rows, a slot the engine draws as a node (`content`, `trigger`, `split` `start`/`end`, `master-detail` `detail`, `kanban` `cardTemplate`, `virtual-list` `item`, `tabs` `panels[]`, `settings-list` `items[].control`, a grid column's `formatter`: a literal widget node or text) nor a node's `props` may be an expression, a `follow-up`'s `event` must be a host event the widget declares, a body that repeats a JSON key, holds `NaN`/`Infinity`, or is not JSON once trimmed the way JavaScript's `trim()` trims is refused, a form may not carry its own `action`/`method`, URLs under any key must be same-site paths (`/x`, `#x`) or https on the profile's host allowlist (empty, so no external URLs; `mailto:`/`tel:` only under link keys), a URL-ish key may not hold an expression, markdown props and toast/validate messages take only plain path expressions (`{state.a.b}`), text is NFKC-folded and stripped of invisible format characters before these checks, `style` values may not load anything (`url(`, `image-set(`, `@import`, `//`, schemes, after CSS escapes), no text may hold a `javascript:` link or a markdown or reference link to `data:`/`file:`/`blob:`, and `ripple-frame`, `embed`, `richtext` and `rich-text` are refused, as is page chrome a chat card never needs (`navbar`, `footer`, `hero`, `marketing-hero`, `newsletter`, `logo-cloud`, `testimonial`, `app-shell`, `sidebar`, `breadcrumb`, `sheet`, `parallax`, `reveal`, `command-palette`, `coachmark`, `notification-center`); Ripple's `illustration` (model-written animated SVG, offered ahead of the vendored manifest: `svg` and `title` text, `caption?` text, `max_height?` a number from 80 to 640) takes no handler or `bind`, and its `svg` (literal, no `{...}` expression) refuses the card for a `script`, `style`, `foreignObject`, `iframe`, `object`, `embed`, `a`, `image`, `feImage`, `audio`, `video` or `canvas` element, an element outside the SVG namespace, any `on*` or `style` attribute, a DOCTYPE, ENTITY, `xml-stylesheet`, CDATA holding markup or a named entity other than the five XML ones, an `href`/`xlink:href` anywhere but `use`/`mpath` with `#id`, an animated `attributeName` off the illustration contract's closed list, a `url(` that is not exactly `url(#id)`, `javascript:`, `data:`, `vbscript:` or `expression(` in an attribute (CSS escapes undone, whitespace and control characters stripped) (text nodes are never held to these), markup that does not parse or whose root is not `svg`, more than 24,000 characters, 400 elements, 24 levels, 40 animation elements or 40 `use` elements, a `use` pointing at a subtree that holds a `use`, a `dur` under 0.5s or a `repeatCount` over 1000; harmless unknowns (a `filter` element, a `class` attribute) pass, since the widget drops them, and a streaming card's `svg` is judged only at the close; a card whose `ui` carries a flow field (`chain`, `chain_map`, `flowId` or `onComplete`) is a multistep flow (Ripple's chain, whose steps advance in the browser with no model call): at most 8 steps counting every `chain` and `chain_map` value, a step may hold only `version`, `id`, `flowId`, `intent`, `title`, `description`, `ui`, `chain`, `chain_map`, `onComplete` and `form_fields`, each step's `ui` is a node tree held to every node rule as its own root (16 levels each) under one 400-node budget for the whole card, `emit` to `flow.next`, `flow.back`, `flow.forward` or `flow.submit` passes only in a flow card (`flow.submit` only from the same explicit visitor action as `ask`, since it sends the terminal step's chat message), and `onComplete`, only on a step, must be `{"kind": "chat", "message"}` with at most 500 characters of plain text, no `{...}` expression (`invoke_tool`, `call_binding`, `create_pocket`, `navigate`, `emit` or any other kind refuses the card); any ripple card may `emit` the `ask` host event, whose `value` is exactly `{"text"}` of at most 500 characters of plain text (no `{...}` expression) and which only an explicit visitor action may fire: an `on_click`, `on_submit` or `on_select` handler, or a composite's button list keyed exactly `actions` (comparison `items[].actions`, entity-detail `actions[].actions`), flows and branches under those included; the outermost handler decides, and `on_focus`, `on_input`, `on_change`, `on_complete`, `on_mount` and every other `on_*`, every other `*Actions` key (`finishActions`, `nextActions`...) and `state` are refused, so the text goes out as the visitor's message only when the visitor acts (the `"pawbar"` profile has neither); the prompt always teaches the Ripple card (the catalog with props for the layout, input and data widgets such as `entity-detail`, `timeline` and `kv-table`; Ripple's data widgets `itinerary`, `booking`, `menu-order`, `growth-projection`, `recipe`, `meal-plan`, `interval-workout`, `flashcard-deck`, `comparison-layout` and `exec-dashboard` (rows mode) with a short type per prop, so the model writes their data and the widget draws the card, and for `booking` and `menu-order` only what the model writes, since the server fills the rest; `illustration` for a small animated picture or diagram, its SVG attributes in single quotes and plain `href`, never `xlink:href`; rules sized for a chat column about 720px wide that still reads at 360px, which layout fits which answer, when to guide the visitor with a flow card (steps of option buttons whose picks the runner records and the landing appends, one line each, to the last step's fixed chat message; a flow's top-level `state` never reaches its steps) and how a click sends an `ask`, with an example flow, and one example card), the reply cap is 8,000 tokens, and the concierge gets the demo frame (`FRAME_DEMO`) whatever the doc-code, lead-capture and page-action switches say: it builds a small card for any everyday ask (a calculator, planner, tracker, checklist or summary) with the visitor's numbers or labelled sample data, answers questions about Ripple from its knowledge, and still writes no code outside the card, no long prose and no medical, legal or financial advice; every other site, a stored `"ripple"` off the list included, keeps the business-site frame; anything else is a 422), `concierge_daily_spend_cap` (USD per UTC day, 0 to 100, else 422; `null`, the default, uses the global `pawbar_concierge_daily_spend_cap`; a site's own cap can only lower the global one, so a value above it is a 403 `ops_only_setting` unless the site is on `pawbar_ops_site_ids`, where it replaces it; 0 pauses the concierge; an explicit `null` on PATCH clears it), `concierge_store_url` (`null` by default: the base URL of the store a ripple concierge orders and books against, see `POST /paw-bar/chat`; it must be `https://` on a public host with no credentials, query or fragment, else 422, and is stored without a trailing `/`; only a site on `pawbar_ops_site_ids` whose profile is `"ripple"`, stored or set in the same PATCH, may set it, else 403 `ops_only_setting`; any site may clear it with `null` or `""`; a turn reads it only while the site is an ops site on the ripple profile) and the guided fields below. Visitor options, each optional on PATCH: `concierge_disclosure` (the bar's AI line, one line of at most 140 characters, `""` keeps the bar's own wording; over the cap is a 422), `concierge_privacy_url` (`""` or an `https://` link of at most 500 characters with no whitespace, quotes or angle brackets, else 422), `concierge_consent_required` (default `false`), `concierge_voice` (default `true`) and `concierge_expandable` (default `true`); `concierge_appearance.size` is `sm`, `md` or `lg` (anything else saves as `sm`). Both frames pass these to the bar as `disclosure`, `privacyHref`, `consentRequired`, `voice`, `expandable` and `barSize`. `branding_removable` says whether the site may hide the "Powered by" line, by the same entitlement as the site badge (`PATCH /sites/{id}/branding`); a PATCH that turns `concierge_appearance.show_branding` from `true` to `false` on a site without it is a 402 `branding_not_entitled` and writes nothing. The frame sends `poweredBy` as `show_branding` or not entitled. `actions_snippet` is the copyable `<script src=".../paw-bar/actions.js" defer data-endpoint="...">` tag for page actions, on the same base as `embed_snippet` and `""` whenever that is. |
| `PATCH /paw-bar/admin/site/{site_id}/widget/spec` | Save the site's concierge widget spec (the Actions editor). Body `{"spec": {...}}`, the full spec; returns `{"id", "spec"}`. Session-authed behind `paw_bar.manage`, no `X-Paw-Bar-Token`. The prior spec is archived as a revision, the same as `PATCH /paw-bar/widgets/{id}/spec`. `spec.widget_id` and `spec.pocket_id` are always set to the site's widget; whatever the body sends for them is ignored. 404 for a site outside your workspace or one with no concierge widget, 422 for an invalid spec, 422 `spec_too_large` past the [spec size cap](#spec-size-and-the-deprecated-catalog), 409 `currency_units_client_outdated` for a catalog with a non-2-decimal currency sent without `X-Paw-Money-Units: iso4217` (see Money below; the same rule holds on `PATCH /paw-bar/widgets/{id}/spec`). A non-empty `spec.catalog` is added to the catalog store (upserted by id, nothing deleted; see the deprecation note there); an absent or empty one leaves it alone. |
| `GET /paw-bar/admin/site/{site_id}/catalog` | A page of the [catalog store](#catalog-store), in the owner's order: `?offset` (default 0), `?limit` (1-200, default 50), `?q` (keeps products whose name or description holds every word, prefix-matched). Returns `{"items", "total"}`; `total` counts what matched. Each item has the catalog fields plus `position`, `source`, `origin` and `updated_at` (see [Site sync](#site-sync)). Behind `paw_bar.read`. |
| `PUT /paw-bar/admin/site/{site_id}/catalog/items/{item_id}` | Create or replace one product; returns it. A replaced product keeps its place, a new one goes last. The body is the catalog fields (`id` may be left out; one that differs from the path is 422 `item_id_mismatch`) plus an optional `source`. Behind `paw_bar.manage`. |
| `POST /paw-bar/admin/site/{site_id}/catalog/items:bulk` | Create or replace up to 500 products in one transaction: body `{"items": [...]}`, returns `{"upserted", "total"}`. This is how an import is applied (in chunks of 500). Existing ids keep their place, new ones are appended in the order sent. A repeated id in one call is 422 `duplicate_id`. Behind `paw_bar.manage`. |
| `DELETE /paw-bar/admin/site/{site_id}/catalog/items` | Remove products: body `{"ids": [...]}` (at most 500), returns `{"deleted", "total"}`. Unknown ids are ignored. A deleted id is remembered, so the [site sync](#site-sync) never adds it back. Behind `paw_bar.manage`. |
| `POST /paw-bar/admin/site/{site_id}/catalog/reorder` | Body `{"ids": [...]}`: those products move to the front in that order, the rest keep their order after them. Returns `{"total"}`. Behind `paw_bar.manage`. |
| `POST /paw-bar/admin/site/{site_id}/catalog/import/preview` | Read the products a store publishes on its own site, for the owner to review. Writes nothing: the owner applies the products they pick through `POST …/catalog/items:bulk`. Behind `paw_bar.manage`; 404 for a site outside your workspace. Body `{}`. Always a 200 with `{"status", "reason", "source", "host", "items", "total_found", "warnings"}`; see [Catalog import](#catalog-import). |
| `POST /paw-bar/admin/site/{site_id}/catalog/import/csv` | Read the products in an uploaded CSV (multipart, field `file`, at most 2 MB, else 413 `too_large`). Writes nothing; same response shape as the preview, `source: "csv"`. See [CSV import](#csv-import). Behind `paw_bar.manage`. |
| `GET /paw-bar/admin/site/{site_id}/conversations` | The inbox. One row per CONVERSATION, not per visitor — a visitor who asked four separate questions is four rows, each carrying its own `conversation_id` and its own last sentence. Supports `?state=open\|needs_human\|snoozed\|closed`, carries per-state `counts`, and each row joins its lifecycle state, unread count, tags and whether an action is pending. |
| `GET /paw-bar/admin/site/{site_id}/conversations/{customer_ref}` | One conversation's transcript, interleaving visitor, assistant, owner and system turns by timestamp. Pass `conversation_id` to read ONE thread; without it the visitor's whole history is merged into a single transcript. Both sources narrow together — narrowing only the runs would interleave one thread's questions with every reply a human ever sent that visitor. Narrowing reads each turn's own session-key token rather than rebuilding a key from the conversation id and the widget's current agent, so a conversation that predates conversation identity — or one answered before its widget was bound to a dedicated agent — opens instead of 404-ing. A conversation the visitor really holds returns an empty transcript rather than a 404 when it has nothing in it yet. |
| `PATCH /paw-bar/admin/site/{site_id}/conversations/{customer_ref}` | Move state, snooze, tag, or append a private note. Send `conversation_id` to file the thread you are READING; omit it and the visitor's conversation in progress is filed instead. |
| `POST /paw-bar/admin/site/{site_id}/conversations/{customer_ref}/reply` | Reply as the owner. Persists the turn, mutes the bot, clears unread, and reopens a closed or snoozed conversation. Send `conversation_id` so the reply lands in the thread being answered — without it an answer to an older question surfaces inside whichever conversation the visitor has open now. A `conversation_id` belonging to another visitor or another site is a 404, never a silent fallback. |
| `GET /paw-bar/admin/agent/{agent_id}/conversations` | The same inbox scoped to an agent rather than a site — the union across every site that agent serves. |
| `GET /paw-bar/admin/site/{site_id}/decisions` | Gated actions awaiting a human. |
| `GET /paw-bar/admin/site/{site_id}/handoffs` | Conversations a visitor asked to escalate. |
| `GET/POST /paw-bar/admin/site/{site_id}/knowledge` | What the concierge can answer from, and a resync. The response also carries the last [catalog site sync](#site-sync) a knowledge sync started: `catalog_synced_at` (`""` when none has run), `catalog_status` (`ok`, `partial`, `empty`, the import's failure reason, or `sync_failed`), `catalog_added`, `catalog_updated` and `catalog_sold_out`. The POST answers before its own catalog sync finishes, so these describe the previous one until the next GET. |
| `GET/POST /paw-bar/admin/site/{site_id}/knowledge/faqs`, `PATCH/DELETE …/knowledge/faqs/{faq_id}` | Pinned answers: question/answer pairs a v2 concierge reads ahead of every KB hit, on every turn. GET returns `{site_id, faqs, max_count, max_chars}`; POST takes `{question, answer}` and returns the new FAQ (201); PATCH takes either field; DELETE is a 204. Both texts are stripped and must not be blank. Caps come from config (`POCKETPAW_PAWBAR_CONCIERGE_FAQ_MAX_COUNT`, default 15, and `…_FAQ_MAX_CHARS`, default 500 for question and answer together): 409 `faq_limit_reached`, 422 `faq_too_long`. GET gates on `paw_bar.read`, the writes on `paw_bar.manage`; a site outside your workspace, or an unknown `faq_id`, is a 404. The text is treated as data, never as instructions to the model. |
| `GET/POST /paw-bar/admin/site/{site_id}/knowledge/sources`, `POST …/knowledge/sources/{source_id}/refetch`, `DELETE …/knowledge/sources/{source_id}` | Uploaded files and single links the concierge answers from, read into the site pocket KB. GET returns `{site_id, sources, plan, max_count, max_bytes, max_chars, accepted_types}`. POST is a form (multipart, or urlencoded for a link alone; a JSON body is a 422) with exactly one of `file` (`.pdf`, `.docx`, `.md`, `.txt`) or `url`, otherwise 422 `one_source_required` (a `url` over 2,048 characters is 422 `url_too_long`), and returns the new row with status `processing` (202); poll GET until it changes. A row is `{id, kind: "file"|"link", name, url, mime, size_bytes, status, reason, chars, truncated, article_ids, sections_total, sections_failed, sections_truncated, created_at, updated_at, indexed_at}`; a long document is compiled section by section, so `article_ids` lists one article per section that landed, `sections_failed` counts the sections that did not compile (the row is `ready` once one did) and `sections_truncated` the sections past `max_chars` that were never read; `status` is `processing`, `ready`, `failed` (`reason`: `unreadable`, `unreachable`, `no_content`, `ingest_failed`, `kb_unavailable`, `interrupted`), `too_large`, `unsupported` or `blocked`. Refusals write nothing and carry the code as `detail`: 409 `over_limit`, 413 `too_large`, 415 `unsupported` (the type is sniffed from the bytes and must match the extension; the client `Content-Type` is ignored), 422 `blocked` (not a public http(s) address). Refetch re-reads a link (409 `not_a_link` for a file, 409 `already_processing`), DELETE un-indexes and is a 204. Links are fetched through the SSRF-safe fetcher, which re-checks every redirect hop. The file bytes are not stored. Caps come from config: `POCKETPAW_PAWBAR_CONCIERGE_SOURCE_MAX_COUNT_FREE`/`_SITE`/`_STAFF` (3/20/50, by the site plan), `…_SOURCE_MAX_BYTES` (10 MiB), `…_SOURCE_MAX_CHARS` (100,000). GET gates on `paw_bar.read`, the writes on `paw_bar.manage`; a site outside your workspace, or an unknown `source_id`, is a 404. |
| `GET /paw-bar/admin/site/{site_id}/preview-frame` | An owner-authed preview of the live bar. Framed by the dashboard origin only, and carries the same CSP as the public frame. Behind the bar it frames the site's own page as a sandboxed scene (`allow-scripts`, no `allow-same-origin`) with `?pawbar=sniff`. For a hosted site that is its published `url`. For a connected site (no `url`) it is `https://<host>/` for the verified, fresh origin, falling back to the first `allowed_origins` entry when none is verified, and no scene when there are no origins. A customer site that refuses framing (X-Frame-Options / `frame-ancestors`) leaves the scene blank and the editor reports the theme as not detected. With the scene loaded, that page's own bar stays down and its loader posts the detected site theme to the preview, so the preview follows the site like the public bar. A page without the loader shows the bar defaults. |
| `POST /paw-bar/admin/site/{site_id}/preview-config` | Render the owner's UNSAVED settings to the frame config they would boot. Writes nothing. Body, every field optional (one not sent, or `null`, is the stored value): `concierge_appearance`, `concierge_disclosure`, `concierge_privacy_url`, `concierge_consent_required`, `concierge_voice`, `concierge_expandable`. A text the settings PATCH would refuse is a 422 here too. Returns `{"config": {...}, "concierge_appearance": {...}}`: `config` holds exactly `tokens`, `tokensDark`, `scheme`, `launcher`, `side`, `barSize`, `logo`, `launcherLabel`, `disclosure`, `privacyHref`, `consentRequired`, `voice`, `poweredBy`, `expandable`, built by the same code as the frame (so with an empty draft it equals the preview frame's boot config); `concierge_appearance` is the appearance as validated (clamped, unsafe values dropped). `poweredBy` applies the real entitlement. The editor posts `config` to the preview frame as `{type: "pawbar:preview-config", config}`. Behind `paw_bar.manage`; a site outside the workspace is 404. `POST …/appearance/preview-tokens` (`{tokens, tokens_dark, concierge_appearance}` for a draft appearance) stays for older editors. |

#### Appearance tokens

The bar follows the website it is embedded in. Its loader reads the host page's accent, page background and text colour, font (and Google Fonts sheet) and button radius, and the bar layers, lowest to highest: bar defaults, the detected site theme, the owner's `tokens`, then `tokensDark` while the bar is dark. So every token the frame emits overrides the site, and a facet the owner has not set emits nothing.

| `concierge_appearance` field | Follow the site (default) | Override | Token(s) |
|---|---|---|---|
| `accent` / `accent_dark` | `""` (`accent_dark` `""` = same as `accent`) | `#rgb` / `#rrggbb` | `--pawbar-accent` |
| `font` | `"site"` | `system`, `geometric`, `humanist`, `serif`, `mono` (fixed stacks) | `--pawbar-font` |
| `radius` | `null` | 0–32 (clamped) | `--pawbar-radius` |
| `colors.surface` | `""` | hex | `--pawbar-bg` (alpha 0.78) and `--pawbar-frame-bg` (0.82 in `tokens`, 0.55 in `tokensDark`), the bar's own glass alpha; without `colors.ink` it also sets a legible `--pawbar-fg` / `--pawbar-frame-fg` |
| `colors.ink` | `""` | hex | `--pawbar-fg` and `--pawbar-frame-fg` |
| `colors.user_bubble` | `""` | hex | `--pawbar-bubble-bg`, plus a legible `--pawbar-bubble-fg` |
| `colors.owner_bubble` | `""` | hex | `--pawbar-owner-bubble-bg` + a legible `--pawbar-owner-bubble-fg` |
| `colors.assistant_bubble`, `accent_fg`, `ring`, `danger` | `""` | hex | `--pawbar-assistant-bubble`, `--pawbar-accent-fg`, `--pawbar-ring`, `--pawbar-danger` |
| `blur` | always emitted (default 28) | 0–48 | `--pawbar-blur` |

`colors_dark` takes the same fields for `tokensDark`; a field left `""` falls back to `colors`. `surface_mode` (`auto`, `light`, `dark`) is the frame's `scheme`. `hero`, `motion`, `bar_resting`, `colors.surface_opacity`, `colors.unread`, `colors.line_strength` and `colors.wash_strength` are still accepted and stored but render nothing: the bar has no surface for them. Sites saved before follow-the-site stored the old defaults (`#3b6fe0`, `system`, `20`) as values; `pocketpaw_ee.sites.migrate_appearance_follow_site` moves exactly those to follow the site once per row at boot (`Site.concierge_appearance_version` marks a row as done) and keeps anything else the owner picked.

#### Money

Every amount field (`price_cents`, `total_cents`, `value_cents`, `amount_cents`) is an
integer in ISO 4217 minor units of the currency next to it; the `_cents` names are
historical. Most currencies have 2 decimals; the exceptions are 0 (BIF, CLP, DJF, GNF,
ISK, JPY, KMF, KRW, PYG, RWF, UGX, UYI, VND, VUV, XAF, XOF, XPF), 3 (BHD, IQD, JOD, KWD,
LYD, OMR, TND) and 4 (CLF, UYW). The table lives in `pocketpaw.money` and, identically,
in `tests/fixtures/currency_exponents.json`, which the clients' copies are tested
against. Currency codes are upper-cased; the agent ledger's per-currency totals group
`usd` and `USD` together. Amounts stored before this rule (major × 100 for every
currency) are converted once by the `money_minor_units_v1` migration in each SQLite
store; client invoices in Mongo by `scripts/migrations/2026_10_01_invoice_minor_units.py`.

A client that writes amounts in minor units sends the request header
`X-Paw-Money-Units: iso4217`. Without it the server assumes a client built before this
rule, which still sends major × 100:

- `POST /sites/{site_id}/invoices` converts such an amount (÷100 for a 0-decimal
  currency, ×10 for a 3-decimal one, unchanged for 2 decimals) before storing it.
  Every invoice it records is stamped `amount_unit: "iso4217"`; `""` marks a legacy
  row the migration script has not converted yet. The script converts only unstamped
  rows and stamps each in the same write, so it is safe to run at any time, more than
  once, before or after any client release. `amount_unit` is returned on each invoice.
- `PATCH /paw-bar/widgets/{id}/spec` and `PATCH /paw-bar/admin/site/{site_id}/widget/spec`
  refuse a catalog holding any item whose currency does not have 2 decimals with 409
  `currency_units_client_outdated`. They do not convert, because an old client also
  re-sends prices it read in minor units. Catalogs whose currencies all have 2 decimals
  are the same in both conventions and save as before.

#### Catalog store

A concierge's products live in their own table beside the widget (`paw_bar.db`,
`paw_bar_catalog_items`), not in the widget spec. Product cards, the cart, the agent
ledger's cart value and the concierge's product list all read it, never what the model
writes. It holds at most `POCKETPAW_PAWBAR_CATALOG_MAX_ITEMS` products (default 5,000);
a write past that is 409 with `detail` `{"code": "catalog_full", "limit": <n>}` and
writes nothing. A site with no concierge widget is 404 `no_concierge_widget` on every
catalog route. Deleting the widget deletes its products.

Only a blank `id` or a negative `price_cents` is a 422; the other fields are cleaned
rather than rejected:

| Field | Type | Rules |
|---|---|---|
| `id` | string | Required, unique within the catalog. Imported items use `shopify:<id>`, `woo:<id>` or `web:<hash of the page path>`; ids the editor mints start `item-`. |
| `name` | string | Trimmed and cut to 200 characters. |
| `price_cents` | int | Non-negative, in ISO 4217 minor units of `currency`: `350` is $3.50, `1500` is ¥1,500, `1250` is 1.250 KWD. The name is historical. A value over 10^12 is read as `0` (and logged), so one absurd stored price cannot make the spec unloadable. |
| `currency` | string | Trimmed and upper-cased. Anything that isn't then 3 letters (including `""`) is stored as `USD`. |
| `image_url` | string | An `http(s)://` URL of at most 2048 characters; anything else is stored as `""`. |
| `url` | string | The product's page: an `http(s)://` URL or a site path starting with a single `/`, at most 2048 characters; anything else is stored as `""`. A product card links to it. |
| `description` | string | Trimmed and cut to 300 characters. Shown on the product card. |
| `in_stock` | bool or `null` | `null` when unknown. `false` lists the product last and marks it sold out in the list the concierge reads, so it stops recommending it. |

Read back, an item also carries `position` (the owner's order), `source` (`manual`,
`shopify`, `woocommerce`, `jsonld`, `opengraph`, `csv` or `site`; when a write leaves it
out it is read off the id prefix, else `manual`), `origin` (`site` or `owner`, below) and
`updated_at`.

#### Site sync

Every knowledge sync that reached the site (hosted: the pocket was read; connected: the
crawl reached the verified origin) starts a background catalog sync for the site's
concierge widget. It runs the same reader as the [import preview](#catalog-import) and
writes the result itself, so the concierge can show the site's products as cards
without the owner pressing Import. It never changes the knowledge sync's result, and a
site with no concierge widget is not read. The rules, by `origin`:

- A new product id is added with `origin: "site"`, after the existing products, until
  the catalog cap; the rest are left out (logged) in the reader's order.
- A `site` product is updated from the site (name, price, currency, image, url,
  description, stock).
- Any write through the catalog routes (PUT, bulk, a spec catalog) stores the product as
  `origin: "owner"`; the sync never changes an `owner` product. Rows from before the
  field existed are `owner`.
- A deleted product is remembered per widget and never added back.
- A `site` product the import no longer lists is set to `in_stock: false`, never
  deleted, and only after a complete import: status `ok`, `total_found` no larger than
  the items returned, no `skipped_by_robots` warning, and no product skipped for having
  no currency. Products with no currency are skipped (their price units are unknown).

The outcome is shown on `GET /paw-bar/admin/site/{site_id}/knowledge` (`catalog_*`).

**What a concierge turn sees.** A catalog of at most 50 products is listed whole, in
the owner's order. A bigger one is searched per turn: the product whose `url` is the
visitor's page (if any), then the 20 best matches for the visitor's message, their
last two messages and the page title (SQLite FTS5 over name and description, English
stemming, the name weighted double; a `LIKE` scan where the SQLite build has no FTS5),
then, when that search finds fewer than 3, the first 10 products in the owner's order.
Duplicates are dropped. Both runtimes use the same rule. A product card may still name
any product in the store, not only the ones listed: the server looks each id up when
it fills the card in.

#### Spec size and the deprecated catalog

A widget spec is at most 64 KB, measured as JSON without its `catalog`. Every spec write
checks it (`PATCH /paw-bar/widgets/{id}/spec`, `PATCH …/admin/site/{id}/widget/spec`,
`POST /paw-bar/widgets/{id}/spec/rollback`) and answers 422 `spec_too_large`; specs
already stored are never refused for their size.

`spec.catalog` is deprecated and kept for one release so an older editor still works.
A spec write whose `catalog` is non-empty adds those products to the catalog store
(still at most 200 per write, the old limit): each is created or updated by `id`, in
place, and nothing the body leaves out is deleted, so an older editor that loaded an
empty list and saved one product adds that one product. The spec is stored without the
catalog; an empty or absent `catalog` leaves the store alone. 409 `catalog_full` when
the new ids would pass the cap, and the 409 `currency_units_client_outdated` check (see
Money above) runs before any of it. Deleting or reordering products takes the catalog
routes. A rollback ignores the `catalog` in an archived revision: the catalog is not
versioned with the spec. The `catalog_to_table_v1` migration moves every
stored spec's catalog into the store once, after `money_minor_units_v1`, one transaction
per widget: the spec as it was is archived as a revision first, its products are added
after any the store already holds for that widget (a product already there is kept as
it is, nothing is deleted), and it logs the largest spec left. A spec save on a widget
the migration has not reached yet moves that widget's catalog the same way before
writing, so a save never drops it. `POST /paw-bar/widgets` applies the same size cap
(422 `spec_too_large`) and item cap (409 `catalog_full`).

#### Catalog import

`POST /paw-bar/admin/site/{site_id}/catalog/import/preview` fetches from one host. A
connected (foreign-origin) site is read on the first of its origins this workspace has
verified within the last 30 days, the same rule as the knowledge crawl. A hosted Paw
Site is read on its first live custom domain, else the host it was deployed to, with no
ownership check, and with the sitemap / JSON-LD / OpenGraph reader only (no Shopify or
WooCommerce endpoints); one that has not been deployed (no URL, or a local one) is
`failed` / `site_not_deployed`. The site builders don't publish `Product` JSON-LD yet,
so most hosted sites come back `empty`; CSV covers them. Every fetch sends the concierge
crawler's user agent (`PawSitesConcierge/1.0`), checks robots.txt for every URL, follows
redirects only on that host, and stops at 30 product pages (sitemap reader) or 40 MB.
Shopify's `/products.json` is paged 250 at a time up to 20 pages and the WooCommerce
Store API 100 at a time up to 50 pages, each stopping at a short page or the catalog
cap.

The whole run has one 60-second deadline: no fetch starts unless it can finish before
it, so platform paging and the page walk stop there and return what they have read as
`partial` with `deadline_reached`; `timeout` is reserved for a run that read no
products by then. Sitemaps are read only as UTF-8 (a UTF-8 BOM is fine); one that is
not UTF-8, declares another encoding, or carries a DOCTYPE or entity declaration is
ignored.

A price is read from a number or a string. A single comma followed by one or two digits
and no dot is a decimal comma (`"19,99"` is 19.99); any other comma is a thousands
separator (`"1,500"`, `"1,299.00"`). A product whose price does not fit (over
10^12 minor units, or too large to compute) is skipped and counted in
`skipped_bad_price:<n>`; the rest of the import stands.

It reads Shopify's `/products.json` or the WooCommerce Store API when the homepage looks
like one of them, and otherwise (or when that endpoint is refused, missing or
robots-disallowed) the store's sitemap and product pages: schema.org `Product` JSON-LD
first, then `og:type=product` tags. One item per product: a Shopify product's price is
its cheapest available variant. Prices are converted to ISO 4217 minor units of the
product's currency (WooCommerce's own `currency_minor_unit` is undone first); a product
whose currency is unknown is converted as two decimals. Products are sorted in stock first, then in the store's
order, and capped at the catalog cap (5,000 by default); `total_found` is the count
before the cap.

| `status` | Meaning |
|---|---|
| `ok` | Products found and every page read. |
| `partial` | Products found, but some pages could not be read, or the byte budget or the 60-second deadline ran out. |
| `empty` | Nothing with a name and a price was found. |
| `failed` | Nothing was read; `reason` says why. |

`reason` is `""` or one of `site_not_deployed` (a hosted site with no public host;
nothing is fetched), `origin_missing`, `origin_unverified`, `origin_verification_stale`,
`blocked_by_robots` (robots.txt disallows the homepage for our crawler), `timeout` or
`fetch_failed`. `source` is `shopify`, `woocommerce`, `jsonld`, `opengraph`, `csv` or
`""`. `warnings` can hold `currency_unknown`, `skipped_no_price:<n>`,
`skipped_bad_price:<n>`, `skipped_by_robots:<n>`, `pages_failed:<n>`,
`byte_budget_reached`, `deadline_reached` and `robots_unreadable`.

Each item has the catalog fields above. `image_url` is always `https://` or empty, and
`url` is a site path on the verified host or empty. Re-importing returns the same ids
for the same products, so the client can update the items it imported before. A `web:` id hashes the
product's page path together with its name, so products without their own URL on one
listing page stay distinct. robots.txt is fetched on the verified host only: if it
redirects elsewhere it counts as unreadable (`robots_unreadable`, everything allowed),
the knowledge crawl's policy for a robots file it can't read.

#### CSV import

`POST /paw-bar/admin/site/{site_id}/catalog/import/csv` reads an uploaded CSV (any site,
hosted or connected) into the same preview. Headers are matched case-insensitively, `_`
reads as a space, and the delimiter (`,`, `;` or tab) is detected. UTF-8 (with or
without a BOM) is expected; anything else is read as Windows-1252.

| Field | Header aliases (first non-empty one wins) |
|---|---|
| `id` | `id`, `handle`, `sku`, `variant sku`, `product id` |
| `name` | `name`, `title`, `product`, `product name` |
| `price` | `price`, `sale price`, `variant price`, `regular price` |
| `currency` | `currency`, `currency code` |
| `image_url` | `image`, `image url`, `photo`, `image src`, `images` (first URL), `variant image` |
| `url` | `url`, `link`, `page`, `product url`, `permalink` |
| `description` | `short description`, `description`, `body (html)`, `body html` |
| `in_stock` | `in stock`, `in stock?`, `available`, `stock`, `variant inventory qty` (yes/no, true/false, 1/0, in stock / out of stock, or a quantity) |

A price is a decimal in the row's currency, converted to its minor units. Currency
symbols and spaces are ignored, then the same comma rule as the site import applies. With no `currency` column the items
come back with `currency: ""` and a `currency_unknown` warning, for the dashboard to ask.
`id` defaults to `csv:<slug of the name>`; a given id becomes `csv:<id>`.

Platform exports upload as they are. Shopify's product export (`Handle`, `Title`,
`Body (HTML)`, `Variant Price`, `Variant Inventory Qty`, `Image Src`, …) has extra rows per
product carrying only the handle: they fold into the product (its lowest price, in stock
when any variant is), and `url` defaults to `/products/<handle>`. WooCommerce's
(`ID`, `Type`, `SKU`, `Name`, `Short description`, `In stock?`, `Sale price`,
`Regular price`, `Images`, …) gets ids `woo:<ID>`, the ids the site import mints, so the
two merge; `variation` rows are skipped (`skipped_variations:<n>`).

Every product goes through the same cleaning as the site import. A row that can't be a
product is a warning with its line number, `line:<n>:no_name`, `line:<n>:no_price` or
`line:<n>:duplicate_id` (the first 50, then `more_row_warnings:<n>`), and the other rows
still import (`status: "partial"`). Past the catalog cap reading stops with
`row_cap_reached`. A file that can't be used is `failed` with `reason` `csv_empty`,
`csv_unreadable`, `csv_no_name_column` or `csv_no_price_column`.

#### Guided concierge fields (v2)

The settings GET and PATCH carry six fields that shape a v2 concierge. Unset reads as
`""`, `null` or `[]`, and an unset field adds nothing to the prompt. A PATCH writes only
the fields it sends; `null` means "not sent", so clear a text field with `""` and the
topics with `[]`. A value past its cap, outside its enum, or not a language code is a
422 and nothing is written.

| Field | Type | Rules |
|---|---|---|
| `concierge_name` | string | At most 40 characters after whitespace is folded to single spaces. |
| `concierge_tone` | `"friendly" \| "professional" \| "concise" \| "playful"` or `null` | Picks one fixed sentence. Can't be reset to `null`. |
| `concierge_languages` | string[] | 1 to 10 BCP-47 codes, case-normalized (`en-us` becomes `en-US`) and de-duplicated. The first is the fallback language. `[]` is a 422. |
| `concierge_about` | string | At most 600 characters. Paragraph breaks are kept; other whitespace folds. |
| `concierge_avoid_topics` | string[] | At most 10 topics of 80 characters each. Blank entries and case-insensitive duplicates are dropped. |
| `concierge_escalation` | `{"mode": "handoff" \| "email" \| "none", "contact": string}` or `null` | Sent whole. `contact` is at most 120 characters and must be an email address when `mode` is `"email"`. It is kept for the other modes but not used. |

Control and invisible formatting characters are stripped from every text value. On v2
none of these fields reaches the model's instructions: they are rendered into fixed
sentences, with owner text quoted, in an `<owner-settings>` block in the data part of
the request, and the fixed instructions tell the model to take its name, tone and
manner from that block (and to call itself the site's assistant when no name is set).
A legacy concierge run gets the same block appended to its instructions. See
`docs/concepts/concierge-knowledge.mdx`.

`concierge_name` also:

- heads the widget. The frame boot's `agentName` is the look editor's own agent name
  when one is set, otherwise `concierge_name`, on the public frame and the owner's
  preview frame;
- names the legacy runtime's dedicated agent (slug `concierge-<site_id>`). A PATCH
  that changes it renames that agent and rewrites its persona, but only while they
  still read as the generated ones: an agent the owner renamed or bound by hand is
  left alone. Clearing the name goes back to `<Site name> Concierge`.

A site's concierge agent never appears in tenant-facing agent listings: `GET /agents`,
`POST /agents/discover`, `@`-mention suggestions, the agents surface snapshot and the
planner's agent matching all leave it out. It is recognised by the `concierge` +
`site:<id>` tags provisioning stamps, or by its `concierge-<site_id>` slug for older
ones. `GET /agents/{id}` and the slug lookup still return it.

Owner replies are stored in their own table rather than as chat runs, because
the metering sweeper bills every terminal run and would otherwise charge the
owner credits for typing their own sentence.

---

## Growth — Prospects

First slice of the `/growth` outbound engine (G-1): a workspace-scoped
prospect store. All routes are license-gated and carry the canonical
`request_context`; every read is workspace-scoped inside the service, so a
prospect id from another workspace returns an identical 404 (existence never
leaks). The company website `domain` is the dedupe key — normalised to a bare
lowercase hostname (scheme, `www.`, path, and port stripped) — unique per
workspace. G-2 adds the bulk-ingestion route below; later slices add drafts
and Instinct-gated sends on the dedicated `growth` arq queue
(`pocketpaw_ee.cloud.growth.worker.WorkerSettings`).

**RBAC (G-4).** Every `/growth` route carries a workspace-role guard on top of
the license gate. Reads (`GET /growth/prospects`, `GET /growth/drafts`, …)
require `growth.read` (MEMBER); authoring writes — create/update/delete a
prospect, bulk ingest, create a draft, non-gated lifecycle moves — require `growth.write`
(MEMBER); and the outbound verbs — `POST /growth/drafts/{id}/propose`,
`POST /growth/drafts/propose-batch`, `POST /growth/linkedin/{id}/mark-sent`,
`POST /growth/queue/{channel}/deliver-approved` and `PATCH /growth/settings` —
require `growth.manage` (ADMIN). The propose route sits at the ADMIN tier deliberately:
`growth.executor` re-checks that same action against the proposer's *current*
role at dispatch time, so a member-filed proposal would always fail closed at
approve. A caller below the required tier gets
`403 workspace.insufficient_role`.

### `POST /api/v1/growth/prospects`

Create a prospect. Body:

```json
{
  "name": "Sam Founder",
  "company": "Acme Dental",
  "domain": "acme-dental.com",
  "source": "manual",
  "tier": "unqualified",
  "research_brief": "",
  "emails": [],
  "linkedin_url": null,
  "whatsapp_number": null,
  "opted_in": false,
  "status": "new"
}
```

Only `domain` and `source` are required. `name` and `company` default to
`""` — **not yet known**, which is the honest shape an import arrives in: a
pasted list of bare domains, enriched by research later on the same
`(workspace, domain)` identity. Nothing renders an empty value as the word
"unknown". The agent surface is stricter: `growth_upsert_prospect` refuses to
CREATE a row without a name and a company.

`project_id` (optional) assigns the prospect to a client project
(`cloud/projects`). It is validated against the caller's workspace — another
tenant's project is `404 project.not_found`. Nullable throughout: a workspace
not using projects is unaffected.

Enums: `source` is
`clay | directory | manual`; `tier` is `a | b | c | unqualified` (default
`unqualified`); `status` is
`new | qualified | drafted | in_sequence | replied | dead` (default `new`).
A duplicate `(workspace, domain)` returns `409 prospect.domain_taken` —
create-or-update callers use the service's `upsert_by_domain` seam instead.

Returns the prospect envelope: all fields above plus `id`, `workspace_id`,
and ISO `created_at` / `updated_at`. The envelope also carries `research`
(the structured profile from the last research run, or `null`) and
`researched_at` (ISO timestamp or `null`). Both are written only by
`POST /growth/prospects/{id}/research` below and cannot be set by create,
bulk or PATCH.

### `POST /api/v1/growth/prospects/bulk`

Batch create-or-update — the ingestion endpoint for Clay exports and
partner-directory scrapes (G-2). Body:

```json
{
  "rows": [
    {
      "name": "Sam Founder",
      "company": "Acme Dental",
      "domain": "acme-dental.com",
      "source": "clay",
      "emails": ["sam@acme-dental.com"]
    }
  ]
}
```

Each row is `CreateProspectRequest`-shaped (same fields, defaults, and enums
as the single-create route above). Max **500 rows** — an oversized payload is
a 422 before any row is processed. Rows are processed individually through
the upsert-by-domain seam:

- a **new** `(workspace, domain)` inserts → counted in `created`;
- an **existing** one updates in place (all fields overwritten except
  `source`, which keeps first-capture provenance) → counted in `updated`;
- an **invalid** row (bad enum, missing field, empty domain) is skipped and
  recorded — the rest of the batch proceeds. No all-or-nothing abort;
  upserts are idempotent, so re-posting the same payload is safe and reports
  every row as updated.
## Knowledge — Living Wiki API

The workspace knowledge browser (`/api/v1/knowledge/*`) is the read/reingest
surface the living-wiki frontend renders. It aggregates the workspace kb-go
scope (`workspace:{wid}`) with every agent scope in the workspace
(`agent:{aid}`). All routes require a valid license plus `kb.read`
(`kb.write` for the reingest POSTs) on the active workspace; routes that
accept a `scope` bind it to the caller through the same allowlist the `/kb`
router uses (own workspace + visible pockets + workspace agents + the
caller's own `user:` scope) and answer `403 kb.scope_forbidden` otherwise.
Errors use the standard envelope `{"error": {"code", "message"}}`.

### `GET /knowledge/articles`

Query params: `workspace_id` (optional, must match the active workspace),
`agent_id` (optional filter; `"workspace"` = workspace-only), `limit` /
`offset` (optional offset pagination; omit `limit` for the legacy one-shot
full listing), and `exclude_upload_derived`.

`exclude_upload_derived` (default `false`) is de-duplication for the Files
panel, which merges these rows with `GET /files` on the client. An upload
records the article it was ingested into (the FL-11b `kb_article_id` /
`kb_scope` columns), so when this is set those articles are dropped and an
uploaded PDF stops appearing beside the article compiled out of it as two
separate documents. Standalone knowledge — an article ingested from chat or a
URL, with no file behind it — is never touched; nor is an article whose upload
is hidden from AI, soft-deleted, or pocket-scoped, because then the article is
the only surviving copy in the panel.

**Leave it off for knowledge browsers.** `/knowledge`, `/knowledge-lab` and the
command palette read this same route, and there the compiled article *is* the
thing being read and repaired — suppressing for them would make upload-derived
knowledge silently vanish from the wiki. The extra query only runs when the
flag is set, so the default path costs nothing. Suppression happens before the
`offset` slice, so `total` and `has_more` describe the filtered set.

Response:

```json
{
  "created": 18,
  "updated": 1,
  "errors": [
    {"index": 4, "code": "prospect.invalid_row", "message": "source: Input should be 'clay', 'directory' or 'manual'"}
  ]
}
```

`index` is the row's position in the submitted `rows` array. Rows land only
in the caller's workspace — the same domains ingested by another workspace
create independent rows.

### `GET /api/v1/growth/prospects`

One page of the workspace's prospects. **The response is an envelope, not a
bare array** — it changed shape in G-10a:

```json
{
  "items": [ { ...prospect envelope }, ... ],
  "next_cursor": "newest:2026-07-28T09:14:02+00:00|66a1...f3",
  "total": 3182
}
```

`total` counts every row matching the current filters (not the page), so the
UI can say "showing 40 of 3,182". `next_cursor` is `null` on the last page.

Query parameters:

| Param | Default | Notes |
|---|---|---|
| `tier`, `status`, `source` | — | Validated against the enums above; an unknown value is a 422, not an empty list. |
| `project_id` | — | Scope to one client's pipeline. Omitted means every project (the whole view for a workspace not using them); an empty string means the rows with no client assigned. |
| `q` | — | Case-insensitive substring search across `name`, `company`, `domain` and `research_brief`. Regex metacharacters are escaped, so `.*` matches nothing rather than everything. Max 200 chars. |
| `sort` | `newest` | `newest` \| `oldest` \| `company` \| `tier`. |
| `cursor` | — | The previous page's `next_cursor`, passed back unchanged. |
| `limit` | 100 | Max 500. |

**Tier sort order is the declared rank `a → b → c → unqualified`**, not a
lexicographic comparison. Today's tier names happen to sort the same way
lexicographically; that is an accident, and renaming a tier would break it
silently. The rank lives in `growth/domain.py` as `TIER_SORT_ORDER` and the
query walks those buckets in order.

**Pagination is keyset**, so a page never skips or repeats a row when the
collection is written to mid-scroll. The cursor is opaque — do not parse or
construct it — and carries the sort mode it was issued under: reusing a
cursor after changing `sort` is a `422 prospect.bad_cursor` rather than a
silently wrong page. Any malformed cursor is the same 422.

**Search scale ceiling.** `q` is an unanchored regex `$or` across four
fields, which Mongo cannot serve from an index — it is a collection scan
bounded by the workspace filter. Fine at the scale this surface targets (tens
of thousands of rows per workspace); past ~100k it needs a real text index or
an external search index. No text index was added here: `models/prospect.py`
carries a unique `(workspace, domain)` index plus a `(workspace, createdAt)`
list cursor, and a Mongo text index is a per-collection singleton that has to
be designed against those rather than bolted on.

### `GET /api/v1/growth/prospects/facets`

Counts behind the filter chips. Takes the same `tier` / `status` / `source` /
`project_id` / `q` filters as the list route and returns:

```json
{
  "tier":   { "a": 12, "b": 40, "c": 8, "unqualified": 300 },
  "status": { "new": 210, "qualified": 90, "drafted": 40, "in_sequence": 12, "replied": 6, "dead": 2 },
  "source": { "clay": 180, "directory": 140, "manual": 40 }
}
```

Each block respects every active filter **except its own**. With
`status=new` on, the tier counts describe the new rows rather than
collapsing to whichever tier is selected — otherwise the selected chip reads
`n` and every sibling reads `0`, which tells the user nothing about where to
go next. `q` constrains all three blocks (it is not a facet of its own), and so does
`project_id` — it is not a chip the user toggles inside the list, it is
*which client's list* they are looking at, so the other three counts have to
be scoped to it.

Every legal value appears, zeros included, so the chip row keeps a stable
shape as the user filters. Served by one workspace-scoped `$facet`
aggregation — three separate queries would be three chances for the counts to
disagree with each other.

### `GET /api/v1/growth/prospects/{prospect_id}`

Fetch one prospect. Cross-tenant or unknown ids: `404 prospect.not_found`.

### `PATCH /api/v1/growth/prospects/{prospect_id}`

Partial update — send only the fields to change. `domain` (the dedupe
identity) and `source` (capture-time provenance) are immutable; the other
fields (`name`, `company`, `tier`, `research_brief`, `emails`,
`linkedin_url`, `whatsapp_number`, `opted_in`, `status`) patch in place.
Returns the updated envelope.

`project_id` is three-valued here: omitting it leaves the assignment alone,
an id reassigns the prospect to that client (validated against the workspace
— a foreign project is `404 project.not_found`), and `""` clears it.
Un-assigning is deliberately explicit: a bulk upsert only ever *sets* the
project, so an enrichment pass that carries no project can never orphan a
client's prospect.

### `POST /api/v1/growth/prospects/bulk-delete`

Delete prospects and every draft attached to them. Requires `growth.write`.
Body: `{"ids": ["<prospect id>", ...]}`, 1 to 500 ids. An empty list or more
than 500 ids is a 422.

Ids that are malformed, unknown, or belong to another workspace are skipped
silently, and nothing outside the caller's workspace is ever touched. The
counts in the response are the record of what was removed:

```json
{"deleted": 2, "drafts_removed": 3, "proposals_withdrawn": 1}
```

If any of those drafts is `proposed`, its pending `_growth_send` Instinct
proposal is rejected with reason `prospect deleted`, and
`proposals_withdrawn` counts the rejections. This step is best-effort. If
the Instinct store fails, the delete still goes through and the executor
fails closed when anyone approves a proposal whose draft is gone.
`growth_message_logs` rows are kept, because they are the audit record of
what was sent.

### `DELETE /api/v1/growth/prospects/{prospect_id}`

Delete one prospect and its drafts. It runs through the same service path as
bulk delete, including proposal withdrawal. Returns `204` with no body.
Requires `growth.write`. An id that is unknown, malformed, or in another
workspace returns `404 prospect.not_found`.

### `POST /api/v1/growth/prospects/{prospect_id}/research`

Research one prospect with the workspace's `growth-researcher` agent (the one
a hunt uses: web search and fetch only) and write what it found onto the row.
No body. Requires `growth.write`. Returns the updated prospect envelope. The
run is synchronous, so expect tens of seconds.

The prospect's hunt (its ICP) is passed as context when one exists. A
prospect with no hunt, or whose hunt was deleted, is researched without it.

`research` holds the profile. Every field has a default, so a sparse answer
still parses:

```json
{
  "summary": "Three-chair family dental practice in Austin.",
  "suggested_tier": "a",
  "tier_reason": "Owner-run, books by phone only.",
  "fit": "Matches the hunt: independent practice, no online booking.",
  "hook": "Their booking page is a phone number.",
  "caveats": ["Hours differ between the site and Google."],
  "next_steps": ["Email the practice manager."],
  "locations": [{"name": "Main office", "address": "…", "hours": "…", "notes": ""}],
  "people": [{"name": "Dana Ruiz", "role": "Practice manager", "notes": ""}],
  "channels": [{"kind": "phone", "value": "+1 512 555 0100", "notes": ""}],
  "facts": [{"label": "Founded", "value": "2009"}],
  "sources": ["https://acme-dental.com/about"]
}
```

`suggested_tier` is `a | b | c | ""`, and anything else becomes `""`.
`channels[].kind` is `phone | whatsapp | email | form | chat | booking |
social | other`, and an unknown kind becomes `other`. Every list holds at most
15 items. Prose (`summary`, `tier_reason`, `fit`, `hook`, each `caveats` and
`next_steps` item, every `notes` and `facts[].value`) is cut at 600
characters, and the short fields (names, roles, addresses, hours, channel
values, fact labels) at 200.
`sources` keeps only `http(s)` URLs.

The run fills gaps and never overwrites what an operator entered:

- `name` and `company` are set only when blank.
- Emails the agent saw on a page (`confidence: observed` with a
  `seen_at_url`) are merged into `emails`. Existing addresses are kept.
  Guessed patterns are never recorded.
- `linkedin_url` is set only when blank, and only to an `https` URL on
  `linkedin.com`.
- `research_brief` is rebuilt from `summary`, `fit` and `hook`. If all three
  are empty, the old brief is kept.
- `source_urls` becomes the ordered, de-duplicated union of the old and new
  sources, capped at 50.
- `tier` takes `suggested_tier` only while the prospect is `unqualified`.
- `status` moves `new → qualified` and never goes backwards.
- `research` is replaced and `researched_at` is set to now (UTC).

The row is re-read after the run, so an edit made while the agent was working
is kept.

Errors: `404 prospect.not_found`; `503 prospect.research_unavailable` when no
research backend is wired; `502 prospect.research_failed` when the run fails
or returns no usable entry for this domain. A 502 writes nothing.

### `POST /api/v1/growth/prospects/{prospect_id}/draft`

Write first-touch copy for one prospect with the workspace's `growth-writer`
agent and store it as drafts. Requires `growth.write`. Body (both fields
optional):

```json
{"channels": ["email", "linkedin"], "instructions": "Mention the Austin office."}
```

`channels` is any of `email | linkedin | whatsapp`. Omitted or `null` means
all three. `instructions` (max 1000 characters) are operator notes passed to
the writer. They cannot override its rule against inventing facts.

The writer has no tools. It works only from what is on the prospect: the
research profile (or `research_brief`), name, company and the hunt's criteria.
It is seeded into each workspace at boot and lazily on first use, with
`tools: []`, `tool_mode: exclusive`, trust level 1, temperature 0.6 and soul
off.

A requested channel is drafted only when the prospect can be reached on it.
Otherwise it is listed in `skipped` with a reason:

| Channel | Needs | Skip reason |
|---|---|---|
| `email` | at least one address in `emails` | `no email address on file` |
| `linkedin` | `linkedin_url` | `no LinkedIn profile on file` |
| `whatsapp` | `whatsapp_number` | `no WhatsApp number on file` |
| `whatsapp` | `opted_in: true` | `the prospect has not opted in to WhatsApp` |

A channel that already has a `first_touch` draft in `draft`, `proposed`,
`approved` or `sent` is skipped as `already drafted`. A `rejected` or
`replied` draft does not block a new one. The writer is only asked for the
eligible channels, and anything it returns for another channel is ignored. An
eligible channel it leaves out is skipped with `the writer returned no draft
for this channel`. An email with no subject is skipped with `the writer
returned an email with no subject`.

Each draft is stored through the same path as
`POST /growth/prospects/{id}/drafts`: `variant: first_touch`, status `draft`,
subject kept for email only. The prospect moves to `drafted` unless it is
already further along. Nothing is proposed or sent. Response:

```json
{
  "drafts": [ { ...draft envelope }, ... ],
  "skipped": [ {"channel": "whatsapp", "reason": "no WhatsApp number on file"} ]
}
```

Errors: `404 prospect.not_found`; `503 prospect.writer_unavailable` when no
writer is wired; `422 prospect.no_channel` when no requested channel is
eligible (this includes `channels: []` and the case where every requested
channel is already drafted); `422` for `instructions` over 1000 characters;
`502 prospect.draft_failed` when the run fails or returns no usable draft. A
502 stores nothing.

## Growth — Drafts

Third slice of the `/growth` outbound engine (G-3): per-channel outreach
drafts attached to a prospect, with an enforced status lifecycle. Same gates
as prospects — license + `request_context`, every read workspace-scoped
(cross-tenant ids 404). The lifecycle is the object the send-gate slice
(G-4) proposes and dispatches on top of:

```
draft → proposed → approved → sent → replied
  └────────┴──────────┴─────────┴──→ rejected   (any non-terminal)
```

`replied` and `rejected` are terminal. Any other move — skipping ahead,
going backwards, leaving a terminal state — is a
`422 draft.illegal_transition`. Transitions are mechanism-only: no side
effects, no sending.

**G-4 — the Instinct send gate owns the `approved` and `sent` edges.** The
public status route refuses those targets with `403 draft.gate_required`
even though they are legal per the table: `approved` is only ever set after
a human approves the draft's `_growth_send` Instinct proposal (which also
enqueues the `growth.dispatch` arq job on the dedicated `growth` queue),
and `sent` only by the dispatch worker (G-5/G-6 — a logging stub in G-4).
Structural, like /ship's destroy gate: nothing sends without an approval.

### `POST /api/v1/growth/prospects/{prospect_id}/drafts`

Attach one channel's copy to a prospect. Body:

```json
{
  "channel": "email",
  "subject": "Quick idea for Acme Dental's booking flow",
  "body": "Saw your online booking stops at a contact form — here's a live demo.",
  "variant": "first_touch",
  "demo_url": null
}
```

`channel` (`email | linkedin | whatsapp`) and `body` (non-empty, max 10 000
chars) are required. `subject` is **email-only** — sending it on another
channel is a 422. `variant` is `first_touch | follow_up` (default
`first_touch`). Drafts are always born in `status: "draft"` — there is no
status field here; lifecycle moves go through the transition route.

The prospect must exist in the caller's workspace (`404 prospect.not_found`
otherwise). A prospect still in `new` / `qualified` flips to `drafted` on
its first draft; later prospect statuses are never regressed.

Returns the draft envelope: the fields above plus `id`, `workspace_id`,
`prospect_id`, `status`, and ISO `created_at` / `updated_at`.

### `GET /api/v1/growth/drafts`

List the workspace's drafts, newest first. Optional query filters:
`prospect_id`, `channel`, `status` (enum-validated — an unknown value is a
422) and `limit` (default 100, max 500).

### `PATCH /api/v1/growth/drafts/{draft_id}`

Edit a draft's copy. Body: any subset of `subject`, `body`, `demo_url`
(an empty body object is a 422 — there is nothing to change). No `status`
field exists on this request: a lifecycle move dressed as an edit would be a
second, unreviewed road to `approved`.

**Only while the draft is still `draft`.** From `proposed` on, the stored body
is what a human is reading in the Tray and what the dispatch worker puts on
the wire, so an edit there would send copy nobody approved — refused with
`403 draft.not_editable`. Revise by rejecting the draft and writing a new one.
`subject` stays email-only (`422 draft.subject_not_allowed` on a linkedin /
whatsapp draft). Cross-tenant or unknown ids: `404 draft.not_found`. Requires
`growth.write` (MEMBER) — editing copy is authoring, not an outbound verb.

### `POST /api/v1/growth/drafts/{draft_id}/status`

Move a draft along the lifecycle. Body: `{"status": "proposed"}` (any
`DraftStatus`). Legal moves per the machine above; anything else is a
`422 draft.illegal_transition` and the draft is unchanged. Cross-tenant or
unknown ids: `404 draft.not_found`. Returns the updated envelope.

The gate-owned targets `approved` and `sent` are refused here with
`403 draft.gate_required` (see the G-4 note above) — approval happens only
in the Instinct Tray, and only the approved dispatch path may send.

### `POST /api/v1/growth/drafts/{draft_id}/propose`

File a gated `_growth_send` Instinct proposal for a draft (G-4). Requires
`growth.manage` (ADMIN). No body.

This route is the **only** way a `_growth_send` proposal comes into existence:
the generic `POST /instinct/actions` (open to any member holding
`instinct.propose`) refuses reserved gated parameter keys with
`422 instinct.reserved_parameter_key`, so nobody can hand-craft a Tray card
that dispatches a send on approval. Approving with edits cannot re-point one
either — the blob's tenancy, proposer, target draft and channel are pinned back
from the stored proposal.

The draft must be able to legally move to `proposed`
(`422 draft.illegal_transition` otherwise — so re-proposing an already
proposed draft is refused and no duplicate proposal is filed); cross-tenant
or unknown ids `404 draft.not_found`.

Flips the draft to `proposed` and files an Instinct `Action` whose
`_growth_send` blob carries the draft/prospect ids, the channel, the
prospect's name + company, and the **rendered preview** (subject + body) —
the human approves the exact copy that was staged. Returns:

```json
{ "proposal_id": "<instinct action id>", "draft": { ...draft envelope, "status": "proposed" } }
```

NOTHING is sent by this route. On **approve** (single or bulk, in the
Instinct Tray) the growth executor flips the draft to `approved` and
enqueues the `growth.dispatch` job `{draft_id, channel}` on the dedicated
`growth` arq queue — with an execute-time re-check that the proposer STILL
holds `growth.manage` (a since-demoted proposer's approved send fails
closed), and `mark_failed` on the Action if the enqueue fails. In a
workspace with mock delivery on (*Growth — Delivery queues*), an approved
`email` or `whatsapp` draft is delivered in-process by a fake provider
instead, and nothing is enqueued. On
**reject** the draft flips to `rejected` and nothing is enqueued. The
`email` branch is live (below) and the `whatsapp` branch is live (*Growth —
WhatsApp dispatch*); `linkedin` keeps the logging stub on purpose — it is
sent by hand from the LinkedIn queue.

### `POST /api/v1/growth/drafts/propose-batch`

Propose a selection of drafts in one call. Requires `growth.manage` (ADMIN)
— the same tier as the single propose, so batching is not a cheaper route to
the outbound verb.

```json
{ "draft_ids": ["66a1...f3", "66a1...f4", "66a1...f5"] }
```

Max 100 ids; an oversized payload is a `422` at the boundary, before a single
proposal is filed. The cap is 100 rather than bulk ingest's 500 because each
id costs a proposal a human then has to triage in the Tray.

Each id goes through the **same** `propose_send` path as the single-draft
route: one gated `_growth_send` Instinct proposal per draft, each approved or
rejected individually. There is no batch proposal object, no batch approval,
and no shortcut into the gate — a "batch" here is a UI convenience over N
gated proposals. Nothing is sent by this route.

Partial success, like bulk ingest — a draft that cannot be proposed (missing,
cross-tenant, already proposed, terminal) records an indexed error entry and
the remaining ids still go:

```json
{
  "proposed": 2,
  "failed": [
    { "index": 1, "draft_id": "not-an-object-id", "code": "draft.not_found", "message": "..." },
    { "index": 2, "draft_id": "66a1...f5", "code": "draft.illegal_transition", "message": "..." }
  ]
}
```

`index` is the id's position in the submitted `draft_ids` array. Nothing is
rolled back on partial failure — the proposals already filed are legitimate
and a human can reject them in the Tray.

### Dispatch — how an approved email actually sends (G-5)

The `growth.dispatch` job's `email` branch is live. It is not an HTTP route —
there is no "send this now" endpoint, by design — but its behaviour is part of
the contract the propose/approve routes above promise.

1. **Load and re-check.** The job re-reads the draft and refuses anything that
   is not `approved`. A job whose draft was rejected while queued, or a
   redelivered job for a draft already `sent`, logs a warning and makes **no**
   provider call. This is the dispatcher's half of the send gate.
2. **Send.** Delivery goes through the workspace's **Mailtrap** connector
   (`connectors/mailtrap.yaml`) over the Email Sending API. The token is a
   per-workspace credential held in that workspace's connector row and read
   through the connector state store — never an inlined process credential,
   and never logged, returned, or put on a DTO. Disabling the connector
   revokes sending immediately (the state store only resolves enabled rows).
   The connector declares **no actions**, so no agent or connector-execute
   call can reach a send: the approved-draft path is the only one.
3. **Record, then flip.** A `MessageLog` row is written first (the audit row
   proves a message physically left even if the following write fails), then
   the draft moves `approved → sent` through the same gate seam the executor
   uses. No second status path is introduced.

**Failure is retryable, not fatal.** A provider rejection, a transport error,
an unconfigured connector, a prospect with no email address, or a
subject-less draft all produce `MessageLog(outcome="failed", error=...)` and
leave the draft `approved` — the human approval still stands, only delivery
failed, so a re-run needs no second approval. Nothing raises out of the job:
the growth worker runs `max_tries=1` precisely so outbound work is never
auto-retried into a double-send, and the `MessageLog` row is the durable
failure record.

**`MessageLog`** (collection `growth_message_logs`, one row per delivery
**attempt**): `workspace`, `draft_id`, `prospect_id`, `channel`, `provider`
(`"mailtrap"`, `"msg91"`, or `"mock"` for mock delivery),
`provider_message_id`, `to_address`, `sent_at`, `outcome`
(`sending | sent | failed | blocked`), `blocked_reason`, `error`. Email rows
are written `sent` / `failed` directly; WhatsApp and mock rows start as
`sending` and are finalised. Written only by the growth service.

**Config — `GROWTH_SENDING_DOMAIN` (required to send).** The secondary
sending domain outreach rides. Unset means nothing goes out; the dispatcher
fails closed rather than guessing. The from-address (default
`outreach@<GROWTH_SENDING_DOMAIN>`, overridable per workspace via the
connector's `MAILTRAP_FROM_EMAIL`) is validated against it at send time, and
the value may not equal the deployment's own host (`POCKETPAW_PUBLIC_BASE_URL`).
Cold outreach draws spam complaints at rates transactional mail never sees,
and every complaint lands on the sending domain's reputation — a burnt
secondary domain costs a DNS record and a warm-up, while a burnt apex takes
password resets, invoices, and receipts down with it.

## Growth — LinkedIn Queue

The manual send surface for LinkedIn outreach (G-8). **Deliberately manual**:
there is no LinkedIn API integration and no automation — the captain
copy-pastes each note by hand (account-ban avoidance is the feature). Same
gates as the rest of `/growth` — license + `request_context`, every read
workspace-scoped.

### `GET /api/v1/growth/linkedin/queue`

The workspace's linkedin-channel drafts in `proposed` / `approved`, newest
first, each joined with its prospect's targeting context. Query:
`limit` (default 100, max 500) and `format` (`json` default, `md`).

JSON items:

```json
{
  "draft": { "id": "…", "body": "…", "variant": "first_touch", "status": "approved", "…": "…" },
  "prospect_name": "Sam Founder",
  "prospect_company": "Acme Dental",
  "linkedin_url": "https://linkedin.com/in/sam-founder",
  "research_brief": "Books via a contact form; no online scheduling.",
  "tier": "a"
}
```

`?format=md` returns `text/markdown` instead — a paste-ready export, one
section per prospect (no tables, no HTML): name + company heading, the
profile URL as a link, tier + the brief's first line, the connect note
(`first_touch` body, with a char count against LinkedIn's 300-char connect
limit), the after-accept message (`follow_up` body, when queued), and each
draft's id for the mark-sent call.

### `POST /api/v1/growth/linkedin/{draft_id}/mark-sent`

Record that a queued LinkedIn draft was manually sent. The draft must be
linkedin-channel (`422 draft.wrong_channel` otherwise) and `approved` — the
move rides the G-3 machine, so anything but approved→sent is a
`422 draft.illegal_transition`. Cross-tenant or unknown ids:
`404 draft.not_found`. Returns the updated draft envelope (`status: "sent"`);
the draft leaves the queue and continues the normal lifecycle
(sent→replied / rejected).

Requires `growth.manage` (ADMIN) — it is an OUTBOUND verb, the same tier as
propose. Because G-4 made `sent` a gate-owned target, this route walks the
gate seam rather than the public status route; the structural guarantee is
unchanged (only an `approved` draft can move, and `approved` is reachable
only through an approved `_growth_send` proposal). The queue read requires
`growth.read` (MEMBER).

## Growth — Delivery queues

One outbound queue per channel, plus a per-workspace **mock delivery** mode
that lets the whole prospect → draft → approve → send loop run without a real
provider. Same gates as the rest of `/growth`.

### `GET /api/v1/growth/queue/{channel}`

`channel` is `email`, `whatsapp` or `linkedin` (anything else is a `422`).
Query: `limit` (default 100, max 500). Requires `growth.read` (MEMBER).

Returns that channel's drafts in `proposed`, `approved` or `sent`, newest
first. Drafts whose prospect has been deleted are left out. Each item:

```json
{
  "draft": { "id": "…", "channel": "email", "status": "approved", "…": "…" },
  "prospect_name": "Sam Founder",
  "prospect_company": "Acme Dental",
  "prospect_domain": "acme-dental.com",
  "tier": "a",
  "to": "sam@acme-dental.com",
  "opted_in": false,
  "delivery": {
    "outcome": "sent",
    "provider": "mock",
    "mock": true,
    "error": null,
    "sent_at": "2026-10-01T09:30:03+00:00",
    "at": "2026-10-01T09:30:03+00:00"
  }
}
```

`to` is the recipient the delivery path would use: the prospect's first email
entry containing `@`, its WhatsApp number, or its LinkedIn URL. `opted_in` is
the prospect's WhatsApp opt-in. `delivery` is the newest `MessageLog` row for
the draft (`null` before the first attempt); `at` is when that row last
changed. The LinkedIn manual queue below is unchanged.

### `GET /api/v1/growth/settings` / `PATCH /api/v1/growth/settings`

The active workspace's growth settings: `{"mock_delivery": false}`. `PATCH`
takes the same body and returns the stored value. It writes only
`settings.growth_mock_delivery` on the workspace, leaving every other setting
as it was, and records a `workspace.settings_updated` audit row. `GET`
requires `growth.read` (MEMBER); `PATCH` requires `growth.manage` (ADMIN),
because it decides whether an approval reaches a real provider. `404` when the
workspace cannot be found.

**What mock delivery does.** With it on, approving an `email` or `whatsapp`
draft in the Tray no longer enqueues `growth.dispatch`. The executor starts an
in-process delivery instead, and the Action's outcome reads
`growth.mock_delivery started for draft <id> (<channel>)`. That delivery
applies the same eligibility checks as real sending (email needs an address
with `@`, a subject and a body; WhatsApp needs an opt-in and a number). A
failed check writes a `blocked` `MessageLog` row with a readable `error` and
leaves the draft `approved`. Otherwise it writes a `sending` row, waits
`GROWTH_MOCK_DELIVERY_SECONDS`, finalises the row to `sent` with `sent_at` and
a `mock-…` provider message id, and moves the draft to `sent`. Every row
carries `provider: "mock"`. Nothing is sent to anyone. Mock rows never count
toward the WhatsApp hourly cap, and a mock-sent draft gets follow-ups exactly
like a real one. LinkedIn is never mock-delivered; it stays manual. With the
setting off (the default), sending is unchanged.

The delivery runs inside the web process, so a restart during the wait loses
it. A graceful shutdown records the row as `failed`; a hard kill leaves it at
`sending`.

| Env var | Default | Meaning |
|---|---|---|
| `GROWTH_MOCK_DELIVERY_SECONDS` | `3` | Simulated provider latency between the `sending` and `sent` rows, in seconds (a float; `0` is allowed). A negative or non-numeric value falls back to the default. |

### `POST /api/v1/growth/queue/{channel}/deliver-approved`

Starts mock delivery for every `approved` draft on `email` or `whatsapp`
whose newest `MessageLog` row is not `sending`. Use it for drafts approved
before mock delivery was switched on, or whose delivery failed. No body.
Requires `growth.manage` (ADMIN).

```json
{ "started": ["66a1…f3", "66a1…f4"] }
```

`linkedin` returns `422 queue.not_deliverable`. With mock delivery off it
returns `409 growth.mock_delivery_off`. A draft that already has a delivery
running in this process is not started twice.

## Growth — Follow-ups

Final slice of the `/growth` v1 outbound engine (G-7): the loop that closes
the cycle. A draft that went out and got no reply produces a **second draft**
— a short nudge — which is filed straight back into the Instinct Tray through
the same `_growth_send` gate. There is **no new API surface** here and no new
authority: the sweep's terminal state is a `proposed` draft plus a pending
Action a human decides on, exactly like a first touch typed by hand. Nothing
auto-approves and nothing auto-sends.

**Where it runs.** `growth.followup_sweep`, a daily arq **cron** at 13:00 UTC
on the dedicated `growth` queue
(`pocketpaw_ee.cloud.growth.worker.WorkerSettings.cron_jobs`, `unique=True`
so a horizontally-scaled worker fleet runs one tick, not N). Deploy it with
the same process that already serves `growth.dispatch`:

```bash
arq pocketpaw_ee.cloud.growth.worker.WorkerSettings
```

**What it does**, per (workspace, prospect, channel) thread:

1. Finds the thread's most recent `sent` draft and checks it is older than
   `GROWTH_FOLLOWUP_DELAY_DAYS`. The clock starts at the LAST touch, not the
   first — a thread that already had a nudge waits the full delay again.
2. Skips the thread when the prospect is `replied` (they answered) or `dead`
   (already retired), or when any draft in the thread is `replied`.
3. Skips the thread when a follow-up is already **open** in it (`draft`,
   `proposed` or `approved`) — that one is the human's move. This is also
   what makes the sweep idempotent: the follow-up it filed on the last pass
   blocks the next one, so re-running a pass creates nothing.
4. Counts the thread's non-rejected follow-ups. At `GROWTH_FOLLOWUP_MAX` the
   prospect is retired to `status: "dead"` and nothing further is created —
   the sweep never touches them again. (A follow-up a human **rejected**
   doesn't burn a cap slot.)
5. Otherwise: creates a `variant: "follow_up"` draft (copy templated in code
   from the thread's first touch — a placeholder the `/growth` crew skill
   replaces; on email the subject is the original's, `Re:`-prefixed, so the
   nudge threads under it) and immediately runs it through the existing
   propose path — filing the `_growth_send` Action and flipping the draft to
   `proposed`.

**Who proposes.** A cron has no user, but the gate re-checks the proposer's
*current* `growth.manage` role at execute time, so a "system"-proposed
follow-up would be approvable and then fail closed at dispatch. The sweep
therefore **inherits the human** who proposed the thread's last send, read off
that draft's own `_growth_send` Action — they become the follow-up's trigger
source, its Tray assignee, and the identity the execute-time re-check runs
against. When no proposer can be resolved (a draft that reached `sent` with no
Tray record) the thread is skipped: a proposal nobody can execute is worse
than none.

**Config.** Both read from the environment at sweep time, so a change takes
effect on the next tick without a redeploy. An unparseable or out-of-range
value logs a warning and falls back to the default — a typo must not take the
outbound loop down.

| Env var | Default | Meaning |
|---|---|---|
| `GROWTH_FOLLOWUP_DELAY_DAYS` | `4` | Days of silence after a send before its follow-up comes due. Minimum `1`. |
| `GROWTH_FOLLOWUP_MAX` | `2` | Follow-ups allowed per (prospect, channel). On the pass where a capped thread comes due again, the prospect is set to `dead` instead. |

**Send timestamp.** The age check reads the draft's `sent_at` when the
dispatch worker's send record supplies one, and otherwise falls back to
`updated_at` — which, for a draft sitting in `sent`, is the moment of the
`sent` transition, since the status flip is the last write to that row.

---

## Growth — WhatsApp dispatch (MSG91)

Sixth slice of the `/growth` outbound engine (G-6): the `channel="whatsapp"`
branch of the `growth.dispatch` arq job actually sends, through MSG91 (an
official Meta WhatsApp Business Solution Provider).

**The opt-in guard is a service-level invariant, not a UI convention.** Meta
bans WhatsApp Business Accounts that send business-initiated template messages
to numbers that never consented — the quality rating collapses, then the number
gets restricted, then banned, for the whole tenant. So dispatch refuses any
draft whose prospect has `opted_in = false`: it makes **no provider call at
all**, raises `growth.whatsapp_opt_in_required`, records a `blocked` row, and
leaves the draft in `approved` so the refusal is visible instead of silent.

Because business-initiated messages must be pre-approved templates, the draft
`body` is sent as the template's first body variable, never as free-form text.

**Guard order** (each refusal writes its own send-log row and raises):

| # | Guard | Error code | Blocked reason |
|---|---|---|---|
| 1 | Draft still `approved` (the G-4 gate owns that status) | `growth.draft_not_approved` | `draft_not_approved` |
| 2 | Prospect still exists in the draft's workspace | `growth.prospect_unavailable` | `prospect_missing` |
| 3 | **`prospect.opted_in`** | `growth.whatsapp_opt_in_required` | `not_opted_in` |
| 4 | Prospect has a WhatsApp number | `growth.prospect_unavailable` | `no_number` |
| 5 | Hourly rate cap | `growth.whatsapp_rate_capped` | `rate_capped` |
| 6 | Resolvable MSG91 credentials | `growth.whatsapp_not_configured` | `not_configured` |

On success the job writes a `sending` row, calls MSG91, finalises the row to
`sent` (or `failed`), and flips the draft `approved → sent` through the gate's
own `service.gate_transition` seam. The growth worker runs with `max_tries = 1`,
so a refusal lands as a failed arq job for operator review — an outbound message
is never retried automatically.

Every attempt — including refused ones — writes a row to
`growth_whatsapp_send_logs` (`WhatsAppSendLog`): workspace, draft, prospect,
recipient number, status, blocked reason, provider message id, and
`opted_in_at_attempt` (the consent fact *as of* the send, which a later prospect
edit cannot rewrite).

### Credentials

The MSG91 authkey is resolved per workspace through the **connector state
pattern** — the workspace's `msg91` `WorkspaceConnector` row, read via
`CloudConnectorStateStore` with the `ws:<workspace_id>` scope key. There is
deliberately **no env-var fallback for the authkey**: a deployment-global
provider key would let one tenant's outbound traffic burn another tenant's WABA
quality rating. No row, no send.

Config keys on that row:

| Key | Required | Notes |
|---|---|---|
| `authkey_enc` | yes* | The authkey as a `_core.crypto` Fernet ciphertext (needs `CLOUD_ENCRYPTION_KEY`). Preferred — keeps the plaintext out of Mongo and out of the connectors entity's own `config` echo. |
| `authkey` | yes* | Plaintext fallback for installs with no encryption key. Warns on every resolve. |
| `integrated_number` | yes | The WABA business number messages are sent from. |
| `template_name` | yes | The pre-approved Meta template. |
| `language_code` | no | Defaults to `en`. |
| `namespace` | no | WABA template namespace, when the account requires one. |
| `base_url` | no | Defaults to `https://api.msg91.com`. Per-workspace override for regional mirrors. |

\* one of `authkey_enc` / `authkey`.

The authkey is never logged, never returned by any DTO, and never persisted into
the send log. `Msg91Credentials.__repr__` redacts it, so a traceback or a `%r`
format cannot spill it either.

### `POST /api/v1/growth/webhooks/msg91`

Inbound MSG91 WhatsApp events. **Unauthenticated by nature** (MSG91 is the
caller) — mounted separately from the licensed `/growth` router, with no license
gate, no RBAC and no `RequestContext`.

**Fails closed.** Trust rests entirely on the signature: HMAC-SHA256 over the
raw request body, keyed by `GROWTH_MSG91_WEBHOOK_SECRET`, hex-encoded, in
`X-Msg91-Signature` (an optional `sha256=` prefix is tolerated). A bad
signature, a missing header, **and an unset secret** all return
`403 growth.webhook_signature_invalid` / `growth.webhook_unsigned` /
`growth.webhook_unverifiable`. Unlike the Recall webhook there is no
accept-while-you-wire-it-up mode — a forged inbound reply would flip
`opted_in` and thereby unlock business-initiated sends to a number that never
consented.

On a verified inbound reply the handler moves the prospect to `replied` and
walks any `sent` WhatsApp draft for that prospect to `replied` (through the gate
seam). What the reply does to consent depends on its text, matched as a whole
message after trimming whitespace and a trailing `.` or `!`, case-insensitively:

| Reply | Effect |
|-------|--------|
| `stop`, `unsubscribe`, `cancel`, `end`, `quit`, `opt out`, `opt-out`, `optout` | `opted_in = false` and `whatsapp_opt_out_at` stamped, so the dispatch guard refuses later sends |
| `start`, `subscribe`, `resume` | `opted_in = true` and `whatsapp_opt_out_at` cleared |
| anything else | `opted_in = true`, unless `whatsapp_opt_out_at` is set (only START undoes an opt-out) |

A user-initiated message opens the 24-hour service window. Reading a plain reply
as consent to later business-initiated sends is current behaviour, not settled
policy. A STOP word inside a longer sentence is a plain reply.

Delivery-status callbacks (`status` / `delivered` / `read` / …) are accepted and
ignored — a receipt is not consent. A number no workspace holds is a 200 no-op.

The response body is a constant `{"ok": true}` for every accepted request —
processed, ignored, or unknown number — so the endpoint cannot be used as a
membership oracle over phone numbers.

Tenancy: the payload carries no workspace, so the lookup starts from the number
and is immediately re-narrowed — when any workspace has actually WhatsApp'd that
number, only those workspaces' rows are touched, so a tenant that merely holds
the same prospect never learns someone else's outreach got a reply.

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `GROWTH_WHATSAPP_MAX_PER_HOUR` | `20` | Per-workspace outbound WhatsApp ceiling per rolling hour. WhatsApp quality rating is computed over a rolling window of recent business-initiated messages, and a burst (bulk approval, retry storm, mis-scoped follow-up cron) is exactly the shape that trips it — with the damage landing on the WABA, not the individual send. The cap bounds the blast radius of a bug. Attempts that reached the provider (`sending` / `sent` / `failed`) consume the window; refused attempts and mock-delivery rows (provider `"mock"`) do not. There is no "disabled" value — `0` refuses every send rather than meaning unlimited, and a non-numeric or negative value falls back to the default, so a fat-fingered setting fails closed. |
| `GROWTH_MSG91_WEBHOOK_SECRET` | *(unset)* | Shared secret for the inbound webhook HMAC. **Required** — while unset, `POST /growth/webhooks/msg91` rejects every request with 403. |
| `CLOUD_ENCRYPTION_KEY` | *(unset)* | Existing deployment-wide Fernet key. Needed to store the MSG91 authkey as `authkey_enc` rather than plaintext. |

## Growth — Hunts (ICPs)

A hunt is an Ideal Customer Profile: a standing, free-text description of who
a workspace wants, plus the cadence the discovery cron runs it on. Discovery
files what the research finds as `source: "discovery"` prospects at `status:
new`; nothing is drafted or sent. Workspace-scoped: another tenant's id is a
404 on every route. Reads need `growth.read`, writes and the preview need
`growth.write`.

| Route | What it does |
|---|---|
| `POST /api/v1/growth/icps` | Create. `name` + `criteria` required; `geography`, `exclusions`, `project_id`, `max_per_run` (1-100, default 10), `status` (`active`/`paused`) optional. `cadence` defaults to `off`, so a new hunt never runs by itself. |
| `GET /api/v1/growth/icps` | The workspace's hunts, newest first, as a bare array. Filters: `project_id` (`""` = unassigned), `status`, `limit`. |
| `GET /api/v1/growth/icps/{icp_id}` | One hunt. |
| `PATCH /api/v1/growth/icps/{icp_id}` | Partial update; `null`/omitted leaves a field as-is. Nothing re-runs on an edit. |
| `POST /api/v1/growth/icps/{icp_id}/preview` | Dry run (below). |
| `DELETE /api/v1/growth/icps/{icp_id}` | Delete the hunt. Prospects it already filed stay. |

**Response (`IcpResponse`)** — every route above except the preview and the
delete returns this shape; the list returns an array of it:

```json
{
  "id": "66f…",
  "workspace_id": "w1",
  "name": "Small dental practices",
  "criteria": "Dental practices with 2-6 chairs that still book by phone.",
  "project_id": null,
  "geography": "",
  "exclusions": "",
  "cadence": "off",
  "max_per_run": 10,
  "status": "active",
  "last_run_at": null,
  "last_preview": {
    "items": [
      {
        "domain": "acme-dental.com",
        "name": "",
        "company": "Acme Dental",
        "research_brief": "Three chairs, books by phone.",
        "source_urls": ["https://acme-dental.com/about"],
        "emails": [],
        "already_known": false
      }
    ],
    "notes": "One strong fit.",
    "error": ""
  },
  "last_preview_at": "2026-09-29T10:00:00Z",
  "created_at": "2026-09-29T09:58:00Z",
  "updated_at": "2026-09-29T10:00:00Z"
}
```

`last_preview` / `last_preview_at` are `null` until the hunt is previewed.

### `POST /api/v1/growth/icps/{icp_id}/preview`

Runs the research once and returns what a run **would** file:
`{icp_id, items, notes, error}`, with the same item shape as
`last_preview.items` above. `emails` has already been through the
observed-only filter, and a company already in the pipeline comes back with
`already_known: true` rather than being hidden.

It writes no prospects. It does record the result on the hunt as
`last_preview` (`items` / `notes` / `error`, the response minus `icp_id`) and
stamps `last_preview_at`, so a page refresh does not lose a research pass that
was already paid for. A failed research attempt is recorded too, with `error`
set, so "the last attempt failed" survives a refresh. Each preview replaces
the previous one.

The stored preview only ever describes the criteria it ran against:

- A `PATCH` that actually changes `criteria`, `geography`, `exclusions` or
  `max_per_run` clears `last_preview` and `last_preview_at`. Changing only
  `name`, `cadence`, `status` or `project_id`, or re-sending an unchanged
  value, keeps them.
- If one of those four fields changes while the research is running, the
  result is still returned to the caller but is not recorded.

| Status | Code | When |
|---|---|---|
| 503 | `icp.research_unavailable` | No research backend is wired on this deployment. Nothing is recorded. |
| 404 | `icp.not_found` | Unknown id, or another workspace's hunt. |

## Growth — Social

The setup wizard, website analysis and post ideas behind `/growth` › Social.
A workspace can hold several profiles, one per brand it posts for. Every
`/profile…` and `/ideas` route takes an optional `?profile_id=`; without it
the route acts on the most recently updated profile, and the first `PUT`
creates one. Ideas belong to one profile. Nothing here leaves
the workspace: there is no posting, scheduling or account connection. Every
route is license-gated and workspace-scoped; reads need `growth.read`, every
other route `growth.write` (both MEMBER).

| Route | What it does |
|---|---|
| `GET /api/v1/growth/social/profiles` | Every profile in the workspace, oldest first: `{items: SocialProfile[]}`. |
| `POST /api/v1/growth/social/profiles` | Start a new, empty profile for another brand. |
| `GET /api/v1/growth/social/profile` | One profile (`?profile_id=`). `404 social_profile.not_found` until the first `PUT`. |
| `PUT /api/v1/growth/social/profile` | Partial upsert of the typed fields and, optionally, a hand-edited `analysis` (below). |
| `POST /api/v1/growth/social/profile/analyze` | Read the website and run the analyst, in the request (below). |
| `POST /api/v1/growth/social/profile/complete` | Finish onboarding: stamps `onboarding_completed_at`. |
| `POST /api/v1/growth/social/ideas/generate` | Generate new post ideas (below). |
| `GET /api/v1/growth/social/ideas` | `{items}`, newest first. Optional `status=new\|approved\|skipped`; omitted returns every idea. Any other value is a 422. |
| `PATCH /api/v1/growth/social/ideas/{idea_id}` | Review or edit one idea (below). `media_choice` (`poster` or `reel`) picks which media goes with the post when it has both. |
| `POST /api/v1/growth/social/ideas/schedule` | `{items: [{idea_id, scheduled_at}], timezone?, duration_minutes?}`: date approved ideas and give each a `/calendar` event (calendar `growth-social`); rescheduling moves the same event. `409 social.idea_not_approved` if any is not approved. Nothing is posted. |
| `POST /api/v1/growth/social/ideas/{idea_id}/unschedule` | Clear the date and delete the idea's calendar event. |
| `GET /api/v1/growth/social/meme-formats` | The meme format templates Create offers: `{items: [{id, name, layout}]}`. Our own layouts; no third-party media. |
| `POST /api/v1/growth/social/characters` | `{name?, description}`: the agent draws an original vector mascot (never a real person or existing franchise character), sanitised, kept on the profile (max 6, else `409 social.character_limit`). Returns the profile. |
| `DELETE /api/v1/growth/social/characters/{character_id}` | Remove a character. Returns the profile. |
| `POST /api/v1/growth/social/memes` | `{format?, character_id?, mention_business, prompt?, platform}`: draw a meme (character redrawn in the format's layout, as SVG) and file it as a new Blitz idea with `format: "meme"` and `poster_svg`. `409` before setup, `404` unknown character, `422` unknown format. |

**Response (`SocialProfile`)** — every profile route returns this shape:

```json
{
  "id": "6702…",
  "workspace_id": "w1",
  "owner_name": "Sam",
  "company_name": "Acme Dental",
  "website": "https://acme-dental.com",
  "description": {
    "product": "Family dentistry",
    "audience": "Parents of young kids",
    "problem": "",
    "benefits": "",
    "tone": "Warm, plain",
    "avoid": "Fear tactics"
  },
  "team_size": "2_10",
  "monthly_revenue": "10k_50k",
  "role": "founder",
  "business_model": "local_business",
  "category": "Health",
  "analysis_status": "ready",
  "analysis_error": null,
  "analysis": {
    "summary": "A family dental practice in Austin.",
    "product": "Checkups and cleanings",
    "audience": "Parents of young kids",
    "problem": "Kids who are scared of the dentist",
    "tone": "Warm, plain",
    "benefits": ["Same-week appointments"],
    "differentiators": ["Kid-only hours"],
    "competitors": [],
    "avoid": ["Fear tactics"],
    "content_pillars": ["First visits", "At-home habits"],
    "hooks": ["What a first dental visit actually looks like"],
    "pages_read": ["https://acme-dental.com/", "https://acme-dental.com/about"],
    "logo_url": "https://acme-dental.com/logo.svg"
  },
  "analyzed_at": "2026-10-06T10:00:00+00:00",
  "onboarding_completed_at": null,
  "created_at": "2026-10-06T09:58:00+00:00",
  "updated_at": "2026-10-06T10:00:00+00:00"
}
```

Enums: `team_size` is `solo | 2_10 | 11_50 | 51_200 | 200_plus`;
`monthly_revenue` is `pre_revenue | under_1k | 1k_10k | 10k_50k | 50k_250k |
250k_plus`; `role` is `founder | marketer | social_media_manager | agency |
creator | other`; `business_model` is `b2b_saas | b2c_app | ecommerce |
services | local_business | creator | marketplace | other`. Each may be
`null`. `analysis_status` is `none | ready | failed`, and `analysis` is `null`
until the first successful analysis or hand edit.

### `PUT /api/v1/growth/social/profile`

Every field is optional. An omitted field is left as it is; an explicit
`null` clears it (`owner_name` and `company_name` clear to `""`).
`description` merges key by key, so one wizard step can send
`{"description": {"audience": "…"}}` without wiping the others. Each
description field is at most 2000 characters, the names 120, `category` 60.
`website` is trimmed and gets `https://` when typed bare (`acme.com` →
`https://acme.com`); anything that is still not an `http(s)` address with a
dotted host is a 422. An enum value outside the lists above is a 422.

`analysis` is a hand edit from the Brand page, in the same shape as the
response's `analysis`. Only the editable fields you send are replaced
(`summary`, `product`, `audience`, `problem`, `tone`, and the six lists).
`pages_read` and `logo_url` belong to the server and are ignored if sent.
Strings are at most 2000 characters; each list at most 20 items of 300
characters (blank items are dropped). A hand edit does not touch
`analyzed_at`. If `analysis_status` was `none` or `failed`, it becomes
`ready` and `analysis_error` is cleared.

### `POST /api/v1/growth/social/profile/analyze`

No body. Runs in the request and takes 20–60 seconds, so give the call a long
client timeout.

The website address and the six description fields go to the analyst agent
(`growth-social-analyst`, seeded in the workspace on first use and re-synced
to its definition on every run). It is pinned to exactly `WebSearch` and
`WebFetch`, the growth researcher's surface: it reads the homepage and up to
four telling pages (about, pricing, product or features, customers) itself,
and can do nothing else. What the
owner typed wins: the analysis may sharpen a typed field but not contradict
it, an empty analysis field is filled from the typed one, and the typed
things-to-avoid are always kept. With no website the analysis uses the
description alone. `pages_read` lists the http(s) URLs the agent reports it
fetched; `logo_url` is not set.

If the fetch or the model fails, the call still returns 200 with
`analysis_status: "failed"` and a short `analysis_error`; the previous `analysis` and `analyzed_at`
are kept. Only the analysis fields are written, so a `PUT` made while the
analysis runs is not overwritten.

| Status | Code | When |
|---|---|---|
| 503 | `social.analyzer_unavailable` | No analyser is wired on this deployment. |
| 404 | `social_profile.not_found` | The workspace has no profile yet. |
| 422 | `social.nothing_to_analyze` | Neither a website nor any description field is set. |

### `POST /api/v1/growth/social/profile/complete`

No body. Needs `owner_name`, `company_name`, `team_size`, `monthly_revenue`,
`role`, `business_model` and `category`; otherwise
`422 social.profile_incomplete`, whose message names the missing fields.
Stamps `onboarding_completed_at` on the first success and keeps that stamp on
later calls. `404 social_profile.not_found` without a profile.

### `POST /api/v1/growth/social/ideas/generate`

Body (optional): `{"count": 6}`, 1–12, default 6. A no-tools ideas agent
(`growth-social-ideas`) writes that many short-form post ideas from the
completed profile and is shown the workspace's 40 most recent hooks so it does
not repeat them. It is told never to claim results or metrics. New ideas are
stored with `status: "new"` and returned as `{items}`:

```json
{
  "items": [
    {
      "id": "6703…",
      "workspace_id": "w1",
      "format": "hook_demo",
      "hook": "What a first dental visit actually looks like",
      "on_screen_text": "No drills. No needles. Just counting teeth.",
      "caption": "Booking a first visit? Here is the whole thing in 30 seconds.",
      "why": "Parents worry about the unknown; showing it removes the fear.",
      "script": ["Open on the waiting room", "Chair goes back", "Counting teeth", "Sticker"],
      "hashtags": ["#kidsdentist", "#firstvisit"],
      "status": "new",
      "created_at": "2026-10-06T10:05:00+00:00",
      "updated_at": "2026-10-06T10:05:00+00:00"
    }
  ]
}
```

`format` is `hook_demo | slideshow | wall_of_text | meme | talking_head`.

| Status | Code | When |
|---|---|---|
| 503 | `social.ideas_unavailable` | No ideas writer is wired on this deployment. |
| 409 | `social.onboarding_incomplete` | No profile, or onboarding not completed. |
| 502 | `social.ideas_failed` | The run failed or returned no usable idea. Nothing is stored. |
| 422 | — | `count` outside 1–12. |

### `PATCH /api/v1/growth/social/ideas/{idea_id}`

Any of `status` (`new | approved | skipped`), `hook` (non-blank, ≤ 300),
`on_screen_text` (≤ 500), `caption` (≤ 2200), `script` and `hashtags` (blank
items dropped, then the first 12 beats and 15 tags kept). At least one is required. `format` and `why` are the
generator's and cannot be edited. Returns the idea. A malformed id, an
unknown id and another workspace's id all return
`404 social_idea.not_found`.

## Growth — the agent surface (`pocketpaw_growth` MCP)

The chat agent on the `/growth` rail reaches the same service layer through
thirteen in-process MCP tools. It is the operator's assistant on that page: it can
research and file a prospect, write and revise the copy, and put a send in
front of a human. It cannot send.

| Tool | RBAC | What it does |
|---|---|---|
| `growth_list_prospects` | `growth.read` | Compact rows + the filter-scoped `total`; `tier` / `status` / `source` / `q` / `sort` / `cursor` / `limit` |
| `growth_get_prospect` | `growth.read` | One prospect in full, with every draft written for it |
| `growth_list_drafts` | `growth.read` | Drafts with truncated body previews; filters by prospect / channel / status |
| `growth_linkedin_queue` | `growth.read` | The manual LinkedIn queue with its prospect context |
| `growth_upsert_prospect` | `growth.write` | Create-or-enrich keyed on `domain`; omitted fields keep their stored values |
| `growth_create_draft` | `growth.write` | Write one channel's copy — born `draft` |
| `growth_update_draft` | `growth.write` | Revise copy, only while the draft is still `draft` |
| `growth_propose_send` | `growth.manage` | Files one `_growth_send` Instinct proposal; returns `{status: "proposed", proposal_id}` |
| `growth_propose_send_batch` | `growth.manage` | The same, over up to 100 draft ids — one proposal each |
| `growth_list_icps` | `growth.read` | Hunts, each with a compact `last_preview` summary (`{found, error}` or `null`) and `last_preview_at` |
| `growth_get_icp` | `growth.read` | One hunt in full, including its whole `last_preview` |
| `growth_create_icp` | `growth.write` | Create a hunt. Takes no `cadence`; it lands `off` |
| `growth_preview_icp` | `growth.write` | Dry-run a hunt. Writes no prospects; records the result on the hunt as its last preview |

The agent's surface is deliberately **narrower than the HTTP one**:

| Verb | Operator over HTTP | Agent over MCP |
|------|--------------------|----------------|
| reads, upsert a prospect, write a draft | runs | runs |
| edit a draft's copy (while `draft`) | runs | runs |
| propose a send | runs | runs |
| move a draft to `approved` or `sent` | **refused** (gate-owned) | **no tool exists** |
| mark a LinkedIn draft sent | runs | **no tool exists** |

**The agent's reach ends at `proposed`.** No tool sends; no tool takes a
`status` argument (the legal move is exposed as the named verb
`growth_propose_send`, so there is no shape of argument that could ask for
`approved`); and `service.gate_transition` — the seam the executor and the
dispatch worker walk — is not reachable from the MCP module at all. The two
`status` fields that do appear are read filters on the list tools.
`growth_update_draft` stops at `draft` for the same reason: from `proposed`
on, the stored body is what the Tray shows and what goes on the wire, so an
edit past that point would be a send bypass wearing an edit's clothes. Tests
assert all of this against the tool list and schemas, so a tool added later
trips them before it ships.

Tenancy comes from the chat stream's identity — no tool accepts a
`workspace_id`, and every schema sets `additionalProperties: false`. The RBAC
tiers mirror the HTTP routes, and the ADMIN tier on the propose verbs is
load-bearing: `growth.executor` re-checks `growth.manage` against the
proposer's **current** role at approve time, so a proposal filed below that
tier could only ever clog the Tray.
  "articles": [
    {
      "id": "deploy-runbook",
      "title": "Deploy runbook",
      "source": "",
      "scope": "workspace:w1",
      "agent_id": null,
      "updated_at": "2026-08-01T12:00:00Z",
      "summary": "How we deploy.",
      "word_count": 250,
      "compiled_with": "claude-haiku-4-5",
      "version": 3,
      "categories": ["Ops"],
      "concepts": ["deploys", "rollbacks"],
      "compiled_at": "2026-08-01T12:00:00Z"
    }
  ],
  "total": 1,
  "agent_ids": ["agent-1"]
}
```

The first six keys are the pre-2026-08-04 row shape, unchanged. The wiki
metadata after them comes from `kb list --json` plus the article's wiki
frontmatter (kb list doesn't emit categories/concepts/compiled_at);
`updated_at` falls back to `compiled_at`. Orphan raw docs — ingested files
whose compile never completed — still appear as synthetic rows with
`compiled_with: null` and `version: null`.

### `GET /knowledge/articles/{article_id}?scope=`

Full article for the reader view. `scope` defaults to the active workspace.
Response is the row shape above plus `content` (markdown), `backlinks`
(list of article ids), `source_docs` (raw-doc ids), `scope`, and `orphan`.
An orphan raw-doc id returns `orphan: true` with the raw text as `content`
and `compiled_with: null`. Unknown id or an id outside the scope →
`404 article.not_found`. A kb failure that is NOT a genuine miss (timeout,
missing binary, transient error) → `500 knowledge.kb_unavailable` — a kb
outage never reads as "the article vanished".

### `GET /knowledge/stats`

Per-scope `kb stats` rollup across the workspace scope and every agent
scope. A scope whose stats call fails is skipped, never a 500.

```json
{
  "stats": [
    {
      "scope": "workspace:w1",
      "agent_id": null,
      "articles": 4,
      "words": 1000,
      "raw_docs": 5,
      "concepts": 12,
      "categories": 3
    }
  ],
  "agent_ids": ["agent-1"]
}
```

### `POST /knowledge/reingest`

Body: `{"article_id": "<id>", "scope": "<scope>" | null}`.

Re-runs an article's linked raw doc through the hardened
`KnowledgeService.ingest_text_to_scope` funnel (agent-backend compile on
keyless boxes, verbatim-fallback rejection). `article_id` may be a compiled
article (its frontmatter's first `source_docs` entry names the raw doc) or
an orphan raw-doc id. Response:

```json
{
  "scope": "workspace:w1",
  "article_id": "deploy-runbook",
  "new_article_id": "deploy-runbook-v2",
  "raw_doc_id": "raw-1",
  "source": "notes.txt",
  "result": { "article": "deploy-runbook-v2", "title": "...", "words": 250, "compiled_with": "llm" }
}
```

`result` is kb-go's ingest receipt passed through verbatim — note the id key
is `article` (finishIngest's shape), not `id`. `new_article_id` is the
server-extracted id of the article the recompile produced; when it differs
from `article_id` (the compile landed under a new slug) the FL-11b tracking
on any upload row pointing at the old id is re-pointed automatically.

Errors: `404 article.not_found` / `404 raw_doc.not_found`,
`422 knowledge.empty_raw_doc`, `500 knowledge.reingest_failed`.

### `POST /knowledge/reingest-upload`

Body: `{"upload_id": "<file id>", "scope": "<scope>" | null}`.

Synchronous counterpart of the FileReady auto-index listener, one upload per
call: resolves the uploaded blob (local or S3 via a temp file), extracts
text through the configured extraction chain, funnels it through
`ingest_text_to_scope` with the original filename as source, and stamps the
FL-11b `kb_article_id`/`kb_scope` tracking on the upload row. Response:

```json
{
  "scope": "workspace:w1",
  "upload_id": "up-1",
  "filename": "report.pdf",
  "article_id": "report-pdf",
  "result": { "article": "report-pdf", "title": "...", "words": 300, "compiled_with": "llm" }
}
```

`article_id` is the server-extracted id from kb-go's receipt (whose own id
key is `article`, not `id`) — clients should read the top-level field.
Pocket-scoped uploads are refused on this workspace surface: ingesting them
into workspace KB would lift pocket-private content across the pocket ACL
boundary; reingest those from the pocket surface instead.

Errors: `404 upload.not_found`, `403 knowledge.upload_hidden`
(`hide_from_ai` files are not ingestable),
`403 knowledge.upload_pocket_scoped` (pocket files belong to the pocket
surface), `422 knowledge.extraction_empty`,
`500 knowledge.extraction_failed` / `knowledge.upload_unreadable` /
`knowledge.reingest_failed`.

### `GET /knowledge/uploads?scope=`

The WORKSPACE's uploaded files eligible for ingest. Excluded: soft-deleted
rows, `hide_from_ai` rows, and any pocket-scoped upload (pocket files are
ACL-gated on the pocket surface and never list here). `has_article` is
derived cheaply: primarily the FL-11b tracking column matched against the
resolved scope; untracked rows fall back to a filename-vs-article-sources
match that only counts when the upload predates the matching article's
`compiled_at` — a fresh re-upload of a same-named file reads as pending,
not compiled.

```json
{
  "uploads": [
    {
      "id": "up-1",
      "filename": "report.pdf",
      "mime": "application/pdf",
      "size": 12345,
      "uploaded_at": "2026-08-01T10:00:00+00:00",
      "has_article": true
    }
  ],
  "total": 1,
  "scope": "workspace:w1"
}
```

## Platform — Discover Moderation

Staff routes for the Discover index under `/api/v1/platform/discover`
(`ee/pocketpaw_ee/cloud/platform/discover.py`). Like every `/platform` route they
need a platform role and an interactive session cookie; a bearer token or API key
is refused. The list is `platform.discover.read` (SUPPORT); every write is
`platform.discover.moderate` (OPERATOR), so SUPPORT gets `403`
(`platform.insufficient_role`) on them and a user with no platform role gets
`403` (`platform.not_operator`) everywhere.

Every write takes a body with a non-empty, free-text `reason` (`422`
`platform.discover.invalid_reason` when blank) and is recorded as a
`PlatformAuditEvent` (action `platform.discover.moderate`): written `attempted`
before the change, then settled `applied` or `failed`. A listing write records
`target_type: "discover_listing"`, the owner's workspace as `target_workspace`,
and the listing id plus the verb and prior flags in `before`. The list read is
recorded too, as `platform.discover.read`.

```json
{ "reason": "Spam reported by three users, confirmed" }
```

### `GET /api/v1/platform/discover` (SUPPORT)

Every listing, hidden ones included, newest first. Query params, all optional:
`source`, `hidden` (`true` / `false`), `featured` (`true` / `false`), `q` (as on the
public list), `cursor`, `limit` (1-200, default 50). Response `200`:
`{"items": [...], "next_cursor": ... | null}`, where each item is the staff view
(never served on a public route):

```json
{
  "id": "6660a1...", "slug": "bakery", "source": "site_template",
  "source_id": "665f1c...", "workspace_id": "w1", "owner": "u1", "kind": "site",
  "title": "Bakery",
  "description": "", "live_url": "https://bakery.pawsites.workers.dev",
  "featured": false, "hidden": true, "report_count": 3,
  "dismissed_reporter_count": 0, "remix_count": 3,
  "created_at": "2026-10-01T09:00:00Z"
}
```

### `POST /api/v1/platform/discover/{listing_id}/feature` · `/unfeature` (OPERATOR)

Sets `featured`; hidden listings can be featured too. Response `200`:
`{"id", "featured", "hidden", "audit_event_id"}`. `404` for an unknown listing,
with no audit row written.

### `POST /api/v1/platform/discover/{listing_id}/hide` · `/unhide` (OPERATOR)

Hide removes the listing from the public reads and hides the source item (a site
template leaves the /sites public list); its reports are kept. Unhide brings both
back, clears the reports and records their authors so their later reports on that
listing are ignored. Same response and `404` as feature.

### `POST /api/v1/platform/discover/reindex?source=site_template` (OPERATOR)

Rebuilds one source's listings now: upserts every public item (a hidden one as a
hidden listing) and removes listings whose item is gone or no longer public.
Idempotent. `source` defaults to `site_template`, the only source that supports
it; any other returns `422` (`discover.reindex_unsupported`). Response `200`:
`{"source", "created", "updated", "unchanged", "removed", "audit_event_id"}`; a
row is only written when something changed.

## Platform — Plan & Entitlement Overrides

Cross-tenant operator routes under `/api/v1/platform/workspaces/{workspace_id}/entitlements*`
(chunk 7 of the Paw Admin PRD, `ee/pocketpaw_ee/cloud/platform/entitlements.py`). These
sit on the platform authority axis, not workspace RBAC: `workspace_id` is a caller-supplied
path parameter (every route under `/platform` inverts the usual "workspace comes from the
session" rule), reads require the `platform.entitlements.read` action (SUPPORT rung),
writes require `platform.entitlements.write` (OPERATOR rung), and every call is recorded
as a `PlatformAuditEvent`.

Only seven fields can be overridden — `monthly_ceiling`, `max_seats`, `max_pockets`,
`max_connectors`, `max_call_seconds_per_day`, `max_storage_bytes`, `included_sites`.
These are exactly the fields `resolve_entitlements` enforces. Two catalog fields,
`monthly_credit_allotment` and `extra_features`, are deliberately absent: both are read
by their enforcement points straight off the plan catalog rather than through
`resolve_entitlements`, so an override on either would be stored and displayed but would
never change behavior. Each overridable field is a tri-state: omitted/`null` means "not
overridden", `"uncapped"` means "override to no limit", and an integer overrides to
exactly that value. An override set's `expires_at` is whole-set — once past, the entire
set reads back as absent, not per-field.

### `GET /api/v1/platform/workspaces/{workspace_id}/entitlements`

Returns the tenant's plan key alongside three views of the seven fields: `catalog` (the
plan alone, no override applied), `resolved` (what `resolve_entitlements` — and therefore
every enforcement path in the codebase — currently returns), and `overrides` (the raw
override document, or `null` if none is active). Keeping all three separate is deliberate:
a merged number can't tell an operator "the plan gives this" from "an override changed it
to that." Returns `404` if the workspace doesn't exist.

```json
{
  "workspace_id": "<id>",
  "plan": "free",
  "catalog": { "monthly_ceiling": 1000, "max_seats": 0, "max_pockets": 1, "max_connectors": 1, "max_call_seconds_per_day": 0, "max_storage_bytes": 104857600, "included_sites": 0 },
  "resolved": { "monthly_ceiling": 1000, "max_seats": 7, "max_pockets": 1, "max_connectors": 1, "max_call_seconds_per_day": 3600, "max_storage_bytes": 104857600, "included_sites": 0 },
  "overrides": { "monthly_ceiling": null, "max_seats": 7, "max_pockets": null, "max_connectors": null, "max_call_seconds_per_day": 3600, "max_storage_bytes": null, "included_sites": null, "expires_at": null }
}
```

### `PUT /api/v1/platform/workspaces/{workspace_id}/entitlements/overrides`

Replaces the workspace's entire override set (PUT, not PATCH — a field left off the body
is "not overridden," the same as sending it `null`). Requires a non-empty, free-text
`reason` (no canned options — a dropdown produces a log that says nothing); a missing or
whitespace-only reason returns `422` before anything is read or written. Accepts an
optional `idempotency_key` for the console to send on retry, but nothing in
`cloud/platform/` has dedup infrastructure to check it against yet, so it is not
persisted or enforced — a plain no-op safety net rather than a promise, documented as a
scoping decision in the module. Returns the same shape as the `GET`, recomputed after the
write. Records an `attempted` audit row before mutating and settles it to `applied` or
`failed`.

Request body:

```json
{
  "max_seats": 7,
  "max_call_seconds_per_day": 3600,
  "monthly_ceiling": "uncapped",
  "expires_at": "2026-12-31T00:00:00Z",
  "reason": "Comping a design-partner trial past the Free caps"
}
```

### `DELETE /api/v1/platform/workspaces/{workspace_id}/entitlements/overrides`

Clears the workspace's override set back to whatever the plan alone gives. Still a write:
requires the same non-empty `reason`, and is audited the same way as the `PUT`, including
when there was nothing to clear.

```json
{ "reason": "Trial ended" }
```

---

## Platform Wallet Credits

Cross-tenant routes under `/api/v1/platform/workspaces/{workspace_id}/credits*`
(`ee/pocketpaw_ee/cloud/platform/credits.py`). Every amount is in micro-credits
(1,000,000 micro is one credit). Reads need `platform.credits.read` (SUPPORT), writes
need `platform.credits.adjust` (OPERATOR), and each call leaves a `PlatformAuditEvent`.

### `GET /api/v1/platform/workspaces/{workspace_id}/credits` · `GET .../credits/history`

The wallet balance with `has_wallet` and `unapplied_count`, and the ledger newest first
(`cursor`, `limit` up to 200, exact-match `cause` filter). An id with no wallet reads as
an empty one; these two routes do not look the workspace up.

### `POST /api/v1/platform/workspaces/{workspace_id}/credits/adjust`

A signed `amount_delta_micro` (non-zero) with a required `reason` and
`idempotency_key`. Positive grants as `operator_grant`, negative claws back as
`operator_debit` and never takes the balance below zero (`402 credits.insufficient`).

```json
{ "amount_delta_micro": 375000, "reason": "Refund for a failed run", "idempotency_key": "case-7" }
```

### `POST /api/v1/platform/workspaces/{workspace_id}/credits/reconcile`

Repairs drift between the ledger and the stored balance. Needs a `reason`. Run it only
while the wallet is quiet: a grant or debit that races it can corrupt the balance.

Both writes return `404 workspace.not_found` when no workspace has that id, including a
malformed id such as `None`. The check runs before the audit row is opened, so a refused
call writes nothing. A soft-deleted workspace still resolves.

---

## Batch reads for the chat sidebar

Three endpoints that each replace a per-item GET the chat sidebar used to fan
out. Each item in a batch gets exactly what its single GET would have answered,
under the same auth and workspace scoping, and a failure on one item never fails
the rest. Duplicate items are read once.

### `POST /api/v1/uploads/grants`

Batch form of `GET /uploads/{file_id}/grant`. Any authenticated caller in the
active workspace; each file goes through the same scoped read as the GET.

```json
{ "items": [ { "id": "f1", "w": 64, "h": 64, "q": 80, "f": "webp" }, { "id": "f2" } ] }
```

`items` holds 1..200 entries. `w` and `h` are 0..2048 (default 0), `q` is 1..100
(default 80), `f` defaults to `"webp"`. Anything out of range is a 422.

```json
{ "grants": [
  { "id": "f1", "w": 64, "h": 64, "q": 80, "f": "webp",
    "url": "/api/v1/uploads/f1?w=64&h=64&q=80&f=webp", "expires_at": 1790000000 },
  { "id": "f2", "w": 0, "h": 0, "q": 80, "f": "webp", "error": "not_found" }
] }
```

`grants` has the same length and order as `items`. A thumbnail (`w` or `h` > 0)
always gets the cookie-authed server URL; a full-size file gets the storage
presigned URL when there is one, else `/api/v1/uploads/{id}`. A missing file or
one in another workspace is `"error": "not_found"` for that entry only.

### `POST /api/v1/paw-bar/admin/sites/conversations`

Batch form of `GET /paw-bar/admin/site/{site_id}/conversations`, with the same
`paw_bar.read` gate (workspace owner/admin).

```json
{ "site_ids": ["66f1…", "66f2…"], "limit": 20, "state": null }
```

`site_ids` holds 1..50 ids, `limit` is 1..100 (default 20), `state` is one of
`open | needs_human | snoozed | closed` or null. An unknown `state` is a 422
`invalid_state`, as on the GET.

```json
{ "sites": { "66f1…": { "items": [], "cursor": null, "unsupported": false, "counts": {} } },
  "errors": { "66f2…": "not_found" } }
```

Each entry in `sites` is the GET's first page (no cursor). A site that is
absent, malformed or in another workspace is `not_found`; any other failure is
logged and reported as `error`.

### `POST /api/v1/sessions/by-agents`

Batch form of `GET /sessions?agent_id=`, gated on `session.read_own`.

```json
{ "agent_ids": ["agent-a", "agent-b"] }
```

`agent_ids` holds 1..100 ids. The answer has every requested id as a key, with
the caller's own non-deleted sessions for that agent in the active workspace,
newest activity first, and `[]` when there are none. Each agent's list holds at
most its 100 most recent sessions. The read count does not grow with the number
of agents (one aggregation picks the ids, one query loads them).

### `GET /api/v1/sessions`

The caller's sessions in the active workspace, newest activity first, as a bare
JSON list. Query params: `agent_id` (one agent's DM sessions), `surface`
(`chat` also matches legacy rows with no surface), `limit` (default 200, 1..500)
and `cursor`. Without `agent_id`, a response that stopped at `limit` carries an
`X-Next-Cursor` header; send it back as `cursor` for the next page. The header
is not in the CORS `expose_headers` list, so a cross-origin browser client
cannot read it; the per-surface `GET /sessions/{chat,files,foresight,pocket-creation}`
endpoints return the cursor in the body.

`GET /api/v1/pockets/{id}/sessions` returns at most the 200 most recent threads.
`GET /api/v1/sessions/runtime` takes `limit` 1..500; `total` counts every row.

```json
{ "sessions": { "agent-a": [ { "id": "…", "sessionId": "…", "agent": "agent-a" } ], "agent-b": [] } }
```

## Workspace API key scopes

A workspace API key (`Authorization: Bearer paw_…`, minted at
`POST /api/v1/workspaces/{workspace_id}/api-keys`) only works on routes built
on `request_context`. Routes that need a signed-in session reject it with 401.

On those routes the key must hold the scope for the route family (the first
path segment after `/api/v1`) and method. `GET` and `HEAD` need the read scope.
Any other method needs the write scope. `POST /files/search` and
`POST /kb/search` only read, so they need the read scope.

| Family | Read (`GET`, `HEAD`) | Write (other methods) |
|---|---|---|
| `/chat/…` | `chat.read` | `chat.send` |
| `/files/…` | `files.read` | `files.write` |
| `/knowledge/…`, `/kb/…` | `knowledge.read` | `knowledge.write` |
| `/agents/…` | `agents.read` | `agents.write` |
| `/workspaces/…` | `workspace.read` | none: always 403 |
| `/audit/…` | `audit.read` | none: always 403 |

Any other family (tasks, cycles, projects, websandbox, sites, …) returns `403`
for every key. A missing scope returns `403` with code `api_key.missing_scope`
and names the scope. An unmapped route returns `403` with code
`api_key.route_not_allowed`. JWT and cookie sessions are not affected.

Today the only `request_context` routes in a mapped family are the file
version routes (`/files/{file_id}/versions…`, `/files/write`,
`PUT /files/{file_id}`). The other rows apply as routes move onto
`request_context`.

## Agent health (paw-lens proxy)

paw-lens is the internal trace store. The browser never calls it: these routes
proxy its read API, scoped to the caller's active workspace. Enterprise only.

| Route | paw-lens route |
|---|---|
| `GET /api/v1/lens/overview` | `GET /v1/overview` |
| `GET /api/v1/lens/issues?status=open\|muted\|resolved` | `GET /v1/issues` |
| `GET /api/v1/lens/issues/{fingerprint}` | `GET /v1/issues/{fingerprint}` |
| `POST /api/v1/lens/issues/{fingerprint}/mute` `{"minutes": n}` | `POST /v1/issues/{fingerprint}/mute` |
| `POST /api/v1/lens/issues/{fingerprint}/resolve` | `POST /v1/issues/{fingerprint}/resolve` |
| `GET /api/v1/lens/runs?agent_id=&automation=&status=ok\|error&limit=` | `GET /v1/runs` |
| `GET /api/v1/lens/runs/{trace_id}` | `GET /v1/runs/{trace_id}` |
| `GET /api/v1/lens/runs/{trace_id}/spans/{span_id}` | `GET /v1/runs/{trace_id}/spans/{span_id}` |
| `POST /api/v1/lens/runs/{trace_id}/overview?refresh=1` | `GET /v1/runs/{trace_id}` (+ spans), then `PUT /v1/runs/{trace_id}/overview` |
| `GET /api/v1/lens/agents` | `GET /v1/agents` |
| `GET /api/v1/lens/monitors` | `GET /v1/monitors` |
| `GET /api/v1/lens/monitors/{slug}` | `GET /v1/monitors/{slug}` |

GET routes accept `since`. `overview`, `issues`, `agents` and the runs list
also take `agent_id` (must match `[A-Za-z0-9_-]{1,64}`) to narrow to one agent.
The runs list (newest first) filters by `automation` (a monitor slug,
`<kind>:<id>`), `status` (`ok` or `error`) and `limit` (1 to 200, paw-lens
defaults to 50); a value outside those rules is a `422` and no upstream call.
The span route returns one span's attributes, events, gen_ai messages and tool
call. The proxy adds `workspace_id` from the session to
every upstream call and drops any `workspace_id` the client sends. Response
bodies are paw-lens's JSON, unchanged for workspace admins.

**Content privacy.** Only a workspace admin or owner (`lens.manage`) sees message,
tool and error content. For anyone else every read (overview, agents, issues,
runs, run, span, monitors) goes through one redaction: run `summary`, span-list
`args_preview`, every span `error` and every monitor check-in `error` become
`""` (the `status` beside each still says it failed); the run's AI `overview`
and span `messages` become `null`; `tool` keeps only `name` and `call_id`
(`arguments` and `result` are `null`); `findings` keep `fingerprint`,
`detector`, `severity`, `tool`, `span_id` and `seen_at`, with `message` `""`
and `evidence` `null`; span `events` become `[]`; and span `attributes` are cut
to an allowlist (`gen_ai.usage.*`, `gen_ai.request.model`,
`gen_ai.response.model`, `gen_ai.operation.name`, `gen_ai.tool.name`,
`gen_ai.agent.name`, `operation.cost`, `paw.*`, `http.method`, `http.route`,
`http.status_code`, `http.request.method`, `http.response.status_code`,
`db.system`); every other key is dropped. Issue `title`s become
`"<detector> · <tool>"` (or just the detector), since paw-lens builds them
from error text or user feedback. A log span's `name` (its formatted message)
becomes its `logfire.msg_template` when logfire extracted one, else `"log"`.
Object bodies gain `"content_hidden": true`; list bodies are stripped without
the flag. The `pocketpaw_lens` agent tools apply the same rule.

**Chat surface.** A chat send with `surface: "agent_health"` (the
`/agent-health` pages; `meta.run_id` is the run the user has open, echoed only
when it is a 32-hex trace id) gets a preamble pointing the agent at the
`pocketpaw_lens` tools: `lens_overview`, `lens_runs`, `lens_run` (pass
`span_id` for one span), `lens_issues` and `lens_monitors`. They are read-only,
always scoped to the chat's workspace, and exist on this surface only: the
server is in `SURFACE_SCOPED_MCP_SERVERS`, so no other chat registers it.

**AI overview.** `POST /runs/{trace_id}/overview` (admin only, else `403`)
returns `{text, model, created_at}`. When the run detail already carries an
`overview` it is returned as is, with no LLM call, unless `?refresh=1`; even
then a cached overview less than 60 s old is returned as is. Concurrent
requests for the same run on one server share a single generation.
Otherwise the proxy builds a capped digest of the run (the user prompt,
assistant turns, each tool call with its arguments and result or error, the
findings, tokens, cost and duration), asks the workspace's agent backend for
3 to 6 bullets (what was asked, what the agent did, what failed and why, cost
and latency notes, one suggested fix), stores it in paw-lens and returns it.
That LLM call is stamped `paw.internal=true`, so paw-lens does not record it as
a run. The digest holds user input and tool output, so the call runs with no
tools and no MCP servers (`tools_enabled=False`; a backend that cannot honour
that is refused with the same `503`) and the digest is fenced in
`<trace_data>` as untrusted data the model must not follow. The call has a 60 s budget; an LLM error, timeout or empty reply is
`503 lens.overview_failed`.

- `POCKETPAW_LENS_API_URL` unset: every route returns `200 {"enabled": false}`
  and makes no network call.
- `POCKETPAW_LENS_API_TOKEN` is sent as `X-Lens-Token` and never logged.
- Path params and `automation` must match `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`,
  otherwise `422`.
- paw-lens down, slow (3 s timeout) or erroring: `503 lens.unavailable`.
  paw-lens rejects the token: `503 lens.misconfigured`. paw-lens 404: `404
  lens.not_found`. paw-lens 400: `400 lens.bad_request`.
- Mute, resolve and the overview need a workspace admin or owner
  (`lens.manage`); a member gets `403 workspace.insufficient_role`. Reads are
  open to any member, with content redacted as above.

### Monitors: check-ins and run attribution

Scheduled work checks in to paw-lens (`POST /v1/checkins`, `X-Lens-Token`)
through `pocketpaw.lens_checkins.automation_run`: `in_progress` when a run
starts, then `ok` or `error` (scrubbed, at most 500 chars). The monitor slug is
`<kind>:<id>`; the first check-in registers it with its schedule (`crontab` or
`interval_seconds`). Posts run in the background with a 1 s budget and never
fail or delay the job. `POCKETPAW_LENS_API_URL` unset: no check-ins are sent.
The run opens a current span named `automation <kind>:<id>`, so everything the
tick does is one trace; with Logfire on, both check-ins carry that trace's
`trace_id` (32 lowercase hex) so paw-lens can link a check-in to its trace.
With Logfire off the field is omitted.

| Kind | What checks in | Id |
|---|---|---|
| `reminder` | reminders; meeting reminder jobs | reminder id; `meeting_reminder` |
| `intention` | cron and stale-session intentions | intention id |
| `heartbeat` | Mission Control heartbeat | job id |
| `automation_rule` | each automation rule fire | rule id |
| `mandate` | mandate autopilot cycles | mandate id |
| `sweep` | each cloud sweep iteration (`_core/periodic.sweep_tick`) | sweep name |
| `job` | arq jobs and crons (not chat runs), meeting auto-start/end, self-audit | function name |

Each run also stamps `paw.workspace_id`, `paw.automation.kind` and
`paw.automation.id` on its spans as attributes (a span processor, not OTel
baggage, so nothing goes out in a `baggage` header). Interactive chat runs
carry `paw.workspace_id` and `paw.agent.id` (the id of the agent serving the
run). A cancelled run (shutdown) posts no final
check-in; set `max_runtime_s` to have paw-lens time it out.

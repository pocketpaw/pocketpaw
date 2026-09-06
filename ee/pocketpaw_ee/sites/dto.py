# ee/pocketpaw_ee/sites/dto.py — request/response DTOs for the Sites control
# plane. Distinct request and response shapes per the cloud 4-file rules.
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).
#
# Updated 2026-09-04 (AD-4 — the overview chart's data): added
# ``SiteAnalyticsSeriesPoint`` / ``SiteAnalyticsSeries`` and ``SiteAnalyticsResponse.series``,
# the per-bucket views and visitors the chart under the headline numbers draws. Two things
# about it are contracts rather than implementation detail. EVERY bucket in the range is
# present, including the ones nobody visited, because a chart handed only the buckets that
# had traffic draws a straight line across a quiet Sunday and reports it as no Sunday at
# all. And every ``bucket`` stamp is UTC, said out loud on the model, because an axis
# labelled ``1PM`` that means UTC is five and a half hours wrong for a reader in Mumbai and
# nothing on the screen would say so.
#
# Updated 2026-09-04 (AD-2 — the visit metrics on the wire): added
# ``SiteAnalyticsVisits`` and ``SiteAnalyticsResponse.visits``, the visits / bounce rate /
# visit duration block the overview leads with. It carries this model's absence rule
# further than the metrics above it needed to: a bounce rate over zero visits and a mean
# duration over zero measurable visits are both None rather than 0.0, because a ratio of
# nothing is not a result of nothing. And the block ITSELF is None, with ``"visits"`` named
# in ``unrecorded``, when every row in the window was written before the counter learned to
# stamp a visit id — the FOURTH empty state, which is the device class's problem one metric
# over.
#
# Updated 2026-09-02 (SA-4 — the visitor-analytics read): added
# ``SiteAnalyticsResponse`` / ``SiteAnalyticsBreakdown`` and the three
# ``ANALYTICS_STATUS_*`` constants, backing GET /sites/{site_id}/analytics. The
# shape is a discriminated one because the alternative renders three different
# customer situations as the same panel of zeros: not on a plan that buys
# analytics, on one but never republished since, and genuinely no traffic. Every
# metric is None unless the status says the numbers are real, and a FAILED read is
# not a status value at all — it is an error response, so an outage can never arrive
# looking like a quiet week.
#
# Updated 2026-09-02 (SA-5 — the entitlement on the wire): added
# ``SiteEntitlementsResponse.analytics``, resolved by the same predicate the publish
# seam and the read endpoint use. Without it the dashboard's only way to learn that
# a site's plan excludes analytics was to request the numbers and read the refusal,
# which is the exact shape this response type was created to stop.
#
# Updated 2026-08-24 (SP-2 — draft preview joins the ephemeral build lane):
# ``NativeArtifactResponse`` gained ``build_status`` / ``build_reason`` /
# ``build_job_id`` and defaulted ``body_html`` / ``css`` to empty strings. A cold
# native-artifact read no longer builds in the request — it queues a sandbox build and
# returns a handle — so the model now carries both shapes. The status vocabulary is the
# publish lane's, reused verbatim rather than re-minted: a client already treats an
# unrecognised ``build_status`` as in-progress, and a second set of names for the same
# idea is how a failure comes to read as progress.
#
# Updated 2026-08-12 (sites Settings consolidation — the client record gets a
# backend): added ``SiteClientResponse`` / ``SiteClientUpdate`` / ``SiteInvoiceOut``
# / ``SiteInvoiceCreate``, backing GET+PATCH /sites/{site_id}/client and
# POST /sites/{site_id}/invoices. The builder's Settings surface had held the
# owner's client details and manual receipts in COMPONENT STATE with a comment
# saying persistence was a later task, so every value typed there was gone on
# reload and on a site switch — the panel demonstrated its own contract without
# honouring it. ``SiteClientUpdate`` is three-way (absent ≠ empty) so a partial
# edit cannot blank a field the caller never sent, and money is integer minor
# units end to end so no receipt is ever a float on the wire.
#
# Updated 2026-08-10 (SL-3 — the build lane reaches the wire): added
# ``SiteResponse.build_reason`` and put all three build fields
# (``build_status`` / ``build_reason`` / ``build_job_id``) on ``SiteStatusResponse``
# too.
#
# SG-9i DECLARED ``build_status`` AND ``build_job_id`` ON ``SiteResponse`` AND NOTHING
# EVER POPULATED THEM. ``service._to_response`` builds the DTO field by field and never
# passed either, so every response carried the field DEFAULTS: ``build_status`` was
# frozen at "none" for every site, no matter what the row said, and there was no
# ``build_reason`` field at all. The frontend build-status UI reads all three — so it was
# polling a value that could not change, which looks exactly like a build that never
# starts. The populating half is in ``service.py``; this file only ever declared the
# shape, which is why the gap survived a review: both halves looked complete alone.
#
# Updated 2026-06-01 (Phase 3 — local fake-deploy): SiteResponse carries ``url``,
# the deployed site's openable address. LOCAL mode returns the localhost URL the
# per-site static server serves so the caller (and the cmux smoke) can open the
# published site directly. Empty in the CF path in v1 (reached via custom
# domain). The frontend Site type mirrors this in Phase 4.
#
# Updated 2026-06-17 (pocketpaw#1345 backend half — by-pocket preview + status):
# added SitePreviewResponse and SiteStatusResponse, the two by-pocket read DTOs
# the #432 frontend already calls (getSitePreviewByPocket / getSiteStatusByPocket
# in core/sites/api.ts). Field names/types mirror the frontend types.ts EXACTLY:
# preview is {pocket_id, engine, content} (content optional — absent when nothing
# drafted; a rippleSpec for engine="ripple", a {path: contents} source map for
# engine="svelte"); status is {pocket_id, status, is_live} plus the optional
# site_id the frontend type also declares. Without these, every Preview-tab fetch
# 404'd and the builder showed "Nothing to preview yet".
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): added
# MakeEditableRequest (the body for POST /sites/by-pocket/{pocket_id}/editable;
# builder_origin optional) and SiteResponse.builder_origin so the UI can tell
# whether a published site carries the edit-bridge (non-empty = editable).
# Updated 2026-06-18 (feat/branch-primitive-sites-draft, BP-2 / pocketpaw#1345):
# SiteStatusResponse gains ``has_unpublished_changes`` — the Branch primitive now
# derives draft/published from the version pointers (versions.get_draft /
# get_published), so a pocket can carry a draft NEWER than its published version.
# This flag (default False, backward-compatible) lets the builder badge "has
# unpublished edits" without inferring it from the Site doc. ``status``/``is_live``
# semantics are unchanged in shape; only their derivation moves onto versions.
# Updated 2026-06-18 (feat/sites-stable-identity, PERF-1): SiteStatusResponse gains
# ``url`` — the canonical live address of the pocket's deployed site. With stable
# per-pocket identity (one Site doc per pocket) pocket_status reads the ONE
# canonical doc and surfaces its non-null url, so the builder/gallery link to the
# address the latest build actually serves at instead of a stale dupe's url=None.
# Optional (default None, backward-compatible) — None when no deployed site exists.
# Updated 2026-06-18 (feat/branch-primitive-revert-history, BP-4): added two DTOs
# for the Branch-primitive surfaces — SiteVersionResponse + VersionHistoryResponse
# (the ordered version timeline GET /sites/by-pocket/{pocket_id}/versions returns)
# and RequestPublishResponse (the Action created by POST
# /sites/by-pocket/{pocket_id}/request-publish, the clean entry to the merge gate
# so the client never hand-builds the Instinct proposal).
# Updated 2026-06-18 (feat/branch-primitive-audit, BP-7): added AuditFinding +
# AuditResponse — the response shape for POST /sites/by-pocket/{pocket_id}/audit
# (the first non-editor PRODUCER). Each finding carries a ``fix_prompt`` the UI
# feeds to the EXISTING edit path so the fix lands as a reviewable draft; there is
# NO new apply endpoint (BP-7 reuses edit_svelte_component / refine).
# Updated 2026-06-19 (P2b-backend — "Last Deployed"): SiteResponse and
# SiteStatusResponse gain ``deployed_at`` — the ISO-8601 string of the pocket's most
# recent successful live deploy (the Site doc's ``deployed_at``), or None before the
# first deploy. The builder/gallery surface a "Last deployed <time>" label without a
# second fetch. Optional (default None) so the field is backward-compatible.
# Updated 2026-06-20 (DS-1a — surface dynamic-site pattern): SiteResponse and
# SiteStatusResponse gain ``pattern`` — the SOURCE pocket's authoring pattern
# ("dynamic" for a live-data site, "landing" for a marketing page, "" / other for
# the rest). It lives on ``Pocket.pattern``, not the Site, so the service resolves
# it from the source pocket (sites/service.py: patterns_for_pockets) per list +
# status response. The frontend uses it to badge dynamic sites in the gallery.
# Default "" so the field is backward-compatible and empty-safe (a pocket with no
# pattern, or a missing/cross-tenant pocket, reads "").
# Updated 2026-06-20 (DS-3 — read a dynamic site's D1 data): added the read-only
# DTOs the operator data-view (DS-4 FE) consumes:
#   * SiteDataTableInfo — one declared table {name, fields, primary_key} from the
#     dynamic pocket's spec ``objects``. Always available (it comes from the spec),
#     even when the live D1 data is not reachable.
#   * SiteDataTablesResponse — the response of
#     GET /sites/by-pocket/{pocket_id}/data: {pocket_id, available, reason, tables}.
#     ``available`` is False with ``reason="live_on_cloudflare_only"`` in local/dev
#     mode (no live D1 to read), but ``tables`` is still listed from the spec so the
#     UI degrades cleanly instead of erroring.
#   * SiteDataRowsResponse — the response of
#     GET /sites/by-pocket/{pocket_id}/data/{table}: {pocket_id, table, available,
#     reason, columns, rows}. ``rows`` is the D1 rows (capped); ``columns`` is the
#     table's declared field names. In local mode ``available`` is False and ``rows``
#     is empty, but ``columns`` is still listed from the spec.
# These are READ-ONLY views (no request DTO — the inputs are path params); the
# data view never writes through this surface.
# Updated 2026-06-24 (feat/charge-first-sites): ``SiteResponse`` gains
# ``checkout_url`` — the Dodo annual-checkout link a PAID-tier publish returns.
# A paid publish defers the live deploy: it creates the site as PENDING
# (deployed=False) and returns this link the caller redirects the buyer to; the
# site deploys + goes live only when the ``subscription.active`` webhook confirms
# payment. None for a free/base publish (which deploys immediately) and for any
# non-publish response (default None, backward-compatible).
# Updated 2026-06-24 (S2 review fix): ``DomainRequest.hostname`` now carries a
# permissive DNS-hostname validator (label-dot-label, letters/digits/hyphens, no
# leading/trailing/double dots, total length capped) so an obviously-malformed
# host is rejected at the DTO (422) before it reaches Cloudflare. Kept permissive
# — it accepts any real registrable hostname, it only blocks garbage.
# Updated 2026-07-01 (NE-4b — native-editing leaf-edit persist): added the request
# / response models for POST /sites/by-pocket/{pocket_id}/leaf-edits — LeafEdit
# ({uid, op}), LeafEditsRequest ({edits}), LeafEditVerdict ({uid, applied, reason?})
# and LeafEditsResponse ({pocket_id, results}). The native editor forwards its
# already-rendered {uid, op} edits and the endpoint returns one verdict per edit.
# ``op`` rides as an open dict — its {kind:setText|setProp,...} shape is validated
# downstream by the paw-sites apply-leaf-edit CLI, not at this DTO boundary.
# Updated 2026-07-09 (DP0-4 — publish async split): ``SiteResponse`` gains
# ``provision_status`` (none | provisioning | provisioned | failed) and
# ``provision_job_id``. A DYNAMIC-site publish no longer deploys inline — it enqueues
# the durable ``provision_site`` job and returns immediately with
# ``provision_status="provisioning"`` / ``deployed=False`` and the enqueued job id;
# the site goes live only when the job finalizes. Both default to "none" / None so a
# static publish and every non-publish response stay backward-compatible.
# Updated 2026-07-01 (NE-5b — native-artifact endpoint): added NativeArtifactResponse
# ({pocket_id, body_html, css}) — the response of GET
# /sites/by-pocket/{pocket_id}/native-artifact. ``body_html`` is the armed svelte
# build's ``<body>`` inner HTML (the data-uid-stamped leaves + the embedded
# ``paw-edit-manifest`` script); ``css`` is the built stylesheet(s) concatenated into
# one string. The native editor injects both into a shadow root to render the site
# natively instead of framing an iframe.
# Updated 2026-07-09 (SR-9 — surface each site's ENGINE): ``SiteResponse`` and
# ``SiteStatusResponse`` gain ``engine`` ("svelte" | "ripple"; "" when unresolved) —
# the sibling of DS-1a's ``pattern``, resolved from the source Pocket.engine so the
# gallery can badge each card's engine (Custom vs Ripple) without a per-site fetch.
# Updated 2026-07-22 (SI-4 — feat/sites-import-endpoint): ``SiteResponse`` gains
# ``import_report`` (the persisted import summary — None for non-imported sites),
# and two new DTOs back the import surface: ImportFromUrlRequest ({url}, shape-
# validated in the service) and ImportFromUrlResponse ({site_id, pocket_id,
# status:"queued"} — the 202 body; the crawler is the next stacked slice). The zip
# import endpoint reuses SiteResponse (it publishes live through the html path).
# Updated 2026-08-07 (SC-1 — a site's card shows its own screenshot):
# ``SiteResponse`` (which IS the gallery list item — GET /sites is
# ``response_model=list[SiteResponse]``) and ``SiteStatusResponse`` gain
# ``preview_image_url`` — the stored URL of a screenshot of the site's live page,
# resolved from the Site doc's own field. The sibling of ``pattern`` / ``engine``
# in role: one more thing the card needs that would otherwise cost a per-card
# fetch. None when no screenshot has landed (never deployed, no public url,
# capture failed, Cloudflare unconfigured) — the card then falls back to its text
# layout, so the field is optional, empty-safe and backward-compatible.
# Updated 2026-08-07 (SC-3 — the card stops lying after a republish): the refresh
# POLICY is now written onto both ``preview_image_url`` fields instead of being
# inferrable only from where the capture is called — re-captured on every
# successful deploy (so a republish updates the card), plus an explicit refresh, no
# TTL, and a different uploads link every capture. New DTO
# ``SitePreviewRefreshResponse`` ({site_id, preview_image_url}) backs that explicit
# path, POST /sites/{site_id}/preview-refresh. It is deliberately its own response
# rather than a reused ``SiteResponse``: the call answers one question ("what is
# the new picture"), and unlike every deploy-triggered capture it REPORTS failure
# — a person asked for it and is waiting on the answer.
# Updated 2026-09-02 (the card's MARK): both DTOs gain ``favicon_url`` — the site's
# own icon, for the chip that had been a hard-coded globe on every card alike. It sits
# beside ``preview_image_url`` and answers a different question ("whose site is this"
# rather than "what does it look like"), and it differs from it in one way worth
# knowing before reading either field: this one carries a data: URI INLINE rather than
# an uploads link. An icon is a few KB, so paying for a blob row and a per-card auth
# grant to serve it would cost more than the bytes; ``sites.favicon`` holds the cap
# that keeps a list response bounded and drops anything over it. None whenever the
# site declares no icon we can use, so the card falls back to the globe.

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, field_validator


class PublishRequest(BaseModel):
    # ``site_plan_key`` (BC-10) is the OPTIONAL per-site plan tier the site is
    # published on. Only the SITE-SCOPED rungs are valid here (free | site | staff
    # — see ``billing.site_plans``); an org flat (studio | agency) covers a whole
    # workspace and ``_apply_site_plan`` refuses it rather than stamping it on one
    # site. Legacy names (basic | pro | business) still resolve. Omitted defaults
    # to the base tier in ``publish_pocket``; a higher tier resells its Cloudflare
    # features when a custom domain is later added.
    pocket_id: str
    site_plan_key: str | None = None


class SitePlanRequestBody(BaseModel):
    """Body for POST /sites/plan-requests — ask an admin to put a site on a paid
    plan. Same two fields a publish takes, because it describes the same
    purchase; the difference is who is allowed to make it happen."""

    pocket_id: str
    site_plan_key: str


class SitePlanRequestResponse(BaseModel):
    """Ack for a filed site-plan request.

    ``action_id`` is the pending Instinct Action an admin approves in The Tray —
    returned so a client can link straight to it rather than telling the employee
    to go looking. Deliberately carries NO site fields: nothing was published and
    nothing was charged, and a SiteResponse here would read like it had been."""

    action_id: str
    status: str
    site_plan_key: str
    monthly_price_usd: int


class MakeEditableRequest(BaseModel):
    """Body for POST /sites/by-pocket/{pocket_id}/editable (SE-2b). The builder
    origin is optional — the service falls back to the configured dashboard
    origin (PAW_SITES_BUILDER_ORIGIN) when it is omitted, so the body may be
    empty."""

    builder_origin: str = ""


class SiteResponse(BaseModel):
    id: str
    pocket_id: str
    name: str
    script_name: str
    deployed: bool
    signed_key: str
    url: str = ""
    # SE-2b: the builder origin the site was published with, or "" for a normal
    # (non-editable) site. Non-empty means the page carries the edit-bridge.
    builder_origin: str = ""
    # P2b: ISO-8601 timestamp of the most recent successful live deploy, or None
    # before the first deploy (a preview-only / never-deployed pocket reads None).
    deployed_at: str | None = None
    # ISO-8601 timestamp of when the Site row was created — the doc's ``createdAt``,
    # which every TimestampedDocument has carried since day one. This only surfaces
    # it.
    #
    # It exists because ``deployed_at`` above is None for every DRAFT, and
    # draft-first create means most of a real workspace is drafts. The gallery
    # orders by "most recent", so with only ``deployed_at`` on the wire the entire
    # draft population had NO ordering key and fell through to alphabetical — a site
    # created a minute ago rendered under "About". None only for a doc that somehow
    # predates the base model's field, so the client still degrades to name order
    # rather than crashing.
    created_at: str | None = None
    # DS-1a: the source pocket's authoring pattern ("dynamic" | "landing" | ...),
    # resolved from Pocket.pattern (it lives on the pocket, not the Site). "" when
    # the pocket has no pattern or could not be resolved. Lets the frontend badge
    # dynamic sites in the gallery without a second fetch.
    pattern: str = ""
    # SR-9: the source pocket's authoring engine ("svelte" | "ripple"), resolved
    # from Pocket.engine (it lives on the pocket, not the Site) — the sibling of
    # ``pattern`` above. Lets the gallery badge each card's engine (Custom vs
    # Ripple) without a second per-site fetch. "" when the pocket predates the
    # engine field or could not be resolved (the card shows no engine badge).
    engine: str = ""
    # charge-first: the Dodo annual-checkout link for a PAID-tier publish. A paid
    # publish creates the site as PENDING (deployed=False) and returns this link
    # the caller redirects the buyer to; the site deploys + goes live only after
    # the ``subscription.active`` webhook confirms payment. None for a free/base
    # publish (which deploys immediately) and for any non-publish response.
    # DP0-4: where a dynamic site sits in the durable D1 provision job
    # ---- per-site billing state (BC-9) -------------------------------------
    # The frontend has declared and branched on these since BC-9: SiteSummary types
    # all three, and the [siteId] page gates its "awaiting checkout" bar on
    # ``subscription_status === "pending"``. None of them was ever mapped onto the
    # wire, so every read came back ``undefined``, every fallback took the "no
    # per-site sub" branch, and a paid site rendered identically to a free one. The
    # optional ``?`` on the TS fields is what kept it quiet — nothing throws when a
    # field that is always absent is always absent.
    #
    # All three live on the Site document already, so sending them costs no query.
    #
    # ``plan_tier`` is "" rather than None when unstamped, so "this site has no
    # tier" and "the backend did not send a tier" stay distinguishable on the wire.
    plan_tier: str = ""
    subscription_status: str = "none"
    renewal_date: str | None = None
    # WHAT THIS PERIOD HAS ALREADY BEEN PAID FOR, in whole USD, so the builder can
    # show what switching plans actually costs instead of the tier's sticker price.
    #
    # The two numbers differ for every move except a first purchase: an upgrade
    # charges only the difference against this, and a downgrade charges nothing.
    # Without it on the wire the plan cards can only show the full monthly price,
    # which for a site already on a paid rung is never the amount that will be
    # taken — so the one screen where a customer is deciding how to spend money
    # quotes them a figure the publish will not charge.
    period_paid_usd: int = 0
    # Is this plan already scheduled to close at ``renewal_date``?
    #
    # Without it the Billing tab cannot tell the two states apart: a site running
    # normally and a site whose last month is running out both read as an active
    # paid plan with a renewal date, because that is exactly what they are. The
    # difference is whether that date is the next charge or the end — and showing
    # "renews on the 14th" to somebody who cancelled is how a cancellation gets
    # made twice, or gets escalated as one that did not take.
    plan_cancels_at_period_end: bool = False
    # (none | provisioning | provisioned | failed). A DYNAMIC-site publish does NOT
    # deploy inline — it enqueues the ``provision_site`` job and returns immediately
    # with ``provision_status="provisioning"`` (``deployed=False``); the site goes
    # live only when the job finalizes. "none" for a static site (deploys inline).
    provision_status: str = "none"
    # DP0-4: the id of the ``provision_site`` job a dynamic publish enqueued, so the
    # caller can poll the job. None for a static publish, and None on a single-flight
    # no-op (a second publish while already provisioning does not enqueue a job).
    provision_job_id: str | None = None
    # ── SG-9i: the ephemeral-build lane's state, on the wire ────────────────
    # ``build_status`` — none | queued | building | built | failed.
    #
    # ``queued`` is the reason this pair exists at all. Once builds run in an ephemeral
    # sandbox behind a concurrency cap, a publish can WAIT before it starts, and a
    # queued build is indistinguishable from a hung one unless the wire says which it
    # is. Without this the cap converts a crash into a support ticket.
    #
    # A client MUST treat an unrecognised status as in-progress rather than as an
    # error: this vocabulary will grow, and a client that errors on unknown values
    # turns every future state addition into a visible outage.
    build_status: str = "none"
    # The build job's id, so a caller can poll it. Unlike ``provision_job_id`` — which
    # comes from a transient PrivateAttr and therefore only exists on the response that
    # enqueued it — this is read from a PERSISTED field, so a client that reloads mid-
    # build still gets it. That matters precisely because a queued build is the case
    # where the user reloads.
    build_job_id: str | None = None
    # SL-3: WHY the build reached ``build_status`` — a fixed ``"<rung>:<cause>"``
    # identifier from ``sites/build_job.py`` (e.g. ``build_failed:install_failed``,
    # ``infra_lost:build_killed_by_signal_137``), read from the persisted
    # ``Site.build_reason``. None when no build has settled.
    #
    # WITHOUT THIS ON THE WIRE, ``build_status="failed"`` IS UNACTIONABLE, which is the
    # exact failure the field was added to prevent: the row can say a build failed and
    # nothing can say whether the user's code broke or we lost the container — and those
    # two need opposite responses from whoever is looking at it.
    #
    # SAFE TO SURFACE, and that is a property of the WRITER, not of this field: the
    # vocabulary is closed on both halves and the build's stderr never enters it (a
    # build's error text is the user's own code and can carry a token pasted into a
    # config). A client may split on the colon to group by rung; it must not assume the
    # set of rungs is closed forever, for the same reason it must not error on an
    # unrecognised ``build_status``.
    build_reason: str | None = None
    # SI-4: the persisted import summary for an IMPORTED site — {pages: [{path,
    # title}], asset_count, asset_bytes, forms: [{page, original_action, rewired}],
    # scripts, warnings} (from-url adds status/source_url). None for every
    # non-imported site, so the field is backward-compatible and empty-safe.
    import_report: dict[str, Any] | None = None
    # SC-1: the stored URL of a screenshot of this site's live page (an uploads
    # link, e.g. "/api/v1/uploads/{id}"), read from the Site doc. This is what
    # turns a gallery card from a title and three pills into a picture of the
    # actual page. Written by a best-effort background capture scheduled from the
    # tail of a successful deploy, so it is None until one lands — and None
    # whenever the capture failed, the site has no public url yet, or Cloudflare
    # Browser Rendering is unconfigured. The card falls back to its text layout on
    # None, so the field is optional and backward-compatible.
    #
    # SC-3 — WHEN THIS CHANGES (the refresh policy, written down so a reader does
    # not have to infer it from behaviour):
    #   * on EVERY successful deploy, including a republish. A deploy is the only
    #     moment the design is known to have changed, it is user-initiated and
    #     already slow, and it is the only trigger with no staleness window. There
    #     is no TTL and no "only if empty" guard — the republish case is exactly
    #     the one where a picture exists and shows the wrong design.
    #   * on an explicit request to POST /sites/{site_id}/preview-refresh, for the
    #     cases a deploy cannot cover (a capture that failed, a deployment that was
    #     unconfigured then, a draft whose markup only became buildable later).
    # The value is a DIFFERENT uploads link every capture (each one mints its own
    # uuid-keyed row), so a client may treat a changed value as new art and a
    # cached one as unchanged — nothing ever overwrites bytes behind a stable URL.
    preview_image_url: str | None = None
    # 2026-09-02: the site's own icon, as a data: URI, for the gallery card's mark
    # chip. Until now that chip was a hard-coded globe tinted by a hash of the site
    # id, so every card in a gallery wore the same glyph.
    #
    # A data: URI and not an uploads link, which is the one way this field differs
    # from ``preview_image_url`` directly above. A screenshot is a 1280x800 PNG and
    # needs blob storage behind the auth-gated ``/api/v1/uploads/{id}``, which is
    # why the card resolves that one through a per-card grant. An icon is a few KB,
    # so it rides on the wire and the card can paint it with no second request —
    # see ``sites.favicon`` for the cap that keeps a list response bounded.
    #
    # WHEN IT CHANGES: looked up on every successful deploy (a republish included,
    # for the reason the screenshot policy above gives) and from the tail of a draft
    # capture. None whenever the site declares no icon we can use, the page could
    # not be read, or the icon was over the cap; the card falls back to the globe,
    # which is exactly the pre-existing card, so this is never a gate on anything.
    favicon_url: str | None = None


class SitePreviewRefreshResponse(BaseModel):
    """The result of an explicit preview re-capture (SC-3) —
    POST /sites/{site_id}/preview-refresh.

    ``preview_image_url`` is the NEWLY stored image, never the previous one: the
    endpoint only answers 200 once a capture has landed and been recorded on the
    Site. Everything else (Cloudflare unconfigured or refusing, a draft with no
    buildable markup) surfaces as an error, because unlike the deploy-triggered
    capture this one was asked for by a person who is watching — the never-fail
    rule protects publishes, not this call.
    """

    site_id: str
    preview_image_url: str


class SitePreviewResponse(BaseModel):
    """Draft content to render in the in-app builder Preview tab. ``engine``
    selects the shape of ``content``: "ripple" → a rippleSpec object; "svelte" →
    a {path: contents} source map. ``content`` is None when there is nothing
    drafted yet. Mirrors the frontend SitePreviewResponse (core/sites/types.ts)."""

    pocket_id: str
    engine: str
    content: dict[str, Any] | None = None


class DevPreviewResponse(BaseModel):
    """The live Vite dev-server URL for a pocket's EDITING preview (Phase 2 / P2a).
    ``url`` is a localhost address (http://127.0.0.1:<port>/) the builder iframe
    frames so edits hot-reload over Vite HMR in ~ms, instead of a full per-edit
    rebuild. This is the editing preview only — publish still does the full prod
    build + workerd smoke."""

    pocket_id: str
    url: str


class SiteStatusResponse(BaseModel):
    """Authoritative draft/published + is_live state for a pocket, so the builder
    labels accurately even before the site appears in the gallery list. ``status``
    is "draft" (no published version) or "published"; ``is_live`` is the ONLY
    signal that earns a "Live" badge — it requires a published version AND a real
    successful deploy (the Site doc's ``deployed``), never an optimistic stamp.
    ``has_unpublished_changes`` is True when a draft version is newer than the
    published one (edits the publish would ship). ``site_id`` carries the deployed
    Site's id when one exists. Mirrors the frontend SiteStatusResponse
    (core/sites/types.ts); ``has_unpublished_changes`` defaults False so the field
    is backward-compatible for callers that do not yet read it."""

    pocket_id: str
    status: str  # draft | published
    is_live: bool
    has_unpublished_changes: bool = False
    site_id: str | None = None
    # PERF-1: the canonical live url of the pocket's deployed site (the address the
    # latest build serves at). None when no deployed site exists.
    url: str | None = None
    # P2b: ISO-8601 timestamp of the pocket's most recent successful live deploy,
    # read from the canonical Site doc's ``deployed_at``. None when the pocket has
    # never been deployed (no Site doc, or a pre-P2b row that predates the field).
    deployed_at: str | None = None
    # DS-1a: the source pocket's authoring pattern ("dynamic" | "landing" | ...),
    # resolved from Pocket.pattern. "" when the pocket has no pattern. Same field
    # the list response carries, so a by-pocket status read can badge a dynamic
    # site too.
    pattern: str = ""
    # SR-9: the source pocket's authoring engine ("svelte" | "ripple"), resolved
    # from Pocket.engine — the same field the list response carries, so a by-pocket
    # status read can badge the engine too. "" when unresolved / pre-engine row.
    engine: str = ""
    # SC-1: the stored URL of a screenshot of the pocket's live site — the same
    # field the list response carries, so a by-pocket status read can show the
    # page too. None until a capture lands (and whenever one failed, the site has
    # no public url, or Cloudflare Browser Rendering is unconfigured).
    #
    # SC-3 — same refresh policy as ``SiteResponse.preview_image_url`` above, which
    # is the fuller write-up: re-captured on every successful deploy (so a
    # republish updates it), plus POST /sites/{site_id}/preview-refresh on demand,
    # and the value is a different uploads link every capture.
    preview_image_url: str | None = None
    # 2026-09-02: the site's own icon as a data: URI — the same field the list
    # response carries (see ``SiteResponse.favicon_url`` above for the full
    # write-up), duplicated onto the by-pocket read for the same reason
    # ``preview_image_url`` already is: a builder polling by pocket has nowhere else
    # to look. None until a lookup lands, and whenever the site declares no icon.
    favicon_url: str | None = None
    # ── SL-3: the build lane's state on the BY-POCKET read too ──────────────────
    # The same three fields ``SiteResponse`` carries (see there for the full write-up
    # of each). They are duplicated onto this DTO for the same reason ``deployed_at`` /
    # ``pattern`` / ``engine`` / ``preview_image_url`` already are: this is the read a
    # builder polls BY POCKET, and it is the only GET keyed on a pocket id, so a client
    # watching a build it just triggered has nowhere else to look. Without them a badge
    # would have to fetch the whole gallery list to find one site's build state.
    #
    # All three default to the "no build has happened" values, so a pocket with no Site
    # doc, and every row that predates the fields, reads as "nothing building" rather
    # than as an error.
    build_status: str = "none"
    build_reason: str | None = None
    build_job_id: str | None = None
    # ── The site's BILLING state on the by-pocket read ──────────────────────────
    #
    # The frontend has declared these on its own ``SiteStatusResponse`` since BC-11
    # and the Billing tab prefers them over the gallery summary row — "the
    # authoritative status", its comment says. This DTO never carried them, so
    # every one of those reads was ``undefined`` and silently fell through to the
    # summary. Nothing broke, which is why it survived: the fallback is the same
    # data one fetch staler. It stops being harmless now that applying a plan
    # changes these fields and the panel re-reads them to price the NEXT change —
    # a stale ``period_paid_usd`` quotes an upgrade at a month the customer has
    # already paid.
    #
    # Defaults are the "no plan" values, so a pocket with no Site doc and every
    # pre-field row read as a free site rather than as an error.
    plan_tier: str = ""
    subscription_status: str = "none"
    renewal_date: str | None = None
    period_paid_usd: int = 0
    plan_cancels_at_period_end: bool = False
    # WHICH RAIL IS PAYING: "plan" (the workspace subscription carries this site),
    # "credits" (the wallet bought it), "addon"/"subscription" (sold before the
    # 2026-09-05 cutover), or "" (free floor).
    #
    # On the wire because the Billing tab cannot otherwise tell the two paid
    # states apart: a carried site and a bought site both read tier ``staff``,
    # status ``active``. Only the rail says whether the number beside it is a
    # price the customer pays or an inclusion they already have, and showing
    # "$19/month" against a site their plan carries is the kind of wrong that
    # generates a support ticket about a charge that never happened.
    billing_rail: str = ""
    # THE WORKSPACE'S SITE SLOTS: how many the plan carries and how many are taken.
    # ``plan_sites_included`` is None for an uncapped plan (Enterprise) and 0 for
    # one that carries none.
    #
    # Here rather than on ``/entitlements`` because the storefront needs BOTH
    # halves to price the next publish, and the used count is a question about
    # sites — it belongs with the read that already owns them. Costs one indexed
    # count on a read that already does several.
    plan_sites_used: int = 0
    plan_sites_included: int | None = None
    # "Paw Pro" — the WORKSPACE plan's display name, so the Billing tab can say
    # "included in Paw Pro" rather than "included in your plan".
    #
    # Sent from here rather than looked up on the client: the client would have to
    # fetch the plan catalog on a page that needs nothing else from it, and the
    # obvious shortcut — title-casing the key — turns "pro_max" into "Pro Max"
    # while the product is called "Paw Pro Max". The catalog is the only place
    # that mapping is right. "" when it cannot be resolved, and every consumer
    # falls back to a generic phrase rather than rendering an empty name.
    workspace_plan_name: str = ""


class SiteVersionResponse(BaseModel):
    """One row of a pocket's version timeline (BP-4). Mirrors the durable
    ArtifactVersion row's reading-relevant fields: ``version_no`` (the monotonic
    ordinal), ``status`` (draft|published|merged|reverted), ``label`` (e.g.
    "Revert to v2"), ``author`` (who wrote it), ``created_at`` (ISO). ``id`` is
    the version id a later revert / request-publish targets."""

    id: str
    version_no: int
    branch: str
    status: str
    label: str | None = None
    author: str | None = None
    created_at: str


class VersionHistoryResponse(BaseModel):
    """The ordered version timeline for a pocket (oldest → newest), returned by
    GET /sites/by-pocket/{pocket_id}/versions. Tenant-scoped."""

    pocket_id: str
    versions: list[SiteVersionResponse]


class RequestPublishResponse(BaseModel):
    """The review Action created by POST /sites/by-pocket/{pocket_id}/request-
    publish (BP-4 Part C). Carries the created Action's id + status so the client
    can show "submitted for review" without re-fetching. ``status`` is "pending"
    on creation (the gate item awaits operator approval)."""

    action_id: str
    status: str
    pocket_id: str
    to_version_id: str
    from_version_id: str | None = None


class AuditFinding(BaseModel):
    """One issue surfaced by the site audit (BP-7). ``check`` is the short check id
    (e.g. "a11y.img_alt"); ``tier`` is "deterministic" for the rule-based core (a
    later "judgment" tier is deferred); ``severity`` is "error" | "warning".
    ``location`` is a {file, hint} pointer (the file + a source snippet). The
    UI feeds ``fix_prompt`` to the EXISTING edit path (edit_svelte_component /
    refine), which lands the fix as a reviewable draft in the Tray — there is no
    separate apply endpoint."""

    id: str
    check: str
    tier: str
    severity: str  # error | warning
    message: str
    fix_prompt: str
    location: dict[str, str] = {}


class AuditResponse(BaseModel):
    """The result of POST /sites/by-pocket/{pocket_id}/audit — the findings for a
    pocket's published-site source. A clean site returns an empty ``findings``
    list. Tenant-scoped; engine selects how the source was read (svelte source map
    vs rippleSpec)."""

    pocket_id: str
    engine: str
    findings: list[AuditFinding] = []


class SiteDataTableInfo(BaseModel):
    """One declared table of a dynamic site, read from the pocket spec's
    ``objects`` (DS-3). ``name`` is the table/object name; ``fields`` is the
    column→type map (the spec's field types); ``primary_key`` is the PRIMARY KEY
    column (empty when the spec did not declare one). This is spec-derived, so it
    is available even when the live D1 data is not reachable (local/dev mode)."""

    name: str
    fields: dict[str, str] = {}
    primary_key: str = ""


class SiteDataTablesResponse(BaseModel):
    """The tables of a dynamic site's data store, for the operator data-view
    (DS-3; backs GET /sites/by-pocket/{pocket_id}/data). The table LIST always
    comes from the pocket spec's ``objects``, so it is populated even when the
    live D1 is not reachable. ``available`` is True only when the rows behind
    those tables can actually be read (a live Cloudflare deploy); it is False in
    local/dev mode with ``reason="live_on_cloudflare_only"`` so the UI degrades
    cleanly — it can show the schema but explain why no rows load."""

    pocket_id: str
    available: bool
    reason: str = ""
    tables: list[SiteDataTableInfo] = []


class SiteDataRowsResponse(BaseModel):
    """The rows of ONE table of a dynamic site's data store (DS-3; backs
    GET /sites/by-pocket/{pocket_id}/data/{table}). ``columns`` is the table's
    declared field names (spec-derived, always present); ``rows`` is the live D1
    rows, CAPPED (the service applies a LIMIT). ``available`` is False in local/dev
    mode (``reason="live_on_cloudflare_only"``) and ``rows`` is then empty, but
    ``columns`` is still listed so the UI can render the table header + an
    explanatory empty state instead of erroring."""

    pocket_id: str
    table: str
    available: bool
    reason: str = ""
    columns: list[str] = []
    rows: list[dict[str, Any]] = []


# A permissive DNS hostname: one or more dot-separated labels, each 1-63 chars of
# letters/digits/hyphens (not starting or ending with a hyphen), at least two
# labels (a registrable name has a TLD). Case-insensitive. This is deliberately
# loose — it accepts any real hostname and only rejects obvious garbage (spaces,
# leading/trailing/double dots, illegal characters, empty labels).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)


class DomainRequest(BaseModel):
    hostname: str

    @field_validator("hostname")
    @classmethod
    def _validate_hostname(cls, v: str) -> str:
        """Reject an obviously-malformed hostname before it reaches Cloudflare.

        Permissive: any real registrable hostname passes; only garbage (spaces,
        bad characters, leading/trailing/double dots, single-label names) fails.
        The trailing-dot FQDN form is normalized away first so ``example.com.``
        is accepted.
        """
        host = (v or "").strip().rstrip(".")
        if not _HOSTNAME_RE.match(host):
            raise ValueError("hostname is not a valid DNS hostname")
        return host


class DomainStatusResponse(BaseModel):
    hostname: str
    cname_target: str
    status: str  # pending | verifying | live | error


class LeafEdit(BaseModel):
    """One native-editor leaf edit (NE-4b): the stable ``uid`` of the edited leaf
    (e.g. ``"Hero:headline:0"``) plus the ``op`` describing the change. ``op`` is an
    open dict — ``{"kind":"setText","html":...}`` or
    ``{"kind":"setProp","name":...,"value":...}`` — because its shape is validated
    downstream by the paw-sites apply-leaf-edit CLI, not at this DTO boundary."""

    uid: str
    op: dict[str, Any]


class LeafEditsRequest(BaseModel):
    """Body for POST /sites/by-pocket/{pocket_id}/leaf-edits (NE-4b): the batch of
    ``{uid, op}`` leaf edits the native editor forwards to persist as a Branch
    draft. An empty batch is rejected by the service (422)."""

    edits: list[LeafEdit]


class LeafEditVerdict(BaseModel):
    """The apply-leaf-edit CLI's per-uid verdict (NE-4b): ``applied`` is whether the
    splice landed; ``reason`` explains a rejection (the caller keeps the whole-file
    re-author for that leaf). ``reason`` is None on an applied edit."""

    uid: str
    applied: bool
    reason: str | None = None


class LeafEditsResponse(BaseModel):
    """Response of POST /sites/by-pocket/{pocket_id}/leaf-edits (NE-4b): the pocket
    id plus one verdict per forwarded edit, in submission order."""

    pocket_id: str
    results: list[LeafEditVerdict]


class ImportFromUrlRequest(BaseModel):
    """Body for POST /sites/import/from-url (SI-4): the site URL to crawl-import.
    Shape validation (http(s), real host, length cap) runs in the import service so
    direct service callers are covered too; the crawler itself is the next stacked
    slice — this endpoint only queues.

    ``mode`` (IR-2a) picks what the URL is FOR. ``copy`` mirrors the source's own
    bytes into an html site, which is what this endpoint has always done. Anything
    else reads the URL as a DESIGN REFERENCE and captures a brief to regenerate a
    native site from.

    IT DEFAULTS TO ``copy`` ON PURPOSE, and IR-7 flips it. Today's client sends
    only ``url`` and reads ``site_id`` out of the response to navigate; defaulting
    to rebuild would hand that client a ``brief_id`` it does not understand and
    break the shipped button before the picker exists. Flip the default in the
    same PR that ships the picker, not before.
    """

    url: str
    mode: Literal["copy", "rebuild"] = "copy"


class ImportFromUrlResponse(BaseModel):
    """202 body of POST /sites/import/from-url (SI-4).

    ``copy`` mode answers exactly as it always has: the DRAFT site minted for the
    queued crawl, with ``site_id`` and ``pocket_id`` set. ``rebuild`` mode mints
    neither — the pocket is the generating agent's to create — so it answers with
    ``brief_id`` instead and leaves those two None. ``mode`` echoes which of the
    two happened, so a client never has to infer it from which fields are absent.
    """

    status: str  # queued
    mode: str = "copy"
    site_id: str | None = None
    pocket_id: str | None = None
    brief_id: str | None = None


class ImportBriefStatusResponse(BaseModel):
    """GET /sites/import/brief/{id} — where a rebuild capture has got to.

    Four states, and they are deliberately distinguishable: ``queued`` (nothing
    has run), ``capturing`` (the crawl is in flight), ``ready`` (there is a brief
    to generate from) and ``failed`` (it ended, and ``error`` says why in safe,
    user-facing words). A client that cannot tell ``queued`` from ``failed``
    shows a spinner forever on a dead capture, which is the bug the import panel
    already had once.

    ``goal`` and ``open_questions`` are the readable half of the brief itself, so
    the panel can show what was understood without shipping the whole baton to
    the browser.
    """

    brief_id: str
    status: str  # queued | capturing | ready | failed
    source_url: str
    error: str = ""
    goal: str = ""
    open_questions: list[str] = []


class SiteInvoiceOut(BaseModel):
    """One manual receipt on the site's client record. ``amount_cents`` is integer
    MINOR units — the wire never carries a float for money, so the reading client
    formats it and nothing rounds in transit."""

    id: str
    issued_at: str
    amount_cents: int
    currency: str
    paid: bool
    note: str = ""


class SiteEntitlementsResponse(BaseModel):
    """What this site is allowed to do, resolved — so the UI can disable a control
    and say WHY instead of offering it and rendering the 402 that comes back.

    ``resolve_site_entitlements`` has computed most of this since BC-9 and nothing
    ever exposed it per site; the frontend fetched entitlements nowhere at all. The
    result was a UI that could only discover a refusal by attempting the action.

    The two fields that are NOT on ``SiteEntitlements`` are the ones that make the
    difference between a usable message and a useless one:

    * ``domained_sites_used`` — how many sites in this workspace already spend the
      floor allowance. "You cannot add a domain" and "your one free domain is on
      another site" are different sentences, and only the count separates them.
    * ``domain_slots_available`` — the same answer ``add_domain`` will give,
      computed by the SAME function it calls (``_domain_cap_exceeded``), so the
      button's enabled state and the endpoint's verdict cannot drift apart. A
      second copy of the rule here would eventually disagree with the gate, and
      the UI would confidently offer a button that 402s.

    ``max_domained_sites`` is None for an uncapped (paid) tier, mirroring the
    catalog. ``subscription_active`` distinguishes a lapsed paid site from a site
    that never had the capability — the tier stays recorded, only the payment
    stopped, and the UI should say so.

    ``analytics`` says whether this site's plan buys visitor counting. It is what
    lets the panel disable itself with a reason BEFORE the call, and it is
    deliberately not the last word: the analytics endpoint's own ``status`` is
    authoritative, because entitlement alone cannot distinguish "your plan does not
    include this" from "it does, and you have not republished since you upgraded".
    Those need different sentences and different buttons. This field is the cheap
    pre-check; the endpoint is the answer.
    """

    site_id: str
    plan_tier: str = ""
    subscription_active: bool = False
    badge_required: bool = True
    custom_domain: bool = False
    max_domained_sites: int | None = 0
    domained_sites_used: int = 0
    domain_slots_available: bool = False
    analytics: bool = False
    concierge_entitled: bool = False
    concierge_enabled: bool = False


class SiteClientResponse(BaseModel):
    """The site owner's record of their own client (backs GET/PATCH
    /sites/{site_id}/client). This is the owner's address book and receipt book for
    the business the site belongs to — NOT the owner's own subscription with us,
    which lives on ``SiteResponse.plan_tier`` / ``subscription_status``. Every field
    defaults empty, so a site that has never been edited returns a valid, blank
    record rather than a 404: the Settings surface renders the same form either way.
    """

    site_id: str
    name: str = ""
    contact: str = ""
    notes: str = ""
    invoices: list[SiteInvoiceOut] = []


class SiteClientUpdate(BaseModel):
    """PATCH body for the client record. Every field is optional and OMISSION MEANS
    "leave unchanged" — the service applies three-way semantics via
    ``model_fields_set``, so a caller editing only the notes cannot blank the name
    it never sent. Sending an explicit empty string DOES clear that field, which is
    how the form deletes a value.

    The caps are the reason this validation lives at the edge rather than on the
    document: an over-long value is a 422 the form can show, instead of a record
    that silently lost its tail on the way to Mongo.
    """

    name: str | None = None
    contact: str | None = None
    notes: str | None = None

    @field_validator("name", "contact")
    @classmethod
    def _cap_short(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 200:
            raise ValueError("value must be 200 characters or fewer")
        return v

    @field_validator("notes")
    @classmethod
    def _cap_notes(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 5000:
            raise ValueError("notes must be 5000 characters or fewer")
        return v


class SiteInvoiceCreate(BaseModel):
    """POST body for recording one manual receipt against the client record.

    ``amount_cents`` is a non-negative integer in minor units. It is bounded on BOTH
    ends on purpose: negative would let a receipt reverse the running total, and the
    upper bound stops a typo (or a paste of an id into an amount field) from writing
    a number no currency has a use for. ``currency`` is normalized to upper case and
    must be a 3-letter ISO-4217-shaped code, so the list cannot end up rendering
    "usd" and "USD" as two different currencies.
    """

    amount_cents: int = 0
    currency: str = "USD"
    paid: bool = True
    note: str = ""

    @field_validator("amount_cents")
    @classmethod
    def _validate_amount(cls, v: int) -> int:
        if v < 0:
            raise ValueError("amount_cents must not be negative")
        if v > 1_000_000_000_000:
            raise ValueError("amount_cents is implausibly large")
        return v

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, v: str) -> str:
        code = (v or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError("currency must be a 3-letter code")
        return code

    @field_validator("note")
    @classmethod
    def _cap_note(cls, v: str) -> str:
        if len(v) > 500:
            raise ValueError("note must be 500 characters or fewer")
        return v


class NativeArtifactResponse(BaseModel):
    """Response of GET /sites/by-pocket/{pocket_id}/native-artifact (NE-5b): the armed
    svelte build's body + CSS, so the native editor can shadow-render the site
    instead of framing an iframe. ``body_html`` is the built page's ``<body>`` INNER
    HTML — the data-uid-stamped editable leaves plus the embedded
    ``<script id="paw-edit-manifest">`` — which the FE injects into a shadow root.
    ``css`` is the built stylesheet(s) concatenated into one string the FE injects as
    a single ``<style>``.

    SP-2 — THE RESPONSE HAS TWO SHAPES AND ``build_status`` IS WHICH ONE THIS IS.
    A cold miss no longer builds in the request; it queues the build in the ephemeral
    Daytona lane and answers immediately with ``body_html`` / ``css`` EMPTY and a job to
    poll. A client renders the artifact when ``build_status`` is ``"none"`` (the served
    render — not a build in any state) and shows progress otherwise, re-fetching until it
    flips. Empty strings rather than nulls so the field's type never changes under a
    client that reads it before checking the status.

    The status vocabulary is the publish lane's, unchanged: ``queued`` / ``building`` /
    ``failed``, with an UNRECOGNISED value meaning in-progress (SL-3's wire contract).
    ``build_reason`` carries a rung name on a failure — never a build's stderr, which is
    the user's own content and stays in the worker log."""

    pocket_id: str
    body_html: str = ""
    css: str = ""
    build_status: str = "none"
    build_reason: str | None = None
    build_job_id: str | None = None


class SiteAssetResponse(BaseModel):
    """One image published to the site's public asset rail.

    ``url`` is the whole point: a durable, credential-free address. It carries no
    signature and no expiry, so it can be pasted into page source that outlives
    this process — unlike a presign (7-day cap) or ``/api/v1/uploads/{id}``
    (auth-gated, and therefore a 401 for the anonymous visitor the site serves).

    ``key`` is the storage key, and the handle DELETE takes. It is scoped to the
    owning workspace and pocket, so it is safe to hand to the client: the delete
    endpoint re-checks the prefix and refuses anything outside this site.
    """

    key: str
    url: str
    mime: str
    size: int
    filename: str


class SiteAssetListResponse(BaseModel):
    """Every asset this site has uploaded."""

    assets: list[SiteAssetResponse]


class SiteAssetDeleteRequest(BaseModel):
    """The storage key to remove. Validated against the site's own prefix."""

    key: str


# ── SA-4: the visitor-analytics read ────────────────────────────────────────
#
# ``status`` is a CLOSED vocabulary and the most important field on the response.
# Four outcomes are possible and three of them would otherwise render identically
# as a panel full of zeros, which is the specific failure this slice exists to
# prevent: "your plan does not include this", "you have not published since you
# upgraded", and "nobody visited" are three different sentences to a customer, and
# only one of them is about their traffic.
#
#   ``ok``            — counted, and the numbers below are real. They may be zero.
#   ``not_entitled``  — this site's plan does not buy analytics. Nothing was ever
#                       recorded, and nothing can be until the plan changes.
#   ``never_counted`` — entitled, but no publish has yet deployed a counter, so
#                       there is nothing to read. Republishing starts it. Upgrading
#                       does NOT backfill: history begins at that publish.
#
# The FOURTH outcome — the read itself failed — is deliberately NOT a status value.
# It surfaces as an error response, because a failed read is not a report about the
# site's traffic and must not arrive on the same shape as one. A client that
# defaults an unknown status to "no data" would otherwise turn a Cloudflare outage
# into a quiet week.
ANALYTICS_STATUS_OK = "ok"
ANALYTICS_STATUS_NOT_ENTITLED = "not_entitled"
ANALYTICS_STATUS_NEVER_COUNTED = "never_counted"


class SiteAnalyticsBreakdown(BaseModel):
    """One row of a breakdown — a page, a referrer host, a country, a device class.

    ``visitors`` is a distinct count within this row and does NOT sum to the
    response's total: one visitor who reads three pages is one visitor overall and
    one visitor on each of three rows. A UI that adds a column here gets a number
    that means nothing, so the field is named for the row rather than for the total.
    """

    label: str
    pageviews: int
    visitors: int


class SiteAnalyticsVisits(BaseModel):
    """Visits, bounce rate and visit duration for one site over one window.

    A VISIT is the run of pageviews sharing the visit id the counting Worker stamps on
    every row, under a salt that rotates on the hour. So a visit that crosses the top of
    an hour is counted as TWO. That is accepted rather than fixed, and it is stated here
    because it is part of what these numbers mean: the error over-splits and never
    over-merges, the direction the daily visitor salt already accepts, and it leaves
    visits slightly generous and duration slightly pessimistic.

    ``bounce_rate`` is a FRACTION and not a percentage — bounces over visits, where a
    bounce is a visit of exactly one pageview. None when there were no visits at all,
    because a rate over zero is undefined and 0.0 would say nobody bounced.

    ``duration_seconds`` is the mean over visits of MORE THAN ONE pageview. A
    single-pageview visit has no measurable duration — its one row carries one timestamp
    — so averaging it in as a zero would drag the mean down in proportion to how well the
    landing page holds people, which is backwards. None when every visit was a bounce,
    for the same reason the rate is None when there were no visits.
    """

    count: int
    bounce_rate: float | None = None
    duration_seconds: float | None = None


class SiteAnalyticsSeriesPoint(BaseModel):
    """One bucket of the overview chart — an hour or a day, and what happened in it.

    ``bucket`` is the ISO-8601 stamp of the bucket's START, and it is UTC. That is said
    here rather than left to be inferred because it is the field a chart turns into an
    axis label: an hour rendered as ``1PM`` that actually means 13:00 UTC is five and a
    half hours wrong for a reader in Mumbai, and nothing on the screen would say so. A
    client that wants local labels has the offset it needs to shift them.

    ``visitors`` is a distinct count WITHIN this bucket and does not sum down the column —
    the same rule ``SiteAnalyticsBreakdown`` carries one field over. One person who reads
    at nine and again at ten is one visitor on the response's total and one visitor in each
    of two buckets, so a UI that adds this column gets a number that means nothing.
    """

    bucket: str
    pageviews: int
    visitors: int


class SiteAnalyticsSeries(BaseModel):
    """Views and visitors per time bucket, oldest first — the chart under the tiles.

    ``interval`` is ``"hour"`` or ``"day"``, and a client needs it to label the axis: the
    same list of points is a day of hours or three months of days depending on this field
    alone. It is chosen from the window rather than requested, because a bar chart stops
    being readable somewhere around a hundred bars and thirty days of hourly buckets is
    seven hundred and twenty of them.

    EVERY BUCKET IN THE RANGE IS HERE, including the ones nobody visited. A series that
    listed only the buckets with traffic would be shorter, and every chart drawn from it
    would join the two ends of a gap with a straight line — reporting a quiet Sunday as no
    Sunday at all. A zero is a measurement; a missing entry is not.

    THE RANGE ITSELF starts at the later of the window's own start and the moment counting
    began, so a site that has been counting for two days does not answer a ninety-day
    request with eighty-eight zeroes for a period nobody was recording. That is the one
    rule this whole endpoint is built on, applied to a chart: those zeroes would look
    exactly like a site nobody visits. ``counting_since`` on the response says where the
    trim came from.
    """

    interval: str  # hour | day
    points: list[SiteAnalyticsSeriesPoint] = []


class SiteAnalyticsResponse(BaseModel):
    """Visitor analytics for one site over one window (GET
    /sites/{site_id}/analytics).

    Shaped after ``cloud.mission_control.dto.AnalyticsResponse`` and it inherits that
    model's one rule about absence: a metric that is not known is None, never 0. Here
    that rule is load-bearing rather than cosmetic — see ``status`` above. Every
    metric field is None unless ``status`` is ``ok``, so a client that renders the
    numbers without reading the status shows blanks rather than a confident zero.

    ``counting_since`` is the ISO stamp of the publish that first deployed a counter,
    and it is what makes an honest chart possible: the series does not begin at the
    site's creation, it begins here, and a window that reaches further back is
    reaching into time nobody recorded. None when nothing has ever counted.

    ``unrecorded`` names the parts of this response the stored rows cannot answer AT
    ALL, as opposed to answered-and-empty. WHICH parts is not fixed, because the row has
    grown twice since this model was written and a window ninety days long can span the
    change: it carries ``"devices"`` while every row in the window predates the device
    class, and ``"visits"`` while every row predates the visit id, and it empties on its
    own once real rows exist. Naming a part beats returning an empty list, which a chart
    renders as "no devices", and beats omitting the field, which a client cannot tell
    from a version skew.
    """

    site_id: str
    window: str
    status: str
    counting_since: str | None = None
    # Retention is three months at Cloudflare, so a window is capped there and a
    # series older than this is gone rather than empty. On the wire so a UI can say
    # why the earliest date it can offer is the one it offers.
    retention_days: int = 90
    pageviews: int | None = None
    visitors: int | None = None
    # None with ``"visits"`` in ``unrecorded`` when no row in the window carries a
    # visit id, which means the site has not republished since the counter learned to
    # stamp one. Never a zeroed block: a 0% bounce rate is not an empty panel, it is a
    # spectacular result, and this site has not measured a single visit.
    visits: SiteAnalyticsVisits | None = None
    # AD-4: the chart's data. None unless ``status`` is ``ok``, like every other metric
    # here — and unlike ``visits`` and ``devices`` it never lands in ``unrecorded``,
    # because the column it is bucketed on is the row's own timestamp and every stored row
    # has carried one since the first. A window with no traffic at all still answers a
    # full range of zeroed points: nothing about a quiet week is unanswerable.
    series: SiteAnalyticsSeries | None = None
    top_pages: list[SiteAnalyticsBreakdown] | None = None
    referrers: list[SiteAnalyticsBreakdown] | None = None
    countries: list[SiteAnalyticsBreakdown] | None = None
    devices: list[SiteAnalyticsBreakdown] | None = None
    unrecorded: list[str] = []

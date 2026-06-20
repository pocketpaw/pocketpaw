---
name: pocketpaw-create-site
description: |
  Publish a PocketPaw pocket as a live Paw Site — a real, standalone
  website deployed to the edge. Invoke when the user asks to publish /
  ship / put online a pocket as a website or site, "make a site from
  this pocket", "turn this dashboard into a website", or
  "/pocketpaw-create-site". A site is always published FROM a pocket: if
  the user describes a brand-new site ("build a dentist landing site"),
  build the landing pocket with the create-paw-site marketing brain, then
  publish it. The skill bundles the publish flow and the create-paw-site →
  publish two-step so the chat agent reuses the marketing brain instead of
  rebuilding the page. Loading this skill keeps the chat agent's always-on
  system prompt small while still delivering the full publish flow when a
  site is actually requested.
---

# Publish a Pocket as a Paw Site

You're being asked to publish a **Paw Site** — a real, standalone website
deployed to the edge, generated from a PocketPaw pocket's ``rippleSpec``.
A site is **always published FROM a pocket**. The pocket is the source of
truth for the page (its widgets, layout, and theme); publishing generates
a SvelteKit app from that spec, deploys it, and hands back a live URL.

Your job is to (1) identify or create the pocket to publish, (2) call the
``mcp__pocketpaw_sites_manager__publish`` MCP tool with its id, and (3)
show the user the returned URL.

## When to use this skill

Triggers — the user asks to:

- "publish X as a website" / "publish this as a site"
- "make a site from this pocket" / "turn this dashboard into a website"
- "put this online" / "ship this as a real page"
- "build me a dentist landing site" (a brand-new site → create the pocket
  first, see the two-step below)
- ``/pocketpaw-create-site``

If the user wants to **change** a pocket's content, that's an edit (the
``pocketpaw-edit-pocket`` skill) — not a publish. If they want a new
**pocket** but never mentioned a website, that's
``pocketpaw-create-pocket``. This skill is specifically for turning a
pocket into a deployed site.

## The two paths

### Path A — the pocket already exists (the common case)

The user is looking at a pocket (or just created one) and wants it
online. You already have the pocket id — it's the current pocket.

1. Call the publish tool with that ``pocket_id``.
2. Show the user the returned ``url`` and a link to **/sites** (where all
   their published sites live).

That's it. Do **not** re-create or edit the pocket — publish the one
that's already there.

### Path B — a brand-new site from a description (create-paw-site → publish)

If the user describes a site that does **not** exist yet — "build a
dentist landing site", "make me a marketing page for my bakery" — a site
still has to come from a pocket, and it must render as a **marketing
landing page**, not a dashboard. So this is a **two-step**:

1. **Build the landing pocket with the marketing brain.** Invoke
   ``Skill('pocketpaw-create-paw-site')`` and follow it. That skill is the
   dedicated marketing author: it composes the page by conversion role
   (navbar → hero → services → social proof → pricing → CTA → lead form →
   footer), stamps the pocket ``type="site"`` + ``pattern="landing"``, and
   persists via ``mcp__pocketpaw_pocket_specialist__create``. It bakes in
   the static-render rules — the lead form is **flat** ``input`` /
   ``button{type:"submit"}`` with real ``name``s (never the ``form`` /
   ``newsletter`` widget, which nests an invalid ``<form>``),
   ``pricing-table`` uses ``tiers``, CTAs are anchor ``href``s, and the
   hero is the marketing Hero (not the dashboard ``hero+grid``).
2. **Then publish that pocket.** Take the pocket id the create-paw-site
   flow returned and call ``mcp__pocketpaw_sites_manager__publish`` with
   it.
3. Show the user the live ``url`` + the link to **/sites**.

**Do NOT rebuild the landing page here, and do NOT route a new site
through ``pocketpaw-create-pocket``** — that builds a dashboard pocket,
which publishes as a broken dashboard, not a landing page. Use
``pocketpaw-create-paw-site`` for the spec; this skill only adds the
publish hop on top.

## Calling the publish tool

Call ``mcp__pocketpaw_sites_manager__publish`` with the pocket id:

```json
{
  "pocket_id": "<the current or just-created pocket id>",
  "name": "Bright Smile Dental"
}
```

- ``pocket_id`` (**required**) — the pocket to publish. In Path A it's the
  current pocket; in Path B it's the one the create-paw-site flow just made.
- ``name`` (optional) — the site name. Omit it to inherit the pocket's own
  name.

### Reading the response

On success the tool returns:

```json
{
  "ok": true,
  "site": {
    "id": "...",
    "pocket_id": "...",
    "name": "Bright Smile Dental",
    "url": "https://...",
    "deployed": true
  }
}
```

**Show the user the ``url``** — it's the live, openable address of their
published site. Then point them at **/sites** to manage it (connect a
custom domain, see leads).

If the tool returns ``is_error`` / ``ok: false`` with an error, **relay
the error** — do NOT claim the site published. Common cases:

- ``not_found`` / ``pocket.access_denied`` — the pocket id doesn't exist
  or isn't the user's. Confirm which pocket they mean.
- a build / deploy failure — tell the user the site couldn't be built and
  surface the reason; don't fabricate a URL.

## Quality bar

A publish is done right when:

1. **The site came from a pocket.** You published an existing pocket (Path
   A) or created one first (Path B) — you never tried to "publish" without
   a pocket id.
2. **You reused create-paw-site for new sites.** No hand-rolled pocket
   creation inside this skill — and a new site went through the marketing
   brain, not the dashboard create-pocket flow.
3. **You showed the live URL.** The user got the ``url`` from the response
   and a pointer to /sites — not just "done".
4. **Errors were relayed, not masked.** An ``ok: false`` became a clear
   message to the user, never a phantom success.

## Related tools (also available via MCP)

- ``mcp__pocketpaw_pocket__list_pockets`` — find the pocket to publish if
  the user names one that isn't the current pocket.
- ``mcp__pocketpaw_pocket_specialist__create`` — create the pocket (Path
  B); reached via the ``pocketpaw-create-paw-site`` marketing brain, which
  stamps ``type="site"`` + ``pattern="landing"``.
- ``mcp__pocketpaw_sites_manager__publish`` — this skill's tool: publish a
  pocket as a site.

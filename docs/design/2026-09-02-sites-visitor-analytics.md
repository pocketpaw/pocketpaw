<!-- Engineering note for Paw Sites visitor analytics (SA-1 .. SA-7), written
     2026-09-02 alongside ee/pocketpaw_ee/sites/analytics_worker.py and the read half
     in sites/service.py. Records the three things a field list cannot show and that
     the customer-facing API reference is the wrong place for: WHY counting is sold
     rather than given away (the cost model, with its derivation, so nobody has to
     re-derive it), which BUILDS actually carry a counter today, and what the privacy
     model is in enough detail to defend it.
     WRITTEN FOR THE STATE AFTER #2049, which merges before this branch does. That
     slice adds the wrapper shim and the device blob, so every engine counts and the
     row carries five blobs. Two earlier drafts of this file were wrong about that:
     the first said svelte never counts (the gate reads the BUILD, and a static svelte
     site deploys assets-only), the second said a build with its own worker never
     counts at all (the shim imports that worker and wraps its fetch). Describe the
     merged state, not this branch's snapshot.
     The cost numbers are Cloudflare's published prices as of this date, with the URL
     against each one. They move; re-check before quoting them at a customer. -->

# Counting visitors on a Paw Site

`GET /sites/{site_id}/analytics` and its wire shape are in
[the API reference](../api-reference.md#sites--visitor-analytics). This note is the
half that belongs to engineering: what counting costs, which sites can do it, and what
a stored row actually holds.

## Why counting is a paid grant

**Serving a page costs nothing and counting it costs money.** The gap is entirely
Cloudflare's billing model rather than ours.

> "Requests to static assets are free and unlimited." … "Requests to the Worker
> script (for example, in the case of SSR content) are billed according to Workers
> pricing."
> — [Workers static assets: billing and limitations](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)

A Paw Site built by the `html` or `react` generator is a static tree. Before SA-1 it
deployed as an assets-only Worker with no `main` at all, so it shipped zero JavaScript
and drew zero billable requests however much traffic it took. Putting a counter in
front of it is what starts the meter. That is the whole reason the capability is
attached to a plan instead of switched on for everyone.

### The derivation

Two line items move per pageview. Both are Workers Paid prices, both are the
**overage** rate above a monthly allowance:

| Line item | Cloudflare price | Included on Workers Paid | Per million pageviews |
|-----------|------------------|--------------------------|-----------------------|
| Worker request | $0.30 per additional million | 10 million / month | $0.30 |
| Analytics Engine data point written | $0.25 per additional million | 10 million / month | $0.25 |
| | | **Total** | **$0.55** |

Sources: [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/),
[Analytics Engine pricing](https://developers.cloudflare.com/analytics/analytics-engine/pricing/).

CPU time is the third component and is left out of the figure deliberately rather
than forgotten. The counter does one SHA-256 over a short string and one
`writeDataPoint`; against 30 million CPU-milliseconds included per month and $0.02
per additional million after that, it does not reach the second decimal place.

Two caveats worth carrying:

- **Analytics Engine is not actually billing yet.** Its pricing page still says
  "Currently, you will not be billed for your use of Workers Analytics Engine." So
  today's marginal cost is the $0.30 request half, and $0.55 is what it becomes when
  that changes. Budget against $0.55; do not be surprised by an invoice that reads
  $0.30.
- **These are overage rates.** Under the included allowance the marginal cost of a
  pageview is zero and the $5/month base fee is the whole bill. The per-million
  figure is the number that matters at the scale where the answer stops being
  obvious, which is the only scale where anyone asks.

### What the allow-list is protecting

A page carries something like twenty subresources — stylesheets, scripts, fonts,
images. Routing every request through the Worker would multiply the per-pageview cost
by roughly that factor, so `analytics_worker.run_worker_first_rules` emits an
**allow-list** of page-shaped paths (`/`, `/*.html`, `/*.htm`, `/*/`) rather than a
deny-list of asset extensions. A deny-list rots: every format nobody remembered to
list — `.avif`, `.woff2`, `.webmanifest` — gets billed at page rates. The allow-list
fails the other way, and an uncounted visit is the direction this is allowed to fail
in.

The allow-list does not buy everything, and the gap is real. Once the config carries
a `main` at all, the asset router's final fallback sends any request for a path with
**no matching asset** to the Worker. So 404 scans — the `/wp-login.php` traffic every
public host receives — become billed invocations under any rule shape. They are not
counted as pageviews (the entry only records a 200 or a 304), but they are billed.
That is inherent to putting a server in front of a static site.

## How a site counts, and which one still does not

`workers_deploy._write_deploy_files` decides whether to count with
`analytics_worker.counting_enabled(entitled=...)`, and decides *which shape* of counter
to write from `engines.resolve_emits_server_worker(project_dir, engine)`.

**That second question is asked of the BUILD, not of the engine.**
`resolve_emits_server_worker` looks for a `_worker.js` on disk in the build output and
answers False when there is not one, even for an engine that usually emits one. It
tests for existence rather than for being a file, because adapter-cloudflare emits
`_worker.js` as a *directory* once an app is large enough. So the row to read is the
build shape, not the engine name:

| Build | `_worker.js` in the output | How it counts |
|-------|---------------------------|---------------|
| `html` | never | assets-only counter as `main` |
| `react` | never | assets-only counter as `main` |
| `svelte`, static | no | assets-only counter as `main` |
| `svelte`, dynamic | yes | wrapper shim as `main` |
| `ripple` | always | wrapper shim as `main` |

**Every build counts. The two column values are two shapes, not a yes and a no.**

A build with no server entry gets `analytics_worker.build_entry_js`: a counter that
serves the page through the `ASSETS` binding and becomes `main`, because there was no
`main` before it.

A build that emits its own `_worker.js` cannot take a counter in front of it from a
config key, so `analytics_worker.build_shim_js` generates a module that imports that
worker, spreads it, overrides `fetch`, counts a delivered response and returns it
untouched. The shim becomes `main` in place of `_worker.js`. The adapter's file is
never edited, because the adapter rewrites it on every build and an edit would survive
exactly until the next one. The spread is load-bearing rather than decorative: it
forwards any other handler the adapter grows (`scheduled`, `queue`, `email`) instead
of dropping it, which an object literal naming only `fetch` would do.

One asymmetry in the shim is worth knowing. Counting is failure-soft on both shapes,
but the **delegation is deliberately not wrapped in a try**: a throw from the site's
own worker is the site's failure and surfaces exactly as it would with no shim in
front of it. Failure-soft governs the counting, which is worth nothing next to the
page. It does not mean swallowing the page's own errors.

`svelte` spans two rows because SL-1 split the track across two adapters, chosen by a
property of the build rather than by the engine name. `engines.expects_server_worker`
returns `None` for it for exactly that reason: either answer is legitimate, so only
the artifact can say. Asking the engine name instead would point `main` at a
`build/_worker.js` that does not exist and fail the deploy outright, which is the
failure that predicate was introduced to prevent.

**What is still open.** One case does not count, and it is not an engine: a dynamic
site provisioned through the durable provision job. That path calls
`deploy_workers(..., analytics_entitled=False)` unconditionally, because it holds a
site id and a directory and cannot resolve a plan from either. Tracked as SA-8.

## Retention: three months, and then it is gone

Cloudflare retains an Analytics Engine row for three months. There is **no rollup
store** — nothing aggregates a day into a smaller row before the source row expires —
so data older than the retention window is not archived anywhere. It is gone.

That is why `90d` is the longest window the endpoint offers: a longer one could only
return the same rows with more empty space in front of them.

Read the retention figure off the response's `retention_days` rather than hard-coding
90. A UI that copies the constant will keep displaying it after the day somebody adds
a rollup store or Cloudflare changes the window, and the response is the only place
that stays true through that change.

`Site.analytics_since` interacts with this in a way worth stating: it is cleared by
any publish that deploys no counter, so a site that lapsed to free and later
re-upgraded loses its earlier era's start date. That looks lossy and is not. A stamp
older than the retention window points at rows that no longer exist, so keeping it
would only let the chart claim a history it cannot draw.

## Privacy: no cookie, and nothing to reverse

There is **no cookie, no `localStorage`, and no consent banner**, because there is no
identifier that persists.

A visitor is identified by `SHA-256(secret | UTC-day | ip | user-agent)`, truncated to
16 bytes. What that construction buys:

- **It cannot be reversed to a person.** The raw IP is never stored — only the digest
  reaches the row.
- **It rotates daily.** The UTC calendar day is part of the input, so today's hash
  cannot be joined to yesterday's even by us. The visible consequence is that a
  visitor who returns tomorrow counts twice, which is the honest cost of the design
  rather than a rounding error.
- **The salt is per-publish and unguessable.** `secrets.token_hex` at publish time,
  not derived from the site id or anything else reconstructable. A salt an attacker
  can rebuild turns the hash into a confirmation oracle: given a candidate IP and
  user-agent, recompute and compare. Republishing rotates it early, which
  over-splits a day's visitors and never over-links them.

That last property is load-bearing on one config line. The salt lives inside the
generated entry file, which sits at the project root — and for an assets-only site the
project root **is** the asset directory. An entry that is not listed in
`.assetsignore` gets uploaded and served, and the salt becomes a public download. The
`.assetsignore` line is a privacy control, not housekeeping, and `_write_deploy_files`
writes it whether counting is on or off for exactly that reason. It also deletes a
leftover entry from an earlier paid publish, because a config with no `main` naming
that file would otherwise upload it as an ordinary asset.

### What the row holds

Five blobs and one index, and that is the entire record of a visit:

| Written as | Queried as | Contents |
|------------|------------|----------|
| `indexes[0]` | `index1` | the site id |
| `blobs[0]` | `blob1` | request path, truncated to 256 characters |
| `blobs[1]` | `blob2` | referrer **host** only, empty for a direct visit or a same-site link |
| `blobs[2]` | `blob3` | `request.cf.country` |
| `blobs[3]` | `blob4` | the visitor hash |
| `blobs[4]` | `blob5` | device class: `desktop`, `mobile`, `tablet` or `unknown` |
| `doubles[0]` | `double1` | `1`, the pageview |

The two columns are the same fields under two names, and mixing them up is the way
this goes wrong quietly. The Worker writes a zero-indexed JavaScript array; Analytics
Engine SQL addresses the same slots one-indexed, and its columns are **positional with
no names at all**, so a query that reads the wrong slot reports referrers as countries
and raises nothing. Reordering the write without changing the reader in the same PR
does exactly that.

**No user-agent string is retained**, and the device class does not change that. The
user-agent is read at the edge for three jobs and then discarded: rejecting bots,
salting the visitor hash, and deriving one of four coarse labels. Only the label
lands. Four values is a ceiling rather than a first pass — the user-agent is the
highest-entropy thing this Worker touches, and the row's privacy claim survives only
while what lands in it cannot single anyone out.

Two details of that classifier are worth not rediscovering. Tablet is tested **first**,
because a tablet's user-agent is a superset of a phone's on both platforms that matter:
an iPad announces `iPad` beside `Mobile`, and an Android tablet is an Android that
omits `Mobile`. Testing mobile first files every iPad under mobile. And desktop is a
**positive** match on a platform token rather than the fallback, so an unrecognised
string lands on `unknown` instead of quietly inflating the one number a site owner is
most likely to act on.

The device class arrived in #2049 and was **appended** at `blobs[4]` rather than
inserted anywhere more logical. Inserting it earlier would have silently re-labelled
every row already in the dataset, and the retention window is three months.
Analytics Engine has no schema and no backfill, so rows written before that slice carry
four blobs forever, and a slot no row carried reads as an empty string rather than
failing. Both shapes therefore arrive in one window and neither may crash the read.

That is what `unrecorded` is for. When every device label in a window is empty, the
reader returns `devices: null` with `"devices"` in `unrecorded` rather than a chart of
one bar called unknown. Read it as **"this site has not published a counter that
records devices yet"** rather than as a permanent gap: it clears once the site is
republished and takes traffic. An empty list would read as "no devices", and an omitted
field is indistinguishable from a version skew, which is why the dimension is named
rather than dropped.

## The read side, briefly

Reads are billed too, on a different meter: $1.00 per million read queries, with one
million included per month on Workers Paid and **10,000 per day on the free plan**.
The free-plan number is the one that constrains, and it is account-wide.

One cache miss costs **five queries** — one for the totals and one per dimension in
`_ANALYTICS_DIMENSIONS`. A dashboard is reloaded by people, so the endpoint caches an
assembled response for 60 seconds per `(workspace, site, window)`. The cache is
in-process and deliberately not shared: a second API replica keeps its own, which puts
the worst case at one query set per replica per minute rather than one per account,
and costs no Redis dependency on a read whose entire value is being cheap. Only
successes are cached, so a Cloudflare blip is not extended into a minute of errors.

The read needs an **Account Analytics Read** token scope, which the deploy paths do
not use. A token without it answers 403, surfaced as `sites.cloudflare_error` with
Cloudflare's own message attached. A site that publishes and counts perfectly well
while every read 403s is exactly the shape that failure takes.

## The kill switch, and the failure it exists for

`PAW_SITES_ANALYTICS_DISABLED` makes the next publish of every site — paid ones
included — emit the pre-analytics config byte for byte: no `main`, no dataset binding,
no `run_worker_first` rules, no entry file on disk.

It is not a feature flag. It exists for one failure mode whose blast radius is larger
than analytics: if this Cloudflare account turns out to be on the **Workers Free
plan**, a config carrying a `main` starts drawing on a 100,000 request/day ceiling
that is account-wide, and breaching that ceiling does not degrade analytics —
Cloudflare stops serving the Workers behind it. Every published site goes dark
together. A code change plus a release is minutes at best; an environment variable
plus a republish is one publish long.

The switch takes effect **at the next publish**, which is the part to plan around
during an incident: already-deployed sites keep their counter until they are
republished, so pulling the switch is the first step and republishing the affected
sites is the second.

Both variables are documented for operators in
[the configuration reference](../api/configuration-reference.mdx#paw-sites--visitor-analytics).

# ee/pocketpaw_ee/sites/analytics_worker.py — the PAGEVIEW COUNTER a published Paw
# Site carries: the generated Worker entry that records one row in Cloudflare
# Workers Analytics Engine per real visit, and the routing rules that keep it off
# every request that is not a page.
#
# Created 2026-09-02 (feat/sites-analytics-counter, SA-1). Slice 1 of Paw Sites
# visitor analytics: an ``html`` site counts a real pageview, queryable by site id.
#
# Updated 2026-09-02 (feat/sites-analytics-gate, SA-2) — NOT EVERY SITE COUNTS ANY
# MORE, and this module gained the two functions that say so. SA-1 wired the counter
# onto every assets-only publish regardless of who was paying; a Worker invocation is
# billed and a static asset is not, so that spent money on free sites. The gate is
# ``counting_enabled(entitled=...)``, and its two halves are deliberately separate:
# the per-site entitlement is resolved by ``entitlements.site_analytics_entitled``
# at the publish seam (the only layer that can see a ``Site`` document), while the
# operator kill switch ``PAW_SITES_ANALYTICS_DISABLED`` lives here and is global.
# When counting is off the emitted config is the PRE-ANALYTICS one, byte for byte,
# and no entry is written — see ``workers_deploy._wrangler_jsonc``.
#
# Updated 2026-09-02 (feat/sites-analytics-shim, SA-3) — the row grew a fifth blob: a
# COARSE DEVICE CLASS (``desktop`` / ``mobile`` / ``tablet`` / ``unknown``) at
# ``blobs[4]``, derived from the user-agent this Worker was already parsing for the bot
# filter and the visitor hash. It is appended, never inserted, and rows written before
# it carry four blobs forever — the reader tolerates both. The full argument for why it
# is this coarse, and why the append is not negotiable, is in the row contract below.
#
# SHAPE MIRRORS ``badge.py`` / ``paw_bar/embed.py`` — a per-site injection into the
# artifact between build and deploy, so what lands is already correct and there is no
# second deploy and no post-publish patch. What differs is WHAT is injected: those two
# write markup into the pages, this one writes a Worker that sits IN FRONT of them.
#
# FAILURE-SOFT, THE SAME WAY ``paw_bar/embed.py`` IS AND THE OPPOSITE OF ``badge.py``.
# The badge is the free tier's enforcement, so it is failure-CLOSED: a page that
# cannot be badged aborts the publish. A pageview is worth nothing next to the page
# itself. Every counting call in the generated Worker is wrapped so a throw can never
# reach the response: a missing binding, a throwing ``writeDataPoint``, a malformed
# referrer and a hash that will not compute all end the same way — the page is served.
# Adding a raise to the serving path of the generated entry is a defect, not a
# hardening.
#
# ── THE COST FLOOR (the constraint that shapes this whole module) ──────────────
#
# Cloudflare bills a Worker INVOCATION but serves a static asset for free. An
# assets-only site shipped zero JavaScript and therefore zero invocations; putting a
# counter in front of it starts billing. A page carries roughly twenty subresources —
# stylesheets, scripts, fonts, images — so a rule that routes every request through
# the Worker costs about twenty times what counting the pageview costs. That is a
# defect, not a tuning choice, and it is why the rules below are an ALLOW-LIST of
# page-shaped paths rather than a deny-list of asset extensions.
#
# The mechanism is wrangler's ``assets.run_worker_first``. OQ-4 asked whether the
# pinned wrangler (4.101.0) accepts negation patterns; it does, and the exact
# semantics are read off the shipped code rather than the docs:
#
#   * ``wrangler-dist/cli.js`` ``validateAssetsConfig`` — the field is ``boolean`` OR
#     an array of strings, and nothing else.
#   * ``parseStaticRouting`` — every rule must start with ``/`` (route to the Worker)
#     or ``!/`` (route to the assets, checked FIRST). A bare ``!`` is rejected, so the
#     ``"!*.css"`` sketched in the task spec would fail the deploy — it has to be
#     ``"!/*.css"``. At least one non-negative rule is required. Max 100 rules, max
#     100 characters each, no duplicates, and a rule ending in ``*`` is an error if
#     another rule in the SAME list shares its prefix.
#   * miniflare's ``router.worker.js`` ``generateGlobOnlyRuleRegExp`` — a rule is
#     matched against ``pathname`` as ``^`` + the rule with every ``*`` replaced by
#     ``.*`` + ``$``. So ``*`` is a wildcard ANYWHERE in the rule, and — the fact the
#     cost floor actually turns on — IT CROSSES ``/``: ``.`` in a JavaScript regex
#     without the ``s`` flag excludes only line terminators, and ``/`` is not one.
#     ``/*.html`` therefore reaches ``/docs/deep/page.html``, and a negation such as
#     ``!/*.css`` would reach ``/assets/deep/theme.css``. Had it stopped at the
#     separator, every rule here would silently miss nested paths and every nested
#     asset would fall through to the Worker — the exact breach this section exists to
#     prevent, and invisible to any test whose fixture is flat.
#
# WHY AN ALLOW-LIST AND NOT THE NEGATION FORM. Both are expressible. The deny-list
# (``["/*", "!/*.css", ...]``) routes to the Worker unless an extension was
# remembered, so every asset type nobody listed — ``.avif``, ``.woff2``, ``.webmanifest``
# — is billed at page rates, and the list rots as the generator learns new formats.
# The allow-list routes to the Worker only for paths that look like pages, so an asset
# of any extension falls through to the free path. Forgetting something costs an
# uncounted visit (an undercount) instead of a multiplied bill, which is the direction
# this is allowed to fail in.
#
# WHAT THE ALLOW-LIST DOES NOT BUY, stated plainly because it is a real cost and
# nothing here fixes it: once the config carries a ``main`` at all, the asset router's
# FINAL fallback (``router.worker.js``: ``has_user_worker && !assetsExist``) sends any
# request for a path with NO matching asset to the Worker. So 404 scans — the
# ``/wp-login.php`` traffic every public host receives — become billed invocations
# whichever rule shape is used. They are not counted as pageviews (the entry only
# counts a 200/304), but they are billed. That is an inherent consequence of putting a
# server in front of a static site, and it belongs in the feature's cost model rather
# than in a rule list.
#
# ── PRIVACY: THE PLAUSIBLE MODEL, NO COOKIE ───────────────────────────────────
#
# A visitor is identified by ``SHA-256(secret | UTC-day | ip | user-agent)`` truncated
# to 16 bytes. There is no cookie, no localStorage and no identifier that survives the
# day: the day component rotates the salt at UTC midnight, so yesterday's hashes
# cannot be linked to today's, and the raw IP is never stored.
#
# The secret is minted fresh per publish (``secrets.token_hex``) rather than derived
# from anything guessable, because a salt an attacker can reconstruct turns the hash
# into a confirmation oracle: given a candidate IP and user-agent, recompute and
# compare. It lives in the generated entry, which is why that file MUST be listed in
# the assets-only ``.assetsignore`` — an assets-only site's asset directory is the
# project root, so an unignored entry is uploaded and the salt becomes publicly
# downloadable, and the hash stops being irreversible. That ``.assetsignore`` line is
# a privacy control, not housekeeping.
#
# Minting per publish means a republish rotates the salt early, so a visitor counted
# before and after a republish on the same day counts twice. That is the safe
# direction (over-splitting, never over-linking) and a republish is rare.
#
# THE RESIDUAL EXPOSURE SA-1 NAMED HERE IS CLOSED (SA-2). The Cloudflare deploy is
# not the only thing that serves a project dir: ``sites.local_server`` copies the
# WHOLE dir and serves it, deploy scaffold included, for the local target. It already
# served ``wrangler.jsonc`` that way, and after a workers publish into the same
# per-pocket working dir it would have served this entry too, salt and all. The reach
# was loopback — a developer's own machine rather than the internet — but the salt is
# exactly what makes the visitor hash irreversible, so it does not get to leak on a
# technicality. ``local_server.persist_site`` now passes an ``ignore`` to that
# copytree, which is where SA-1 said the fix belonged.
#
# THE STALE-ENTRY CASE, which the gate created and which is handled in
# ``workers_deploy``. A site that publishes paid and then publishes free reuses the
# same working dir, so an entry written by the earlier publish is still sitting
# there. The free config names no ``main``, so wrangler would upload that leftover as
# a plain asset — the salt, downloadable, from a config that mentions nothing.
# ``_write_deploy_files`` therefore DELETES the entry whenever counting is off, and
# the ``.assetsignore`` keeps listing it unconditionally as the second line of
# defence.
#
# ── THE ANALYTICS ENGINE ROW ──────────────────────────────────────────────────
#
# The limits, from Cloudflare's own docs rather than memory: ONE index of at most 96
# bytes, up to 20 blobs totalling at most 16 KB, up to 20 doubles, and at most 250
# data points per Worker invocation. A 24-hex site id fits the index with room to
# spare. Two facts beyond the row shape matter to whoever builds the reader: data is
# retained for THREE MONTHS and then gone, and the product is on the Workers FREE plan
# too (100,000 writes and 10,000 read queries per day), so the counter does not drag
# this deploy path off the free tier it exists to stay on.
#
# The row is deliberately small, and its layout is a CONTRACT the reader (SA-2)
# depends on:
#
#   indexes[0] — the site id. The one index, so it is what the dataset is queried by.
#   blobs[0]   — request path (truncated).
#   blobs[1]   — referrer host, empty for a direct visit or a same-site link.
#   blobs[2]   — ``request.cf.country``.
#   blobs[3]   — the visitor hash.
#   blobs[4]   — the device class: ``desktop`` / ``mobile`` / ``tablet`` / ``unknown``.
#   doubles[0] — 1, the pageview.
#
# Do not reorder these without changing the reader in the same PR: Analytics Engine
# columns are positional and have no names. APPEND-ONLY for the same reason: SA-3 added
# ``blobs[4]`` at the END rather than anywhere more logical, because inserting it
# earlier would silently re-label every row already in the dataset, and the retention
# window is three months. Rows written before SA-3 carry FOUR blobs and are not
# backfilled — Analytics Engine has no update — so the reader must tolerate both
# lengths and read a missing device as ``unknown``.
#
# WHY A DEVICE CLASS IS THE ONE THING WORTH ADDING HERE, and why it is this coarse. The
# user-agent is already parsed on this path (the bot filter reads it, and it is an
# input to the visitor hash), but the hash is one-way and the string itself is never
# stored, so a device breakdown is not recoverable later from what is in the row — it
# has to be derived at write time or not at all. FOUR VALUES IS THE CEILING, not a
# starting point: the privacy claim this whole feature rests on is that a row cannot be
# traced to a person, and every bit of user-agent entropy that reaches the row chips at
# it. A browser name, a version, an OS build, a screen size — each is individually
# reasonable and collectively a fingerprint. Two bits is not.

from __future__ import annotations

import json
import os
from pathlib import Path

# The Worker entry this module generates, written at the PROJECT ROOT (not inside the
# asset dir) and named by ``main`` in the emitted wrangler config. The leading
# underscore matches the convention Cloudflare's own scaffold files use.
ENTRY_FILENAME = "_paw_analytics.js"

# The binding name the generated entry reads the dataset off ``env`` by. Deliberately
# NOT ``ANALYTICS`` — a site's own code never sees this env, but a name that generic
# would collide the moment a site-authored binding lands beside it.
DATASET_BINDING = "PAW_ANALYTICS"

# The Analytics Engine dataset every site writes into. ONE dataset for all sites,
# partitioned by the site id in ``indexes[0]`` — Analytics Engine is queried by index,
# and a dataset per site would need provisioning per site for no gain.
#
# Overridable so a dev or staging publish cannot pollute the production dataset. There
# is no per-environment config object on this path (the deploy reads ``PAW_CF_*`` vars
# straight from the environment), so an env var is the seam that already exists.
_DEFAULT_DATASET = "paw_site_pageviews"
_DATASET_ENV = "PAW_SITES_ANALYTICS_DATASET"

# THE KILL SWITCH. Set it and the very next publish emits the pre-analytics config
# again — no ``main``, no dataset binding, no routing rules, and no generated entry
# on disk — for every site, paid ones included.
#
# It exists for ONE failure mode, and the mitigation has to be faster than a deploy
# because of what that failure does. If this Cloudflare account turns out to be on
# the Workers FREE plan, a config carrying a ``main`` starts drawing on a 100,000
# request/day ceiling that is ACCOUNT-WIDE, and breaching it does not degrade
# analytics — Cloudflare stops serving the Workers behind it. Every published site
# goes dark together. A code change plus a release is minutes at best; an
# environment variable plus a republish is the length of one publish.
#
# The value is read at PUBLISH time, not at import, so setting it takes effect for
# the next publish without restarting anything that is already running.
_DISABLED_ENV = "PAW_SITES_ANALYTICS_DISABLED"

# The truthy spellings, matching ``sites.service``'s own env flags. A kill switch
# read with ``bool(os.environ.get(...))`` would fire on ``=0`` and ``=false``, which
# is the wrong direction to be surprising in — an operator who explicitly writes
# "false" must not silently disable the feature.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Page suffixes the route enumeration walks — the same pair ``badge.py`` and
# ``paw_bar/embed.py`` treat as pages, so a file that earns a badge is a file that
# earns a pageview.
_HTML_SUFFIXES = (".html", ".htm")

# The rules that hold for every site regardless of what it built:
#
#   ``/``        the root page. An exact match — ``^/$`` — so it cannot widen.
#   ``/*.html``  any path ending .html, wherever it sits in the tree.
#   ``/*.htm``   the same for the other suffix the generator can emit.
#   ``/*/``      any path ending in a slash: a directory-style URL, which
#                ``html_handling`` resolves to that directory's index page.
#
# None of these can match a stylesheet, a script, a font or an image, which is the
# whole point. None of them ends in ``*``, so wrangler's redundancy check (a rule
# ending in ``*`` forbids any other rule sharing its prefix) has nothing to fire on.
_BASE_ROUTE_RULES: tuple[str, ...] = ("/", "/*.html", "/*.htm", "/*/")

# wrangler's own caps are 100 rules of at most 100 characters. We stop well short: the
# enumeration below is a convenience for clean URLs on a small brochure site, and a
# site large enough to approach the cap has long since stopped being one. Truncating
# costs uncounted clean-URL visits on the pages past the cap, never a wrong bill.
_MAX_ROUTE_RULES = 64
_MAX_RULE_LENGTH = 100

# How much of the request path reaches the row. Analytics Engine caps a data point's
# total blob bytes, and a path is the only field here a visitor controls the length of.
_PATH_MAX = 256


def dataset_name() -> str:
    """The Analytics Engine dataset name this deploy writes into.

    ``PAW_SITES_ANALYTICS_DATASET`` overrides the default so a non-production publish
    writes somewhere else. An empty or whitespace-only value falls back rather than
    emitting an empty dataset name, which wrangler would accept and Cloudflare would
    not.
    """
    return os.environ.get(_DATASET_ENV, "").strip() or _DEFAULT_DATASET


def counting_disabled() -> bool:
    """Is the operator kill switch set? See ``_DISABLED_ENV`` for what it is for.

    Separate from ``counting_enabled`` below so a test — and an operator reading a
    log line — can tell "this site is not entitled" apart from "counting is off
    everywhere". They call for completely different responses.
    """
    return os.environ.get(_DISABLED_ENV, "").strip().lower() in _TRUTHY


def counting_enabled(*, entitled: bool) -> bool:
    """Does THIS publish carry a pageview counter?

    The whole decision in one place: the site's plan entitles it AND the operator
    has not pulled the switch. Both halves have to be false-able independently —
    the entitlement is per site and answers to billing, the switch is global and
    answers to an incident — but the config builder needs one boolean, and deriving
    it at each call site is how the two halves eventually stop agreeing.

    ``entitled`` is resolved by ``entitlements.site_analytics_entitled`` at the
    publish seam, which is the only layer that can see a ``Site`` document. This
    module deliberately does not resolve it: it would have to reach across into the
    cloud billing layer to do so, and a generator of Worker source is the wrong
    place to decide who is paying.
    """
    return entitled and not counting_disabled()


def _clean_url(rel_posix: str, suffix: str) -> str:
    """The extensionless URL that ``html_handling`` serves this page at.

    ``about.html`` → ``/about``; ``docs/index.html`` → ``/docs``; ``index.html`` →
    ``/`` (already covered by the base rules, and deduped away by the caller).
    """
    stem = rel_posix[: -len(suffix)]
    if stem == "index":
        return "/"
    if stem.endswith("/index"):
        stem = stem[: -len("/index")]
    return f"/{stem}"


def run_worker_first_rules(out_dir: Path) -> list[str]:
    """The ``assets.run_worker_first`` allow-list for a built static tree.

    The base rules cover the shapes every site has — the root, ``.html``/``.htm``
    paths, and directory-style URLs. On top of them this enumerates the EXTENSIONLESS
    clean URL of every page in the tree (``/about`` for ``about.html``), because
    ``html_handling`` serves those and no glob can express "a last segment with no
    extension" without also matching every extension we are trying to avoid.

    Missing a route here is an undercount, never an overcharge — see the cost-floor
    note at the top of this module. A missing directory returns the base rules rather
    than raising: the caller is about to fail on the missing build against a concrete
    path, which is a truer error than this function refusing to decide.

    The result satisfies every constraint wrangler's ``parseStaticRouting`` enforces:
    every rule starts with ``/``, there is at least one non-negative rule, rules are
    unique, each is under 100 characters, and the list is capped well under 100.
    """
    rules = list(_BASE_ROUTE_RULES)
    seen = set(rules)
    if not out_dir.is_dir():
        return rules
    for path in sorted(out_dir.rglob("*")):
        if len(rules) >= _MAX_ROUTE_RULES:
            break
        suffix = path.suffix.lower()
        if suffix not in _HTML_SUFFIXES or not path.is_file():
            continue
        rule = _clean_url(path.relative_to(out_dir).as_posix(), suffix)
        if rule in seen or len(rule) > _MAX_RULE_LENGTH:
            continue
        seen.add(rule)
        rules.append(rule)
    return rules


def build_entry_js(*, site_id: str, secret: str) -> str:
    """The Worker entry that counts one pageview and then serves the page.

    ``site_id`` becomes ``indexes[0]`` — the one index Analytics Engine allows, and
    therefore the only thing the dataset can be queried by. ``secret`` is the salt
    base described at the top of this module; the caller mints a fresh one per publish.
    Both are embedded through ``json.dumps``, which produces a valid JavaScript string
    literal for any input (ASCII-escaped, so nothing in a site id can break out of the
    quotes or smuggle a line terminator).

    The helpers are NAMED EXPORTS beside the default handler so they can be driven
    directly by a test — the alternative is asserting on the generated source text,
    which pins the spelling of the code rather than what it does. workerd only treats
    an export specially when it is a class extending a known entrypoint base, so plain
    functions ride along unused.
    """
    return f"""// Generated by ee/pocketpaw_ee/sites/analytics_worker.py at publish. Do not edit:
// a republish overwrites this file.
//
// Counts one Analytics Engine row per pageview, then serves the page from the ASSETS
// binding. Every counting path is failure-soft — a throw here must never cost the
// visitor the page.

const SITE_ID = {json.dumps(site_id)};
const SALT = {json.dumps(secret)};
const DATASET_BINDING = {json.dumps(DATASET_BINDING)};
const PATH_MAX = {_PATH_MAX};

// Obvious automated traffic, matched on the user-agent. Bot rows are neither stored
// nor billed for storage, and a bot in the pageview count is worse than a gap in it.
// This is deliberately a coarse net: it catches the crawlers, monitors, previewers
// and HTTP clients that announce themselves, and does not try to catch one that lies.
// An ABSENT user-agent counts as a bot — every real browser sends one.
//
// Every token here is long enough to be unambiguous. A false positive costs a real
// visit, silently and permanently, so the bar for adding one is that no shipping
// browser's user-agent can contain it: `ping` and `search` were both dropped for
// failing that, and `pingdom` carries the case that motivated `ping`.
const BOT_RE =
  /(bot|crawl|spider|slurp|scrape|preview|monitor|uptime|pingdom|lighthouse|pagespeed|headless|phantom|puppeteer|playwright|selenium|curl|wget|python-requests|httpx|aiohttp|go-http-client|java\\/|okhttp|axios|node-fetch|libwww|apachebench|facebookexternalhit|embedly|whatsapp|telegram|discord|slackbot|vkshare|skypeuripreview)/i;

export function isBot(ua) {{
  if (!ua) return true;
  return BOT_RE.test(ua);
}}

// The device class stored as blobs[4] — FOUR VALUES AND NO MORE. See the row contract
// in analytics_worker.py for why the ceiling is the point rather than a first pass: the
// user-agent is the highest-entropy thing this Worker touches, and the row's privacy
// claim survives only while what lands in it cannot single anyone out.
//
// ORDER IS LOAD-BEARING. Tablet is tested FIRST because a tablet's user-agent is a
// superset of a phone's on both platforms that matter: an iPad announces `iPad` beside
// `Mobile`, and an Android tablet is an Android that OMITS `Mobile` — which is why the
// Android arm is a negative lookahead rather than a token. Testing mobile first would
// file every iPad under mobile and every Android tablet under desktop.
//
// Desktop is a POSITIVE match on a platform token, not the fallback. Making it the
// fallback would label every unrecognised string `desktop` and quietly inflate the one
// number a site owner is most likely to act on; `unknown` is the honest answer for a
// user-agent this does not recognise, and it stays reachable because of that choice.
const TABLET_RE = /(ipad|tablet|playbook|silk|kindle|android(?!.*mobile))/i;
const MOBILE_RE = /(android|iphone|ipod|iemobile|blackberry|opera mini|mobile|phone)/i;
const DESKTOP_RE = /(windows nt|macintosh|mac os x|x11|linux|cros)/i;

export function deviceClass(ua) {{
  if (!ua) return "unknown";
  if (TABLET_RE.test(ua)) return "tablet";
  if (MOBILE_RE.test(ua)) return "mobile";
  if (DESKTOP_RE.test(ua)) return "desktop";
  return "unknown";
}}

// The salt's rotating half: the UTC calendar day. Rotating at midnight is what makes
// the visitor hash self-expiring — today's hash cannot be joined to yesterday's.
export function dayKey(nowMs) {{
  return new Date(nowMs).toISOString().slice(0, 10);
}}

// SHA-256 over the secret, the day, the IP and the user-agent, truncated to 16 bytes.
// Truncation is a storage decision, not a security one: 128 bits is far past the
// collision budget of one site-day, and the row is smaller for it.
export async function visitorHash(ip, ua, secret, day) {{
  const data = new TextEncoder().encode(`${{secret}}|${{day}}|${{ip}}|${{ua}}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  const bytes = new Uint8Array(digest).subarray(0, 16);
  let hex = "";
  for (const byte of bytes) hex += byte.toString(16).padStart(2, "0");
  return hex;
}}

// A same-site referrer is not a referrer — it is internal navigation, and reporting it
// as one buries the real acquisition sources under the site's own pages.
function referrerHost(request, selfHost) {{
  const raw = request.headers.get("referer");
  if (!raw) return "";
  try {{
    const host = new URL(raw).host;
    return host && host !== selfHost ? host : "";
  }} catch (err) {{
    return "";
  }}
}}

// Never rejects. Every failure — no binding, a binding without writeDataPoint, an
// unparseable URL, a crypto error — resolves quietly, because the caller may hand
// this to waitUntil where a rejection is an error the visitor paid for.
export async function count(request, env, nowMs) {{
  try {{
    const dataset = env && env[DATASET_BINDING];
    if (!dataset || typeof dataset.writeDataPoint !== "function") return;
    const ua = request.headers.get("user-agent") || "";
    if (isBot(ua)) return;
    const url = new URL(request.url);
    const ip = request.headers.get("cf-connecting-ip") || "";
    const day = dayKey(typeof nowMs === "number" ? nowMs : Date.now());
    const visitor = await visitorHash(ip, ua, SALT, day);
    dataset.writeDataPoint({{
      indexes: [SITE_ID],
      blobs: [
        url.pathname.slice(0, PATH_MAX),
        referrerHost(request, url.host),
        (request.cf && request.cf.country) || "",
        visitor,
        deviceClass(ua),
      ],
      doubles: [1],
    }});
  }} catch (err) {{
    // Swallowed on purpose. See the failure-soft note in analytics_worker.py.
  }}
}}

export default {{
  async fetch(request, env, ctx) {{
    const response = await env.ASSETS.fetch(request);
    try {{
      // 200 and 304 are the two ways a page is actually delivered. A redirect is not
      // a pageview (the browser follows it and the target counts), and a 404 is not
      // one either — which is what keeps path-scanning bots out of the numbers even
      // though the asset router still invokes us for them.
      if (response.status === 200 || response.status === 304) {{
        const pending = count(request, env);
        if (ctx && typeof ctx.waitUntil === "function") ctx.waitUntil(pending);
      }}
    }} catch (err) {{
      // Counting must never reach the response.
    }}
    return response;
  }},
}};
"""


__all__ = [
    "DATASET_BINDING",
    "ENTRY_FILENAME",
    "build_entry_js",
    "counting_disabled",
    "counting_enabled",
    "dataset_name",
    "run_worker_first_rules",
]

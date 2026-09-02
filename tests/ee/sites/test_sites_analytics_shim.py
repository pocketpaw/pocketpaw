# tests/ee/sites/test_sites_analytics_shim.py — SA-3, the SERVER-WORKER counter: the
# generated shim that wraps adapter-cloudflare's ``_worker.js``
# (``sites/analytics_worker.build_shim_js``) and the ripple / dynamic-svelte deploy
# shape that carries it (``sites/workers_deploy.py``).
#
# Created 2026-09-02 (feat/sites-analytics-shim).
#
# THE TWO CLAIMS THIS FILE EXISTS TO KEEP HONEST, because both fail silently:
#
#   1. THE SITE STILL WORKS. The shim sits in front of the module that RENDERS a
#      dynamic site. Everything the SvelteKit worker needs has to survive being wrapped
#      — ``nodejs_compat``, the ``ASSETS`` binding, the D1 binding its remote functions
#      reach their database by — and the response it produced has to come back
#      unchanged, status and headers included. A counter that quietly drops a header or
#      a binding trades a working site for a counted broken one, and the pageview
#      numbers would look fine.
#
#   2. THE COUNTER ACTUALLY RUNS. With a ``main`` and an ``assets`` block and no
#      routing rules, Cloudflare's asset router serves an EXISTING asset itself and only
#      falls through to the Worker when none matches — which is exactly how a ripple
#      site's prerendered pages are served. A shim deployed without
#      ``run_worker_first`` would be invoked for SSR routes and nothing else, count
#      almost nothing, and look completely deployed while doing it. That is
#      ``test_a_prerendered_page_reaches_the_shim``, and it is the reason this branch's
#      config gained rules at all.
#
# The routing decision is REPLAYED through SA-1's transcription of Cloudflare's own
# router (``_route_for``) rather than transcribed a second time here. Two copies of an
# algorithm read off a shipped bundle is two copies that can drift, and the one that
# drifts is the one that stops proving the cost floor.
#
# The generated JavaScript is DRIVEN, not pattern-matched, for the reason SA-1 gives:
# grepping the source for ``writeDataPoint`` pins the spelling of the code and cannot
# tell a failure-soft counter from one that throws. The shim is written into a project
# tree with a real ``_worker.js`` at the path it imports, so the driver also proves the
# import specifier resolves — a specifier that is wrong fails the deploy, and no
# assertion about the config would notice.

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import analytics_worker, local_server, workers_deploy

from tests.ee.sites.test_sites_analytics_counter import _route_for, _wrangler_rule_errors

SITE_ID = "507f1f77bcf86cd799439011"

HUMAN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

OUTPUT_REL = ".svelte-kit/cloudflare"

# What a ripple page pulls in. All of these EXIST in the fixture tree below, because the
# asset router's fallback sends a request for a MISSING path to the Worker whatever the
# rules say — a test using invented paths would prove nothing about the rules. Nesting
# is the point of most of them: a rule's ``*`` expands to ``.*``, which crosses ``/``,
# so a flat fixture passes identically whether or not the rules reach into a directory.
SUBRESOURCE_PATHS = (
    "/_app/immutable/entry/start.js",
    "/_app/immutable/chunks/index.js",
    "/_app/immutable/nodes/0.js",
    "/_app/immutable/assets/brand.css",
    "/_app/version.json",
    "/favicon.png",
    "/fonts/inter.woff2",
)

# The paths that are pages and must reach the shim to be counted.
PAGE_PATHS = ("/", "/index.html", "/about.html", "/about", "/docs/", "/docs/guide.html")


def _build_ripple_project(tmp_path: Path) -> Path:
    """A built ripple / dynamic-svelte project: adapter-cloudflare's output tree, with
    the prerendered pages and the ``_app/immutable`` bundle a real build emits.

    ``package.json`` carries ``type: module`` because the scaffold's does
    (``paw-sites``'s svelte-scaffold writes it), which is what makes the ``.js`` files
    below ES modules to node — and therefore what lets the driver import the shim at
    all."""
    project = tmp_path / "project"
    out = project / OUTPUT_REL
    (out / "_app" / "immutable" / "entry").mkdir(parents=True)
    (out / "_app" / "immutable" / "chunks").mkdir(parents=True)
    (out / "_app" / "immutable" / "nodes").mkdir(parents=True)
    (out / "_app" / "immutable" / "assets").mkdir(parents=True)
    (out / "fonts").mkdir(parents=True)
    (out / "docs").mkdir(parents=True)

    (project / "package.json").write_text('{"name":"paw-site-x","type":"module"}')
    (out / "_worker.js").write_text(_ADAPTER_WORKER, encoding="utf-8")
    (out / "index.html").write_text("<!doctype html><h1>home</h1>")
    (out / "about.html").write_text("<!doctype html><h1>about</h1>")
    (out / "docs" / "index.html").write_text("<!doctype html><h1>docs</h1>")
    (out / "docs" / "guide.html").write_text("<!doctype html><h1>guide</h1>")
    (out / "_app" / "immutable" / "entry" / "start.js").write_text("export default 1")
    (out / "_app" / "immutable" / "chunks" / "index.js").write_text("export default 2")
    (out / "_app" / "immutable" / "nodes" / "0.js").write_text("export default 3")
    (out / "_app" / "immutable" / "assets" / "brand.css").write_text("body{margin:0}")
    (out / "_app" / "version.json").write_text('{"version":"1"}')
    (out / "favicon.png").write_bytes(b"\x89PNG\r\n")
    (out / "fonts" / "inter.woff2").write_bytes(b"wOF2")
    return project


# A stand-in for adapter-cloudflare's ``_worker.js``, in ITS export shape: a plain
# object default export with an async ``fetch``, plus one other handler so the test can
# see whether the shim forwards it. Checked against the pinned adapter (``^7.0.0``;
# npm's latest 7 is 7.2.9), whose ``index.js`` COPIES ``src/worker.js`` to a single-file
# ``_worker.js`` exporting exactly ``export default { async fetch(req, env, ctx) }`` and
# nothing else.
#
# It echoes what it was given, so a test can tell a delegated response from a fabricated
# one and can see whether ``env`` and ``ctx`` arrived intact.
_ADAPTER_WORKER = """export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/boom") throw new Error("the site's own worker failed");
    const status = Number(url.searchParams.get("status") || 200);
    return new Response(`rendered ${url.pathname}`, {
      status,
      headers: {
        "content-type": "text/html; charset=utf-8",
        "x-sveltekit": "rendered",
        "set-cookie": "sid=abc; Path=/",
        "x-saw-assets": String(Boolean(env && env.ASSETS)),
        "x-saw-db": String(Boolean(env && env.DB)),
        "x-saw-ctx": String(Boolean(ctx && typeof ctx.waitUntil === "function")),
      },
    });
  },
  scheduled() {
    return "the adapter's other handler";
  },
};
"""


def _write_ripple_deploy(tmp_path: Path, **kwargs) -> tuple[Path, dict]:
    """Run the real deploy-file write for a ripple site; return the project dir and the
    parsed config."""
    project = _build_ripple_project(tmp_path)
    workers_deploy._write_deploy_files(
        str(project),
        f"paw-site-{SITE_ID}",
        "ripple",
        kwargs.pop("d1", None),
        site_id=SITE_ID,
        **kwargs,
    )
    return project, json.loads((project / "wrangler.jsonc").read_text())


# ── the emitted config ───────────────────────────────────────────────────────


def test_a_counting_ripple_config_points_main_at_the_shim(tmp_path):
    """``main`` moves to the generated shim, and everything the SvelteKit worker needs
    at runtime moves with it: ``nodejs_compat`` and the ``ASSETS`` binding it reads
    prerendered pages and static files through. The dataset binding is what makes the
    counting possible at all."""
    project, cfg = _write_ripple_deploy(tmp_path)

    assert cfg["main"] == analytics_worker.SHIM_FILENAME
    assert (project / cfg["main"]).is_file()
    assert cfg["compatibility_flags"] == ["nodejs_compat"]
    assert cfg["assets"]["binding"] == "ASSETS"
    assert cfg["assets"]["directory"] == OUTPUT_REL
    assert cfg["analytics_engine_datasets"] == [
        {"binding": analytics_worker.DATASET_BINDING, "dataset": analytics_worker.dataset_name()}
    ]
    # The counter never points into the build output; the adapter's worker is reached by
    # an import, not by a config key.
    assert cfg["main"] != f"{OUTPUT_REL}/_worker.js"


def test_a_counting_dynamic_site_keeps_its_database(tmp_path):
    """The binding a dynamic site's remote functions read (``platform.env.DB``) survives
    the shim.

    Stated as the failure it prevents: a site that renders live data would go on
    rendering, but every query would fail on a missing binding — and the pageview
    numbers would look perfect while it happened."""
    project, cfg = _write_ripple_deploy(tmp_path, d1="d1-uuid-0001")

    assert cfg["main"] == analytics_worker.SHIM_FILENAME
    assert cfg["d1_databases"] == [
        {
            "binding": "DB",
            "database_name": f"paw-site-{SITE_ID}",
            "database_id": "d1-uuid-0001",
        }
    ]
    assert cfg["compatibility_flags"] == ["nodejs_compat"]


def test_the_adapters_worker_is_never_modified(tmp_path):
    """The adapter owns ``_worker.js`` and regenerates it on every build, so an edit
    would survive exactly one build. Asserted on the BYTES, before and after the deploy
    write, because "we only append" is the kind of claim that stops being true
    quietly."""
    project = _build_ripple_project(tmp_path)
    worker_path = project / OUTPUT_REL / "_worker.js"
    before = worker_path.read_bytes()

    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "ripple", None, site_id=SITE_ID
    )

    assert worker_path.read_bytes() == before


def test_the_shim_is_excluded_from_the_uploaded_tree(tmp_path):
    """The shim carries the per-publish hash salt, exactly as the assets-only entry
    does, so it is named in the ``.assetsignore`` on every publish. Removing that line
    does not break a deploy — it breaks the privacy claim, silently."""
    project, _ = _write_ripple_deploy(tmp_path)

    lines = (project / OUTPUT_REL / ".assetsignore").read_text().splitlines()

    assert analytics_worker.SHIM_FILENAME in lines
    assert analytics_worker.ENTRY_FILENAME in lines
    # The proven recipe's own three lines are still first and still in order.
    assert lines[:3] == ["_worker.js", "_routes.json", "_headers"]


# ── the routing rules, without which the shim never runs ─────────────────────


def test_a_prerendered_page_reaches_the_shim(tmp_path):
    """THE REASON THIS BRANCH'S CONFIG GAINED RULES AT ALL.

    With a ``main`` and no ``run_worker_first``, Cloudflare's asset router serves any
    request whose asset EXISTS and only falls through to the Worker when none does. A
    ripple site's pages are prerendered into the asset dir, so the shim would have been
    invoked for SSR routes and nothing else — counting almost nothing while looking
    completely deployed.

    Every page shape is checked with ``asset_exists=True`` on purpose: that is the case
    the rules have to win, and the one a fixture without prerendered files would miss.
    """
    _, cfg = _write_ripple_deploy(tmp_path)
    rules = cfg["assets"]["run_worker_first"]

    for path in PAGE_PATHS:
        assert _route_for(rules, path, asset_exists=True) == "worker", (
            f"{path} is a prerendered page served straight from the assets, so it would "
            f"go uncounted; rules={rules}"
        )


def test_the_cost_floor_holds_on_the_server_worker_branch_too(tmp_path):
    """A page pulls in roughly twenty subresources, and Cloudflare bills an invocation
    where it serves an asset free. The rules that make pages reach the shim must not
    drag the ``_app/immutable`` bundle, the fonts and the icons along with them —
    routing those through the Worker multiplies the per-pageview cost by about twenty,
    which is a defect and not a tuning choice."""
    _, cfg = _write_ripple_deploy(tmp_path)
    rules = cfg["assets"]["run_worker_first"]

    for path in SUBRESOURCE_PATHS:
        assert _route_for(rules, path, asset_exists=True) == "assets", (
            f"{path} would invoke the Worker and be billed as a pageview; rules={rules}"
        )


def test_the_server_worker_rules_pass_wranglers_own_validation(tmp_path):
    """A rule wrangler rejects is not a cheaper deploy — it is no deploy at all, and on
    this branch that takes a working dynamic site down with it."""
    _, cfg = _write_ripple_deploy(tmp_path)

    assert _wrangler_rule_errors(cfg["assets"]["run_worker_first"]) == []


def test_an_ssr_route_still_reaches_the_worker(tmp_path):
    """The half the rules must not break. A dynamic route is prerendered nowhere, so no
    asset matches it and the router's fallback carries it to the Worker regardless of
    the rules — which is how a dynamic site worked before this slice and has to keep
    working after it."""
    _, cfg = _write_ripple_deploy(tmp_path)
    rules = cfg["assets"]["run_worker_first"]

    assert _route_for(rules, "/dashboard/orders/42", asset_exists=False) == "worker"
    assert _route_for(rules, "/api/submit", asset_exists=False) == "worker"


# ── the free tier, and the stale counter a republish leaves ──────────────────


@pytest.mark.asyncio
async def test_a_free_ripple_publish_deploys_no_counter(tmp_path, monkeypatch):
    """End to end through the public deploy entry, because that is the seam both publish
    lanes reach.

    A Worker invocation is billed and a static asset is not, and on this branch counting
    ALSO turns prerendered pages that were served free into billed invocations. A free
    site must get the config that was proven before analytics existed."""

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"https://paw-site-x.acct.workers.dev\n", b""

    async def _fake_exec(*argv, cwd=None, env=None, stdout=None, stderr=None):
        return _Proc()

    monkeypatch.setattr(workers_deploy.asyncio, "create_subprocess_exec", _fake_exec)
    project = _build_ripple_project(tmp_path)

    await workers_deploy.deploy_workers(
        SITE_ID, str(project), engine="ripple", analytics_entitled=False
    )

    cfg = json.loads((project / "wrangler.jsonc").read_text())
    assert cfg["main"] == f"{OUTPUT_REL}/_worker.js"
    assert "analytics_engine_datasets" not in cfg
    assert "run_worker_first" not in cfg["assets"]
    assert not (project / analytics_worker.SHIM_FILENAME).exists()


@pytest.mark.asyncio
async def test_the_public_deploy_entry_writes_the_shim(tmp_path, monkeypatch):
    """The seam pin, matching SA-1's. Both publish lanes end at ``deploy_workers``, so
    the counter has to land when THAT is called and not merely when the private writer
    is. The build host is never the deploy host — Daytona builds and the worker
    deploys — and this is the one deploy implementation both lanes share."""

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"https://paw-site-x.acct.workers.dev\n", b""

    async def _fake_exec(*argv, cwd=None, env=None, stdout=None, stderr=None):
        return _Proc()

    monkeypatch.setattr(workers_deploy.asyncio, "create_subprocess_exec", _fake_exec)
    project = _build_ripple_project(tmp_path)

    await workers_deploy.deploy_workers(SITE_ID, str(project), engine="ripple")

    cfg = json.loads((project / "wrangler.jsonc").read_text())
    assert cfg["main"] == analytics_worker.SHIM_FILENAME
    assert (project / cfg["main"]).is_file()
    for path in SUBRESOURCE_PATHS:
        assert _route_for(cfg["assets"]["run_worker_first"], path, asset_exists=True) == "assets"


def test_a_paid_then_free_republish_leaves_no_shim_behind(tmp_path):
    """A publish reuses the pocket's working dir, so a site that publishes paid and then
    free still has the earlier shim on disk. The free config names no shim, and the
    shim carries the salt the visitor hash is built on — wrangler would upload it as an
    ordinary asset and hand that salt to anyone who asks."""
    project = _build_ripple_project(tmp_path)
    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "ripple", None, site_id=SITE_ID
    )
    assert (project / analytics_worker.SHIM_FILENAME).is_file()

    workers_deploy._write_deploy_files(
        str(project),
        f"paw-site-{SITE_ID}",
        "ripple",
        None,
        site_id=SITE_ID,
        analytics_entitled=False,
    )

    assert not (project / analytics_worker.SHIM_FILENAME).exists()


def test_the_other_branchs_counter_is_removed_too(tmp_path):
    """The stale file a change of BUILD SHAPE leaves, which is the case a single-file
    delete misses. The two counters are not interchangeable — one imports the adapter's
    worker, the other serves through ``ASSETS`` — so whichever this publish did not
    write is a salt with no config naming it, exactly like a leftover from the free
    tier."""
    project = _build_ripple_project(tmp_path)
    stale_entry = project / analytics_worker.ENTRY_FILENAME
    stale_entry.write_text("// left by an earlier assets-only publish\n")

    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "ripple", None, site_id=SITE_ID
    )

    assert (project / analytics_worker.SHIM_FILENAME).is_file()
    assert not stale_entry.exists()


def test_the_kill_switch_reaches_the_server_worker_branch(tmp_path, monkeypatch):
    """The operator switch has to be global or it is not a mitigation. Its one failure
    mode is an account-wide Workers request ceiling, which stops sites being served
    rather than degrading analytics — so a branch that kept counting through it would
    keep the account against the ceiling."""
    monkeypatch.setenv("PAW_SITES_ANALYTICS_DISABLED", "1")
    project, cfg = _write_ripple_deploy(tmp_path)

    assert cfg["main"] == f"{OUTPUT_REL}/_worker.js"
    assert "analytics_engine_datasets" not in cfg
    assert not (project / analytics_worker.SHIM_FILENAME).exists()


# ── driving the generated shim under node ────────────────────────────────────

_DRIVER_PRELUDE = """
import shim from "./%s";

const request = (url, headers = {}, cf = {}) => ({
  url,
  headers: { get: (k) => headers[k.toLowerCase()] ?? null },
  cf,
});
const recorder = () => {
  const rows = [];
  return { rows, writeDataPoint: (row) => rows.push(row) };
};
const emit = (out) => console.log(JSON.stringify(out));
const HUMAN = %s;
"""


def _run_node(tmp_path: Path, body: str) -> dict:
    """Write a real project tree, run the real deploy write into it, then drive the
    generated shim under node.

    The shim is driven WHERE IT WAS WRITTEN rather than copied somewhere flat, because
    its import specifier is relative to the project root: a wrong specifier resolves to
    nothing and fails the deploy, and every assertion about the config would still pass.
    Renamed to ``.mjs`` only so node reads the DRIVER as a module; the shim keeps the
    name and the location the deploy gave it.

    Skips when node is absent, the convention ``test_dev_bridge_source.py`` established
    — the config assertions above run on any machine."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH — the generated shim cannot be driven")

    project = _build_ripple_project(tmp_path)
    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "ripple", None, site_id=SITE_ID
    )
    driver = project / "driver.mjs"
    driver.write_text(
        (_DRIVER_PRELUDE % (analytics_worker.SHIM_FILENAME, json.dumps(HUMAN_UA))) + body,
        encoding="utf-8",
    )

    proc = subprocess.run([node, str(driver)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"node driver failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_delegated_response_passes_through_unchanged(tmp_path):
    """The site's own response, byte for byte and header for header.

    A wrapper that rebuilds the response is the quiet way to break a dynamic site: a
    dropped ``set-cookie`` is a lost session, a dropped ``content-type`` is a page the
    browser renders as text, and both look like a working deploy. The env and the ctx
    the site's worker was handed are echoed back too, since a wrapper that forwards a
    response but not its arguments breaks rendering instead."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const pending = [];
const res = await shim.fetch(
  request("https://site.example.dev/about", { "user-agent": HUMAN }),
  { ASSETS: {}, DB: {}, PAW_ANALYTICS: rec },
  { waitUntil: (p) => pending.push(p) },
);
await Promise.all(pending);
emit({
  status: res.status,
  body: await res.text(),
  contentType: res.headers.get("content-type"),
  sveltekit: res.headers.get("x-sveltekit"),
  cookie: res.headers.get("set-cookie"),
  sawAssets: res.headers.get("x-saw-assets"),
  sawDb: res.headers.get("x-saw-db"),
  sawCtx: res.headers.get("x-saw-ctx"),
  rows: rec.rows.length,
});
""",
    )

    assert out["status"] == 200
    assert out["body"] == "rendered /about"
    assert out["contentType"] == "text/html; charset=utf-8"
    assert out["sveltekit"] == "rendered"
    assert out["cookie"] == "sid=abc; Path=/"
    assert out["sawAssets"] == "true"
    assert out["sawDb"] == "true"
    assert out["sawCtx"] == "true"
    assert out["rows"] == 1


def test_a_throwing_write_data_point_still_serves_the_page(tmp_path):
    """FAILURE-SOFT, the posture that separates this from ``badge.py``. A pageview is
    worth nothing next to the page, so a throw inside the counter must not reach the
    response — and must not surface later as a rejected ``waitUntil`` either, which on
    Cloudflare is an error the visitor already paid for."""
    out = _run_node(
        tmp_path,
        """
const pending = [];
const res = await shim.fetch(
  request("https://site.example.dev/", { "user-agent": HUMAN }),
  {
    ASSETS: {},
    PAW_ANALYTICS: { writeDataPoint: () => { throw new Error("analytics is down"); } },
  },
  { waitUntil: (p) => pending.push(p) },
);
const body = await res.text();
let settled = "resolved";
try { await Promise.all(pending); } catch (err) { settled = "rejected"; }
emit({ status: res.status, body, settled });
""",
    )

    assert out["status"] == 200
    assert out["body"] == "rendered /"
    assert out["settled"] == "resolved"


def test_a_missing_analytics_binding_still_serves_the_page(tmp_path):
    """The other failure a deploy can actually produce: the dataset binding absent from
    ``env`` — a config that lost it, an account without the product. The page is served
    exactly the same, and nothing rejects."""
    out = _run_node(
        tmp_path,
        """
const pending = [];
const res = await shim.fetch(
  request("https://site.example.dev/", { "user-agent": HUMAN }),
  { ASSETS: {} },
  { waitUntil: (p) => pending.push(p) },
);
const body = await res.text();
let settled = "resolved";
try { await Promise.all(pending); } catch (err) { settled = "rejected"; }
emit({ status: res.status, body, settled });
""",
    )

    assert out["status"] == 200
    assert out["body"] == "rendered /"
    assert out["settled"] == "resolved"


def test_the_sites_own_failure_is_not_swallowed(tmp_path):
    """The one thing on this path that must NOT be failure-soft, stated so it cannot be
    "hardened" away later.

    Failure-soft covers the counting, which is worth nothing next to the page. It does
    not cover the page: a throw from the site's own worker has to surface exactly as it
    would without a shim in front of it. Swallowing it would replace a clear error with
    a blank response nobody can trace back to the site's own code."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
let raised = "";
try {
  await shim.fetch(
    request("https://site.example.dev/boom", { "user-agent": HUMAN }),
    { ASSETS: {}, PAW_ANALYTICS: rec },
    { waitUntil: () => {} },
  );
} catch (err) {
  raised = err.message;
}
emit({ raised, rows: rec.rows.length });
""",
    )

    assert out["raised"] == "the site's own worker failed"
    assert out["rows"] == 0


def test_a_redirect_and_a_404_pass_through_and_are_not_counted(tmp_path):
    """Only a delivered page is a pageview. A SvelteKit worker answers redirects (the
    trailing-slash form of every prerendered page) and 404s, and the asset router's
    fallback sends every unmatched path here — so path-scanning bots reach the shim
    however the rules are written. They must not reach the numbers, and their responses
    must still be passed back untouched."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const pending = [];
const env = { ASSETS: {}, PAW_ANALYTICS: rec };
const ctx = { waitUntil: (p) => pending.push(p) };
const redirect = await shim.fetch(
  request("https://site.example.dev/docs?status=308", { "user-agent": HUMAN }), env, ctx);
const missing = await shim.fetch(
  request("https://site.example.dev/wp-login.php?status=404", { "user-agent": HUMAN }), env, ctx);
await Promise.all(pending);
emit({ redirect: redirect.status, missing: missing.status, rows: rec.rows.length });
""",
    )

    assert out["redirect"] == 308
    assert out["missing"] == 404
    assert out["rows"] == 0


def test_the_shim_forwards_the_adapters_other_handlers(tmp_path):
    """``...worker`` is not decoration. adapter-cloudflare exports only ``fetch`` today,
    but a wrapper that names one method silently drops every other one an entry can
    carry (``scheduled``, ``queue``, ``email``) — and a dropped handler is a feature
    that stops firing with nothing logged. Spreading forwards them; overriding ``fetch``
    afterwards is what makes this a shim rather than a replacement."""
    out = _run_node(
        tmp_path,
        """
emit({ scheduled: typeof shim.scheduled === "function" ? shim.scheduled() : null });
""",
    )

    assert out["scheduled"] == "the adapter's other handler"


def test_the_counted_row_is_the_same_row_the_assets_only_entry_writes(tmp_path):
    """One counting core, one row shape. The reader is built against a single layout, so
    a shim that wrote its own would put two incompatible shapes in one dataset —
    positionally, with no column names to tell them apart."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const pending = [];
await shim.fetch(
  request(
    "https://site.example.dev/docs/guide.html",
    { "user-agent": HUMAN, "cf-connecting-ip": "203.0.113.9",
      referer: "https://news.example.com/story" },
    { country: "DE" },
  ),
  { ASSETS: {}, PAW_ANALYTICS: rec },
  { waitUntil: (p) => pending.push(p) },
);
await Promise.all(pending);
emit({ rows: rec.rows });
""",
    )

    row = out["rows"][0]
    assert row["indexes"] == [SITE_ID]
    assert row["blobs"][0] == "/docs/guide.html"
    assert row["blobs"][1] == "news.example.com"
    assert row["blobs"][2] == "DE"
    assert re.fullmatch(r"[0-9a-f]{32}", row["blobs"][3])
    assert row["blobs"][4] == "desktop"
    assert row["doubles"] == [1]


def test_bot_traffic_is_not_counted_on_this_branch_either(tmp_path):
    """The bot net is in the shared core, so this is really a check that the shim USES
    it rather than counting before delegating. A bot in the pageview count is worse than
    a gap in it."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const pending = [];
const env = { ASSETS: {}, PAW_ANALYTICS: rec };
const ctx = { waitUntil: (p) => pending.push(p) };
for (const ua of ["Mozilla/5.0 (compatible; Googlebot/2.1)", "curl/8.4.0", ""]) {
  await shim.fetch(request("https://site.example.dev/", { "user-agent": ua }), env, ctx);
}
await shim.fetch(request("https://site.example.dev/", { "user-agent": HUMAN }), env, ctx);
await Promise.all(pending);
emit({ rows: rec.rows.length });
""",
    )

    assert out["rows"] == 1


def test_the_shim_carries_a_fresh_salt_per_publish(tmp_path):
    """The salt is what makes the visitor hash irreversible, and it is minted per
    publish rather than derived from anything guessable — a salt an attacker can
    reconstruct turns the hash into a confirmation oracle. Asserted across two writes
    into the SAME project, which is what a republish is."""
    project = _build_ripple_project(tmp_path)
    shim = project / analytics_worker.SHIM_FILENAME

    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "ripple", None, site_id=SITE_ID
    )
    first = re.search(r'const SALT = "([0-9a-f]+)";', shim.read_text())
    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "ripple", None, site_id=SITE_ID
    )
    second = re.search(r'const SALT = "([0-9a-f]+)";', shim.read_text())

    assert first and second
    assert first.group(1) != second.group(1)
    assert SITE_ID not in first.group(1)


# ── the salt does not reach the local server either ─────────────────────────


def _get(url: str):
    """Fetch ``url``, treating an HTTP error as a result — a 404 is the assertion."""
    req = urllib.request.Request(url)  # noqa: S310 - localhost
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - localhost
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A freshly-rooted local static server, torn down afterwards. The server is a
    process singleton that captures its served root at startup, so resetting it is what
    makes this test's root the one actually served."""
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))

    previous = local_server._server
    local_server._server = None
    try:
        yield local_server
    finally:
        started = local_server._server
        if started is not None:
            started.shutdown()
            started.server_close()
        local_server._server = previous


def test_the_local_server_does_not_serve_the_shim(server, tmp_path):
    """The shim carries the same per-publish salt the assets-only entry does, so it gets
    the same treatment when the local target copies a project dir.

    THE CASE IS AN ENGINE FLIP, and it is why this is worth a test rather than a
    symmetry. Both counters live at the project ROOT. For ripple that is outside the
    served tree (``.svelte-kit/cloudflare``), so nothing there can leak. For html the
    served tree IS the project root — so a shim left by an earlier ripple publish into
    the same working dir lands in a tree the local server hands out, salt and all. The
    deploy write deletes that leftover; this is the second line of defence behind it,
    which is exactly the case a test has to construct by hand.

    Asserted OVER HTTP rather than by listing the copied dir, because "was it copied"
    and "is it served" are different questions and only the second is the exposure. The
    page beside it is fetched too — an ignore that swallowed the site would pass a
    404-only test."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text("<!doctype html><h1>hi</h1>")
    (project / "styles.css").write_text("h1{color:#111}")
    shim = project / analytics_worker.SHIM_FILENAME
    shim.write_text(
        analytics_worker.build_shim_js(
            site_id=SITE_ID, secret="0123456789abcdef0123456789abcdef", output_rel=OUTPUT_REL
        ),
        encoding="utf-8",
    )
    assert shim.is_file(), "the fixture must actually have a shim to leak"

    url = server.deploy_local("site-shim-1", str(project), engine="html")

    assert _get(f"{url}{analytics_worker.SHIM_FILENAME}")[0] == 404
    status, body = _get(url)
    assert status == 200
    assert b"<h1>hi</h1>" in body
    assert _get(f"{url}styles.css")[0] == 200

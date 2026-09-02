# tests/ee/sites/test_sites_analytics_counter.py — SA-1, the Paw Sites pageview
# counter: the generated Worker entry (``sites/analytics_worker.py``) and the
# assets-only deploy shape that carries it (``sites/workers_deploy.py``).
#
# Created 2026-09-02 (feat/sites-analytics-counter).
#
# Updated 2026-09-02 (feat/sites-analytics-shim, SA-3) — the row grew ``blobs[4]``, a
# coarse device class. The tests for it live HERE rather than beside the shim, because
# what they pin is this file's subject: the shape of the row the counting core writes,
# which both generated entries share. The section at the foot asserts the append is an
# APPEND — positions 0 through 3 read the same in the row that carries a device as they
# did before there was one — since a shifted column silently re-labels three months of
# history and nothing else in the suite would notice.
#
# THE CENTRE OF THIS FILE IS THE COST MODEL, not the config keys. Cloudflare bills a
# Worker invocation and serves a static asset free, so a ``run_worker_first`` rule that
# matches a page's subresources multiplies the per-pageview cost by roughly twenty.
# ``test_existing_static_assets_never_reach_the_worker`` is the assertion that pins it.
#
# The question underneath it is whether ``*`` crosses ``/``. That is what decides
# whether a NESTED asset (``/assets/deep/nested/theme.css``) is reached by a rule at
# all — and a fixture with only top-level assets passes identically either way, which
# is why every asset path below is checked at depth. It does cross; see
# ``test_a_wildcard_crosses_a_path_separator``.
#
# It pins the cost model by REPLAYING CLOUDFLARE'S OWN ROUTING DECISION rather than by
# reading the rule list back and hoping it means what we think. ``_route_for`` is the
# asset router's algorithm transcribed from the code that ships in the pinned
# toolchain — ``generateGlobOnlyRuleRegExp`` and the dispatch in
# ``miniflare@4.20260616.0``'s ``dist/src/workers/assets/router.worker.js``, which is
# the wrangler 4.101.0 dependency. ``_wrangler_rule_errors`` is likewise
# ``parseStaticRouting`` + ``validateStaticRoutingRules`` from
# ``wrangler-dist/cli.js``. A rule those reject is a deploy that fails outright, so
# both halves are worth asserting: one says the rules are legal, the other says they
# are cheap.
#
# The generated JavaScript is DRIVEN, not pattern-matched. Asserting that the source
# text contains ``writeDataPoint`` pins the spelling of the code and proves nothing
# about what it does — in particular it cannot tell a failure-soft counter from one
# that throws. So the entry is written to a temp ``.mjs``, imported by node, and
# called. Those tests skip when node is absent (the convention
# ``test_dev_bridge_source.py`` established for the same reason); the always-runs
# proofs below them cover the config shape and the no-cookie claim without a
# toolchain.

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import analytics_worker, workers_deploy

SITE_ID = "507f1f77bcf86cd799439011"

# A user-agent the bot filter must let through. Real, boring, and desktop.
HUMAN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


# ── Cloudflare's routing, transcribed ────────────────────────────────────────


def _rule_regex(rule: str) -> re.Pattern[str]:
    """``generateGlobOnlyRuleRegExp`` — split on ``*``, escape the literals, join with
    ``.*``, anchor both ends. ``*`` is therefore a wildcard ANYWHERE in a rule and it
    crosses ``/``, which is what makes ``/*.html`` a legal "any path ending .html"."""
    return re.compile("^" + ".*".join(re.escape(part) for part in rule.split("*")) + "$")


def _route_for(rules: list[str], path: str, *, asset_exists: bool) -> str:
    """Where the asset router sends ``path``: ``"worker"`` (billed) or ``"assets"``
    (free). The real decision, in the real order.

    Negative rules are checked FIRST and win. Then the positive rules. Then — and this
    is the part that is easy to forget — the fallback: once a config carries a
    ``main``, a request whose asset does NOT exist goes to the Worker anyway. So a
    404-scanning bot is billed no matter what the rules say, and the cost claim these
    tests can honestly make is about assets that EXIST.
    """
    asset_rules = [rule[1:] for rule in rules if rule.startswith("!/")]
    user_rules = [rule for rule in rules if rule.startswith("/")]
    if any(_rule_regex(rule).match(path) for rule in asset_rules):
        return "assets"
    if any(_rule_regex(rule).match(path) for rule in user_rules):
        return "worker"
    return "assets" if asset_exists else "worker"


def _wrangler_rule_errors(rules: list[str]) -> list[str]:
    """Everything wrangler 4.101.0 rejects a ``run_worker_first`` list for.

    From ``parseStaticRouting`` / ``validateStaticRoutingRules``: a rule must start
    with ``/`` or ``!/`` (a bare ``!`` is its own error message), the list may not be
    only-negative, and within EACH of the two lists rules must be unique, at most 100
    characters, and free of the trailing-``*`` redundancy check. The list itself is
    capped at 100 rules. Any of these fails the deploy, not just the counting.
    """
    errors: list[str] = []
    if not rules:
        errors.append("empty rule list")
    if len(rules) > 100:
        errors.append(f"{len(rules)} rules exceeds the max of 100")
    asset_raw = [rule for rule in rules if rule.startswith("!/")]
    user = [rule for rule in rules if rule.startswith("/")]
    for rule in rules:
        if not rule.startswith(("/", "!/")):
            errors.append(f"{rule!r}: rules must start with '/' or '!/'")
    if asset_raw and not user:
        errors.append("only negative rules were provided")
    for group in (asset_raw, user):
        seen: set[str] = set()
        for rule in group:
            if len(rule) > 100:
                errors.append(f"{rule!r}: over 100 characters")
            if rule in seen:
                errors.append(f"{rule!r}: duplicate")
            seen.add(rule)
        for rule in group:
            if not rule.endswith("*"):
                continue
            for other in group:
                if other != rule and other.startswith(rule[:-1]):
                    errors.append(f"{other!r}: rule {rule!r} makes it redundant")
    return errors


# ── fixtures: a built html site with the asset types that must stay free ─────

# The paths a real page pulls in. Every one of these must route to the assets, and
# every one is a real file in the tree below — the fallback would send a MISSING path
# to the Worker regardless of the rules, so a test using invented paths would prove
# nothing about the rules at all.
#
# NESTING IS THE POINT OF HALF OF THESE. A rule's ``*`` expands to ``.*``, which in a
# JavaScript regex crosses ``/`` — so a pattern is only safe or only useful once it has
# been checked against a DEEP path, not just a top-level file. A top-level-only fixture
# would pass identically whether ``*`` crossed the separator or not, which is exactly
# the question that decides the cost floor.
STATIC_ASSET_PATHS = (
    "/styles.css",
    "/assets/app.js",
    "/assets/logo.png",
    "/assets/hero.webp",
    "/assets/deep/nested/theme.css",
    "/assets/deep/nested/vendor/chunk.js",
    "/assets/deep/nested/img/icon.png",
    "/fonts/inter.woff2",
    "/favicon.ico",
    "/robots.txt",
)

# The paths that ARE pages and must reach the Worker to be counted.
PAGE_PATHS = (
    "/",
    "/index.html",
    "/about.html",
    "/about",
    "/docs/",
    "/docs",
    "/docs/guide.html",
    "/docs/guide",
    "/docs/deep/nested/page.html",
)


def _build_html_project(tmp_path: Path) -> str:
    """A built html site: ``static_output_rel("html")`` is ``"."``, so the static tree
    IS the project dir. Multi-page and multi-asset on purpose — a single-file brochure
    cannot show a rule matching the wrong thing."""
    (tmp_path / "index.html").write_text("<!doctype html><html><body><h1>hi</h1></body></html>")
    (tmp_path / "about.html").write_text("<!doctype html><html><body><h1>about</h1></body></html>")
    (tmp_path / "styles.css").write_text("h1{color:#111}")
    (tmp_path / "robots.txt").write_text("User-agent: *\n")
    (tmp_path / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "assets" / "hero.webp").write_bytes(b"RIFF")
    # Deeply nested assets — the shape that separates "``*`` crosses ``/``" from "``*``
    # stops at a separator", and therefore the shape the cost floor actually rests on.
    nested = tmp_path / "assets" / "deep" / "nested"
    (nested / "vendor").mkdir(parents=True)
    (nested / "img").mkdir()
    (nested / "theme.css").write_text("body{margin:0}")
    (nested / "vendor" / "chunk.js").write_text("export default 1")
    (nested / "img" / "icon.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "inter.woff2").write_bytes(b"wOF2")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.html").write_text("<!doctype html><h1>docs</h1>")
    (tmp_path / "docs" / "guide.html").write_text("<!doctype html><h1>guide</h1>")
    (tmp_path / "docs" / "deep" / "nested").mkdir(parents=True)
    (tmp_path / "docs" / "deep" / "nested" / "page.html").write_text("<!doctype html><h1>deep</h1>")
    return str(tmp_path)


def _build_server_worker_project(tmp_path: Path) -> str:
    """A built ripple/svelte site — adapter-cloudflare output WITH a ``_worker.js``.
    The branch SA-1 must leave alone."""
    out = tmp_path / ".svelte-kit" / "cloudflare"
    out.mkdir(parents=True)
    (out / "_worker.js").write_text("export default {}")
    (out / "index.html").write_text("<h1>hi</h1>")
    return str(tmp_path)


def _write_html_deploy(tmp_path: Path) -> tuple[Path, dict]:
    """Run the real deploy-file write for an html site; return the project dir and the
    parsed config."""
    project = Path(_build_html_project(tmp_path))
    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "html", None, site_id=SITE_ID
    )
    return project, json.loads((project / "wrangler.jsonc").read_text())


# ── the cost floor ───────────────────────────────────────────────────────────


def test_existing_static_assets_never_reach_the_worker(tmp_path):
    """THE COST MODEL. A stylesheet, script, font or image that exists in the built
    tree must be served by the asset path, which Cloudflare does not bill.

    A page pulls in roughly twenty of these. Routing them through the Worker would
    multiply the per-pageview cost by about twenty, which is a defect and not a
    tuning choice — hence the routing decision is replayed here rather than assumed
    from the shape of the rule list."""
    _, cfg = _write_html_deploy(tmp_path)
    rules = cfg["assets"]["run_worker_first"]

    for path in STATIC_ASSET_PATHS:
        assert _route_for(rules, path, asset_exists=True) == "assets", (
            f"{path} would invoke the Worker and be billed as a pageview; rules={rules}"
        )


def test_pages_do_reach_the_worker(tmp_path):
    """The other half of the same claim: narrowing the rules until nothing is billed
    would also count nothing. Every page shape the site actually serves — the root, a
    ``.html`` path, the extensionless clean URL ``html_handling`` resolves, and a
    directory-style URL — must route to the Worker."""
    _, cfg = _write_html_deploy(tmp_path)
    rules = cfg["assets"]["run_worker_first"]

    for path in PAGE_PATHS:
        assert _route_for(rules, path, asset_exists=True) == "worker", (
            f"{path} is a page and would go uncounted; rules={rules}"
        )


def test_query_strings_do_not_change_the_routing_decision(tmp_path):
    """The router matches ``pathname`` only. A campaign-tagged stylesheet
    (``/styles.css?v=3``) is still a stylesheet, and a tagged page is still a page."""
    _, cfg = _write_html_deploy(tmp_path)
    rules = cfg["assets"]["run_worker_first"]

    assert _route_for(rules, "/styles.css", asset_exists=True) == "assets"
    assert _route_for(rules, "/about.html", asset_exists=True) == "worker"


def test_a_wildcard_crosses_a_path_separator(tmp_path):
    """OQ-4, stated as the question that actually decides the cost floor.

    Not "does wrangler accept negation" — it does — but whether ``*`` matches ACROSS
    ``/``. If it stopped at a separator, ``/*.html`` would miss every nested page and
    ``!/*.css`` would miss every nested stylesheet, and nested assets would fall
    through to the Worker with nothing saying so.

    It crosses. ``generateGlobOnlyRuleRegExp`` joins the escaped literals with ``.*``,
    and ``.`` in a JavaScript regex without the ``s`` flag excludes only line
    terminators — ``/`` is not one. Both directions are asserted: our shipped positive
    rule reaching a deep page, and the negation form the task sketched reaching a deep
    stylesheet. The second is not what we deploy; it is the spike's answer, kept as a
    test so the finding cannot rot into folklore."""
    _, cfg = _write_html_deploy(tmp_path)
    rules = cfg["assets"]["run_worker_first"]

    # The rule we ship. Three levels down and still matched.
    assert _rule_regex("/*.html").match("/docs/deep/nested/page.html")
    assert _route_for(rules, "/docs/deep/nested/page.html", asset_exists=True) == "worker"

    # The negation form, checked on the same axis. A deny-list would work — we ship an
    # allow-list for a different reason (see the module header), not because this fails.
    deny = ["/*", "!/*.css", "!/*.js", "!/*.png"]
    assert _wrangler_rule_errors(deny) == []
    assert _route_for(deny, "/assets/deep/nested/theme.css", asset_exists=True) == "assets"
    assert _route_for(deny, "/assets/deep/nested/vendor/chunk.js", asset_exists=True) == "assets"
    assert _route_for(deny, "/about", asset_exists=True) == "worker"

    # And the fact that makes the allow-list safe rather than lucky: a page-shaped rule
    # cannot reach an asset no matter how deep, because it anchors on the suffix.
    assert not _rule_regex("/*.html").match("/assets/deep/nested/theme.css")


def test_emitted_rules_pass_wranglers_own_validation(tmp_path):
    """A rule wrangler rejects is not a cheaper deploy — it is no deploy at all.

    Also the record of OQ-4's one correction: negation IS supported by the pinned
    wrangler, but a negative rule must start with ``!/``. The ``"!*.css"`` form in the
    original sketch is rejected by ``parseStaticRouting`` with "negative rules must
    start with '!/'"."""
    _, cfg = _write_html_deploy(tmp_path)
    rules = cfg["assets"]["run_worker_first"]

    assert _wrangler_rule_errors(rules) == []
    assert _wrangler_rule_errors(["/*", "!*.css"]) != []


def test_rule_list_stays_far_under_the_cap_on_a_big_tree(tmp_path):
    """A site with more pages than the enumeration budget truncates rather than
    emitting an over-cap list. Truncating costs uncounted clean-URL visits on the
    pages past the budget; exceeding the cap costs the whole deploy."""
    project = Path(_build_html_project(tmp_path))
    for i in range(200):
        (project / f"page-{i}.html").write_text("<h1>x</h1>")

    rules = analytics_worker.run_worker_first_rules(project)

    assert len(rules) <= 64
    assert _wrangler_rule_errors(rules) == []
    # The generic rules survive the truncation, so every one of those pages is still
    # counted at its ``.html`` URL — only the extensionless alias is lost.
    assert _route_for(rules, "/page-199.html", asset_exists=True) == "worker"


# ── the emitted config ───────────────────────────────────────────────────────


def test_html_config_carries_the_counter_and_its_bindings(tmp_path):
    """A published html site's ``wrangler.jsonc`` names the generated entry as
    ``main``, binds ``ASSETS`` so the entry can serve the page it just counted, scopes
    ``run_worker_first``, and binds exactly one Analytics Engine dataset."""
    _, cfg = _write_html_deploy(tmp_path)

    assert cfg["name"] == f"paw-site-{SITE_ID}"
    assert cfg["main"] == analytics_worker.ENTRY_FILENAME
    assert cfg["workers_dev"] is True
    assert cfg["assets"]["binding"] == "ASSETS"
    assert cfg["assets"]["directory"] == "."
    assert isinstance(cfg["assets"]["run_worker_first"], list)
    assert cfg["analytics_engine_datasets"] == [
        {"binding": analytics_worker.DATASET_BINDING, "dataset": analytics_worker.dataset_name()}
    ]
    # Still no node runtime and still no database: the counter needs neither.
    assert "compatibility_flags" not in cfg
    assert "d1_databases" not in cfg
    # ``main`` never points into the build output — that is the RX-1 failure this
    # branch exists to avoid, and adding a ``main`` must not reintroduce it.
    assert "_worker.js" not in json.dumps(cfg)


def test_the_entry_is_written_and_excluded_from_the_served_tree(tmp_path):
    """The entry file exists where ``main`` says it does, and is listed in
    ``.assetsignore``.

    That listing is a PRIVACY control. An html site's asset dir is the project root,
    so an unignored entry is uploaded as a public asset — and the entry carries the
    per-publish salt the visitor hash is built on. Downloadable salt, reversible
    hash."""
    project, cfg = _write_html_deploy(tmp_path)

    entry = project / cfg["main"]
    assert entry.is_file()

    assetsignore = (project / ".assetsignore").read_text().splitlines()
    assert analytics_worker.ENTRY_FILENAME in assetsignore
    assert "wrangler.jsonc" in assetsignore
    assert ".assetsignore" in assetsignore


def test_the_dataset_name_is_overridable_per_environment(tmp_path, monkeypatch):
    """So a staging publish cannot write into the production dataset. A blank override
    falls back rather than emitting an empty dataset name."""
    monkeypatch.setenv("PAW_SITES_ANALYTICS_DATASET", "paw_site_pageviews_staging")
    _, cfg = _write_html_deploy(tmp_path)
    assert cfg["analytics_engine_datasets"][0]["dataset"] == "paw_site_pageviews_staging"

    monkeypatch.setenv("PAW_SITES_ANALYTICS_DATASET", "   ")
    assert analytics_worker.dataset_name() == "paw_site_pageviews"


def test_the_site_id_is_the_analytics_index(tmp_path):
    """Analytics Engine allows ONE index per data point, so the site id is the only
    thing the dataset can be queried by. It has to reach the generated entry."""
    project, cfg = _write_html_deploy(tmp_path)
    entry = (project / cfg["main"]).read_text()
    assert f'"{SITE_ID}"' in entry


def test_the_entry_sets_no_cookie_and_stores_no_raw_ip(tmp_path):
    """The Plausible model: identification is a salted hash, not an identifier handed
    to the browser. Always runs — no toolchain needed — because this is the claim the
    privacy posture rests on."""
    project, cfg = _write_html_deploy(tmp_path)
    entry = (project / cfg["main"]).read_text()

    assert "Set-Cookie" not in entry
    assert "document.cookie" not in entry
    assert "localStorage" not in entry
    assert "crypto.subtle.digest" in entry


def test_two_publishes_mint_different_salts(tmp_path):
    """The salt is a RANDOM per-publish secret, not the date.

    If the salt were only the rotating date, the stored hash would be
    ``sha256(date + ip + user-agent)`` over an IPv4 space of about four billion — a few
    seconds of brute force for anyone holding the dataset, which would make the row
    personal data and the irreversibility claim false. The random secret is what closes
    that, and this asserts it is actually random rather than derived from the site id
    or the day (either of which an attacker also holds)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    project_a, cfg_a = _write_html_deploy(tmp_path / "a")
    project_b, cfg_b = _write_html_deploy(tmp_path / "b")

    salt_a = _extract_salt((project_a / cfg_a["main"]).read_text())
    salt_b = _extract_salt((project_b / cfg_b["main"]).read_text())

    assert salt_a != salt_b
    assert len(salt_a) >= 32
    # Not the site id, not the date, not the worker name — nothing the holder of a row
    # also holds.
    assert SITE_ID not in salt_a
    assert not re.search(r"\d{4}-\d{2}-\d{2}", salt_a)


def _extract_salt(entry_js: str) -> str:
    """The generated entry's baked secret, read back out of the source."""
    match = re.search(r'const SALT = "([0-9a-f]+)";', entry_js)
    assert match, "the generated entry carries no SALT constant"
    return match.group(1)


def test_the_hash_is_not_reproducible_from_date_ip_and_user_agent(tmp_path):
    """The attack the random secret exists to stop, run as an attacker would run it.

    Someone holding a row has the site id, the day it was written, and a candidate IP
    and user-agent. Every hash they can compute from those alone must miss. Only the
    secret they do not have produces the stored value."""
    out = _run_node(
        tmp_path,
        """
const SECRET = "0123456789abcdef0123456789abcdef";
const day = dayKey(Date.UTC(2026, 8, 2, 12, 0, 0));
const ip = "203.0.113.9";
const stored = await visitorHash(ip, HUMAN, SECRET, day);
const guesses = {
  noSecret: await visitorHash(ip, HUMAN, "", day),
  siteIdAsSecret: await visitorHash(ip, HUMAN, "507f1f77bcf86cd799439011", day),
  dayAsSecret: await visitorHash(ip, HUMAN, day, day),
  wrongSecret: await visitorHash(ip, HUMAN, "0123456789abcdef0123456789abcdee", day),
};
emit({ stored, guesses });
""",
    )

    for name, guess in out["guesses"].items():
        assert guess != out["stored"], f"the {name} guess reproduced the stored hash"


@pytest.mark.asyncio
async def test_the_public_deploy_entry_writes_the_counter(tmp_path, monkeypatch):
    """The seam pin. Both publish lanes end at ``deploy_workers``, so the counter has
    to land when it is called — not merely when ``_write_deploy_files`` is called
    directly, which is how every other test here reaches it.

    THIS MATTERS BECAUSE THE BUILD HOST IS NOT THE DEPLOY HOST. Daytona is always the
    build host, but its sandbox only ever installs, builds, tars and deletes itself
    (``daytona_runner`` uploads the source, a ``bunfig.toml`` and a wrapper script, and
    nothing else). The deploy runs back on the worker: ``build_job`` materialises the
    artifact and calls ``service.deploy_prebuilt_site``, which is ``_deploy_site_doc``'s
    tail — the SAME tail the inline publish runs — and that tail calls this function.
    So there is one deploy implementation, this is it, and a counter wired through it
    reaches a real published site on both lanes."""

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"https://paw-site-x.acct.workers.dev\n", b""

    async def _fake_exec(*argv, cwd=None, env=None, stdout=None, stderr=None):
        return _Proc()

    monkeypatch.setattr(workers_deploy.asyncio, "create_subprocess_exec", _fake_exec)
    project = Path(_build_html_project(tmp_path))

    await workers_deploy.deploy_workers(SITE_ID, str(project), engine="html")

    cfg = json.loads((project / "wrangler.jsonc").read_text())
    assert (project / cfg["main"]).is_file()
    assert cfg["analytics_engine_datasets"][0]["binding"] == analytics_worker.DATASET_BINDING
    for path in STATIC_ASSET_PATHS:
        assert _route_for(cfg["assets"]["run_worker_first"], path, asset_exists=True) == "assets"


# ── the server-worker branch is untouched ────────────────────────────────────


def test_server_worker_config_is_unchanged(tmp_path):
    """SA-1 is scoped to the assets-only branch. A ripple / dynamic-svelte deploy
    keeps the exact proven SvelteKit config: its own ``main``, ``nodejs_compat``, the
    adapter output as the asset dir, and the three-line ``.assetsignore``."""
    project = Path(_build_server_worker_project(tmp_path))
    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "ripple", None, site_id=SITE_ID
    )
    cfg = json.loads((project / "wrangler.jsonc").read_text())

    assert cfg["main"] == ".svelte-kit/cloudflare/_worker.js"
    assert cfg["compatibility_flags"] == ["nodejs_compat"]
    assert cfg["assets"] == {"binding": "ASSETS", "directory": ".svelte-kit/cloudflare"}
    assert (project / ".svelte-kit/cloudflare/.assetsignore").read_text().splitlines() == [
        "_worker.js",
        "_routes.json",
        "_headers",
    ]


def test_server_worker_branch_gets_no_counter(tmp_path):
    """Stated as the failure it prevents: no counter file, no dataset binding and no
    routing rules on the branch where ``main`` is already SvelteKit's own worker. A
    second entry cannot be bolted in front of that one from a config key — that is a
    later slice, not a silent partial."""
    project = Path(_build_server_worker_project(tmp_path))
    workers_deploy._write_deploy_files(
        str(project), f"paw-site-{SITE_ID}", "ripple", None, site_id=SITE_ID
    )
    cfg = json.loads((project / "wrangler.jsonc").read_text())

    assert "analytics_engine_datasets" not in cfg
    assert "run_worker_first" not in cfg["assets"]
    assert not (project / analytics_worker.ENTRY_FILENAME).exists()


# ── driving the generated Worker under node ──────────────────────────────────

_DRIVER_PRELUDE = """
import worker, { isBot, dayKey, visitorHash, count, deviceClass } from "./entry.mjs";

const request = (url, headers = {}, cf = {}) => ({
  url,
  headers: { get: (k) => headers[k.toLowerCase()] ?? null },
  cf,
});
const recorder = () => {
  const rows = [];
  return { rows, writeDataPoint: (row) => rows.push(row) };
};
const page = (status = 200) => ({
  fetch: async () => new Response("<h1>hi</h1>", { status }),
});
const emit = (out) => console.log(JSON.stringify(out));
const HUMAN = %s;
"""


def _run_node(tmp_path: Path, body: str, *, entry_js: str | None = None) -> dict:
    """Write the generated entry as an ES module, drive it with ``body`` under node,
    and return what the driver emitted.

    Skips when node is absent, the convention ``test_dev_bridge_source.py`` uses for
    the same reason: the always-runs assertions above cover the config shape and the
    privacy claim, so a machine with no toolchain still gates the important half."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH — the generated Worker cannot be driven")

    work = tmp_path / "node-drive"
    work.mkdir(parents=True, exist_ok=True)
    if entry_js is None:
        entry_js = analytics_worker.build_entry_js(
            site_id=SITE_ID, secret="0123456789abcdef0123456789abcdef"
        )
    (work / "entry.mjs").write_text(entry_js, encoding="utf-8")
    (work / "driver.mjs").write_text(
        (_DRIVER_PRELUDE % json.dumps(HUMAN_UA)) + body, encoding="utf-8"
    )

    proc = subprocess.run(
        [node, str(work / "driver.mjs")], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f"node driver failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_real_visit_writes_one_row_in_the_agreed_shape(tmp_path):
    """The end-to-end claim of the slice: a visit lands one Analytics Engine row,
    indexed by the site id.

    The blob ORDER is a contract the reader depends on — Analytics Engine columns are
    positional and unnamed — so it is asserted position by position."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const env = { ASSETS: page(200), PAW_ANALYTICS: rec };
const pending = [];
const res = await worker.fetch(
  request(
    "https://site.example.dev/about.html?utm_source=x",
    { "user-agent": HUMAN, "cf-connecting-ip": "203.0.113.9",
      referer: "https://news.example.com/story" },
    { country: "DE" },
  ),
  env,
  { waitUntil: (p) => pending.push(p) },
);
await Promise.all(pending);
emit({ status: res.status, body: await res.text(), rows: rec.rows });
""",
    )

    assert out["status"] == 200
    assert out["body"] == "<h1>hi</h1>"
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["indexes"] == [SITE_ID]
    assert row["blobs"][0] == "/about.html"
    assert row["blobs"][1] == "news.example.com"
    assert row["blobs"][2] == "DE"
    assert re.fullmatch(r"[0-9a-f]{32}", row["blobs"][3])
    assert row["doubles"] == [1]
    # Analytics Engine caps a data point at 20 blobs, 20 doubles and one index.
    assert len(row["blobs"]) <= 20
    assert len(row["doubles"]) <= 20


def test_a_throwing_write_data_point_still_serves_the_page(tmp_path):
    """FAILURE-SOFT, the posture that separates this from ``badge.py``. A pageview is
    worth nothing next to the page, so a throw inside the counter must not reach the
    response — and must not surface later as a rejected ``waitUntil`` promise
    either."""
    out = _run_node(
        tmp_path,
        """
const env = {
  ASSETS: page(200),
  PAW_ANALYTICS: { writeDataPoint: () => { throw new Error("analytics is down"); } },
};
const pending = [];
const res = await worker.fetch(
  request("https://site.example.dev/", { "user-agent": HUMAN }),
  env,
  { waitUntil: (p) => pending.push(p) },
);
const body = await res.text();
let settled = "resolved";
try { await Promise.all(pending); } catch (err) { settled = "rejected"; }
emit({ status: res.status, body, settled });
""",
    )

    assert out["status"] == 200
    assert out["body"] == "<h1>hi</h1>"
    assert out["settled"] == "resolved"


def test_a_missing_analytics_binding_still_serves_the_page(tmp_path):
    """The other failure the deploy can actually produce: the dataset binding absent
    from ``env`` (a config that lost the binding, an account without the product).
    The page is served exactly the same."""
    out = _run_node(
        tmp_path,
        """
const pending = [];
const res = await worker.fetch(
  request("https://site.example.dev/", { "user-agent": HUMAN }),
  { ASSETS: page(200) },
  { waitUntil: (p) => pending.push(p) },
);
const body = await res.text();
let settled = "resolved";
try { await Promise.all(pending); } catch (err) { settled = "rejected"; }
emit({ status: res.status, body, settled });
""",
    )

    assert out["status"] == 200
    assert out["body"] == "<h1>hi</h1>"
    assert out["settled"] == "resolved"


def test_the_visitor_hash_is_stable_within_a_day_and_rotates_with_the_salt(tmp_path):
    """The Plausible property. Stable within the UTC day so a visitor is one visitor;
    different after the rotation so yesterday cannot be joined to today. Different per
    IP so it distinguishes visitors at all, and never the raw IP."""
    out = _run_node(
        tmp_path,
        """
const SECRET = "0123456789abcdef0123456789abcdef";
const morning = Date.UTC(2026, 8, 2, 0, 0, 1);
const night = Date.UTC(2026, 8, 2, 23, 59, 59);
const nextDay = Date.UTC(2026, 8, 3, 0, 0, 1);
emit({
  early: await visitorHash("203.0.113.9", HUMAN, SECRET, dayKey(morning)),
  late: await visitorHash("203.0.113.9", HUMAN, SECRET, dayKey(night)),
  tomorrow: await visitorHash("203.0.113.9", HUMAN, SECRET, dayKey(nextDay)),
  otherVisitor: await visitorHash("198.51.100.4", HUMAN, SECRET, dayKey(morning)),
  otherSalt: await visitorHash("203.0.113.9", HUMAN, "a-different-secret", dayKey(morning)),
  days: [dayKey(morning), dayKey(night), dayKey(nextDay)],
});
""",
    )

    assert out["days"] == ["2026-09-02", "2026-09-02", "2026-09-03"]
    assert out["early"] == out["late"]
    assert out["tomorrow"] != out["early"]
    assert out["otherVisitor"] != out["early"]
    assert out["otherSalt"] != out["early"]
    assert "203.0.113.9" not in json.dumps(out)


def test_the_rotation_reaches_the_recorded_row(tmp_path):
    """The same rotation, proved through ``count`` rather than the helper — so a
    counter that computed the hash correctly and then wrote a constant would still
    fail."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const env = { PAW_ANALYTICS: rec };
const req = request(
  "https://site.example.dev/",
  { "user-agent": HUMAN, "cf-connecting-ip": "203.0.113.9" },
  { country: "US" },
);
await count(req, env, Date.UTC(2026, 8, 2, 6, 0, 0));
await count(req, env, Date.UTC(2026, 8, 2, 18, 0, 0));
await count(req, env, Date.UTC(2026, 8, 3, 6, 0, 0));
emit({ visitors: rec.rows.map((row) => row.blobs[3]) });
""",
    )

    early, late, tomorrow = out["visitors"]
    assert early == late
    assert tomorrow != early


def test_bot_traffic_is_neither_stored_nor_billed_for_storage(tmp_path):
    """Bots are filtered before ``writeDataPoint``, so their traffic costs no rows. An
    ABSENT user-agent counts as a bot — every real browser sends one."""
    out = _run_node(
        tmp_path,
        """
const agents = [
  "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
  "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
  "curl/8.4.0",
  "python-requests/2.32.3",
  "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/128.0 Safari/537.36",
  "facebookexternalhit/1.1",
  "",
];
const flags = agents.map((ua) => isBot(ua));
const rec = recorder();
for (const ua of agents) {
  await count(request("https://site.example.dev/", { "user-agent": ua }), { PAW_ANALYTICS: rec });
}
const human = recorder();
const humanReq = request("https://site.example.dev/", { "user-agent": HUMAN });
await count(humanReq, { PAW_ANALYTICS: human });
emit({ flags, botRows: rec.rows.length, humanRows: human.rows.length, humanIsBot: isBot(HUMAN) });
""",
    )

    assert out["flags"] == [True] * 7
    assert out["botRows"] == 0
    assert out["humanIsBot"] is False
    assert out["humanRows"] == 1


def test_a_404_is_served_but_not_counted(tmp_path):
    """The asset router's fallback sends every request for a MISSING path to the
    Worker once a ``main`` exists, so path-scanning bots reach us whatever the rules
    say. They must not reach the pageview numbers."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const pending = [];
const res = await worker.fetch(
  request("https://site.example.dev/wp-login.php", { "user-agent": HUMAN }),
  { ASSETS: page(404), PAW_ANALYTICS: rec },
  { waitUntil: (p) => pending.push(p) },
);
await Promise.all(pending);
emit({ status: res.status, rows: rec.rows.length });
""",
    )

    assert out["status"] == 404
    assert out["rows"] == 0


def test_a_same_site_referrer_is_reported_as_no_referrer(tmp_path):
    """Internal navigation is not acquisition. Reporting a site's own pages as
    referrers buries the real sources under them."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const env = { PAW_ANALYTICS: rec };
await count(request("https://site.example.dev/b.html",
  { "user-agent": HUMAN, referer: "https://site.example.dev/a.html" }), env);
await count(request("https://site.example.dev/b.html",
  { "user-agent": HUMAN, referer: "not a url" }), env);
await count(request("https://site.example.dev/b.html", { "user-agent": HUMAN }), env);
emit({ referrers: rec.rows.map((row) => row.blobs[1]) });
""",
    )

    assert out["referrers"] == ["", "", ""]


# ── SA-3: the device class at blobs[4] ───────────────────────────────────────

# One user-agent per class, all real. The Android pair is the reason the classifier
# tests tablet first: an Android TABLET is an Android that omits ``Mobile``, so the two
# strings differ by one token and a naive order files the tablet under desktop.
_DEVICE_AGENTS = {
    "desktop": HUMAN_UA,
    "mobile": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
    "tablet": (
        "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
    "unknown": "Mozilla/5.0",
}


def test_the_row_carries_a_device_class_at_blob_four(tmp_path):
    """SA-3's half of the row contract, driven end to end through the default handler
    rather than through ``deviceClass`` alone — a classifier that is right and a
    ``count`` that never calls it would pass the unit check and store nothing."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
const pending = [];
const res = await worker.fetch(
  request(
    "https://site.example.dev/about.html",
    { "user-agent": HUMAN, "cf-connecting-ip": "203.0.113.9" },
    { country: "DE" },
  ),
  { ASSETS: page(200), PAW_ANALYTICS: rec },
  { waitUntil: (p) => pending.push(p) },
);
await Promise.all(pending);
emit({ status: res.status, rows: rec.rows });
""",
    )

    assert out["status"] == 200
    assert len(out["rows"]) == 1
    assert out["rows"][0]["blobs"][4] == "desktop"


def test_the_device_class_is_appended_and_shifts_nothing(tmp_path):
    """THE MUTATION THIS SECTION EXISTS FOR. Analytics Engine columns are positional
    and unnamed, so inserting the device anywhere but the end re-labels every row
    already written — three months of them, unfixably, because there is no update.

    Asserted on the row that CARRIES the device, so it cannot pass by reading a
    pre-SA-3 row: path, referrer host, country and visitor hash must still be at 0, 1,
    2 and 3 with the fifth blob present beside them."""
    out = _run_node(
        tmp_path,
        """
const rec = recorder();
await count(
  request(
    "https://site.example.dev/docs/guide.html",
    { "user-agent": HUMAN, "cf-connecting-ip": "203.0.113.9",
      referer: "https://news.example.com/story" },
    { country: "DE" },
  ),
  { PAW_ANALYTICS: rec },
);
emit({ rows: rec.rows });
""",
    )

    blobs = out["rows"][0]["blobs"]
    assert blobs[0] == "/docs/guide.html"
    assert blobs[1] == "news.example.com"
    assert blobs[2] == "DE"
    assert re.fullmatch(r"[0-9a-f]{32}", blobs[3])
    assert blobs[4] == "desktop"
    assert len(blobs) == 5
    assert out["rows"][0]["doubles"] == [1]


def test_every_device_class_is_reachable_and_correct(tmp_path):
    """All four values, one real user-agent each — including ``unknown``, which is only
    reachable because desktop is a POSITIVE platform match rather than the fallback. A
    classifier that returned ``desktop`` for anything unrecognised would inflate the
    number a site owner is most likely to act on, and no other test here would see it.

    The Android pair is the ordering trap: the two strings differ by the ``Mobile``
    token alone."""
    out = _run_node(
        tmp_path,
        """
const agents = %s;
emit({ classes: Object.fromEntries(Object.entries(agents).map(([k, ua]) => [k, deviceClass(ua)])) });
"""
        % json.dumps(
            {
                **_DEVICE_AGENTS,
                "android-phone": (
                    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
                ),
                "android-tablet": (
                    "Mozilla/5.0 (Linux; Android 14; SM-X700) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                "windows": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                "linux": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                "empty": "",
            }
        ),
    )

    assert out["classes"] == {
        "desktop": "desktop",
        "mobile": "mobile",
        "tablet": "tablet",
        "unknown": "unknown",
        "android-phone": "mobile",
        "android-tablet": "tablet",
        "windows": "desktop",
        "linux": "desktop",
        "empty": "unknown",
    }


def test_the_device_class_never_carries_the_user_agent(tmp_path):
    """The privacy ceiling, asserted as a property of the stored row rather than as a
    promise in a comment. The user-agent is the highest-entropy thing this Worker
    touches; the row's claim is that it cannot single anyone out, so what lands in
    ``blobs[4]`` must be one of exactly four words and never a substring of the agent
    that produced it."""
    out = _run_node(
        tmp_path,
        """
const agents = %s;
const rec = recorder();
for (const ua of Object.values(agents)) {
  await count(request("https://site.example.dev/", { "user-agent": ua }), { PAW_ANALYTICS: rec });
}
emit({ devices: rec.rows.map((row) => row.blobs[4]), rows: rec.rows.length });
"""
        % json.dumps(_DEVICE_AGENTS),
    )

    assert out["rows"] == len(_DEVICE_AGENTS)
    assert set(out["devices"]) <= {"desktop", "mobile", "tablet", "unknown"}
    # Nothing version-shaped, platform-shaped or otherwise narrowing rides along.
    for device in out["devices"]:
        assert re.fullmatch(r"desktop|mobile|tablet|unknown", device)

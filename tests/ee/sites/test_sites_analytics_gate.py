# tests/ee/sites/test_sites_analytics_gate.py — SA-2, the gate in front of the Paw
# Sites pageview counter: who gets counted, the switch that stops everyone being
# counted, and the salt that must not reach the local server.
#
# Created 2026-09-02 (feat/sites-analytics-gate).
#
# SA-1 wired the counter onto EVERY assets-only publish. Cloudflare bills a Worker
# invocation and serves a static asset for free, so that spent money on sites paying
# nothing — this file is the proof it no longer does, and the proof rests on three
# separate claims that are easy to confuse for one another:
#
#   1. THE PREDICATE IS RIGHT. ``entitlements.site_analytics_entitled`` is the one
#      function both the publish (SA-2) and the read endpoint (SA-4) gate on, so its
#      edges — a lapsed subscription, an org key on a site, a legacy tier name — are
#      asserted directly rather than only through a deploy.
#   2. THE PUBLISH PATH ACTUALLY CALLS IT. A correct predicate nothing consults is the
#      bug this slice exists to fix, and ``_write_deploy_files`` defaults to counting.
#      So the gate is asserted END TO END through ``sites_service.publish``, where the
#      Site document and its plan are real. A unit test on the deploy writer alone
#      would pass just as happily against a publish path that never passed the flag.
#   3. THE OFF SHAPE IS THE PRE-ANALYTICS SHAPE. Not "close to it" — the kill switch
#      exists to be pulled during an incident, and a rollback that leaves an unfamiliar
#      config behind is not a rollback. The config is compared against a literal of the
#      pre-SA-1 recipe rather than against a list of keys that must be absent.
#
# The kill switch's own reason is worth restating where a reader will meet it: if this
# Cloudflare account turns out to be on the Workers FREE plan, a config carrying a
# ``main`` draws on a 100,000 request/day ACCOUNT-WIDE ceiling, and breaching it stops
# sites being served rather than degrading analytics. Every published site goes dark
# together. The mitigation has to be faster than a deploy, which is why it is an
# environment variable and why "byte for byte" is a real requirement rather than
# tidiness.

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.entitlements import service as entitlements  # noqa: E402
from pocketpaw_ee.sites import analytics_worker, local_server, workers_deploy  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402

SITE_ID = "507f1f77bcf86cd799439011"
WORKER_NAME = f"paw-site-{SITE_ID}"

# The exact assets-only config an un-analytics build produced — the pre-SA-1 recipe,
# transcribed. Written out in full rather than derived, because the whole claim of the
# kill switch is that it restores THIS and a derivation would restore whatever the
# current code happens to produce.
PRE_ANALYTICS_HTML_CONFIG = {
    "name": WORKER_NAME,
    "compatibility_date": "2024-09-23",
    "workers_dev": True,
    "assets": {"directory": "."},
}


def _build_html_project(tmp_path: Path) -> Path:
    """A built html site — ``static_output_rel("html")`` is ``"."``, so the static tree
    IS the project dir. Smaller than SA-1's fixture on purpose: nothing here is about
    the routing rules, and a second copy of that fixture would drift from it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "index.html").write_text("<!doctype html><html><body><h1>hi</h1></body></html>")
    (tmp_path / "about.html").write_text("<!doctype html><html><body><h1>about</h1></body></html>")
    (tmp_path / "styles.css").write_text("h1{color:#111}")
    return tmp_path


def _write(project: Path, *, entitled: bool) -> dict:
    """Run the real deploy-file write for an html site and return the parsed config."""
    workers_deploy._write_deploy_files(
        str(project), WORKER_NAME, "html", None, site_id=SITE_ID, analytics_entitled=entitled
    )
    return json.loads((project / "wrangler.jsonc").read_text())


# ── 1. the predicate ─────────────────────────────────────────────────────────
#
# Named for what each case COSTS, not for the input it takes, because that is what
# makes a wrong answer recognisable when one of these goes red.


def test_a_free_site_is_not_entitled_to_analytics():
    """The whole point of the slice. Counting a free site's traffic bills a Worker
    invocation against no revenue, so the floor tier buys nothing here — unlike the
    custom-domain allowance, which free genuinely does include."""
    assert (
        entitlements.site_analytics_entitled(plan_tier="free", subscription_status="active")
        is False
    )


def test_a_paid_site_with_an_active_subscription_is_entitled():
    assert (
        entitlements.site_analytics_entitled(plan_tier="site", subscription_status="active") is True
    )
    assert (
        entitlements.site_analytics_entitled(plan_tier="staff", subscription_status="active")
        is True
    )


def test_a_lapsed_paid_site_is_not_entitled():
    """Cancellation never resets ``plan_tier``, so a cancelled site still reads as
    ``site``. Gating on the tier alone would keep counting it forever. ``pending`` is
    the same shape from the other end — a checkout that was opened and never paid."""
    for status in ("cancelled", "pending", "none", None, ""):
        assert (
            entitlements.site_analytics_entitled(plan_tier="site", subscription_status=status)
            is False
        ), f"a {status!r} subscription bought analytics"


def test_an_unset_or_unknown_tier_is_not_entitled():
    """Fail closed on every unknown. An absent tier is a site that never chose one; a
    typo'd or retired key is a document nobody can vouch for. Both are free."""
    for tier in (None, "", "enterprise", "Site", "analytics"):
        assert (
            entitlements.site_analytics_entitled(plan_tier=tier, subscription_status="active")
            is False
        ), f"the {tier!r} tier bought analytics"


def test_an_org_key_stored_on_a_site_is_not_entitled():
    """``studio`` and ``agency`` DO resell analytics, and they are still refused here.
    An org flat is bought once for many sites and its key is not a legal
    ``Site.plan_tier``, so finding one on a single site means a bug, a hand-edit or a
    replayed webhook. ``site_scoped_tier`` refuses it, which is what keeps this
    predicate from reading an org-wide entitlement off one site's field."""
    for tier in ("studio", "agency"):
        assert site_plans.ANALYTICS_FEATURE in site_plans.get_site_plan(tier).cloudflare_features
        assert (
            entitlements.site_analytics_entitled(plan_tier=tier, subscription_status="active")
            is False
        ), f"the org-scoped {tier!r} key entitled a single site"


def test_a_legacy_tier_key_still_resolves():
    """``Site.plan_tier`` holds ``pro`` / ``business`` in production and nothing
    rewrites a document on read. A site that has been paying since before the 2026-08-22
    rekey must not lose its analytics for holding the name it was sold under."""
    assert (
        entitlements.site_analytics_entitled(plan_tier="pro", subscription_status="active") is True
    )
    assert (
        entitlements.site_analytics_entitled(plan_tier="basic", subscription_status="active")
        is False
    )


def test_the_catalog_still_pairs_the_feature_name_with_the_paid_tiers():
    """``ANALYTICS_FEATURE`` names a member of ``_SITE_PLAN_CF_FEATURES``, and the two
    are written separately — the constant so seams can read it, the map so the catalog
    stays flat and readable. Renaming one without the other would entitle nobody, and
    every test above would still pass because they all go through the same constant."""
    assert site_plans.ANALYTICS_FEATURE == "analytics"
    entitled = {
        tier.key
        for tier in site_plans.list_site_plans()
        if site_plans.ANALYTICS_FEATURE in tier.cloudflare_features
    }
    assert entitled == {"site", "staff", "studio", "agency"}


# ── 2. the deploy shape ──────────────────────────────────────────────────────


def test_a_free_publish_writes_no_counter(tmp_path):
    """The cost claim, at the artifact. No ``main`` (nothing invokes a Worker), no
    dataset binding (nothing could write a row), no routing rules, and no entry on
    disk carrying a salt."""
    project = _build_html_project(tmp_path)
    cfg = _write(project, entitled=False)

    assert "main" not in cfg
    assert "analytics_engine_datasets" not in cfg
    assert "run_worker_first" not in cfg["assets"]
    assert not (project / analytics_worker.ENTRY_FILENAME).exists()


def test_a_free_publish_emits_exactly_the_pre_analytics_config(tmp_path):
    """Stronger than the absence checks above, and the one that catches a key nobody
    thought to assert about. The free config is the pre-SA-1 recipe entire — including
    an ``assets`` block of nothing but ``directory``, because a free site has no entry
    for an ``ASSETS`` binding to point at."""
    cfg = _write(_build_html_project(tmp_path), entitled=False)
    assert cfg == PRE_ANALYTICS_HTML_CONFIG


def test_a_paid_publish_writes_the_counting_config(tmp_path):
    """The other half: narrowing the gate until nothing counts would satisfy every
    assertion above. A ``site``-tier publish gets the full SA-1 shape."""
    project = _build_html_project(tmp_path)
    cfg = _write(project, entitled=True)

    assert cfg["main"] == analytics_worker.ENTRY_FILENAME
    assert cfg["assets"]["binding"] == "ASSETS"
    assert cfg["assets"]["run_worker_first"]
    assert cfg["analytics_engine_datasets"] == [
        {
            "binding": analytics_worker.DATASET_BINDING,
            "dataset": analytics_worker.dataset_name(),
        }
    ]
    entry = project / analytics_worker.ENTRY_FILENAME
    assert entry.is_file()
    assert SITE_ID in entry.read_text()


def test_the_entry_is_deleted_when_a_counting_site_publishes_free(tmp_path):
    """THE DOWNGRADE. A publish reuses the pocket's working dir, so the entry a paid
    publish wrote is still sitting there when the same site publishes free. The new
    config names no ``main``, so wrangler would upload that leftover as an ordinary
    static asset and serve the per-publish salt from a config that mentions nothing.

    Asserted as a sequence rather than as a fresh dir, because the fresh-dir version
    passes against code that simply never writes the file."""
    project = _build_html_project(tmp_path)
    _write(project, entitled=True)
    assert (project / analytics_worker.ENTRY_FILENAME).is_file()

    _write(project, entitled=False)
    assert not (project / analytics_worker.ENTRY_FILENAME).exists()


def test_the_assetsignore_names_the_entry_even_when_nothing_counts(tmp_path):
    """The second line of defence behind that delete. Naming a file that is not there
    costs nothing; not naming one that IS there costs the salt."""
    project = _build_html_project(tmp_path)
    _write(project, entitled=False)

    lines = (project / ".assetsignore").read_text().splitlines()
    assert analytics_worker.ENTRY_FILENAME in lines
    assert "wrangler.jsonc" in lines


# ── 3. the kill switch ───────────────────────────────────────────────────────


def test_the_kill_switch_restores_the_pre_analytics_config_for_a_paid_site(tmp_path, monkeypatch):
    """The incident path. With the switch set, a site that IS entitled deploys the
    config an un-analytics build produced — byte for byte, so an operator pulling it
    gets the shape that was proven before analytics existed rather than a near miss."""
    monkeypatch.setenv("PAW_SITES_ANALYTICS_DISABLED", "1")
    project = _build_html_project(tmp_path)

    cfg = _write(project, entitled=True)

    assert cfg == PRE_ANALYTICS_HTML_CONFIG
    assert not (project / analytics_worker.ENTRY_FILENAME).exists()


def test_the_killed_config_is_byte_for_byte_the_free_one(tmp_path, monkeypatch):
    """Compared as TEXT, not as parsed JSON. Key order, indentation and the trailing
    newline are all part of what a rollback restores, and a dict comparison sees none
    of them."""
    paid = _build_html_project(tmp_path / "paid")
    free = _build_html_project(tmp_path / "free")

    workers_deploy._write_deploy_files(
        str(free), WORKER_NAME, "html", None, site_id=SITE_ID, analytics_entitled=False
    )
    free_text = (free / "wrangler.jsonc").read_text()

    monkeypatch.setenv("PAW_SITES_ANALYTICS_DISABLED", "true")
    workers_deploy._write_deploy_files(
        str(paid), WORKER_NAME, "html", None, site_id=SITE_ID, analytics_entitled=True
    )

    assert (paid / "wrangler.jsonc").read_text() == free_text


def test_the_kill_switch_reads_the_value_not_the_presence(monkeypatch):
    """An operator who writes ``=false`` means false. A switch read as
    ``bool(os.environ.get(...))`` fires on every value including that one, which is a
    bad way to learn how a kill switch parses during an incident."""
    for raw in ("1", "true", "TRUE", "yes", "on", " true "):
        monkeypatch.setenv("PAW_SITES_ANALYTICS_DISABLED", raw)
        assert analytics_worker.counting_disabled() is True, f"{raw!r} did not disable"
    for raw in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("PAW_SITES_ANALYTICS_DISABLED", raw)
        assert analytics_worker.counting_disabled() is False, f"{raw!r} disabled"
    monkeypatch.delenv("PAW_SITES_ANALYTICS_DISABLED", raising=False)
    assert analytics_worker.counting_disabled() is False


def test_the_two_halves_of_the_decision_are_independent(monkeypatch):
    """``counting_enabled`` is an AND, and both halves have to be able to say no on
    their own: the entitlement answers to billing and is per site, the switch answers
    to an incident and is global."""
    monkeypatch.delenv("PAW_SITES_ANALYTICS_DISABLED", raising=False)
    assert analytics_worker.counting_enabled(entitled=True) is True
    assert analytics_worker.counting_enabled(entitled=False) is False

    monkeypatch.setenv("PAW_SITES_ANALYTICS_DISABLED", "1")
    assert analytics_worker.counting_enabled(entitled=True) is False
    assert analytics_worker.counting_enabled(entitled=False) is False


# ── 4. the publish path actually consults the gate ───────────────────────────
#
# The tests above drive ``_write_deploy_files`` directly, which defaults to counting.
# Every one of them would pass against a publish path that never passed the flag at
# all, so the gate is worth nothing until it is asserted where the Site document is.


class _FakeGenerator:
    """Stand-in for the SvelteKit generator — never touches Bun or workerd."""

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


def _recording_deployer(seen: dict):
    async def _deploy(site_id: str, project_dir: str, *, analytics_entitled=..., **_: object):
        # A sentinel default rather than a bool: "the publish path passed nothing" and
        # "the publish path passed False" must not read the same, and they would if the
        # fake defaulted to False.
        seen["analytics_entitled"] = analytics_entitled
        return f"https://paw-site-{site_id}.acct.workers.dev"

    return _deploy


async def _publish(pocket_id: str, deployer):
    return await sites_service.publish(
        workspace_id="ws-analytics-gate",
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="Gate Site",
        _generator=_FakeGenerator(),
        _bundle_reader=lambda d: b"unused-in-workers-mode",
        _workers_deploy=deployer,
    )


@pytest.mark.asyncio
async def test_the_publish_path_passes_a_resolved_entitlement(beanie_test_db, monkeypatch):
    """A FIRST publish has no Site document yet — the row is inserted after the deploy —
    so "no document" has to resolve to free. It does, and the value reaching the
    deployer is an explicit False rather than the parameter's own default."""
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    seen: dict = {}

    await _publish("pk-gate-free", _recording_deployer(seen))

    assert seen["analytics_entitled"] is False


@pytest.mark.asyncio
async def test_a_paying_site_republishes_with_the_counter(beanie_test_db, monkeypatch):
    """The same publish path, on a site whose document says it is paying. Two publishes
    because the first is what creates the document — which is also the honest shape of
    the product rule: a site's history begins at the publish that first carried a
    counter, and upgrading backfills nothing."""
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    seen: dict = {}

    site = await _publish("pk-gate-paid", _recording_deployer(seen))
    assert seen["analytics_entitled"] is False

    doc = await sites_service._SiteDoc.find_one({"_id": site.id})
    assert doc is not None
    doc.plan_tier = "site"
    doc.subscription_status = "active"
    await doc.save()

    await _publish("pk-gate-paid", _recording_deployer(seen))

    assert seen["analytics_entitled"] is True


@pytest.mark.asyncio
async def test_a_cancelled_site_republishes_without_the_counter(beanie_test_db, monkeypatch):
    """The seam that pays for itself. ``plan_tier`` still says ``site`` after a
    cancellation — nothing rewrites it — so a publish path reading the tier alone would
    keep billing us for a customer who stopped paying."""
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    seen: dict = {}

    site = await _publish("pk-gate-lapsed", _recording_deployer(seen))
    doc = await sites_service._SiteDoc.find_one({"_id": site.id})
    assert doc is not None
    doc.plan_tier = "site"
    doc.subscription_status = "cancelled"
    await doc.save()

    await _publish("pk-gate-lapsed", _recording_deployer(seen))

    assert seen["analytics_entitled"] is False


# ── 5. the salt does not reach the local server ──────────────────────────────


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


def test_the_local_server_does_not_serve_the_generated_entry(server, tmp_path):
    """THE SALT IS THE POINT. ``deploy_local`` copies the whole static-output tree, and
    for an html site that tree IS the project dir — so after a workers publish into the
    same working dir the generated entry is sitting in it, carrying the per-publish
    secret the visitor hash is built on. A readable salt turns that hash into a
    confirmation oracle: recompute from a candidate IP and user-agent, compare.

    Asserted OVER HTTP rather than by listing the copied dir, because "was it copied"
    and "is it served" are different questions and only the second one is the exposure.
    The page beside it is fetched too — an ignore that swallowed the site would pass a
    404-only test."""
    project = _build_html_project(tmp_path / "project")
    workers_deploy._write_deploy_files(
        str(project), WORKER_NAME, "html", None, site_id=SITE_ID, analytics_entitled=True
    )
    secret = project / analytics_worker.ENTRY_FILENAME
    assert secret.is_file(), "the fixture must actually have an entry to leak"

    url = server.deploy_local("site-gate-1", str(project), engine="html")

    assert _get(f"{url}{analytics_worker.ENTRY_FILENAME}")[0] == 404
    status, body = _get(url)
    assert status == 200
    assert b"<h1>hi</h1>" in body


def test_the_local_server_does_not_serve_the_deploy_config(server, tmp_path):
    """The rest of the scaffold travels with the entry. ``wrangler.jsonc`` is deploy
    plumbing a visitor has no business fetching, and Cloudflare already excludes it from
    what it uploads — this is the local target agreeing with the deployed one."""
    project = _build_html_project(tmp_path / "project")
    workers_deploy._write_deploy_files(
        str(project), WORKER_NAME, "html", None, site_id=SITE_ID, analytics_entitled=True
    )

    url = server.deploy_local("site-gate-2", str(project), engine="html")

    assert _get(f"{url}wrangler.jsonc")[0] == 404
    assert _get(f"{url}.assetsignore")[0] == 404
    assert _get(f"{url}styles.css")[0] == 200

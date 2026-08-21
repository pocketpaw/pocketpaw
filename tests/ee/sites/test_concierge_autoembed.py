# tests/ee/sites/test_concierge_autoembed.py — a published site grows its own
# concierge.
# Created 2026-07-30 (feat/paw-bar-autoembed). The bug this pins shut: a site we
# generated, with a concierge we auto-provisioned, went live with an empty <head>
# and no script tag anywhere — the bar was embedded ONLY by a snippet the dashboard
# printed for a human to paste. Three layers, matching where it can break again:
#   * Pure injection (no I/O): the snippet lands before </body>, a page with no
#     </body> still gets it, a second pass is a no-op (idempotence), and a
#     multi-page tree gets a bar on every page.
#   * The gates: a site earns a bar only with the concierge on, an embed key, a
#     widget for its pocket and an agent bound to that widget. The widget lookup is
#     the provisioner's own ``site_widget``, so the two can't drift.
#   * The publish path: a LIVE publish injects into the built tree BEFORE it
#     deploys and stamps its own deployed host onto allowed_origins; a PREVIEW
#     publish does neither; and an injection failure never costs the site its
#     deploy.
# The allowed_origins half matters as much as the snippet: ``_default_allowed_origins``
# seeds localhost only, so a visitor on the real deployed host was refused by the
# very origin gate the bar and the capture endpoint share — a bar that loads and
# then can't talk is not a shipped feature.

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.paw_bar import embed
from pocketpaw_ee.sites import service as sites_service

_API_BASE = "http://localhost:8888/api/v1"
_KEY = "site_key_" + "a" * 24


def _snippet(widget_id: str = "w-1") -> str:
    return embed.build_embed_snippet(api_base=_API_BASE, site_key=_KEY, widget_id=widget_id)


# --------------------------------------------------------------------------- #
# Layer 1 — injection (pure)
# --------------------------------------------------------------------------- #


def test_snippet_loads_the_loader_from_the_api_base_not_a_cdn():
    """The URL is derived from the base the site's own capture endpoint uses, so a
    locally served site gets a working localhost URL instead of an unprovisioned
    CDN host."""
    s = _snippet()

    assert 'src="http://localhost:8888/api/v1/paw-bar/widget.js"' in s
    assert "pocketpaw.dev" not in s
    assert f'data-site-key="{_KEY}"' in s
    assert 'data-widget-id="w-1"' in s
    assert 'data-endpoint="http://localhost:8888/api/v1"' in s


def test_injects_before_the_closing_body():
    page = "<!doctype html><html><body><h1>Atlas AC</h1></body></html>"

    out = embed.inject_into_html(page, _snippet())

    assert out is not None
    assert out.index("<script") < out.index("</body>")
    assert out.endswith("</body></html>")


def test_a_page_without_a_body_tag_still_gets_the_bar():
    """Some generated pages are fragments. A script tag at the end of a document
    still runs, so appending beats skipping."""
    out = embed.inject_into_html("<h1>Atlas AC</h1>", _snippet())

    assert out is not None
    assert "paw-bar/widget.js" in out


def test_reinjection_is_a_no_op():
    """A re-publish must not leave two bars on the page. The guard is the marker
    attribute, not the whole snippet, so it still holds after the URL or the widget
    id inside the snippet changes."""
    page = "<html><body>hi</body></html>"
    once = embed.inject_into_html(page, _snippet())
    assert once is not None

    # Same snippet, and a DIFFERENT one — both must decline.
    assert embed.inject_into_html(once, _snippet()) is None
    assert embed.inject_into_html(once, _snippet("w-other")) is None
    assert once.count("<script") == 1


def test_every_page_in_the_tree_gets_a_bar(tmp_path):
    root = tmp_path / "built"
    (root / "nested").mkdir(parents=True)
    for rel in ("index.html", "contact.html", "nested/pricing.html"):
        (root / rel).write_text("<html><body>x</body></html>", encoding="utf-8")
    # Not a page — must be left alone.
    (root / "app.js").write_text("console.log('x')", encoding="utf-8")

    changed = embed.inject_into_tree(root, _snippet())

    assert len(changed) == 3
    assert all(
        "paw-bar/widget.js" in (root / r).read_text() for r in ("index.html", "contact.html")
    )
    assert "paw-bar" not in (root / "app.js").read_text()


def test_a_missing_build_tree_is_survivable(tmp_path):
    assert embed.inject_into_tree(tmp_path / "never-built", _snippet()) == []


def test_deployed_host_reduces_a_url_to_the_bare_host():
    """``origin_allowed`` matches bare, lowercased hosts, so what we store has to be
    that same shape or the runtime match can never hit it."""
    assert embed.deployed_host("https://Ridgeline.paw.dev/some/page") == "ridgeline.paw.dev"
    assert embed.deployed_host("http://127.0.0.1:8123/site-1/") == "127.0.0.1"
    assert embed.deployed_host("") == ""


def test_allowlist_gains_the_deployed_host_once():
    seeded = ["localhost", "127.0.0.1"]

    once = sites_service._with_deployed_host(seeded, "https://ridgeline.paw.dev/")
    twice = sites_service._with_deployed_host(once, "https://ridgeline.paw.dev/")

    assert once == ["localhost", "127.0.0.1", "ridgeline.paw.dev"]
    assert twice == once  # a re-publish does not grow the list
    # A deploy with no public URL leaves the list exactly as it was.
    assert sites_service._with_deployed_host(seeded, "") == seeded


def test_a_connected_custom_domain_survives_a_republish():
    """``add_domain`` appends the production hostname; a re-publish only ever GROWS
    this list, so the domain must still be there afterwards."""
    with_domain = ["localhost", "brewco.com"]

    out = sites_service._with_deployed_host(with_domain, "https://site-1.paw.dev")

    assert "brewco.com" in out
    assert "site-1.paw.dev" in out


# --------------------------------------------------------------------------- #
# Layer 2 — the gates
# --------------------------------------------------------------------------- #


def _fake_store(monkeypatch, widget):
    """Point the provisioner's widget lookup at a stand-in. ``embed`` resolves the
    widget THROUGH ``agent_provisioning.site_widget``, so patching the store here is
    also the proof that the two share one lookup."""
    from pocketpaw_ee.paw_bar import agent_provisioning

    class _Store:
        async def list_widgets(self, *, pocket_id, workspace_id, limit):
            self.seen = (pocket_id, workspace_id, limit)
            return [widget] if widget is not None else []

    store = _Store()
    monkeypatch.setattr(agent_provisioning, "_store", lambda: store)
    return store


def _widget(**ov):
    fields = {"id": "w-1", "agent_id": "agent-1"}
    fields.update(ov)
    return type("W", (), fields)()


async def _snippet_for(monkeypatch, *, widget, **ov):
    _fake_store(monkeypatch, widget)
    kwargs = {
        "workspace_id": "ws-1",
        "pocket_id": "pocket-1",
        "site_key": _KEY,
        "api_base": _API_BASE,
        "concierge_enabled": True,
        # Required since feat/sites-concierge-entitlement. These cases are about the
        # OTHER gates (owner switch, key, widget, binding), so entitlement is granted
        # and held constant; the billing gate has its own tree in
        # tests/cloud/test_paw_bar_concierge_entitlement.py.
        "concierge_entitled": True,
    }
    kwargs.update(ov)
    return await embed.concierge_snippet(**kwargs)


@pytest.mark.asyncio
async def test_a_bound_widget_on_an_enabled_site_earns_a_bar(monkeypatch):
    assert 'data-widget-id="w-1"' in await _snippet_for(monkeypatch, widget=_widget())


@pytest.mark.asyncio
async def test_the_kill_switch_means_no_bar(monkeypatch):
    """A re-publish with the concierge off is how an owner takes the bar back off
    their site."""
    assert await _snippet_for(monkeypatch, widget=_widget(), concierge_enabled=False) == ""


@pytest.mark.asyncio
async def test_no_widget_means_no_bar(monkeypatch):
    assert await _snippet_for(monkeypatch, widget=None) == ""


@pytest.mark.asyncio
async def test_an_unbound_widget_means_no_bar(monkeypatch):
    """An unbound widget's chat 409s. A bar that renders and then refuses to answer
    is worse than no bar."""
    assert await _snippet_for(monkeypatch, widget=_widget(agent_id="")) == ""


@pytest.mark.asyncio
async def test_no_embed_key_means_no_bar(monkeypatch):
    """The key IS the credential the loader presents; without one the frame endpoint
    401s every visitor."""
    assert await _snippet_for(monkeypatch, widget=_widget(), site_key="") == ""


@pytest.mark.asyncio
async def test_an_empty_pocket_never_widens_onto_a_sibling(monkeypatch):
    """An unfiltered list_widgets would reach across the workspace and hand back
    another site's widget."""
    store = _fake_store(monkeypatch, _widget())

    out = await embed.concierge_snippet(
        workspace_id="ws-1",
        pocket_id="",
        site_key=_KEY,
        api_base=_API_BASE,
        concierge_enabled=True,
        concierge_entitled=True,
    )

    assert out == ""
    assert not hasattr(store, "seen")  # the store was never queried


# --------------------------------------------------------------------------- #
# Layer 3 — the publish path
# --------------------------------------------------------------------------- #


class _FakeGenerator:
    """Writes a real two-page static tree so the injection has something to walk."""

    def __init__(self, project_dir: Path, engine: str = "ripple"):
        self.project_dir = project_dir
        self.engine = engine

    async def build(self, **kw):
        from pocketpaw_ee.sites.engines import static_output_rel
        from pocketpaw_ee.sites.generator_client import BuildResult

        out = Path(self.project_dir, static_output_rel(self.engine))
        out.mkdir(parents=True, exist_ok=True)
        for rel in ("index.html", "contact.html"):
            (out / rel).write_text(
                "<!doctype html><html><body><h1>Ridgeline HVAC</h1></body></html>",
                encoding="utf-8",
            )
        return BuildResult(project_dir=str(self.project_dir), ripple_version="0.2.0")


async def _publish(tmp_path, *, preview: bool = False, deploy_url: str = "https://site-1.paw.dev"):
    return await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pocket-1",
        ripple_spec={"type": "container"},
        theme={},
        name="Ridgeline HVAC",
        preview=preview,
        _generator=_FakeGenerator(tmp_path / "project"),
        _cloudflare=None,
        _bundle_reader=lambda d: b"x",
        _local_deploy=lambda site_id, project_dir: deploy_url,
    )


def _built_pages(tmp_path) -> list[str]:
    root = tmp_path / "project" / ".svelte-kit" / "cloudflare"
    return [p.read_text(encoding="utf-8") for p in sorted(root.glob("*.html"))]


@pytest.mark.asyncio
async def test_a_live_publish_embeds_the_bar_into_every_built_page(
    beanie_test_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _fake_store(monkeypatch, _widget())

    await _publish(tmp_path)

    pages = _built_pages(tmp_path)
    assert len(pages) == 2
    for page in pages:
        assert "/paw-bar/widget.js" in page
        assert page.index("<script") < page.index("</body>")


@pytest.mark.asyncio
async def test_a_live_publish_allows_its_own_deployed_host(beanie_test_db, tmp_path, monkeypatch):
    """Without this the bar loads on the real page and is then refused by the origin
    gate — the site's own visitors look like strangers."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _fake_store(monkeypatch, _widget())

    site = await _publish(tmp_path, deploy_url="https://ridgeline.paw.dev/")

    assert "ridgeline.paw.dev" in site.allowed_origins
    assert "localhost" in site.allowed_origins  # the seeded dev hosts survive


@pytest.mark.asyncio
async def test_republishing_does_not_stack_bars_or_hosts(beanie_test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _fake_store(monkeypatch, _widget())

    await _publish(tmp_path)
    site = await _publish(tmp_path)

    assert _built_pages(tmp_path)[0].count("<script") == 1
    assert site.allowed_origins.count("site-1.paw.dev") == 1


@pytest.mark.asyncio
async def test_a_preview_publish_never_embeds(beanie_test_db, tmp_path, monkeypatch):
    """A preview is a draft nobody approved. Baking the live embed key into it would
    put a working concierge on an unreviewed page."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _fake_store(monkeypatch, _widget())

    await _publish(tmp_path, preview=True)

    assert all("paw-bar/widget.js" not in page for page in _built_pages(tmp_path))


@pytest.mark.asyncio
async def test_a_broken_injection_never_costs_the_site_its_deploy(
    beanie_test_db, tmp_path, monkeypatch
):
    """This runs mid-publish. A site going live matters more than its bar, so an
    injection that blows up must log and let the deploy through."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _fake_store(monkeypatch, _widget())

    def _boom(root, snippet):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(embed, "inject_into_tree", _boom)

    site = await _publish(tmp_path)

    assert site.deployed is True
    assert all("paw-bar/widget.js" not in page for page in _built_pages(tmp_path))


# --------------------------------------------------------------------------- #
# The BILLING gate at the publish seam (feat/sites-concierge-entitlement).
#
# These exist because review found the publish-path resolver in
# ``_embed_concierge_bar`` had no test at all: the entitlement tree calls
# ``concierge_snippet`` directly, so deleting the whole
# ``if get_settings().billing_enforced:`` block left the suite green. That block is
# also exactly where the charge-first bug lived, which is why it went unnoticed.
#
# Updated 2026-08-21 (fix/sites-concierge-flag): the block now reads
# ``concierge_enforced()``, the concierge's own switch. It briefly rode on
# ``sites_billing_enforced`` and enabling that flag for the DOMAIN caps took the
# concierge off every production site. Neither paywall flag reaches this gate any
# more, which is why the stub below sets all three rather than one.
# --------------------------------------------------------------------------- #


def _billing(monkeypatch, *, on: bool) -> None:
    """Point the lazily-imported ``get_settings`` at a billing-posture stub.

    ``sites_concierge_enforced`` is the one that arms this gate. The other two are
    set alongside it only so the stub resembles a real Settings; a test proving
    they must NOT arm it lives in
    tests/cloud/sites/test_concierge_not_on_the_domain_flag.py.
    """
    from types import SimpleNamespace

    import pocketpaw.config as ppconfig

    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(
            billing_enforced=on,
            sites_billing_enforced=on,
            sites_concierge_enforced=on,
            dodo_site_products=None,
        ),
    )


@pytest.mark.asyncio
async def test_a_free_site_publishes_with_no_bar_when_billing_is_enforced(
    beanie_test_db, tmp_path, monkeypatch
):
    """The publish-seam half of the billing gate. A first publish has no Site doc, so
    no ``plan_tier``, which resolves to the free floor — the page ships bar-less
    rather than carrying a bar that would 403 every visitor."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _billing(monkeypatch, on=True)
    _fake_store(monkeypatch, _widget())

    await _publish(tmp_path)

    assert all("paw-bar/widget.js" not in page for page in _built_pages(tmp_path))


@pytest.mark.asyncio
async def test_with_billing_off_a_publish_is_byte_for_byte_what_it_was(
    beanie_test_db, tmp_path, monkeypatch
):
    """OSS / self-host, and every in-repo deploy today. The gate must not take the
    bar off a publish that never had billing."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _billing(monkeypatch, on=False)
    _fake_store(monkeypatch, _widget())

    await _publish(tmp_path)

    pages = _built_pages(tmp_path)
    assert len(pages) == 2
    assert all("/paw-bar/widget.js" in page for page in pages)


@pytest.mark.asyncio
async def test_a_paying_site_mid_activation_still_gets_its_bar(
    beanie_test_db, tmp_path, monkeypatch
):
    """THE CHARGE-FIRST REGRESSION, at the seam it actually broke.

    ``activate_site`` runs on the ``subscription.active`` webhook — payment already
    confirmed — and deploys the site. A republish onto a paid tier likewise parks a
    live site at "pending" until the new sub confirms, and ``_apply_site_plan``
    stamps the plan AFTER ``publish()`` has deployed. All three mean this seam can
    see "pending" for someone who has paid.

    Refusing there ships a page with no loader script, and nothing re-runs the embed
    afterwards — it stays bar-less until some unrelated publish. So "pending" is read
    as paid HERE, while the runtime gate keeps refusing it (pinned in
    tests/cloud/test_paw_bar_concierge_entitlement.py).
    """
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.models.site import Site

    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _billing(monkeypatch, on=True)
    _fake_store(monkeypatch, _widget())
    paid = next(
        t.key for t in site_plans.list_site_plans() if t.key != site_plans.BASE_SITE_PLAN_KEY
    )

    # First publish creates the doc; then put it in the exact mid-activation state.
    site = await _publish(tmp_path)
    doc = await Site.get(site.id)
    doc.plan_tier = paid
    doc.subscription_status = "pending"
    await doc.save()

    await _publish(tmp_path)

    pages = _built_pages(tmp_path)
    assert len(pages) == 2
    assert all("/paw-bar/widget.js" in page for page in pages), (
        "a paying customer's page shipped with no concierge loader"
    )


@pytest.mark.asyncio
async def test_a_cancelled_paid_site_loses_its_bar_on_the_next_publish(
    beanie_test_db, tmp_path, monkeypatch
):
    """The control for the test above: leniency is scoped to "pending" alone.

    Cancellation never resets ``plan_tier``, so a resolver reading the tier by itself
    would keep serving this site forever.
    """
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.models.site import Site

    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    _billing(monkeypatch, on=True)
    _fake_store(monkeypatch, _widget())
    paid = next(
        t.key for t in site_plans.list_site_plans() if t.key != site_plans.BASE_SITE_PLAN_KEY
    )

    site = await _publish(tmp_path)
    doc = await Site.get(site.id)
    doc.plan_tier = paid
    doc.subscription_status = "cancelled"
    await doc.save()

    await _publish(tmp_path)

    assert all("paw-bar/widget.js" not in page for page in _built_pages(tmp_path))

# tests/ee/sites/test_domain_routing.py
# Created: 2026-08-12 (the custom-domain routing lane).
#
# WHAT THIS COVERS AND WHY IT DID NOT EXIST BEFORE. ``add_domain`` made exactly one
# Cloudflare call: create the custom hostname. That call only makes Cloudflare willing
# to terminate TLS for the domain — it says nothing about WHICH site answers it. So a
# connected domain validated, went green in the Domains panel, and served an error page
# from the fallback origin. Every signal reported success; the site was unreachable.
#
# The missing half is a Worker route scoped to that one hostname. Cloudflare's
# worker-as-origin doc supports a route set to an exact custom hostname, so the control
# plane writes ``<hostname>/*`` -> the site's own Worker at add time. One route per
# domain, no dispatcher, no KV, no extra hop.
#
# The five behaviours pinned here are the ones where "it made the API call" and "the
# domain works" come apart:
#   1. The route is written, with the pattern and script name Cloudflare needs, and its
#      id is STORED — a route nobody recorded is an orphan nobody can delete.
#   2. A site that has never been published is REFUSED. A route can only name a Worker
#      that exists; allowing this produces a live-looking dead domain.
#   3. A failed route rolls the hostname back. Cloudflare rejects duplicate hostnames,
#      so a half-made pair would make the user's obvious retry fail on a conflict they
#      cannot see or clear.
#   4. Deploy modes with no per-site Worker (wfp / local) keep the prior hostname-only
#      behaviour rather than writing a route naming a script that does not exist.
#   5. Teardown removes the route BEFORE the hostname, and drops the local row and the
#      origin grant. There was no teardown path at all before this.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus

pytestmark = pytest.mark.asyncio


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    """Records every call in order, so the test can assert the SEQUENCE and not just
    the set. Ordering is load-bearing in teardown: the route has to stop serving the
    domain before Cloudflare stops recognising it."""

    def __init__(self, *, route_id: str = "route_9", route_error: Exception | None = None):
        self.calls: list[tuple] = []
        self._route_id = route_id
        self._route_error = route_error

    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True

    async def create_custom_hostname(self, hostname, *, features=None):
        self.calls.append(("create_hostname", hostname))
        return CustomHostname(
            id="ch_1",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="sites.pawzone.test",
        )

    async def create_worker_route(self, *, pattern, script):
        self.calls.append(("create_route", pattern, script))
        if self._route_error is not None:
            raise self._route_error
        return self._route_id

    async def delete_worker_route(self, route_id):
        self.calls.append(("delete_route", route_id))

    async def delete_custom_hostname(self, hostname_id):
        self.calls.append(("delete_hostname", hostname_id))


async def _make_site(
    *, workspace_id: str = "ws1", pocket_id: str = "pk-domains", deployed: bool = True
) -> str:
    """Publish a throwaway site and return its id.

    ``deployed`` is set explicitly rather than relying on what publish happens to do
    with a fake Cloudflare — the refusal under test keys on that exact flag, so a test
    that inherited it would be asserting publish's behaviour instead of add_domain's.
    """
    site = await sites_service.publish(
        workspace_id=workspace_id,
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        name="Bright Smile",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        # The fake generator's project dir does not exist on disk, so the real bundle
        # reader would fail looking for _worker.js. Nothing here cares what deployed.
        _bundle_reader=lambda _d: b"x",
    )
    site.deployed = deployed
    await site.save()
    return str(site.id)


def _workers_mode(monkeypatch) -> None:
    """Select the one deploy mode that produces a per-site, route-addressable Worker."""
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")


async def test_add_domain_routes_the_hostname_to_this_sites_worker(monkeypatch, beanie_test_db):
    """The route is the half that was missing.

    Both the pattern and the script name are checked exactly. ``<hostname>/*`` and not
    a bare host: a bare host matches only the root, so the home page would load and
    every other page 404 — which reads as a broken site, not a broken route. The script
    name comes from the same function the deploy uses, because Cloudflare rejects a
    route naming a script that does not exist.

    MUTATION THAT BREAKS THIS: dropping the ``create_worker_route`` call; changing the
    pattern to ``ch.hostname``; storing ``cf_route_id=""``.
    """
    site_id = await _make_site()
    _workers_mode(monkeypatch)
    cf = _FakeCF(route_id="route_9")

    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    assert cf.calls == [
        ("create_hostname", "www.example.com"),
        ("create_route", "www.example.com/*", f"paw-site-{site_id}"),
    ]

    domains = await sites_service.list_domains(workspace_id="ws1", site_id=site_id)
    assert [d.hostname for d in domains] == ["www.example.com"]
    # The route id has to survive on the document: teardown deletes BY id, so a route
    # created and not recorded is exactly the orphan this lane exists to stop making.
    site = await sites_service._load("ws1", site_id)
    assert site.domains[0].cf_route_id == "route_9"
    # The domain's own origin is authorized so its forms can post.
    assert "www.example.com" in site.allowed_origins


async def test_add_domain_refuses_a_site_that_was_never_published(monkeypatch, beanie_test_db):
    """A route can only name a Worker that exists, and an unpublished site has none.

    The refusal costs the user one ordering hint. Allowing it costs them a domain that
    validates, shows Live, and serves an error page — with nothing in the UI able to
    tell that apart from working.

    MUTATION THAT BREAKS THIS: removing the ``not site.deployed`` guard.
    """
    site_id = await _make_site(pocket_id="pk-unpublished", deployed=False)
    _workers_mode(monkeypatch)
    cf = _FakeCF()

    with pytest.raises(ValidationError) as exc:
        await sites_service.add_domain(
            workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
        )

    assert exc.value.code == "sites.domain_needs_publish"
    # And it refuses BEFORE creating anything on the zone — a hostname created here
    # would occupy the name while blocking the retry that would fix it.
    assert cf.calls == []


async def test_a_failed_route_rolls_the_custom_hostname_back(monkeypatch, beanie_test_db):
    """Cloudflare rejects a duplicate hostname (1406). So a create that succeeds
    followed by a route that fails leaves the domain's name occupied by a half-made
    pair, and the user's obvious next move — press Add again — then fails with a
    conflict about a resource they can neither see nor clear.

    The ORIGINAL error propagates, not the rollback's outcome: the rollback is
    housekeeping, and reporting it instead would replace the real reason with a less
    useful one.

    MUTATION THAT BREAKS THIS: removing the ``except`` block around the route call, or
    swallowing the re-raise.
    """
    site_id = await _make_site(pocket_id="pk-rollback")
    _workers_mode(monkeypatch)
    boom = ValidationError("sites.cloudflare_error", "script_not_found")
    cf = _FakeCF(route_error=boom)

    with pytest.raises(ValidationError) as exc:
        await sites_service.add_domain(
            workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
        )

    assert "script_not_found" in str(exc.value)
    assert cf.calls == [
        ("create_hostname", "www.example.com"),
        ("create_route", "www.example.com/*", f"paw-site-{site_id}"),
        ("delete_hostname", "ch_1"),
    ]
    # Nothing half-connected is left on the site either.
    domains = await sites_service.list_domains(workspace_id="ws1", site_id=site_id)
    assert domains == []


@pytest.mark.parametrize("mode", ["wfp", "local"])
async def test_modes_without_a_per_site_worker_write_no_route(
    mode: str, monkeypatch, beanie_test_db
):
    """Only ``workers`` mode deploys a site as its own addressable Worker. ``local``
    serves from localhost; ``wfp`` puts the script inside a dispatch namespace where it
    is not route-addressable at all and the namespace's own dispatch Worker routes.

    Writing a route in those modes would name a script Cloudflare cannot find, so the
    add would fail for a site that is otherwise fine. Prior behaviour — hostname only —
    is preserved exactly.

    MUTATION THAT BREAKS THIS: dropping the ``mode != "workers"`` check in
    ``_route_target`` so every mode writes a route.
    """
    site_id = await _make_site(pocket_id=f"pk-{mode}")
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", mode)
    cf = _FakeCF()

    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    assert cf.calls == [("create_hostname", "www.example.com")]
    site = await sites_service._load("ws1", site_id)
    assert site.domains[0].cf_route_id == ""


async def test_a_provisioned_site_gets_a_route_even_in_local_mode(monkeypatch, beanie_test_db):
    """The deploy MODE does not decide whether a Worker exists, and reading it as if it
    did was a bug this suite originally shipped with.

    ``provision_deploy`` degrades ``local`` to ``workers`` for a dynamic site — nothing
    serves a D1 binding on localhost — so a provisioned site on a local-mode box has a
    real ``paw-site-<id>`` Worker while ``_deploy_mode()`` still answers ``local``. The
    first version of ``_route_target`` asked only the mode, returned "", and wrote no
    route: the domain would validate, go green, and serve the wrong thing, which is the
    entire failure this lane exists to remove.

    ``provision_status == "provisioned"`` is the artifact of that degradation actually
    having happened, which is a better question than "what kind of site was this meant
    to be" — the same move ``workers_deploy`` made for engines in SL-1.

    MUTATION THAT BREAKS THIS: removing the ``provision_status`` branch from
    ``_route_target``, restoring the mode-only check.
    """
    site_id = await _make_site(pocket_id="pk-dynamic-local")
    site = await sites_service._load("ws1", site_id)
    site.provision_status = "provisioned"
    await site.save()
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    cf = _FakeCF(route_id="route_dyn")

    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    assert cf.calls == [
        ("create_hostname", "www.example.com"),
        ("create_route", "www.example.com/*", f"paw-site-{site_id}"),
    ]
    site = await sites_service._load("ws1", site_id)
    assert site.domains[0].cf_route_id == "route_dyn"


async def test_a_site_still_provisioning_in_local_mode_writes_no_route(monkeypatch, beanie_test_db):
    """The degradation only applies once provisioning SUCCEEDED. A site mid-provision
    (or failed) has no Worker yet, so a route would name a script Cloudflare cannot
    find and the add would fail for a site that is merely not ready.

    Pinned separately from the happy path because "provisioned" and "has a
    provision_status at all" are easy to conflate, and conflating them turns a clean
    "publish first" refusal into a Cloudflare error.
    """
    site_id = await _make_site(pocket_id="pk-provisioning")
    site = await sites_service._load("ws1", site_id)
    site.provision_status = "provisioning"
    await site.save()
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    cf = _FakeCF()

    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    assert cf.calls == [("create_hostname", "www.example.com")]


async def test_adding_the_same_hostname_twice_makes_no_second_route(monkeypatch, beanie_test_db):
    """Re-adding a connected domain answers from the stored row and spends nothing.

    This mattered less before routing: a duplicate add appended a second ``SiteDomain``
    row and only ``allowed_origins`` was de-duped. With a route per domain, a second row
    means two routes claiming one pattern and a teardown that removes half of it —
    leaving the domain served by a route nothing points at.

    MUTATION THAT BREAKS THIS: dropping the ``existing is not None`` early return.
    """
    site_id = await _make_site(pocket_id="pk-dupe")
    _workers_mode(monkeypatch)
    cf = _FakeCF()

    first = await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )
    cf.calls.clear()
    again = await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    # No second hostname, no second route — and the same instruction comes back, so
    # pressing Add twice is not an error the user has to interpret.
    assert cf.calls == []
    assert (again.hostname, again.cname_target) == (first.hostname, first.cname_target)
    site = await sites_service._load("ws1", site_id)
    assert len(site.domains) == 1


async def test_remove_domain_tears_down_route_then_hostname(monkeypatch, beanie_test_db):
    """Teardown did not exist. A hostname left on the zone counts against quota
    forever, keeps pointing at a Worker that may be gone, and — because Cloudflare
    rejects duplicates — permanently blocks that domain from being connected to a
    different site.

    Order matters: the ROUTE goes first so the domain stops being served before it
    stops being recognised. The reverse leaves a window where Cloudflare no longer
    knows the hostname while a route still claims it.

    MUTATION THAT BREAKS THIS: swapping the two delete calls; leaving the row in
    ``site.domains``; leaving the host in ``allowed_origins``.
    """
    site_id = await _make_site(pocket_id="pk-teardown")
    _workers_mode(monkeypatch)
    cf = _FakeCF(route_id="route_9")
    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )
    cf.calls.clear()

    await sites_service.remove_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    assert cf.calls == [("delete_route", "route_9"), ("delete_hostname", "ch_1")]
    site = await sites_service._load("ws1", site_id)
    assert site.domains == []
    # The origin grant went with it: with the domain gone it is a standing permission
    # for a host we no longer serve.
    assert "www.example.com" not in site.allowed_origins


async def test_remove_domain_on_an_unconnected_hostname_is_a_404(monkeypatch, beanie_test_db):
    """A hostname this site does not have is NotFound, not a silent success — a
    no-op here would let a typo report that a domain was disconnected while it kept
    serving."""
    site_id = await _make_site(pocket_id="pk-missing")
    _workers_mode(monkeypatch)

    with pytest.raises(NotFound):
        await sites_service.remove_domain(
            workspace_id="ws1", site_id=site_id, hostname="nope.example.com", _cloudflare=_FakeCF()
        )

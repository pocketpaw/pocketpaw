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
from pocketpaw_ee.cloud.models.site import SiteDomain as _SiteDomainDoc
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
    *,
    workspace_id: str = "ws1",
    pocket_id: str = "pk-domains",
    deployed: bool = True,
    deploy_target: str = "workers",
) -> str:
    """Publish a throwaway site and return its id.

    ``deployed`` and ``deploy_target`` are set explicitly rather than inherited from
    whatever publish does with a fake Cloudflare. Both are exactly what the behaviour
    under test keys on, so a test that inherited them would be asserting publish's
    behaviour instead of add_domain's — and would keep passing if publish stopped
    stamping either one.

    ``deploy_target`` is the target the last deploy actually USED, which is the only
    thing that answers "does this site have its own route-addressable Worker". The
    environment cannot: it is read at request time while the Worker was made at deploy
    time, and the two disagree on republish, on a mode change, and on the local→workers
    degradation for dynamic sites.
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
    site.deploy_target = deploy_target
    await site.save()
    return str(site.id)


async def test_add_domain_routes_the_hostname_to_this_sites_worker(beanie_test_db):
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


async def test_add_domain_refuses_a_site_that_was_never_published(beanie_test_db):
    """A route can only name a Worker that exists, and an unpublished site has none.

    The refusal costs the user one ordering hint. Allowing it costs them a domain that
    validates, shows Live, and serves an error page — with nothing in the UI able to
    tell that apart from working.

    MUTATION THAT BREAKS THIS: removing the ``not site.deployed`` guard.
    """
    site_id = await _make_site(pocket_id="pk-unpublished", deployed=False)
    cf = _FakeCF()

    with pytest.raises(ValidationError) as exc:
        await sites_service.add_domain(
            workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
        )

    assert exc.value.code == "sites.domain_needs_publish"
    # And it refuses BEFORE creating anything on the zone — a hostname created here
    # would occupy the name while blocking the retry that would fix it.
    assert cf.calls == []


async def test_a_failed_route_rolls_the_custom_hostname_back(beanie_test_db):
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


@pytest.mark.parametrize("target", ["wfp", "local", ""])
async def test_targets_without_a_per_site_worker_write_no_route(target: str, beanie_test_db):
    """Only a ``workers`` deploy produces a site's own addressable Worker. ``local``
    serves from localhost; ``wfp`` uploads into a dispatch namespace, where the script is
    not route-addressable and the namespace's dispatch Worker routes. ``""`` is a site
    that has never deployed at all.

    Writing a route for any of those names a script Cloudflare cannot find, so the add
    would fail for a site that is otherwise fine. Prior behaviour — hostname only — is
    preserved exactly.

    MUTATION THAT BREAKS THIS: dropping the ``deploy_target != "workers"`` check in
    ``_route_target`` so every site gets a route.
    """
    site_id = await _make_site(pocket_id=f"pk-{target or 'never'}", deploy_target=target)
    cf = _FakeCF()

    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    assert cf.calls == [("create_hostname", "www.example.com")]
    site = await sites_service._load("ws1", site_id)
    assert site.domains[0].cf_route_id == ""


async def test_the_environment_does_not_decide_whether_a_worker_exists(monkeypatch, beanie_test_db):
    """The deploy MODE cannot answer "does this site have a Worker", and two earlier
    versions of ``_route_target`` asked it anyway — wrongly, in opposite directions.

    The mode is read at REQUEST time; the Worker was made at DEPLOY time. They disagree
    routinely: ``provision_deploy`` degrades local→workers for a dynamic site, nothing
    ever deletes a Worker so a site keeps its own after the env moves to ``wfp``, and a
    republish resets ``provision_status`` (the second attempt's predicate) while last
    deploy's Worker is still live and serving — the ordinary lifecycle of any site that
    has been up a while.

    So this pins the property directly: with the env set to ``local``, a site whose last
    deploy WAS ``workers`` still gets its route, and a site whose last deploy was ``wfp``
    still does not. Neither answer moves when the environment does.

    MUTATION THAT BREAKS THIS: reading ``_deploy_mode()`` in ``_route_target`` instead of
    ``site.deploy_target``.
    """
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")

    workers_site = await _make_site(pocket_id="pk-was-workers", deploy_target="workers")
    cf = _FakeCF(route_id="route_dyn")
    await sites_service.add_domain(
        workspace_id="ws1", site_id=workers_site, hostname="www.example.com", _cloudflare=cf
    )
    assert cf.calls == [
        ("create_hostname", "www.example.com"),
        ("create_route", "www.example.com/*", f"paw-site-{workers_site}"),
    ]
    site = await sites_service._load("ws1", workers_site)
    assert site.domains[0].cf_route_id == "route_dyn"

    wfp_site = await _make_site(pocket_id="pk-was-wfp", deploy_target="wfp")
    cf2 = _FakeCF()
    await sites_service.add_domain(
        workspace_id="ws1", site_id=wfp_site, hostname="shop.example.com", _cloudflare=cf2
    )
    assert cf2.calls == [("create_hostname", "shop.example.com")]


async def test_a_republishing_site_keeps_the_worker_its_last_deploy_made(beanie_test_db):
    """A republish of a dynamic site sets ``deployed=False`` and ``provision_status`` back
    to ``"provisioning"`` — while the Worker from the LAST successful deploy is still
    live and still serving, because nothing in this codebase deletes a Worker.

    Under the ``provision_status`` predicate this window returned no script, which then
    short-circuited the ``deployed`` guard (``if script and not site.deployed``) — so the
    add succeeded, wrote no route, and once the republish finished the row looked
    entirely healthy while permanently missing its route. Worse than the first-provision
    case precisely because nothing afterwards looks wrong.

    ``deploy_target`` survives a republish, so the site is correctly seen to HAVE a
    Worker, which re-arms the ``deployed`` guard and turns a silent mis-add into a clean
    "publish first" refusal.

    MUTATION THAT BREAKS THIS: keying ``_route_target`` on ``provision_status`` again.
    """
    site_id = await _make_site(pocket_id="pk-republishing", deployed=True)
    site = await sites_service._load("ws1", site_id)
    site.deployed = False
    site.provision_status = "provisioning"
    await site.save()
    cf = _FakeCF()

    with pytest.raises(ValidationError) as exc:
        await sites_service.add_domain(
            workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
        )

    assert exc.value.code == "sites.domain_needs_publish"
    assert cf.calls == []


async def test_adding_the_same_hostname_twice_makes_no_second_route(beanie_test_db):
    """Re-adding a connected domain answers from the stored row and spends nothing.

    This mattered less before routing: a duplicate add appended a second ``SiteDomain``
    row and only ``allowed_origins`` was de-duped. With a route per domain, a second row
    means two routes claiming one pattern and a teardown that removes half of it —
    leaving the domain served by a route nothing points at.

    MUTATION THAT BREAKS THIS: dropping the ``existing is not None`` early return.
    """
    site_id = await _make_site(pocket_id="pk-dupe")
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


async def test_re_adding_repairs_a_domain_that_has_no_route(beanie_test_db):
    """Every domain connected before this lane shipped has no route and is silently
    serving the fallback origin — and Cloudflare reports those hostnames ``active``, so
    the panel shows them green. Nothing distinguishes them in the API response either.

    Pressing Add again is the only self-service action a user has, and the dedupe guard
    turned it into a no-op that returned the stored row. So the repair rides the guard:
    a stored domain with no ``cf_route_id`` gets its route created and persisted, and
    the same CNAME comes back. No new endpoint, no migration, no support ticket.

    MUTATION THAT BREAKS THIS: dropping the ``not existing.cf_route_id`` repair branch
    so the early return changes nothing.
    """
    site_id = await _make_site(pocket_id="pk-legacy")
    site = await sites_service._load("ws1", site_id)
    # A row exactly as the pre-routing code left it: hostname connected, no route.
    site.domains.append(
        _SiteDomainDoc(
            hostname="www.example.com",
            cf_hostname_id="ch_legacy",
            cname_target="sites.pawzone.test",
            status="live",
        )
    )
    await site.save()
    cf = _FakeCF(route_id="route_repaired")

    res = await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    # Only the missing half is created — no second custom hostname, which Cloudflare
    # would reject anyway.
    assert cf.calls == [("create_route", "www.example.com/*", f"paw-site-{site_id}")]
    assert res.cname_target == "sites.pawzone.test"
    site = await sites_service._load("ws1", site_id)
    assert len(site.domains) == 1
    assert site.domains[0].cf_route_id == "route_repaired"


async def test_a_hostname_spelled_differently_still_finds_its_domain(beanie_test_db):
    """DNS is case-insensitive and the trailing dot is optional; Python's ``==`` is
    neither, and the stored hostname is whatever Cloudflare echoed back (lowercased).

    ``remove_domain`` takes its hostname from a raw URL path segment with no DTO
    validator in between, so this is the one place a caller can spell a connected domain
    in a way that 404s it. ``add_domain``'s dedupe guard had the same gap in the other
    direction: ``Example.com`` after ``example.com`` slipped past it into a Cloudflare
    1406 about a conflict the user could not see.

    MUTATION THAT BREAKS THIS: dropping ``_normalize_hostname`` from either lookup.
    """
    site_id = await _make_site(pocket_id="pk-spelling")
    cf = _FakeCF(route_id="route_9")
    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )
    cf.calls.clear()

    # Same name, three legal spellings: the guard must recognise all of them.
    again = await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="WWW.Example.com", _cloudflare=cf
    )
    assert cf.calls == []
    assert again.hostname == "www.example.com"

    # ...and the trailing-dot FQDN form must not 404 on the way out.
    await sites_service.remove_domain(
        workspace_id="ws1", site_id=site_id, hostname="WWW.example.com.", _cloudflare=cf
    )
    site = await sites_service._load("ws1", site_id)
    assert site.domains == []
    assert "www.example.com" not in site.allowed_origins


async def test_remove_domain_tears_down_route_then_hostname(beanie_test_db):
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


async def test_remove_domain_writes_only_the_domain_fields(beanie_test_db):
    """A domain write must not carry the rest of the document with it.

    This module's own header warns why: a build runs for MINUTES beside a publish
    writing ``url`` / ``deployed`` / ``build_status`` on the same row, and a full
    ``save()`` from a doc loaded before those writes rolls them back. Land after the
    terminal build write and ``build_status`` reverts to in-flight permanently, at which
    point ``build_state.should_enqueue`` refuses to republish that site ever again.

    Simulated by mutating the row underneath an already-loaded service call — which is
    exactly what a concurrent build does.

    MUTATION THAT BREAKS THIS: swapping the targeted ``set`` back to ``save()``.
    """
    site_id = await _make_site(pocket_id="pk-concurrent")
    cf = _FakeCF(route_id="route_9")
    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=cf
    )

    class _RacingCF(_FakeCF):
        """Writes a build field mid-teardown, between the service's load and its save —
        the window a real build occupies for minutes."""

        async def delete_worker_route(self, route_id):
            await super().delete_worker_route(route_id)
            doc = await sites_service._load("ws1", site_id)
            await doc.set({"build_status": "ok", "url": "https://live.example"})

    await sites_service.remove_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.example.com", _cloudflare=_RacingCF()
    )

    site = await sites_service._load("ws1", site_id)
    assert site.domains == []
    # The concurrent writes SURVIVED. Under save() they would be rolled back to the
    # values the stale in-memory doc was holding.
    assert site.build_status == "ok"
    assert site.url == "https://live.example"


async def test_remove_domain_on_an_unconnected_hostname_is_a_404(beanie_test_db):
    """A hostname this site does not have is NotFound, not a silent success — a
    no-op here would let a typo report that a domain was disconnected while it kept
    serving."""
    site_id = await _make_site(pocket_id="pk-missing")

    with pytest.raises(NotFound):
        await sites_service.remove_domain(
            workspace_id="ws1", site_id=site_id, hostname="nope.example.com", _cloudflare=_FakeCF()
        )


async def test_publish_stamps_the_target_it_actually_deployed_to(monkeypatch, beanie_test_db):
    """The routing decision is only as good as the field it reads, and nothing else in
    this file would notice if publish stopped writing it — every other test sets
    ``deploy_target`` by hand, which is the right call for isolating add_domain and
    exactly why this gap needs its own test.

    MUTATION THAT BREAKS THIS: dropping ``deploy_target=mode`` / ``doc.deploy_target =
    mode`` from ``_deploy_site_doc``'s upsert.
    """
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "wfp")
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk-stamped",
        ripple_spec={"type": "container"},
        theme={},
        name="Stamped",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda _d: b"x",
    )
    assert site.deploy_target == "wfp"

    # A republish onto a different target updates it — a stale value would route a
    # site at the Worker its PREVIOUS deploy made.
    #
    # No ``_cloudflare`` on this one: an injected CF client deliberately overrides a
    # local-mode env to wfp (the seam predates deploy modes and exists so CF-branch
    # tests are not hijacked). Passing one here would assert that publish stamps the
    # mode it was CONFIGURED with rather than the branch it TOOK — the exact confusion
    # this field exists to end, written into its own test.
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk-stamped",
        ripple_spec={"type": "container"},
        theme={},
        name="Stamped",
        _generator=_FakeGenerator(),
        _bundle_reader=lambda _d: b"x",
        _local_deploy=lambda _s, _d: "http://localhost:9999",
    )
    assert site.deploy_target == "local"

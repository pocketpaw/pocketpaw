# Tests for the project engine's no-build lane (a ``"build": "none"`` starter such as
# ``static``) and the paw.js / gateway-token reach of project sites: detection, the
# served tree and its exclusions, paw.js injection on and off (sites/paw_client.py),
# an in-process build that never spends a sandbox, publish as an assets-only Worker,
# ``project-build --site-config`` behind its generator version guard, and the
# PAW_GATEWAY_TOKEN binding. Daytona, Cloudflare and the paw-sites CLI are faked.
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import (
    build_job,
    bundle_deploy,
    paw_client,
    preview_origin,
    project_build,
    site_secrets,
)
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.bundle_deploy import ProvisionedResources

from tests.ee.sites.test_project_engine import (
    GEN,
    SOURCE,
    STATIC_MANIFEST,
    _bundle,
    _Pool,
    _Records,
    _result,
    _Runner,
    _Store,
)

TEMPLATE = {
    "slug": "static",
    "name": "Static HTML",
    "target": "assets",
    "backend": {"builtin": []},
    "secrets": [],
    "customization": {"tokens": "styles/tokens.css"},
    "build": "none",
}

NOBUILD = {
    "paw.template.json": json.dumps(TEMPLATE),
    "AGENTS.md": "Use data-paw-checkout on a button to sell a product.\n",
    "index.html": "<html><body><h1>Bakery</h1></body></html>",
    "about/index.html": "<html><body>About</body></html>",
    "styles/tokens.css": ":root{--ink:#111}\n",
    ".env": "SECRET=1\n",
    ".well-known/x.txt": "hidden\n",
    "paw.recipes.json": "{}",
}

SELLING = {
    **NOBUILD,
    "index.html": '<html><body><button data-paw-checkout="p1">Buy</button></body></html>',
}

SITE_CONFIG = {
    "siteId": "site1",
    "captureApiBase": "https://api.paw.test/api/v1/",
    "captureSignedKey": "site_key_abc",
}

CLIENT = b"/* paw.js */"


@pytest.fixture(autouse=True)
def _preview_base(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")


@pytest.fixture
def gen_dir(monkeypatch, tmp_path):
    """A vendored generator with paw.js, the way the image lays it out."""
    root = tmp_path / "gen"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "cli.js").write_text("// cli: project-build [--site-config <json>]")
    (root / "package.json").write_text("{}")
    (root / "templates" / "_paw").mkdir(parents=True)
    (root / "templates" / "_paw" / "paw.js").write_bytes(CLIENT)
    monkeypatch.setenv(project_build.GEN_DIR_ENV, str(root))
    return root


# ------------------------------------------------------------------ detection


def test_no_build_detection():
    assert project_build.is_no_build(NOBUILD)
    # No template manifest: no package.json plus a root index.html (paw-sites rule).
    assert project_build.is_no_build({"index.html": "<h1>x</h1>", "a.css": ""})
    assert project_build.is_no_build({"./index.html": "<h1>x</h1>"})
    assert not project_build.is_no_build(SOURCE)
    assert not project_build.is_no_build({"a.css": ""})
    # A template with a build block always builds, even without a package.json.
    building = {**NOBUILD, "paw.template.json": json.dumps({**TEMPLATE, "build": {"x": 1}})}
    assert not project_build.is_no_build(building)
    with pytest.raises(project_build.ValidationError) as exc:
        project_build.project_files(building)
    assert exc.value.code == "sites.project_no_package_json"
    # The no-build tree is accepted where package.json used to be required.
    assert "index.html" in project_build.project_files(NOBUILD)


@pytest.mark.parametrize(
    ("rel", "excluded"),
    [
        ("index.html", False),
        ("styles/site.css", False),
        ("about/AGENTS.md", False),
        ("AGENTS.md", True),
        ("paw.template.json", True),
        ("paw-build.json", True),
        ("paw.recipes.json", True),
        ("package.json", True),
        ("bun.lock", True),
        ("wrangler.toml", True),
        (".env", True),
        (".paw/static/index.html", True),
        ("img/.DS_Store", True),
        ("node_modules/x/index.js", True),
    ],
)
def test_static_output_exclusions(rel, excluded):
    assert project_build.excluded_from_static_output(rel) is excluded


def test_bundle_is_the_tree_minus_tooling_and_dotfiles():
    files = project_build.project_files(NOBUILD)
    manifest, bundle, shipped = project_build.no_build_bundle(files)
    assert not shipped
    assert manifest["assetsDir"] == project_build.STATIC_OUT_DIR
    assert manifest["workerModules"] == []
    assert manifest["framework"] == "static" and manifest["slug"] == "static"
    assert set(project_build.preview_files(manifest, bundle)) == {
        "index.html",
        "about/index.html",
        "styles/tokens.css",
    }
    assert project_build.preview_mode(manifest) == "full"


def test_a_tree_without_a_served_index_is_refused():
    files = project_build.project_files({"index.html": "x"})
    files.pop("index.html")
    files["a.css"] = b""
    with pytest.raises(project_build.ValidationError) as exc:
        project_build.no_build_bundle(files)
    assert exc.value.code == "sites.project_no_index"


# --------------------------------------------------------------------- paw.js


def test_paw_js_is_injected_only_with_config_and_usage(gen_dir):
    files = project_build.project_files(SELLING)
    _m, bundle, shipped = project_build.no_build_bundle(files, site_config=SITE_CONFIG)
    assert shipped
    page = bundle[".paw/static/index.html"].decode()
    assert (
        '<script src="/_paw/paw.js" data-api="https://api.paw.test/api/v1" '
        'data-site-id="site1" data-site-key="site_key_abc" defer></script>\n</body>'
    ) in page
    assert "_paw/paw.js" in bundle[".paw/static/about/index.html"].decode()
    assert bundle[".paw/static/_paw/paw.js"] == CLIENT

    # No capture config: nothing changes.
    _m, bundle, shipped = project_build.no_build_bundle(files)
    assert not shipped and ".paw/static/_paw/paw.js" not in bundle
    # Config, but the only mention of the client is in AGENTS.md (docs are not usage).
    files = project_build.project_files(NOBUILD)
    _m, bundle, shipped = project_build.no_build_bundle(files, site_config=SITE_CONFIG)
    assert not shipped
    assert b"_paw" not in bundle[".paw/static/index.html"]


def test_paw_js_needs_the_vendored_client(monkeypatch, tmp_path):
    monkeypatch.setenv(project_build.GEN_DIR_ENV, str(tmp_path / "none"))
    files = project_build.project_files(SELLING)
    _m, bundle, shipped = project_build.no_build_bundle(files, site_config=SITE_CONFIG)
    # A tag pointing at a missing file would only 404: ship nothing instead.
    assert not shipped and b"_paw" not in bundle[".paw/static/index.html"]


def test_injection_mirrors_the_generator():
    cfg = paw_client.client_config({**SITE_CONFIG, "turnstileSiteKey": 'a"b'})
    assert cfg == {
        "api": "https://api.paw.test/api/v1",
        "siteId": "site1",
        "siteKey": "site_key_abc",
        "turnstileSiteKey": 'a"b',
    }
    tagged = paw_client.inject("<body>x</body>", cfg)
    assert 'data-turnstile-sitekey="a&quot;b"' in tagged
    # Idempotent; a bare authored tag gets its config; no </body> appends.
    assert paw_client.inject(tagged, cfg) == tagged
    bare = paw_client.inject('<script src="/_paw/paw.js" defer></script>', cfg)
    assert bare.startswith('<script data-api="https://api.paw.test/api/v1"')
    assert paw_client.inject("<p>x", cfg).endswith(paw_client.client_tag(cfg) + "\n")
    assert paw_client.client_config({"siteId": "s"}) is None
    assert paw_client.uses_client(["Paw.call('x')"])
    assert not paw_client.uses_client(["<button>Buy</button>"])


# ----------------------------------------------------------------- build lane


@pytest.mark.asyncio
async def test_no_build_job_builds_in_process_without_a_sandbox(monkeypatch, tmp_path):
    # No generator on this host either: a no-build site does not need one.
    monkeypatch.setenv(project_build.GEN_DIR_ENV, str(tmp_path / "none"))
    store, records = _Store(), _Records()
    runner = _Runner(_result(artifact=None))
    out = await project_build.run_project_preview_build(
        {}, "pk1", "h1", {"source": NOBUILD}, 600,
        _runner=runner, _store=store, _verify_store=records,
    )  # fmt: skip
    job_id = build_job._preview_job_id("pk1", "h1")
    assert out == {"status": "built", "reason": None, "job_id": job_id, "preview_mode": "full"}
    assert runner.calls == []
    manifest, files = project_build.read_bundle(store.dist[("pk1", "h1.bundle")])
    assert manifest["workerModules"] == []
    assert set(preview_origin.unpack_files(store.dist[("pk1", "h1")])) == {
        "index.html",
        "about/index.html",
        "styles/tokens.css",
    }
    assert ("pk1", "h1") in store.tokens
    record = records.read("pk1", project_build.build_record_key(job_id))
    assert record["status"] == "built" and record["framework"] == "static"
    assert "No build step" in record["log"]


@pytest.mark.asyncio
async def test_no_build_without_an_index_records_a_failure():
    records = _Records()
    source = {"paw.template.json": json.dumps(TEMPLATE), "a.css": ""}
    out = await project_build.run_project_preview_build(
        {}, "pk1", "h2", {"source": source}, 600, _store=_Store(), _verify_store=records,
    )  # fmt: skip
    assert out["status"] == "failed"
    assert out["reason"] == "scaffold_failed:sites.project_no_index"


async def _make_pocket(source: dict) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Bakery",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="project",
        source=dict(source),
        trusted=True,
    )
    assert err is None, err
    return pocket_id


@pytest.mark.asyncio
async def test_draft_artifact_is_built_at_once_and_never_queued(beanie_test_db):
    pocket_id = await _make_pocket(NOBUILD)
    store, records, pool = _Store(), _Records(), _Pool()
    art = await sites_service._project_draft_artifact(
        pocket_id=pocket_id, source=dict(NOBUILD), store=store, _pool=pool, _records=records
    )
    assert art["build_status"] == "none"
    assert art["preview_mode"] == "full"
    assert art["preview_url"].startswith("https://")
    assert pool.calls == []
    # Read again: served from the record, not rebuilt.
    again = await sites_service._project_draft_artifact(
        pocket_id=pocket_id, source=dict(NOBUILD), store=store, _pool=pool, _records=records
    )
    assert again["preview_url"] == art["preview_url"] and pool.calls == []


@pytest.mark.asyncio
async def test_site_config_needs_a_signed_key(beanie_test_db):
    pocket_id = await _make_pocket(NOBUILD)
    assert await sites_service._project_site_config("ws1", pocket_id) is None
    site_id = str(sites_service._live_object_id("ws1", pocket_id))
    await sites_service._ensure_undeployed_site_doc(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        site_id=site_id,
        signed_key="site_key_live",
        site_name="Bakery",
        builder_origin=None,
    )
    config = await sites_service._project_site_config("ws1", pocket_id)
    assert config["siteId"] == site_id
    assert config["captureSignedKey"] == "site_key_live"
    assert config["captureApiBase"] == sites_service._capture_base()


@pytest.mark.asyncio
async def test_no_build_preview_carries_paw_js_with_the_site_config(beanie_test_db, gen_dir):
    pocket_id = await _make_pocket(SELLING)
    store, records = _Store(), _Records()
    await sites_service._project_draft_artifact(
        pocket_id=pocket_id,
        source=dict(SELLING),
        store=store,
        site_config=SITE_CONFIG,
        _pool=_Pool(),
        _records=records,
    )
    content_hash = project_build.project_content_hash(SELLING)
    preview = preview_origin.unpack_files(store.dist[(pocket_id, content_hash)])
    assert b'data-site-key="site_key_abc"' in preview["index.html"]
    assert preview["_paw/paw.js"] == CLIENT


@pytest.mark.asyncio
async def test_a_queued_sandbox_build_never_carries_the_site_key(beanie_test_db):
    """build_job's rule: the per-site key never enters Redis or a sandbox."""
    pocket_id = await _make_pocket(SOURCE)
    pool = _Pool()
    await sites_service._project_draft_artifact(
        pocket_id=pocket_id,
        source=dict(SOURCE),
        store=_Store(),
        site_config=SITE_CONFIG,
        _pool=pool,
        _records=_Records(),
    )
    payload = pool.calls[0]["args"][2]
    assert payload["siteConfig"]["captureSignedKey"] == ""
    assert payload["siteConfig"]["siteId"] == "site1"


# --------------------------------------------------------- sandbox site config


def _gen(*, flag: bool) -> list[tuple[bytes, str]]:
    cli = b"// --site-config" if flag else b"// cli"
    return [
        (b"{}", "/tmp/paw-sites-gen/package.json"),
        (cli, "/tmp/paw-sites-gen/dist/cli.js"),
        (CLIENT, "/tmp/paw-sites-gen/templates/_paw/paw.js"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_sandbox_build_passes_site_config_only_to_a_generator_that_has_it(flag):
    runner = _Runner(_result(artifact=_bundle(STATIC_MANIFEST)))
    out = await project_build.run_project_preview_build(
        {}, "pk1", "h3", {"source": SOURCE, "siteConfig": SITE_CONFIG}, 600,
        _runner=runner, _store=_Store(), _verify_store=_Records(), _gen_uploads=_gen(flag=flag),
    )  # fmt: skip
    assert out["status"] == "built"
    uploads = {p: b for b, p in runner.calls[0]["extra_uploads"]}
    script = uploads[project_build.SANDBOX_BUILD_SCRIPT].decode()
    if flag:
        assert f"--site-config {project_build.SANDBOX_SITE_CONFIG} --json" in script
        assert json.loads(uploads[project_build.SANDBOX_SITE_CONFIG]) == SITE_CONFIG
    else:
        assert "--site-config" not in script
        assert project_build.SANDBOX_SITE_CONFIG not in uploads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generator_input",
    [
        {"source": SOURCE},
        # A scrubbed config (the queued path) has no key, so no client could ship.
        {"source": SOURCE, "siteConfig": {**SITE_CONFIG, "captureSignedKey": ""}},
    ],
)
async def test_sandbox_build_without_a_keyed_config_is_unchanged(generator_input):
    runner = _Runner(_result(artifact=_bundle(STATIC_MANIFEST)))
    await project_build.run_project_preview_build(
        {}, "pk1", "h4", generator_input, 600,
        _runner=runner, _store=_Store(), _verify_store=_Records(), _gen_uploads=_gen(flag=True),
    )  # fmt: skip
    uploads = {p: b for b, p in runner.calls[0]["extra_uploads"]}
    assert b"--site-config" not in uploads[project_build.SANDBOX_BUILD_SCRIPT]
    assert project_build.SANDBOX_SITE_CONFIG not in uploads


def test_generator_uploads_carry_paw_js(gen_dir):
    uploads = project_build.generator_uploads()
    paths = {p for _b, p in uploads}
    assert "/tmp/paw-sites-gen/templates/_paw/paw.js" in paths
    assert project_build.supports_site_config(uploads)
    assert not project_build.supports_site_config(GEN)


def test_publish_applies_paw_js_to_a_sandbox_bundle(gen_dir, tmp_path):
    work = tmp_path / "bundle"
    manifest = project_build.materialize_bundle(_bundle(STATIC_MANIFEST), work)
    source = {**SOURCE, "src/pages/index.astro": "<button data-paw-subscribe>Join</button>"}
    assert project_build.apply_paw_client_to_bundle(work, manifest, source, SITE_CONFIG)
    assert "/_paw/paw.js" in (work / "dist" / "index.html").read_text()
    assert (work / "dist" / "_paw" / "paw.js").read_bytes() == CLIENT
    # A source that never uses the client is left byte-identical.
    other = tmp_path / "other"
    manifest = project_build.materialize_bundle(_bundle(STATIC_MANIFEST), other)
    assert not project_build.apply_paw_client_to_bundle(other, manifest, SOURCE, SITE_CONFIG)
    assert (other / "dist" / "index.html").read_bytes() == b"<h1>Hi</h1>"


# -------------------------------------------------------------------- publish


@pytest.mark.asyncio
async def test_publish_deploys_an_assets_only_worker_without_a_sandbox(
    beanie_test_db, monkeypatch, gen_dir
):
    pocket_id = await _make_pocket(SELLING)
    site_id = str(sites_service._live_object_id("ws1", pocket_id))
    # An empty store: a no-build publish needs no stored draft build.
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: _Store())
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "wfp")
    calls: list[dict] = []

    async def _deploy_bundle(cf, *, script_name, build_dir, **_kw):
        root = Path(build_dir)
        calls.append(
            {
                "manifest": json.loads((root / "paw-build.json").read_text()),
                "index": (root / ".paw/static/index.html").read_text(),
                "client": (root / ".paw/static/_paw/paw.js").read_bytes(),
                "agents": (root / ".paw/static/AGENTS.md").exists(),
            }
        )
        return SimpleNamespace(script_name=script_name, modules=0, assets=3, warnings=[])

    monkeypatch.setattr(bundle_deploy, "deploy_bundle", _deploy_bundle)

    async def _no_sandbox(*a, **k):
        raise AssertionError("a no-build site never builds in a sandbox")

    monkeypatch.setattr(build_job, "enqueue_preview_build", _no_sandbox)
    doc = await sites_service._deploy_site_doc(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        site_id=site_id,
        signed_key="site_key_live",
        site_name="Bakery",
        ripple_spec=None,
        theme={},
        engine="project",
        source=dict(SELLING),
        pattern="landing",
        cloudflare=SimpleNamespace(),
    )
    assert doc.deployed is True
    (call,) = calls
    assert call["manifest"]["workerModules"] == []
    assert call["manifest"]["assetsDir"] == project_build.STATIC_OUT_DIR
    assert f'data-site-id="{site_id}"' in call["index"]
    assert 'data-site-key="site_key_live"' in call["index"]
    assert call["client"] == CLIENT
    assert call["agents"] is False


# --------------------------------------------------------------- gateway token


@pytest.fixture
def _encryption_key(monkeypatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.mark.asyncio
async def test_a_linked_site_always_deploys_with_its_gateway_token(beanie_test_db, _encryption_key):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration
    from pocketpaw_ee.integrations import bindings

    site = SimpleNamespace(workspace="ws1", pocket_id="pk-linked")
    # No link: nothing is minted.
    assert not await bindings.ensure_gateway_token_if_linked("ws1", "pk-linked")
    assert await site_secrets.secrets_for_deploy(site) == {}
    # A link whose token was never minted (an older link, or a failed mint) gets it now.
    await SiteIntegration(workspace="ws1", pocket_id="pk-linked", provider="demo").insert()
    assert await bindings.ensure_gateway_token_if_linked("ws1", "pk-linked")
    token = (await site_secrets.secrets_for_deploy(site))["PAW_GATEWAY_TOKEN"]
    # Idempotent: a second deploy keeps the same token.
    await bindings.ensure_gateway_token_if_linked("ws1", "pk-linked")
    assert (await site_secrets.secrets_for_deploy(site))["PAW_GATEWAY_TOKEN"] == token


@pytest.mark.asyncio
async def test_the_deploy_provisioner_mints_the_token_for_a_linked_site(
    beanie_test_db, _encryption_key, monkeypatch
):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration
    from pocketpaw_ee.sites import binding_provisioner

    async def _ensure(site, requests, **_kw):
        return ProvisionedResources()

    monkeypatch.setattr(binding_provisioner, "ensure_bindings", _ensure)
    await SiteIntegration(workspace="ws1", pocket_id="pk-prov", provider="demo").insert()
    site = SimpleNamespace(workspace="ws1", pocket_id="pk-prov", plan_tier=None)
    provision = sites_service._bundle_provisioner(site, None)
    res = await provision([])
    assert set(res.secrets) == {"PAW_GATEWAY_TOKEN"}


def test_an_assets_only_worker_binds_no_secrets(tmp_path):
    work = tmp_path / "static"
    project_build.materialize_bundle(_bundle(STATIC_MANIFEST), work)
    provisioned = ProvisionedResources(secrets={"PAW_GATEWAY_TOKEN": "t"})
    bundle = bundle_deploy.load_bundle(work, provisioned)
    assert not [b for b in bundle.bindings if b["type"] == "secret_text"]


def test_a_worker_bundle_still_binds_its_secrets(tmp_path):
    manifest = {
        **STATIC_MANIFEST,
        "assetsDir": "dist/client",
        "mainModule": "index.js",
        "workerModules": [".paw/worker/index.js"],
        "workerModuleDir": ".paw/worker",
    }
    work = tmp_path / "worker"
    project_build.materialize_bundle(_bundle(manifest), work)
    provisioned = ProvisionedResources(secrets={"PAW_GATEWAY_TOKEN": "t"})
    bundle = bundle_deploy.load_bundle(work, provisioned)
    assert {"type": "secret_text", "name": "PAW_GATEWAY_TOKEN", "text": "t"} in bundle.bindings

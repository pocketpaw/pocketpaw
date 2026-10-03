# tests/ee/sites/test_ai_ready.py — AI-ready site files (AV-1).
#
# Created 2026-10-03 (feat/ai-ready-sites): the pure builders in
# ``sites/ai_ready.py`` (robots training on/off, search bots never blocked, sitemap
# absolute URLs, llms.txt, _headers syntax, JSON-LD present/absent, key file), the
# IndexNow ping (never raises), and the ``deploy_workers`` wiring per engine: files
# land in the engine's asset dir, are not excluded by ``.assetsignore`` while the
# deploy scaffold still is, an author's robots.txt is kept, and the ping fires only
# after a successful deploy and can never fail a publish.

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import Internal
from pocketpaw_ee.sites import ai_ready, workers_deploy

HOST = "acme.paw.workers.dev"
KEY = "0123456789abcdef0123456789abcdef"

_HOME = (
    "<html><head><title>Acme Bakery</title></head><body><h1>Fresh bread</h1>"
    '<a href="tel:+1 555 0100">Call</a><address>1 Main St, Springfield</address>'
    "</body></html>"
)
_ABOUT = "<html><head><title>About us</title></head><body><h2>Our story</h2></body></html>"


def _inputs(**kw) -> ai_ready.AiReadyInputs:
    base = dict(site_name="Acme Bakery", description="Bread daily.", indexnow_key=KEY)
    base.update(kw)
    return ai_ready.AiReadyInputs(**base)


# ---------------------------------------------------------------- builders


def _groups(robots: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    current = None
    for line in robots.splitlines():
        if line.startswith("User-agent:"):
            current = line.split(":", 1)[1].strip()
            groups[current] = []
        elif current and line.strip():
            groups[current].append(line)
    return groups


def test_robots_blocks_training_bots_by_default() -> None:
    robots = ai_ready.build_robots_txt(HOST, ai_training_allowed=False)
    groups = _groups(robots)
    assert "Content-Signal: search=yes, ai-input=yes, ai-train=no" in groups["*"]
    assert "Allow: /" in groups["*"]
    for bot in ai_ready.TRAINING_BOTS:
        assert groups[bot][0] == "Disallow: /"
    assert robots.rstrip().endswith(f"Sitemap: https://{HOST}/sitemap.xml")


def test_robots_training_allowed_changes_only_training_lines() -> None:
    robots = ai_ready.build_robots_txt(HOST, ai_training_allowed=True)
    assert "ai-train=yes" in robots
    assert "Disallow" not in robots
    assert not any(bot in robots for bot in ai_ready.TRAINING_BOTS)


@pytest.mark.parametrize("allowed", [True, False])
def test_search_and_assistant_bots_are_never_disallowed(allowed: bool) -> None:
    groups = _groups(ai_ready.build_robots_txt(HOST, ai_training_allowed=allowed))
    for bot in ai_ready.SEARCH_BOTS:
        # Not named at all => the ``*`` group (Allow: /) governs it.
        assert bot not in groups or "Disallow: /" not in groups[bot]
    assert "Disallow: /" not in groups["*"]


def test_sitemap_uses_absolute_urls_and_lastmod() -> None:
    xml = ai_ready.build_sitemap_xml(HOST, ["/", "/about"], "2026-10-03")
    locs = re.findall(r"<loc>(.*?)</loc>", xml)
    assert locs == [f"https://{HOST}/", f"https://{HOST}/about"]
    assert xml.count("<lastmod>2026-10-03</lastmod>") == 2
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')


def test_page_routes_and_markdown_paths() -> None:
    assert ai_ready.page_route("index.html") == "/"
    assert ai_ready.page_route("about.html") == "/about"
    assert ai_ready.page_route("docs/index.html") == "/docs/"
    assert ai_ready.page_route("404.html") is None
    assert ai_ready.page_route("_app/x.html") is None
    assert ai_ready.page_route("style.css") is None
    assert ai_ready.markdown_path("/") == "/index.md"
    assert ai_ready.markdown_path("/about") == "/about.md"
    assert ai_ready.markdown_path("/docs/") == "/docs.md"


def test_llms_txt_lists_pages_with_markdown_links() -> None:
    txt = ai_ready.build_llms_txt(
        HOST, "Acme", "Bread daily.", [("/", "Home"), ("/about", "About")]
    )
    assert txt.startswith("# Acme\n\n> Bread daily.\n")
    assert f"- [Home](https://{HOST}/index.md)" in txt
    assert f"- [About](https://{HOST}/about.md)" in txt


def test_headers_block_syntax_and_merge_is_idempotent() -> None:
    block = ai_ready.build_headers_block(["/", "/about"])
    lines = block.splitlines()
    assert lines[1] == "/"
    assert lines[2] == '  Link: </index.md>; rel="alternate"; type="text/markdown"'
    assert lines[3] == "/about"
    adapter = "/_app/immutable/*\n  Cache-Control: public\n"
    once = ai_ready.merge_headers(adapter, block)
    twice = ai_ready.merge_headers(once, block)
    assert once == twice
    assert once.startswith(adapter)


def test_json_ld_present_when_contact_found_and_absent_otherwise() -> None:
    pages = [ai_ready.Page("index.html", _HOME)]
    biz = ai_ready.find_business(pages)
    assert biz == {"telephone": "+1 555 0100", "address": "1 Main St, Springfield"}
    block = ai_ready.build_json_ld(HOST, "Acme", "", biz)
    data = json.loads(re.search(r">(.*)</script>", block).group(1))
    assert data["@type"] == "LocalBusiness"
    assert data["url"] == f"https://{HOST}/"
    assert data["telephone"] == "+1 555 0100"
    assert ai_ready.build_json_ld(HOST, "Acme", "", {}) == ""


def test_json_ld_cannot_break_out_of_script_tag() -> None:
    block = ai_ready.build_json_ld(HOST, "</script><b>x", "", {"telephone": "1"})
    assert block.count("</script>") == 1


def test_json_ld_respects_author_json_ld_and_replaces_our_own() -> None:
    authored = '<html><head><script type="application/ld+json">{}</script></head></html>'
    assert ai_ready.inject_json_ld(authored, "<script>x</script>") is None
    block = ai_ready.build_json_ld(HOST, "A", "", {"telephone": "1"})
    once = ai_ready.inject_json_ld(_ABOUT, block)
    assert once is not None and once.count("application/ld+json") == 1
    again = ai_ready.inject_json_ld(once, block)
    assert again is not None and again.count("application/ld+json") == 1


def test_build_files_full_set() -> None:
    pages = [ai_ready.Page("index.html", _HOME), ai_ready.Page("about.html", _ABOUT)]
    files = ai_ready.build_ai_ready_files(HOST, pages, _inputs(), "2026-10-03")
    assert {"robots.txt", "sitemap.xml", "llms.txt", "index.md", "about.md", f"{KEY}.txt"} <= set(
        files
    )
    assert files[f"{KEY}.txt"] == KEY.encode()
    assert "Fresh bread" in files["index.md"].decode()
    assert "Our story" in files["about.md"].decode()
    assert b"application/ld+json" in files["index.html"]
    assert b"[About us]" in files["llms.txt"]


def test_bad_key_is_not_written() -> None:
    files = ai_ready.build_ai_ready_files(
        HOST, [ai_ready.Page("index.html", _ABOUT)], _inputs(indexnow_key="bad key!"), "x"
    )
    assert not any(k.endswith(".txt") and k not in ("robots.txt", "llms.txt") for k in files)


def test_enabled_flag(monkeypatch) -> None:
    monkeypatch.delenv("POCKETPAW_SITES_AI_READY", raising=False)
    assert ai_ready.enabled()
    monkeypatch.setenv("POCKETPAW_SITES_AI_READY", "off")
    assert not ai_ready.enabled()


async def test_ping_never_raises(monkeypatch) -> None:
    import httpx

    class _Boom:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            raise httpx.ConnectError("down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    assert await ai_ready.ping_indexnow(HOST, KEY, [f"https://{HOST}/"]) is False
    assert await ai_ready.ping_indexnow(HOST, "", [f"https://{HOST}/"]) is False


async def test_ping_posts_indexnow_payload(monkeypatch) -> None:
    import httpx

    sent: dict = {}

    class _Resp:
        status_code = 202

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            sent["url"], sent["json"] = url, json
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await ai_ready.ping_indexnow(HOST, KEY, [f"https://{HOST}/"]) is True
    assert sent["url"] == ai_ready.INDEXNOW_ENDPOINT
    assert sent["json"] == {
        "host": HOST,
        "key": KEY,
        "keyLocation": f"https://{HOST}/{KEY}.txt",
        "urlList": [f"https://{HOST}/"],
    }


# ---------------------------------------------------------------- deploy wiring


class _Proc:
    def __init__(self, rc: int) -> None:
        self.returncode = rc

    async def communicate(self):
        return (f"https://x.{HOST}\n".encode(), b"boom")


def _project(tmp_path: Path, engine: str) -> Path:
    """A built project in the engine's real output shape; returns the asset dir."""
    if engine == "html":
        out = tmp_path
    elif engine == "react":
        out = tmp_path / "dist"
    else:  # ripple / svelte — adapter-cloudflare output with its own _headers
        out = tmp_path / ".svelte-kit" / "cloudflare"
    out.mkdir(parents=True, exist_ok=True)
    if engine in ("ripple", "svelte"):
        (out / "_worker.js").write_text("export default {}")
        (out / "_headers").write_text("/_app/immutable/*\n  Cache-Control: immutable\n")
    (out / "index.html").write_text(_HOME)
    (out / "about.html").write_text(_ABOUT)
    return out


def _patch(monkeypatch, rc: int = 0) -> list:
    pings: list = []

    async def _exec(*argv, **kw):
        return _Proc(rc)

    async def _ping(host, key, urls):
        pings.append((host, key, urls))
        return True

    monkeypatch.setattr(workers_deploy.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(workers_deploy.ai_ready_mod, "ping_indexnow", _ping)
    monkeypatch.delenv("POCKETPAW_SITES_AI_READY", raising=False)
    monkeypatch.setenv("PAW_CF_WORKERS_SUBDOMAIN", "paw")
    return pings


def _ignored(out: Path) -> set[str]:
    return set((out / ".assetsignore").read_text().split())


@pytest.mark.parametrize("engine", ["html", "react", "ripple", "svelte"])
async def test_deploy_writes_ai_ready_files_into_asset_dir(tmp_path, monkeypatch, engine) -> None:
    pings = _patch(monkeypatch)
    out = _project(tmp_path, engine)
    await workers_deploy.deploy_workers(
        "s1", str(tmp_path), engine=engine, analytics_entitled=False, ai_ready=_inputs()
    )
    for name in ("robots.txt", "sitemap.xml", "llms.txt", "index.md", "about.md", f"{KEY}.txt"):
        assert (out / name).is_file(), name
        assert name not in _ignored(out)
    host = "paw-site-s1.paw.workers.dev"
    assert f"https://{host}/about" in (out / "sitemap.xml").read_text()
    headers = (out / "_headers").read_text()
    assert 'Link: </about.md>; rel="alternate"; type="text/markdown"' in headers
    if engine in ("ripple", "svelte"):
        assert "Cache-Control: immutable" in headers  # adapter rules kept
    if engine == "html":
        assert {"wrangler.jsonc", ".assetsignore"} <= _ignored(out)  # scaffold still hidden
    assert "application/ld+json" in (out / "index.html").read_text()
    assert pings == [(host, KEY, [f"https://{host}/", f"https://{host}/about"])]


async def test_deploy_keeps_author_robots_and_republish_is_stable(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch)
    out = _project(tmp_path, "html")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /private\n")
    for _ in range(2):
        await workers_deploy.deploy_workers(
            "s1", str(tmp_path), engine="html", analytics_entitled=False, ai_ready=_inputs()
        )
    assert (out / "robots.txt").read_text() == "User-agent: *\nDisallow: /private\n"
    assert (out / "_headers").read_text().count("START POCKETPAW AI-READY") == 1
    assert (out / "index.html").read_text().count("application/ld+json") == 1


async def test_our_robots_is_rewritten_when_training_flag_flips(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch)
    out = _project(tmp_path, "html")
    for allowed in (False, True):
        await workers_deploy.deploy_workers(
            "s1",
            str(tmp_path),
            engine="html",
            analytics_entitled=False,
            ai_ready=_inputs(ai_training_allowed=allowed),
        )
    assert "ai-train=yes" in (out / "robots.txt").read_text()


async def test_no_ping_after_failed_deploy(tmp_path, monkeypatch) -> None:
    pings = _patch(monkeypatch, rc=1)
    _project(tmp_path, "html")
    with pytest.raises(Internal):
        await workers_deploy.deploy_workers(
            "s1", str(tmp_path), engine="html", analytics_entitled=False, ai_ready=_inputs()
        )
    assert pings == []


async def test_ping_failure_never_fails_publish(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch)
    import httpx

    def _raise(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(workers_deploy.ai_ready_mod, "ping_indexnow", ai_ready.ping_indexnow)
    monkeypatch.setattr(httpx, "AsyncClient", _raise)
    _project(tmp_path, "html")
    url = await workers_deploy.deploy_workers(
        "s1", str(tmp_path), engine="html", analytics_entitled=False, ai_ready=_inputs()
    )
    assert url


async def test_disabled_flag_and_no_inputs_write_nothing(tmp_path, monkeypatch) -> None:
    pings = _patch(monkeypatch)
    out = _project(tmp_path, "html")
    await workers_deploy.deploy_workers("s1", str(tmp_path), engine="html")
    monkeypatch.setenv("POCKETPAW_SITES_AI_READY", "0")
    await workers_deploy.deploy_workers("s1", str(tmp_path), engine="html", ai_ready=_inputs())
    assert not (out / "robots.txt").exists()
    assert not (out / "_headers").exists()
    assert pings == []


async def test_no_host_skips_files(tmp_path, monkeypatch) -> None:
    pings = _patch(monkeypatch)
    monkeypatch.delenv("PAW_CF_WORKERS_SUBDOMAIN")
    out = _project(tmp_path, "html")
    await workers_deploy.deploy_workers("s1", str(tmp_path), engine="html", ai_ready=_inputs())
    assert not (out / "sitemap.xml").exists()
    assert pings == []


async def test_explicit_host_wins(tmp_path, monkeypatch) -> None:
    pings = _patch(monkeypatch)
    out = _project(tmp_path, "html")
    await workers_deploy.deploy_workers(
        "s1", str(tmp_path), engine="html", ai_ready=_inputs(host="www.acme.com")
    )
    assert "https://www.acme.com/about" in (out / "sitemap.xml").read_text()
    assert pings[0][0] == "www.acme.com"


# ---------------------------------------------------------------- publish seam + endpoint


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


async def _publish_recording(seen: list, pocket_id: str = "pk-ai"):
    from pocketpaw_ee.sites import service as sites_service

    async def _deploy(site_id, project_dir, *, worker_name=None, ai_ready=None, **_):
        seen.append(ai_ready)
        return f"https://{worker_name}.acct.workers.dev"

    return await sites_service.publish(
        workspace_id="ws-ai",
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="Acme Bakery",
        _generator=_FakeGenerator(),
        _bundle_reader=lambda d: b"",
        _workers_deploy=_deploy,
    )


async def test_publish_mints_one_indexnow_key_and_reads_the_flag(beanie_test_db, monkeypatch):
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.dto import SiteAiVisibilityUpdate

    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    seen: list = []

    site = await _publish_recording(seen)
    first = seen[0]
    assert first.ai_training_allowed is False
    assert re.fullmatch(r"[0-9a-f]{32}", first.indexnow_key)
    assert site.indexnow_key == first.indexnow_key

    resp = await sites_service.update_site_ai_visibility(
        workspace_id="ws-ai",
        site_id=str(site.id),
        body=SiteAiVisibilityUpdate(ai_training_allowed=True),
    )
    assert resp.ai_training_allowed is True

    await _publish_recording(seen)
    second = seen[1]
    assert second.indexnow_key == first.indexnow_key  # minted once, reused
    assert second.ai_training_allowed is True
    # the row's own workers.dev host is reused for absolute URLs on a republish
    assert second.host == f"{site.worker_name}.acct.workers.dev"


async def test_ai_visibility_is_tenant_scoped(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.dto import SiteAiVisibilityUpdate

    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    site = await _publish_recording([], pocket_id="pk-ai-2")
    with pytest.raises(NotFound):
        await sites_service.update_site_ai_visibility(
            workspace_id="someone-else",
            site_id=str(site.id),
            body=SiteAiVisibilityUpdate(ai_training_allowed=True),
        )

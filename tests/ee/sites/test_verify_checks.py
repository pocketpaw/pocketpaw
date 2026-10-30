# tests/ee/sites/test_verify_checks.py — the warn-only page checks verify_site attaches
# as ``checks`` (pocketpaw_ee.sites.verify_checks, verify._with_checks).
#
# Each sites-check id has a fixture page that trips it and one that is clean, plus the
# false positives the checks must not raise (hash routes, anchors wired as buttons,
# bracketed citations, the page title, aria-hidden art, server paths, generator-owned
# react pages). The verdict tests pin the contract that matters most: warnings ride the
# verdict, capped with an omitted count and merged across pages, NEVER change its
# status, and are computed off the event loop. The served tree is the html source, the
# svelte / react draft files (``read_dist`` under the content hash) or a project's
# bundle for its current files. Mutations: tests/mutations/sites_verify_checks.json.
from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import preview_origin, project_build, verify, verify_checks, verify_store

from .test_verify_pipeline import Check, MemoryVerifyStore, Pool, Waiter, report

_HEAD = (
    "<head><title>Bright Smile</title>"
    '<meta name="description" content="A dental studio in Leeds.">'
    '<meta property="og:title" content="Bright Smile">'
    '<meta property="og:description" content="A dental studio in Leeds.">'
    '<link rel="icon" href="/favicon.svg"></head>'
)


def _doc(body: str, head: str = _HEAD) -> str:
    if "<h1" not in body:
        body = "<h1>Bright Smile</h1>" + body
    return f"<!doctype html><html>{head}<body>{body}</body></html>"


def _ids(result: dict[str, Any], page: str | None = None) -> list[str]:
    return [w["id"] for w in result["warnings"] if page is None or w["page"] == page]


def _run(body: str, *, extra: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    files = {"index.html": _doc(body), "favicon.svg": "<svg/>", **(extra or {})}
    return verify_checks.run_checks(files, **kw)


class TestLinks:
    def test_anchor_without_target_is_link_01(self) -> None:
        out = _run('<a href="#pricing">Pricing</a><section id="about"></section>')
        assert _ids(out) == ["SC-LINK-01"]
        assert "#pricing" in out["warnings"][0]["detail"]
        assert "Pricing" in out["warnings"][0]["detail"]

    def test_anchor_with_target_is_clean(self) -> None:
        out = _run('<a href="#pricing">Pricing</a><a href="#top">Top</a><h2 id="pricing">P</h2>')
        assert _ids(out) == []

    @pytest.mark.parametrize("href", ["#/pricing", "#!/about", "/#/team"])
    def test_hash_routes_are_not_anchors(self, href: str) -> None:
        assert _ids(_run(f'<a href="{href}">Route</a>')) == []

    def test_cross_page_anchor_checks_the_other_page(self) -> None:
        about = _doc('<h2 id="team">Team</h2>')
        bad = _run('<a href="/about.html#history">History</a>', extra={"about.html": about})
        good = _run('<a href="about.html#team">Team</a>', extra={"about.html": about})
        assert _ids(bad, "index.html") == ["SC-LINK-01"]
        assert _ids(good) == []

    @pytest.mark.parametrize("href", ["#", "", "javascript:void(0)"])
    def test_empty_action_is_link_02(self, href: str) -> None:
        out = _run(f'<a href="{href}">Book now</a>')
        assert _ids(out) == ["SC-LINK-02"]
        assert "Book now" in out["warnings"][0]["detail"]

    @pytest.mark.parametrize(
        "attrs", ['role="button"', 'data-bs-toggle="collapse"', 'data-toggle="menu"']
    )
    def test_anchor_wired_as_a_button_is_not_link_02(self, attrs: str) -> None:
        assert _ids(_run(f'<a href="#" {attrs}>Menu</a>')) == []

    def test_anchor_with_a_handler_is_not_link_02(self) -> None:
        out = _run('<a href="#" onclick="openMenu()">Menu</a>')
        assert "SC-LINK-02" not in _ids(out)

    def test_real_links_are_clean(self) -> None:
        out = _run(
            '<a href="https://example.org">x</a><a href="mailto:hi@bright.test">m</a>'
            '<a href="tel:+441130000000">t</a>'
        )
        assert _ids(out) == []

    def test_missing_page_and_asset_are_link_03(self) -> None:
        out = _run('<a href="/services">Services</a><img src="img/hero.webp" alt="Chair">')
        assert _ids(out) == ["SC-LINK-03"]
        detail = out["warnings"][0]["detail"]
        assert "/services" in detail and "img/hero.webp" in detail

    def test_one_page_html_with_handlers_still_checks_page_links(self) -> None:
        body = '<button onclick="toggle()">Menu</button><a href="/about">About</a>'
        assert _ids(_run(body + "<script>function toggle(){}</script>")) == ["SC-LINK-03"]
        assert _ids(_run(body)) == ["SC-LINK-03"]

    @pytest.mark.parametrize("engine", ["react", "svelte", "project"])
    def test_one_page_client_app_skips_page_links(self, engine: str) -> None:
        body = '<a href="/about">About</a><script type="module" src="/app.js"></script>'
        out = _run(body, extra={"app.js": ""}, engine=engine)
        assert "SC-LINK-03" not in _ids(out)

    def test_paths_that_resolve_are_clean(self) -> None:
        out = _run(
            '<a href="/services">S</a><a href="team/">T</a><a href="/">Home</a>'
            '<img src="/img/hero.webp" alt="Chair"><link rel="stylesheet" href="styles.css">'
            '<script src="/_paw/paw.js"></script>'
            '<form method="POST" action="__CAPTURE_API_BASE__/capture/form"></form>',
            extra={
                "services.html": _doc("s"),
                "team/index.html": _doc("t"),
                "img/hero.webp": b"x",
                "styles.css": "",
            },
        )
        assert _ids(out) == []

    def test_server_routes_skip_page_links_and_server_paths_but_not_assets(self) -> None:
        out = _run(
            '<a href="/dashboard">D</a><img src="/missing.png" alt="m">'
            '<script src="/_next/static/chunk.js"></script><img src="/_image?url=x" alt="i">'
            '<link rel="preload" href="/api/data">',
            server_routes=True,
        )
        assert _ids(out) == ["SC-LINK-03"]
        detail = out["warnings"][0]["detail"]
        assert "/missing.png" in detail
        for skipped in ("/dashboard", "_next", "_image", "/api/"):
            assert skipped not in detail

    def test_plain_form_with_no_action_is_link_04(self) -> None:
        out = _run('<form id="contact"><input name="email"><button>Send</button></form>')
        assert _ids(out) == ["SC-LINK-04"]
        assert "contact" in out["warnings"][0]["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            '<form action="/thanks.html"><button>Send</button></form>',
            '<form onsubmit="send(event)"><button>Send</button></form>',
            '<form><button>Send</button></form><script type="module">bind()</script>',
            '<form><button onclick="send()">Send</button></form>',
            '<form method="dialog"><button>Close</button></form>',
        ],
    )
    def test_forms_with_a_target_or_a_script_are_not_judged(self, body: str) -> None:
        extra = {"thanks.html": _doc("ok")}
        assert "SC-LINK-04" not in _ids(_run(body, extra=extra))


class TestCopy:
    def test_em_dash_in_visible_text_is_copy_01(self) -> None:
        out = _run("<p>Teeth — done right</p><img src='/favicon.svg' alt='a — b'>")
        assert _ids(out) == ["SC-COPY-01"]
        assert out["warnings"][0]["detail"].startswith("2 em dashes")

    def test_entity_em_dash_counts(self) -> None:
        assert _ids(_run("<p>Teeth &mdash; done</p>")) == ["SC-COPY-01"]

    def test_em_dash_outside_visible_text_is_clean(self) -> None:
        out = _run(
            "<script>const a = '—'</script>"
            '<script type=\'application/ld+json\'>{"a": "—"}</script>'
            "<style>p::after{content:'—'}</style>"
            "<pre>a — b</pre><code>—</code>"
            '<div data-note="—"></div><p>Open 9–5</p>'
        )
        assert _ids(out) == []

    def test_title_and_aria_hidden_are_not_copy(self) -> None:
        head = _HEAD.replace("<title>Bright Smile</title>", "<title>Home — Bright</title>")
        files = {
            "index.html": _doc(
                '<div aria-hidden="true"><span>—</span><div>—</div></div>'
                '<img aria-hidden="true" alt="—" src="/favicon.svg"><p>calm</p>',
                head=head,
            ),
            "favicon.svg": "",
        }
        assert _ids(verify_checks.run_checks(files)) == []

    def test_text_after_an_aria_hidden_block_still_counts(self) -> None:
        out = _run('<div aria-hidden="true"><div>x</div></div><p>after — it</p>')
        assert _ids(out) == ["SC-COPY-01"]


class TestHonest:
    @pytest.mark.parametrize(
        "text",
        [
            "[Phone]",
            "[Price]",
            "[Client name]",
            "[Role]",
            "[NEED: proof]",
            "Your Studio",
            "Lorem ipsum dolor",
            "hello@example.com",
        ],
    )
    def test_placeholder_is_honest_07(self, text: str) -> None:
        out = _run(f"<p>Call {text}</p>")
        assert _ids(out) == ["SC-HONEST-07"]

    @pytest.mark.parametrize("text", ["[sic]", "[Smith 2020]", "[1]", "your studio visit"])
    def test_bracketed_words_are_not_placeholders(self, text: str) -> None:
        assert _ids(_run(f"<p>Quote {text}</p>")) == []

    def test_input_placeholder_attribute_is_clean(self) -> None:
        out = _run('<form action="/t.html"><input placeholder="[Phone]"></form>')
        assert "SC-HONEST-07" not in _ids(out)

    @pytest.mark.parametrize(
        "body",
        [
            "<blockquote>Great care. Sarah Chen</blockquote>",
            "<p>Elena Vasquez, practice manager</p>",
            "<p>Call 555-0123</p>",
            "<p>Call (555) 234-5678</p>",
        ],
    )
    def test_default_people_are_honest_03(self, body: str) -> None:
        assert _ids(_run(body)) == ["SC-HONEST-03"]

    def test_johndoe_is_honest_03_only(self) -> None:
        assert _ids(_run('<a href="mailto:johndoe@example.com">Email</a>')) == ["SC-HONEST-03"]

    @pytest.mark.parametrize("text", ["Dr Priya Raman, 0113 496 0000", "Trusted by Acme Dental"])
    def test_real_names_are_clean(self, text: str) -> None:
        assert _ids(_run(f"<p>{text}</p>")) == []


class TestBasics:
    def test_a_complete_head_is_clean(self) -> None:
        assert _ids(_run("<p>Hi</p>")) == []

    def test_missing_basics_are_build_12(self) -> None:
        out = verify_checks.run_checks({"index.html": _doc("<h1>Hi</h1>", head="<head></head>")})
        assert _ids(out) == ["SC-BUILD-12"]
        detail = out["warnings"][0]["detail"]
        for part in ("no <title>", "no meta description", "og:title", "og:description"):
            assert part in detail
        assert "no favicon" in detail

    def test_vite_favicon_and_generator_are_build_12(self) -> None:
        head = _HEAD.replace("/favicon.svg", "/vite.svg").replace(
            "</head>", '<meta name="generator" content="Astro v5"></head>'
        )
        out = verify_checks.run_checks({"index.html": _doc("<p>x</p>", head=head), "vite.svg": ""})
        assert _ids(out) == ["SC-BUILD-12"]
        detail = out["warnings"][0]["detail"]
        assert "vite.svg" in detail and "Astro" in detail

    def test_root_favicon_file_counts(self) -> None:
        head = _HEAD.replace('<link rel="icon" href="/favicon.svg">', "")
        out = verify_checks.run_checks({"index.html": _doc("hi", head=head), "favicon.ico": b"x"})
        assert _ids(out) == []

    def test_react_index_is_the_generators_so_basics_are_skipped(self) -> None:
        files = {"index.html": _doc("<p>x</p>", head="<head></head>")}
        assert "SC-BUILD-12" not in _ids(verify_checks.run_checks(files, engine="react"))
        assert "SC-BUILD-12" in _ids(verify_checks.run_checks(files, engine="svelte"))

    @pytest.mark.parametrize("body", ["<p>no heading</p><h2>x</h2>", "<h1>a</h1><h1>b</h1>"])
    def test_not_exactly_one_h1_is_a11y_06(self, body: str) -> None:
        page = f"<!doctype html><html>{_HEAD}<body>{body}</body></html>"
        out = verify_checks.run_checks({"index.html": page, "favicon.svg": ""})
        assert _ids(out) == ["SC-A11Y-06"]


class TestReport:
    def test_capped_with_an_omitted_count_and_strong_ids_first(self) -> None:
        pages = {f"p{i}.html": _doc(f"<p>a{i} — b</p>") for i in range(40)}
        pages["index.html"] = _doc('<a href="#nope">x</a>', head="<head></head>")
        pages["favicon.svg"] = "<svg/>"
        out = verify_checks.run_checks(pages)
        assert len(out["warnings"]) == verify_checks.MAX_WARNINGS
        assert out["omitted"] == 42 - verify_checks.MAX_WARNINGS
        assert out["warnings"][0]["id"] == "SC-LINK-01"
        assert set(out["warnings"][0]) == {"id", "page", "detail"}

    def test_one_shared_defect_is_one_warning_with_a_page_count(self) -> None:
        footer = '<footer><a href="#">Privacy</a></footer>'
        pages = {f"p{i}.html": _doc(footer) for i in range(3)}
        out = verify_checks.run_checks({**pages, "favicon.svg": ""})
        assert out["warnings"] == [
            {
                "id": "SC-LINK-02",
                "page": "p0.html",
                "detail": 'link goes nowhere: "Privacy -> #"',
                "pages": 3,
            }
        ]

    def test_a_tree_with_no_pages_is_skipped(self) -> None:
        assert verify_checks.run_checks({"app.js": "x"}) == verify_checks.skipped("no_pages")

    def test_pages_past_the_byte_budget_are_counted(self, monkeypatch) -> None:
        pages = {f"p{i}.html": _doc("<p>" + "x" * 300 + "</p>") for i in range(3)}
        monkeypatch.setattr(verify_checks, "MAX_TOTAL_BYTES", len(pages["p0.html"]) + 10)
        out = verify_checks.run_checks({**pages, "favicon.svg": ""})
        assert out["pages"] == 1
        assert out["pages_skipped"] == 2
        assert "2 pages not checked" in out["note"]


# ── on the verdict ────────────────────────────────────────────────────────────

_SLOPPY = _doc(
    '<h1>Smiles — fast</h1><a href="#">Book</a><a href="#pricing">Pricing</a>'
    "<p>Sarah Chen says hi. Call [Phone].</p>",
    head="<head></head>",
)


class Artifacts:
    def __init__(self, dist: dict[tuple[str, str], bytes] | None = None) -> None:
        self.dist = dist or {}
        self.reads: list[tuple[str, str]] = []

    def read_dist(self, pocket_id: str, key: str) -> bytes | None:
        self.reads.append((pocket_id, key))
        return self.dist.get((pocket_id, key))


async def _pocket(engine: str, source: dict[str, Any]) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine=engine,
        source=source,
        trusted=True,
    )
    assert err is None, err
    return pocket_id


async def _verify(pocket_id: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("_store", MemoryVerifyStore())
    kw.setdefault("_pool", Pool())
    kw.setdefault("_check", Check())
    kw.setdefault("_wait", Waiter())
    kw.setdefault("_artifacts", Artifacts())
    return await verify.verify_site(workspace_id="ws1", user_id="u1", pocket_id=pocket_id, **kw)


class TestVerdict:
    async def test_warnings_never_flip_a_passed_verdict(self, beanie_test_db) -> None:
        verdict = await _verify(await _pocket("html", {"index.html": _SLOPPY}))
        assert verdict["status"] == "passed"
        assert verdict["errors"] == []
        assert set(_ids(verdict["checks"])) == {
            "SC-LINK-01",
            "SC-LINK-02",
            "SC-HONEST-03",
            "SC-HONEST-07",
            "SC-COPY-01",
            "SC-BUILD-12",
        }

    async def test_warnings_never_flip_a_failed_verdict(self, beanie_test_db) -> None:
        err = {"layer": "browser", "code": "pageerror", "file": "index.html", "message": "x"}
        verdict = await _verify(
            await _pocket("html", {"index.html": _doc("<h1>Clean</h1>")}),
            _wait=Waiter(report("passed", "failed", errors=[err])),
        )
        assert verdict["status"] == "failed"
        assert verdict["checks"]["warnings"] == [
            {"id": "SC-LINK-03", "page": "index.html", "detail": 'not in the build: "/favicon.svg"'}
        ]

    async def test_a_crashing_check_is_skipped_and_the_status_holds(
        self, beanie_test_db, monkeypatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise RuntimeError("parser exploded")

        monkeypatch.setattr(verify_checks, "run_checks", boom)
        verdict = await _verify(await _pocket("html", {"index.html": _SLOPPY}))
        assert verdict["status"] == "passed"
        assert verdict["checks"]["skipped"] == "check_crashed"

    async def test_checks_run_off_the_event_loop(self, beanie_test_db, monkeypatch) -> None:
        threads: list[int] = []
        real = verify._page_checks

        def recording(*args: Any) -> dict[str, Any]:
            threads.append(threading.get_ident())
            return real(*args)

        monkeypatch.setattr(verify, "_page_checks", recording)
        verdict = await _verify(await _pocket("html", {"index.html": _SLOPPY}))
        assert threads and threads[0] != threading.get_ident()
        assert verdict["checks"]["pages"] == 1

    async def test_svelte_reads_the_draft_files_under_its_hash(self, beanie_test_db) -> None:
        from .test_verify_pipeline import _SVELTE

        pocket_id = await _pocket("svelte", dict(_SVELTE))
        store = MemoryVerifyStore()
        first = await _verify(pocket_id, _store=store)
        assert first["checks"]["skipped"] == "no_built_output"
        dist = preview_origin.pack_files({"index.html": _SLOPPY.encode()})
        artifacts = Artifacts({(pocket_id, first["content_hash"]): dist})
        verdict = await _verify(pocket_id, _store=store, _artifacts=artifacts, force=True)
        assert verdict["status"] == "passed"
        assert "SC-LINK-02" in _ids(verdict["checks"])

    async def test_a_cached_verdict_keeps_its_checks(self, beanie_test_db) -> None:
        store = MemoryVerifyStore()
        pocket_id = await _pocket("html", {"index.html": _SLOPPY})
        first = await _verify(pocket_id, _store=store)
        again = await _verify(pocket_id, _store=store)
        assert again["cached"] is True
        assert again["checks"] == first["checks"]

    async def test_an_old_cached_verdict_is_upgraded_once(self, beanie_test_db) -> None:
        store = MemoryVerifyStore()
        pocket_id = await _pocket("html", {"index.html": _SLOPPY})
        first = await _verify(pocket_id, _store=store)
        key = verify_store.verdict_key(first["content_hash"])
        record = store.read(pocket_id, key)
        record["verdict"].pop("checks")
        store.write(pocket_id, key, record)
        again = await _verify(pocket_id, _store=store)
        assert again["cached"] is True and "SC-LINK-02" in _ids(again["checks"])
        assert "checks" in store.read(pocket_id, key)["verdict"]


class TestProject:
    async def test_project_build_bundle_is_checked_and_stays_unverified(self) -> None:
        source = {"package.json": json.dumps({"name": "x"}), "src/main.ts": "x"}
        content_hash = project_build.project_content_hash(source)
        bundle = preview_origin.pack_files(
            {
                "paw-build.json": json.dumps({"assetsDir": "dist"}).encode(),
                "dist/index.html": _SLOPPY.encode(),
            }
        )
        artifacts = Artifacts({("p1", project_build.bundle_key(content_hash)): bundle})
        verdict = await verify.verify_pocket(
            {"engine": "project", "source": source}, pocket_id="p1", _artifacts=artifacts
        )
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "engine_not_verifiable"
        assert verdict["note"] == verify.PROJECT_CHECKS_NOTE
        assert "SC-LINK-02" in _ids(verdict["checks"])
        assert verdict["checks"]["warnings"][0]["page"] == "index.html"

    async def test_a_worker_bundle_with_no_pages_is_skipped(self) -> None:
        source = {"package.json": "{}", "src/main.ts": "x"}
        content_hash = project_build.project_content_hash(source)
        manifest = {"assetsDir": "dist", "workerEntry": "_worker.js"}
        bundle = preview_origin.pack_files(
            {"paw-build.json": json.dumps(manifest).encode(), "dist/app.js": b"x"}
        )
        artifacts = Artifacts({("p1", project_build.bundle_key(content_hash)): bundle})
        verdict = await verify.verify_pocket(
            {"engine": "project", "source": source}, pocket_id="p1", _artifacts=artifacts
        )
        assert verdict["checks"]["skipped"] == "no_pages"

    async def test_unbuilt_project_says_so(self) -> None:
        source = {"package.json": "{}", "src/main.ts": "x"}
        verdict = await verify.verify_pocket(
            {"engine": "project", "source": source}, pocket_id="p1", _artifacts=Artifacts()
        )
        assert verdict["checks"]["skipped"] == "no_built_output"

    async def test_no_build_project_checks_its_own_files(self) -> None:
        source = {
            "paw.template.json": json.dumps({"build": "none"}),
            "index.html": _SLOPPY,
            "AGENTS.md": "[Phone] in the notes is not served",
        }
        verdict = await verify.verify_pocket(
            {"engine": "project", "source": source}, pocket_id="p1", _artifacts=Artifacts()
        )
        assert "SC-HONEST-07" in _ids(verdict["checks"])
        assert {w["page"] for w in verdict["checks"]["warnings"]} == {"index.html"}

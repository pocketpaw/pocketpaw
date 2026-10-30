# tests/ee/sites/test_preview_deploy_engine_aware.py
# Created: 2026-08-02 (fix/site-rendering-ripple-and-draft) — reproduce-first cover
# for "a site renders BROKEN while it is in DRAFT state".
#
# WHY THIS EXISTS. HE-4 (c4275e46, "make the local deploy engine-aware") taught the
# LIVE deploy that where a built site's servable files live depends on the engine:
# ``.svelte-kit/cloudflare`` for ripple/svelte, the project root for html
# (``engines.static_output_rel``). It only fixed ``_deploy_site_doc``. The DRAFT
# half of the same function — ``publish(preview=True)``, the arm-for-edit path
# behind ``POST /sites/by-pocket/{id}/editable`` — still calls
# ``local_server.deploy_local(preview_id, project_dir)`` with NO ``engine=``, so it
# falls back to the ``"ripple"`` default and looks for a SvelteKit build dir an html
# site never emits. The draft then either 500s (``MissingBuildOutput``) or, worse,
# takes ``deploy_local``'s fail-soft branch and silently re-serves the PREVIOUS
# deploy — the draft shows stale content and nobody is told.
#
# These tests assert on the SERVED (deployed) draft — the bytes the builder iframe
# actually loads — not on the build output, so they pin the whole preview→serve
# seam rather than an internal detail. The build is faked behind the
# ``GeneratorClient._runner`` seam (no bun / node / workerd spawns).

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.sites import local_server

pytestmark = pytest.mark.asyncio

# A minimal, self-contained page: no local asset refs, so the html static smoke
# (_html_static_smoke) passes without the test having to materialize a whole tree.
_HTML_DRAFT = (
    "<!doctype html>\n"
    "<html lang='en'><head><meta charset='utf-8'><title>Draft</title></head>\n"
    "<body><h1>draft html page</h1></body></html>\n"
)

_CLOUDFLARE_BUILD_REL = ".svelte-kit/cloudflare"


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a recording EventBus so the pockets service's ``emit`` calls don't
    raise (the real bus is only wired by ``init_realtime()`` at boot)."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


class _HtmlRunner:
    """Fake runner for the html lane: ``generate`` writes the raw static tree at the
    project ROOT (what the real generator's ``materializeHtml`` emits) and nothing
    else ever runs — html has ``needs_node_build == False``, so install /
    build_static / smoke are never reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        root = Path(out_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "index.html").write_text(_HTML_DRAFT, encoding="utf-8")
        return {"projectDir": out_dir, "engine": "html", "staticDir": out_dir}

    def install_inputs_hash(self, project_dir: str) -> str:
        return "h1"

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        return True, "ok"

    async def build_static(self, project_dir: str, *, gate: bool) -> tuple[bool, str]:
        self.calls.append("build_static")
        return True, "ok"


async def test_html_draft_preview_serves_the_built_page(beanie_test_db, tmp_path, monkeypatch):
    """An html site armed for editing must SERVE its draft.

    Reproduce-first: on the pre-fix code the preview deploy drops the ``engine``
    kwarg, so ``deploy_local`` resolves ``static_output_rel("ripple")`` —
    ``.svelte-kit/cloudflare`` — which an html build never emits. With no prior
    deploy to fall back on that raises ``MissingBuildOutput`` and the draft never
    renders. After the fix the preview resolves the html static root (the project
    dir) exactly like the LIVE deploy does, and the served file is the draft page.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path / "builds"))
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_CF_DEPLOY_MODE", raising=False)

    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Brochure",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="html",
        source={"index.html": _HTML_DRAFT},
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None

    gen = sites_service.GeneratorClient(_runner=_HtmlRunner())

    site = await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        preview=True,
        builder_origin="http://localhost:8888",
        _generator=gen,
    )

    served = local_server.sites_home() / f"preview-{pocket_id}" / "index.html"
    assert served.is_file(), (
        f"no served draft at {served} — the preview deploy resolved the wrong "
        "static root for engine='html' (it used the ripple default)"
    )
    assert "draft html page" in served.read_text()
    assert site.url.endswith(f"/preview-{pocket_id}/")
    assert site.deployed is False  # a preview is never a live deploy


async def test_html_draft_preview_does_not_silently_serve_a_stale_deploy(
    beanie_test_db, tmp_path, monkeypatch
):
    """The nastier half of the same bug: SILENT staleness.

    ``deploy_local`` fails SOFT — when the static root it was pointed at is
    missing but a PRIOR deploy of the same id is on disk, it keeps that prior
    deploy and returns its URL. With the wrong (ripple) root for an html site that
    turns every draft re-arm into "serve whatever was there last": the operator
    edits, the preview reloads, and the OLD page comes back with no error anywhere.
    Seeding a stale prior deploy makes the pre-fix code return it; the fix must
    overwrite it with the fresh draft.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path / "builds"))
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_CF_DEPLOY_MODE", raising=False)

    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Brochure",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="html",
        source={"index.html": _HTML_DRAFT},
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None

    # A stale prior deploy sitting at the stable preview path.
    stale_dir = local_server.sites_home() / f"preview-{pocket_id}"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "index.html").write_text("<h1>stale previous draft</h1>", encoding="utf-8")

    gen = sites_service.GeneratorClient(_runner=_HtmlRunner())
    await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        preview=True,
        builder_origin="http://localhost:8888",
        _generator=gen,
    )

    served = (stale_dir / "index.html").read_text()
    assert "draft html page" in served, (
        "the draft preview silently re-served the PRIOR deploy — deploy_local's "
        "fail-soft branch fired because the preview looked for a SvelteKit build "
        "dir an html site never emits"
    )
    assert "stale previous draft" not in served

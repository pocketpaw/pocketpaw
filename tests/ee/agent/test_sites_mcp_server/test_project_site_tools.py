# tests/ee/agent/test_sites_mcp_server/test_project_site_tools.py — the project-site
# agent tools (``agent/mcp_servers/sites_project.py``) on the sites_manager server.
#
# Registration (ids on SITES_TOOL_IDS, the built server lists all twelve), then the
# handlers end to end against a mongomock pocket: start_site_from_template persists a
# correct project pocket from the (fake) CLI's template copy; the file tools refuse
# other engines and unsafe paths, and every write lands in the source map and queues
# a build; apply_site_recipe writes the CLI's files back, records the recipe, returns
# secret names, writes NOTHING on a conflict, and refuses below the recipe's plan;
# run_build / get_build_log read A1's build records. The CLI is the fake in
# tests/ee/sites/project_cli_fake.py; the sandbox queue is stubbed.
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from tests.ee.sites.project_cli_fake import TEMPLATE_FILES, FakeCli  # noqa: E402

TOOL_NAMES = {
    "list_site_templates",
    "start_site_from_template",
    "list_site_recipes",
    "apply_site_recipe",
    "list_files",
    "read_file",
    "read_files",
    "write_files",
    "patch_file",
    "delete_files",
    "run_build",
    "get_build_log",
}


@pytest.fixture(autouse=True)
def _default_sites_plan():
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


@pytest.fixture()
def cli(monkeypatch) -> FakeCli:
    from pocketpaw_ee.sites import project_tools

    fake = FakeCli()
    monkeypatch.setattr(project_tools, "_create_subprocess_exec", fake)
    monkeypatch.setattr(project_tools, "cli_argv", lambda: ["paw-sites-gen"])
    project_tools.reset_caches()
    yield fake
    project_tools.reset_caches()


class BuildQueue:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.answer: dict[str, Any] = {
            "build_status": "queued",
            "build_job_id": "site-preview-p-abc",
            "preview_url": None,
            "preview_mode": None,
        }

    async def __call__(self, *, workspace_id: str, user_id: str, pocket_id: str) -> dict:
        self.calls.append(pocket_id)
        return dict(self.answer)


@pytest.fixture()
def builds(monkeypatch) -> BuildQueue:
    from pocketpaw_ee.sites import service as sites_service

    queue = BuildQueue()
    monkeypatch.setattr(sites_service, "queue_project_build", queue)
    return queue


@pytest.fixture()
def recording_bus():
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _Bus:
        async def publish(self, event) -> None:  # noqa: ANN001
            return

        def subscribe(self, event_type, handler) -> None:  # noqa: ANN001, ARG002
            return

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _Bus()  # type: ignore[attr-defined]
    yield
    bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.fixture()
def identity():
    from bson import ObjectId

    ws, user = str(ObjectId()), str(ObjectId())
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value=ws),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value=user),
    ):
        yield ws, user


def _body(out: dict) -> dict:
    return json.loads(out["content"][0]["text"])


async def _call(name: str, args: dict) -> dict:
    from pocketpaw_ee.agent.mcp_servers import sites_project

    return await getattr(sites_project, f"_{name}_handler")(args)


async def _start(cli: FakeCli, **extra) -> str:
    out = await _call("start_site_from_template", {"slug": "astro", "brief": "A blog.", **extra})
    assert not out.get("is_error"), out
    return _body(out)["pocket_id"]


async def _doc(pocket_id: str):
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.pocket import Pocket

    return await Pocket.get(ObjectId(pocket_id))


async def _make_pocket(identity, engine: str, source: dict) -> str:
    from pocketpaw_ee.cloud.pockets.service import agent_create

    ws, user = identity
    _view, pocket_id, err = await agent_create(
        workspace_id=ws,
        owner_id=user,
        name="x",
        type_="site",
        pattern="landing",
        engine=engine,
        source=source,
        trusted=True,
    )
    assert err is None
    return pocket_id


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_every_id_rides_the_sites_allow_list(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.sites_project import SITES_PROJECT_TOOL_IDS

        assert len(SITES_PROJECT_TOOL_IDS) == 12
        assert {t.rsplit("__", 1)[1] for t in SITES_PROJECT_TOOL_IDS} == TOOL_NAMES
        for tid in SITES_PROJECT_TOOL_IDS:
            assert tid.startswith("mcp__pocketpaw_sites_manager__")
            assert tid in SITES_TOOL_IDS

    def test_the_built_server_lists_every_tool(self) -> None:
        import asyncio

        from mcp import types
        from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

        built = build_sites_manager_server()
        if built is None:
            pytest.skip("claude_agent_sdk not installed")
        handler = built[1]["instance"].request_handlers[types.ListToolsRequest]
        tools = asyncio.run(handler(types.ListToolsRequest(method="tools/list"))).root.tools
        names = {t.name for t in tools}
        assert TOOL_NAMES <= names
        patch_schema = next(t for t in tools if t.name == "patch_file").inputSchema
        assert patch_schema["properties"]["edits"]["items"]["required"] == ["old", "new"]
        recipe = next(t for t in tools if t.name == "apply_site_recipe")
        assert "request_site_secret" in (recipe.description or "")

    def test_the_sites_surface_allows_them(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites_project import SITES_PROJECT_TOOL_IDS
        from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

        create = resolve_profile(SurfaceKind.SITES, SurfaceMeta(engine="project"))
        assert set(SITES_PROJECT_TOOL_IDS) <= set(create.allow_mcp_tool_ids or ())
        assert create.ripple_mode == "off"
        assert "pocketpaw-create-project-site" in (create.skill_names or set())
        refine = resolve_profile(
            SurfaceKind.SITES, SurfaceMeta(pocket_id="pkt_1", engine="project")
        )
        assert refine.ripple_mode == "off"
        assert set(SITES_PROJECT_TOOL_IDS) <= set(refine.allow_mcp_tool_ids or ())


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TestTemplates:
    async def test_list_site_templates(self, cli, identity) -> None:
        body = _body(await _call("list_site_templates", {}))
        assert [t["slug"] for t in body["templates"]] == [
            "astro",
            "tanstack-start",
            "vite-react-hono",
            "next",
            "sveltekit",
        ]
        assert body["templates"][0]["when_to_use"]

    async def test_start_creates_a_project_pocket(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        out = await _call(
            "start_site_from_template", {"slug": "astro", "brief": "A blog.", "name": "Notes"}
        )
        assert not out.get("is_error"), out
        body = _body(out)
        assert body["pocket"]["engine"] == "project"
        assert body["status"] == "draft" and body["is_live"] is False
        assert body["verification"]["status"] == "pending"
        assert body["verification"]["job_id"] == "site-preview-p-abc"
        # AGENTS.md rides as its own verbatim text block.
        assert (
            out["content"][1]["text"] == "=== FILE: AGENTS.md ===\n" + TEMPLATE_FILES["AGENTS.md"]
        )

        doc = await _doc(body["pocket_id"])
        assert doc.engine == "project"
        assert doc.type == "site" and doc.pattern == "landing"
        assert doc.rippleSpec is None
        assert doc.source == TEMPLATE_FILES
        assert doc.name == "Notes" and doc.description == "A blog."
        assert doc.site_meta == {
            "project": {"template": "astro", "framework": "astro", "recipes": []}
        }
        assert builds.calls == [body["pocket_id"]]

    async def test_start_refuses_an_unknown_template(self, cli, identity) -> None:
        out = await _call("start_site_from_template", {"slug": "rails", "brief": "x"})
        assert out["is_error"] and "sites.unknown_template" in out["content"][0]["text"]

    async def test_start_needs_a_brief(self, cli, identity) -> None:
        out = await _call("start_site_from_template", {"slug": "astro"})
        assert out["is_error"] and "brief" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class TestFileTools:
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("list_files", {}),
            ("read_file", {"path": "index.html"}),
            ("read_files", {"paths": ["index.html"]}),
            ("write_files", {"files": {"index.html": "<p>x</p>"}}),
            ("patch_file", {"path": "index.html", "edits": [{"old": "a", "new": "b"}]}),
            ("delete_files", {"paths": ["index.html"]}),
            ("run_build", {}),
            ("get_build_log", {}),
            ("apply_site_recipe", {"recipe_id": "d1-drizzle"}),
        ],
    )
    async def test_other_engines_are_refused(
        self, tool, args, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _make_pocket(identity, "html", {"index.html": "<p>a</p>"})
        out = await _call(tool, {"pocket_id": pocket_id, **args})
        assert out["is_error"]
        assert "project sites only" in out["content"][0]["text"]
        doc = await _doc(pocket_id)
        assert doc.source == {"index.html": "<p>a</p>"}

    async def test_list_and_read(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        listed = _body(await _call("list_files", {"pocket_id": pocket_id, "prefix": "src/"}))
        assert [f["path"] for f in listed["files"]] == ["src/pages/index.astro"]
        out = await _call(
            "read_files", {"pocket_id": pocket_id, "paths": ["package.json", "./AGENTS.md"]}
        )
        assert [f["path"] for f in _body(out)["files"]] == ["package.json", "AGENTS.md"]
        assert out["content"][2]["text"].endswith(TEMPLATE_FILES["AGENTS.md"])
        missing = await _call("read_file", {"pocket_id": pocket_id, "path": "nope.ts"})
        assert missing["is_error"] and "nope.ts" in missing["content"][0]["text"]

    async def test_read_truncates_a_huge_file(
        self, cli, identity, builds, beanie_test_db, recording_bus, monkeypatch
    ) -> None:
        from pocketpaw_ee.sites import project_tools

        monkeypatch.setattr(project_tools, "MAX_READ_FILE_BYTES", 10)
        pocket_id = await _start(cli)
        out = await _call("read_file", {"pocket_id": pocket_id, "path": "package.json"})
        assert _body(out)["files"][0]["truncated"] is True
        assert len(out["content"][1]["text"].split("\n", 1)[1]) == 10

    async def test_write_patch_delete_round_trip(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        out = await _call(
            "write_files",
            {"pocket_id": pocket_id, "files": {"src/pages/about.astro": "<h1>About</h1>\n"}},
        )
        body = _body(out)
        assert body["created"] == ["src/pages/about.astro"]
        assert body["verification"]["status"] == "pending"

        out = await _call(
            "patch_file",
            {
                "pocket_id": pocket_id,
                "path": "src/pages/about.astro",
                "edits": [{"old": "About", "new": "About us"}],
            },
        )
        assert not out.get("is_error"), out
        out = await _call(
            "delete_files", {"pocket_id": pocket_id, "paths": ["src/pages/index.astro"]}
        )
        assert _body(out)["deleted"] == ["src/pages/index.astro"]

        doc = await _doc(pocket_id)
        assert doc.source["src/pages/about.astro"] == "<h1>About us</h1>\n"
        assert "src/pages/index.astro" not in doc.source
        # Every write queued the draft build of the new source.
        assert len(builds.calls) == 4

    async def test_patch_needs_exactly_one_match_and_saves_nothing_otherwise(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        out = await _call(
            "patch_file",
            {
                "pocket_id": pocket_id,
                "path": "src/pages/index.astro",
                "edits": [{"old": "---", "new": "--"}],
            },
        )
        assert out["is_error"]
        assert (await _doc(pocket_id)).source == TEMPLATE_FILES

    @pytest.mark.parametrize(
        "path", ["../x.ts", "/abs.ts", "C:/x.ts", ".env", "node_modules/a.js", "paw-build.json"]
    )
    async def test_write_refuses_unsafe_paths(
        self, path, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        out = await _call(
            "write_files", {"pocket_id": pocket_id, "files": {"ok.ts": "1", path: "x"}}
        )
        assert out["is_error"] and "sites.project_bad_path" in out["content"][0]["text"]
        assert (await _doc(pocket_id)).source == TEMPLATE_FILES

    async def test_write_size_cap(
        self, cli, identity, builds, beanie_test_db, recording_bus, monkeypatch
    ) -> None:
        from pocketpaw_ee.sites import project_tools

        monkeypatch.setattr(project_tools, "MAX_WRITE_FILE_BYTES", 5)
        pocket_id = await _start(cli)
        out = await _call("write_files", {"pocket_id": pocket_id, "files": {"a.ts": "123456"}})
        assert out["is_error"] and "sites.project_file_too_large" in out["content"][0]["text"]

    async def test_a_dependency_write_drops_the_lockfile(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        pkg = json.loads(TEMPLATE_FILES["package.json"])
        pkg["dependencies"]["zod"] = "4.1.0"
        body = _body(
            await _call(
                "write_files", {"pocket_id": pocket_id, "files": {"package.json": json.dumps(pkg)}}
            )
        )
        assert body["lockfile_removed"] == ["bun.lock"]
        assert "bun.lock" not in (await _doc(pocket_id)).source

    async def test_package_json_cannot_be_deleted(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        out = await _call("delete_files", {"pocket_id": pocket_id, "paths": ["package.json"]})
        assert out["is_error"]
        missing = await _call("delete_files", {"pocket_id": pocket_id, "paths": ["gone.ts"]})
        assert missing["is_error"]
        assert (await _doc(pocket_id)).source == TEMPLATE_FILES


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


class TestRecipes:
    async def test_list_filters_by_template(self, cli, identity) -> None:
        body = _body(await _call("list_site_recipes", {"template": "astro"}))
        assert {r["id"] for r in body["recipes"]} == {"d1-drizzle", "better-auth", "r2"}
        assert _body(await _call("list_site_recipes", {"template": "rails"}))["recipes"] == []

    async def test_apply_writes_back_records_and_returns_secret_names(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        out = await _call("apply_site_recipe", {"pocket_id": pocket_id, "recipe_id": "better-auth"})
        assert not out.get("is_error"), out
        body = _body(out)
        assert body["secret_names"] == ["BETTER_AUTH_SECRET"]
        assert body["glue_tasks"] == [{"task": "Wire getUser.", "file": "src/server/auth/index.ts"}]
        assert "request_site_secret" in body["message"]
        assert "never write" in body["message"].lower()
        assert body["lockfile_removed"] == ["bun.lock"]
        assert body["verification"]["status"] == "pending"
        # The template is passed so the CLI checks applies_to.
        call = next(c for c in cli.calls if "apply-recipe" in c)
        assert call[call.index("--template") + 1] == "astro"

        doc = await _doc(pocket_id)
        assert doc.source["src/server/auth/better-auth.ts"] == "export const auth = 1;\n"
        assert "better-auth" in json.loads(doc.source["package.json"])["dependencies"]
        assert "bun.lock" not in doc.source
        assert doc.site_meta["project"]["recipes"] == ["better-auth"]
        assert doc.site_meta["project"]["template"] == "astro"
        # No secret value anywhere in the source.
        assert not any("BETTER_AUTH_SECRET=" in v for v in doc.source.values())

    async def test_a_conflict_writes_nothing(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        cli.mode = "conflict"
        out = await _call("apply_site_recipe", {"pocket_id": pocket_id, "recipe_id": "better-auth"})
        assert out["is_error"]
        body = _body(out)
        assert body["status"] == "conflict" and body["written"] == []
        assert body["conflicts"][0]["path"] == "src/server/auth/index.ts"
        doc = await _doc(pocket_id)
        assert doc.source == TEMPLATE_FILES
        assert doc.site_meta["project"]["recipes"] == []
        assert builds.calls == [pocket_id]  # only the create's build

    async def test_errors_write_nothing(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        cli.mode = "error"
        out = await _call("apply_site_recipe", {"pocket_id": pocket_id, "recipe_id": "better-auth"})
        assert out["is_error"] and _body(out)["errors"]
        assert (await _doc(pocket_id)).source == TEMPLATE_FILES

    async def test_dry_run_writes_nothing(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        out = await _call(
            "apply_site_recipe",
            {"pocket_id": pocket_id, "recipe_id": "better-auth", "dry_run": True},
        )
        body = _body(out)
        assert body["status"] == "dry_run" and body["would_write"]
        assert (await _doc(pocket_id)).source == TEMPLATE_FILES

    async def test_plan_gate_refuses_before_running(
        self, cli, identity, builds, beanie_test_db, recording_bus, monkeypatch
    ) -> None:
        from pocketpaw_ee.sites import service as sites_service

        pocket_id = await _start(cli)
        plan = AsyncMock(return_value=(None, None))
        monkeypatch.setattr(sites_service, "project_site_plan", plan)
        out = await _call("apply_site_recipe", {"pocket_id": pocket_id, "recipe_id": "r2"})
        assert out["is_error"]
        assert "sites.recipe_plan_required" in out["content"][0]["text"]
        assert not any("apply-recipe" in c for c in cli.calls)

        plan.return_value = ("site", "active")
        out = await _call("apply_site_recipe", {"pocket_id": pocket_id, "recipe_id": "r2"})
        assert not out.get("is_error"), out


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


class TestBuilds:
    async def test_run_build_waits_for_a_failure_and_returns_the_log(
        self, cli, identity, builds, beanie_test_db, recording_bus, monkeypatch
    ) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_project
        from pocketpaw_ee.sites import project_build

        pocket_id = await _start(cli)
        records = iter(
            [
                {"status": "building"},
                {"status": "failed", "reason": "build_failed:build_failed", "log": "x" * 50},
            ]
        )
        monkeypatch.setattr(sites_project, "RUN_BUILD_POLL_SEC", 0)
        monkeypatch.setattr(project_build, "read_build_record", lambda *_a: next(records))
        out = await _call("run_build", {"pocket_id": pocket_id})
        assert out["is_error"]
        body = _body(out)
        assert body["status"] == "failed" and body["reason"] == "build_failed:build_failed"
        assert out["content"][1]["text"].endswith("x" * 50)

    async def test_run_build_reports_static_preview_mode(
        self, cli, identity, builds, beanie_test_db, recording_bus
    ) -> None:
        pocket_id = await _start(cli)
        builds.answer = {
            "build_status": "none",
            "build_job_id": "site-preview-p-abc",
            "preview_url": "https://preview.example/x/",
            "preview_mode": "static",
        }
        body = _body(await _call("run_build", {"pocket_id": pocket_id}))
        assert body["status"] == "built"
        assert body["preview_url"] == "https://preview.example/x/"
        assert "after publish" in body["message"]

    async def test_run_build_still_building_says_so(
        self, cli, identity, builds, beanie_test_db, recording_bus, monkeypatch
    ) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_project
        from pocketpaw_ee.sites import project_build

        pocket_id = await _start(cli)
        monkeypatch.setattr(sites_project, "RUN_BUILD_WAIT_SEC", 0)
        monkeypatch.setattr(project_build, "read_build_record", lambda *_a: {"status": "building"})
        body = _body(await _call("run_build", {"pocket_id": pocket_id}))
        assert body["status"] == "building" and body["preview_url"] is None
        assert "do not report the site as built" in body["message"]

    async def test_get_build_log_reads_the_latest(
        self, cli, identity, builds, beanie_test_db, recording_bus, monkeypatch
    ) -> None:
        from pocketpaw_ee.sites import service as sites_service

        pocket_id = await _start(cli)
        monkeypatch.setattr(
            sites_service,
            "project_latest_build",
            AsyncMock(return_value={"job_id": "site-preview-p-1", "current": True}),
        )
        log = AsyncMock(
            return_value={
                "status": "failed",
                "reason": "build_failed:install_failed",
                "log": "error: [REDACTED]\n",
                "log_truncated": False,
                "preview_mode": None,
                "updated_at": "now",
            }
        )
        monkeypatch.setattr(sites_service, "project_build_log", log)
        out = await _call("get_build_log", {"pocket_id": pocket_id})
        body = _body(out)
        assert body["job_id"] == "site-preview-p-1" and body["current"] is True
        assert log.await_args.kwargs["job_id"] == "site-preview-p-1"
        assert out["content"][1]["text"].endswith("error: [REDACTED]\n")


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


def test_the_project_skill_ships_lean_and_routes_by_template() -> None:
    from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

    path = bundled_skills_plugin_dir() / "skills" / "pocketpaw-create-project-site" / "SKILL.md"
    text = path.read_text("utf-8")
    assert path.stat().st_size <= 6_000
    assert text.startswith("---\nname: pocketpaw-create-project-site\n")
    for slug in ("astro", "tanstack-start", "vite-react-hono", "next", "sveltekit"):
        assert f"`{slug}`" in text
    for tool in (
        "start_site_from_template",
        "apply_site_recipe",
        "request_site_secret",
        "patch_file",
        "run_build",
        "get_build_log",
    ):
        assert tool in text
    assert "preview_mode" in text and "static" in text


# ---------------------------------------------------------------------------
# The /sites preambles name the project tools
# ---------------------------------------------------------------------------


def test_create_and_refine_preambles_route_project_sites() -> None:
    from pocketpaw_ee.cloud.surface import SurfaceMeta
    from pocketpaw_ee.cloud.surface.handlers import sites as handler

    assert handler._preamble_engine("project", default="html") == "project"
    create = handler._create_preamble(SurfaceMeta(engine="project"))
    assert 'engine="project"' in create
    assert "pocketpaw-create-project-site" in create
    assert "start_site_from_template" in create and "run_build" in create
    assert "request_site_secret" in create
    assert "create_html_site" not in create

    refine = handler._refine_preamble(SurfaceMeta(pocket_id="pk1"), engine="project")
    assert "patch_file" in refine and "`pk1`" in refine
    assert "mcp__pocketpaw_ask__ask_user" in refine
    assert "edit_html_file" not in refine

    # The html default names the project route for full-stack asks.
    html = handler._create_preamble(SurfaceMeta())
    assert "pocketpaw-create-project-site" in html

# tests/ee/sites/test_project_tools.py — the host-side half of the project-site agent
# tools (``sites/project_tools.py``): the path policy, size caps, lockfile rule, recipe
# plan gate, and the paw-sites CLI calls (``starters`` / ``recipes`` / ``template-copy``
# / ``apply-recipe``) driven through a fake CLI that acts on the real temp dirs.
from __future__ import annotations

import json

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import ValidationError  # noqa: E402
from pocketpaw_ee.sites import project_tools  # noqa: E402

from tests.ee.sites.project_cli_fake import TEMPLATE_FILES, FakeCli  # noqa: E402


@pytest.fixture()
def cli(monkeypatch) -> FakeCli:
    fake = FakeCli()
    monkeypatch.setattr(project_tools, "_create_subprocess_exec", fake)
    monkeypatch.setattr(project_tools, "cli_argv", lambda: ["paw-sites-gen"])
    project_tools.reset_caches()
    yield fake
    project_tools.reset_caches()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/pages/index.astro", "src/pages/index.astro"),
        ("./src/app.ts", "src/app.ts"),
        ("src//lib/x.ts", "src/lib/x.ts"),
        ("bun.lock", "bun.lock"),
        (".dev.vars.example", ".dev.vars.example"),
        (".env.example", ".env.example"),
        ("migrations/0001_init.sql", "migrations/0001_init.sql"),
    ],
)
def test_normalize_path_accepts_project_paths(raw: str, expected: str) -> None:
    assert project_tools.normalize_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "/etc/passwd",
        "../outside.ts",
        "src/../../x",
        "C:/Windows/x",
        "c:x",
        "src\\app.ts",
        "src/a\x00b",
        ".",
        "node_modules/react/index.js",
        "src/node_modules/x.js",
        ".git/config",
        ".paw/out/paw-build.json",
        "paw-build.json",
        ".dev.vars",
        ".env",
        ".env.local",
        "apps/web/.env.production",
        ".dev.vars.staging",
        "a" * 600,
        123,
        None,
    ],
)
def test_normalize_path_refuses_unsafe_and_reserved(raw) -> None:
    with pytest.raises(ValidationError) as exc:
        project_tools.normalize_path(raw)
    assert exc.value.code == "sites.project_bad_path"


def test_secret_file_refusal_points_at_request_site_secret() -> None:
    with pytest.raises(ValidationError) as exc:
        project_tools.normalize_path(".dev.vars")
    assert "request_site_secret" in exc.value.message


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


def test_write_caps_per_file_per_call_and_count(monkeypatch) -> None:
    monkeypatch.setattr(project_tools, "MAX_WRITE_FILE_BYTES", 10)
    monkeypatch.setattr(project_tools, "MAX_WRITE_CALL_BYTES", 15)
    monkeypatch.setattr(project_tools, "MAX_PATHS_PER_CALL", 3)
    project_tools.check_write_sizes({"a": "x" * 10})
    with pytest.raises(ValidationError, match="at most 10"):
        project_tools.check_write_sizes({"a": "x" * 11})
    with pytest.raises(ValidationError) as exc:
        project_tools.check_write_sizes({"a": "x" * 8, "b": "x" * 8})
    assert exc.value.code == "sites.project_call_too_large"
    with pytest.raises(ValidationError) as exc:
        project_tools.check_write_sizes({"a": "", "b": "", "c": "", "d": ""})
    assert exc.value.code == "sites.project_too_many_files"


def test_project_total_cap(monkeypatch) -> None:
    from pocketpaw_ee.sites import project_build

    monkeypatch.setattr(project_build, "MAX_SOURCE_FILES", 2)
    project_tools.check_project_totals({"a": "1", "b": "2"})
    with pytest.raises(ValidationError) as exc:
        project_tools.check_project_totals({"a": "1", "b": "2", "c": "3"})
    assert exc.value.code == "sites.project_too_large"


# ---------------------------------------------------------------------------
# Lockfiles
# ---------------------------------------------------------------------------


def _pkg(deps: dict[str, str], **extra) -> str:
    return json.dumps({"name": "app", "dependencies": deps, **extra})


def test_a_dependency_change_drops_the_lockfile() -> None:
    source = {"package.json": _pkg({"astro": "7"}), "bun.lock": "{}"}
    writes = {"package.json": _pkg({"astro": "7", "zod": "4"})}
    assert project_tools.stale_lockfiles(source, writes) == ["bun.lock"]


def test_a_script_change_keeps_the_lockfile() -> None:
    source = {"package.json": _pkg({"astro": "7"}), "bun.lock": "{}"}
    writes = {"package.json": _pkg({"astro": "7"}, scripts={"dev": "astro dev"})}
    assert project_tools.stale_lockfiles(source, writes) == []


def test_a_lockfile_written_in_the_same_call_is_kept() -> None:
    source = {"package.json": _pkg({"astro": "7"}), "bun.lock": "{}"}
    writes = {"package.json": _pkg({"zod": "4"}), "bun.lock": "{ }"}
    assert project_tools.stale_lockfiles(source, writes) == []


# ---------------------------------------------------------------------------
# Plan gate
# ---------------------------------------------------------------------------


def test_recipe_plan_gate(monkeypatch) -> None:
    from pocketpaw_ee.cloud.entitlements import service as entitlements

    allowed = project_tools.recipe_plan_allowed
    assert allowed("free", plan_tier=None, subscription_status=None)
    assert not allowed("site", plan_tier=None, subscription_status=None)
    assert not allowed("site", plan_tier="site", subscription_status="cancelled")
    assert allowed("site", plan_tier="site", subscription_status="active")
    assert not allowed("staff", plan_tier="site", subscription_status="active")
    assert allowed("staff", plan_tier="staff", subscription_status="active")
    assert not allowed("enterprise", plan_tier="staff", subscription_status="active")

    calls = []

    def spy(**kw):
        calls.append(kw)
        return True

    # The paid rungs go through the provisioner's own predicate.
    monkeypatch.setattr(entitlements, "site_paid_backends_entitled", spy)
    assert allowed("site", plan_tier="x", subscription_status="y")
    assert calls == [{"plan_tier": "x", "subscription_status": "y"}]


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


async def test_list_templates_reads_starters_and_caches(cli: FakeCli) -> None:
    first = await project_tools.list_templates()
    second = await project_tools.list_templates()
    assert [t["slug"] for t in first][:2] == ["astro", "tanstack-start"]
    assert set(first[0]) == {
        "slug",
        "name",
        "summary",
        "when_to_use",
        "stack",
        "target",
        "recipes",
    }
    assert first is second
    assert sum("starters" in c for c in cli.calls) == 1
    assert "--json" in cli.calls[0]


async def test_list_recipes_carries_plan_and_secret_names(cli: FakeCli) -> None:
    recipes = {r["id"]: r for r in await project_tools.list_recipes()}
    assert recipes["r2"]["plan"] == "site"
    assert recipes["better-auth"]["requires"] == ["d1-drizzle"]
    assert recipes["better-auth"]["secrets"][0]["name"] == "BETTER_AUTH_SECRET"
    assert recipes["better-auth"]["env"] == ["BETTER_AUTH_URL"]


async def test_a_cli_without_json_is_a_clear_error(monkeypatch) -> None:
    from tests.ee.sites.project_cli_fake import _Proc

    async def broken(*_a, **_k):
        return _Proc(None, 1)

    monkeypatch.setattr(project_tools, "_create_subprocess_exec", broken)
    monkeypatch.setattr(project_tools, "cli_argv", lambda: ["paw-sites-gen"])
    project_tools.reset_caches()
    with pytest.raises(project_tools.ProjectCliError) as exc:
        await project_tools.list_templates()
    assert exc.value.code == "catalogue_unavailable"


async def test_a_missing_generator_is_a_clear_error(monkeypatch) -> None:
    async def missing(*_a, **_k):
        raise FileNotFoundError("paw-sites-gen")

    monkeypatch.setattr(project_tools, "_create_subprocess_exec", missing)
    monkeypatch.setattr(project_tools, "cli_argv", lambda: ["paw-sites-gen"])
    project_tools.reset_caches()
    with pytest.raises(project_tools.ProjectCliError) as exc:
        await project_tools.list_recipes()
    assert exc.value.code == "generator_missing"


async def test_copy_template_returns_the_tree_as_a_source_map(cli: FakeCli) -> None:
    source = await project_tools.copy_template("astro")
    assert source == TEMPLATE_FILES
    copy = next(c for c in cli.calls if "template-copy" in c)
    assert copy[copy.index("template-copy") + 1] == "astro"
    assert "--out" in copy and "--json" in copy


async def test_copy_template_refuses_an_unknown_slug(cli: FakeCli) -> None:
    with pytest.raises(ValidationError) as exc:
        await project_tools.copy_template("rails")
    assert exc.value.code == "sites.unknown_template"
    assert not any("template-copy" in c for c in cli.calls)


async def test_copy_template_refuses_binary_files(cli: FakeCli) -> None:
    cli.template_files["public/logo.png"] = b"\x89PNG\r\n\x1a\n\xff\xfe"  # type: ignore[assignment]
    with pytest.raises(ValidationError) as exc:
        await project_tools.copy_template("astro")
    assert exc.value.code == "sites.template_binary_files"
    assert "public/logo.png" in exc.value.message


async def test_apply_recipe_materializes_and_reads_back_the_written_files(
    cli: FakeCli,
) -> None:
    result, writes = await project_tools.apply_recipe(
        dict(TEMPLATE_FILES), "better-auth", template="astro"
    )
    assert result["ok"] is True
    # The CLI saw the real project tree.
    assert cli.seen_project["src/pages/index.astro"] == TEMPLATE_FILES["src/pages/index.astro"]
    call = next(c for c in cli.calls if "apply-recipe" in c)
    assert call[call.index("--template") + 1] == "astro"
    assert set(writes) == {
        "AGENTS.md",
        "package.json",
        "paw.recipes.json",
        "src/server/auth/better-auth.ts",
    }
    assert "better-auth" in json.loads(writes["package.json"])["dependencies"]


async def test_apply_recipe_conflict_and_dry_run_return_no_writes(cli: FakeCli) -> None:
    cli.mode = "conflict"
    result, writes = await project_tools.apply_recipe(dict(TEMPLATE_FILES), "better-auth")
    assert result["conflicts"] and writes == {}
    cli.mode = "ok"
    result, writes = await project_tools.apply_recipe(
        dict(TEMPLATE_FILES), "better-auth", dry_run=True
    )
    assert result["written"] and writes == {}
    assert "--dry-run" in cli.calls[-1]

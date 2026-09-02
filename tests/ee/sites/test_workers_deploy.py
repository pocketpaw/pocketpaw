# tests/ee/sites/test_workers_deploy.py — unit tests for the "workers" deploy mode
# (workers_deploy.deploy_workers): deploy a STATIC Paw Site as a regular Worker on
# the free workers.dev tier. The wrangler subprocess is MOCKED (no network / no
# real CF account), mirroring the sites tests' convention of faking the subprocess
# so the orchestration is unit-testable. The LIVE deploy (with a real token) is run
# by the captain, not here.
#
# Coverage:
#   * deploy_workers writes a correct ``.assetsignore`` (exact 3 lines) +
#     ``wrangler.jsonc`` (name/main/assets/workers_dev/compatibility_flags) into the
#     built project dir;
#   * name sanitization: an ObjectId → ``paw-site-<id>`` (lowercase, no underscores);
#     a non-conforming id is sanitized to a valid worker name;
#   * URL parsing from a fake wrangler stdout containing a workers.dev line, and the
#     fallback construction from PAW_CF_WORKERS_SUBDOMAIN when the parse fails;
#   * a non-zero wrangler exit raises Internal with the stderr tail;
#   * a missing static-build dir raises a clean ValidationError before any subprocess.
#
# Created: 2026-06-25 (feat/sites-workers-deploy-mode).
#
# Updated 2026-09-02 (SA-1 — the visitor counter): the two assets-only tests asserting
# ``"main" not in cfg`` were REPLACED, not removed. The assets-only config now names
# the generated pageview counter as ``main``, so the old assertion is false — but the
# bug it guarded (a ``main`` pointing at a ``_worker.js`` no static build produces,
# which fails the deploy outright) is still real, and each test now asserts that
# directly. The counter's own behaviour, and the ``run_worker_first`` rules that keep
# it from being billed on every subresource, live in test_sites_analytics_counter.py.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import Internal, ValidationError
from pocketpaw_ee.sites import workers_deploy


class _FakeProc:
    """A stand-in for the asyncio subprocess wrangler runs: returns a fixed
    (returncode, stdout, stderr) from communicate()."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _build_project(tmp_path: Path) -> str:
    """Create a minimal built project: the static output dir generator.build()
    emits (``.svelte-kit/cloudflare/``) with a worker entry, so deploy_workers has
    something to write into."""
    out = tmp_path / ".svelte-kit" / "cloudflare"
    out.mkdir(parents=True)
    (out / "_worker.js").write_text("export default {}")
    (out / "index.html").write_text("<h1>hi</h1>")
    return str(tmp_path)


def _patch_subprocess(monkeypatch, proc: _FakeProc) -> dict:
    """Patch asyncio.create_subprocess_exec to return ``proc`` and capture the call
    (argv, cwd, env) so a test can assert on the invocation."""
    captured: dict = {}

    async def _fake_exec(*argv, cwd=None, env=None, stdout=None, stderr=None):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        captured["env"] = env
        return proc

    monkeypatch.setattr(workers_deploy.asyncio, "create_subprocess_exec", _fake_exec)
    return captured


# ── name sanitization ────────────────────────────────────────────────────────


def test_worker_name_from_objectid_is_lowercase_no_underscore():
    # A 24-hex ObjectId is already lowercase-safe → paw-site-<id> verbatim.
    site_id = "507f1f77bcf86cd799439011"
    name = workers_deploy._worker_name(site_id)
    assert name == f"paw-site-{site_id}"
    assert workers_deploy._WORKER_NAME_RE.match(name)
    assert "_" not in name
    assert name == name.lower()


def test_worker_name_sanitizes_bad_id():
    # A hypothetical non-ObjectId id with underscores / uppercase / spaces is
    # coerced into a valid worker-name segment.
    name = workers_deploy._worker_name("Bad_ID__With  Spaces!")
    assert workers_deploy._WORKER_NAME_RE.match(name)
    assert "_" not in name
    assert name == "paw-site-bad-id-with-spaces"


def test_sanitize_empty_falls_back():
    assert workers_deploy._sanitize("___") == "site"
    assert workers_deploy._sanitize("") == "site"


# ── the recipe files ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_writes_assetsignore_and_wrangler_jsonc(tmp_path, monkeypatch):
    project = _build_project(tmp_path)
    site_id = "507f1f77bcf86cd799439011"
    proc = _FakeProc(0, b"https://paw-site-507f1f77bcf86cd799439011.acct.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    url = await workers_deploy.deploy_workers(site_id, project)

    # .assetsignore — EXACTLY the three recipe lines, in order.
    assetsignore = Path(project, ".svelte-kit/cloudflare/.assetsignore").read_text()
    assert assetsignore.splitlines() == ["_worker.js", "_routes.json", "_headers"]

    # wrangler.jsonc — the clean static config (parse it; it is JSON-compatible).
    cfg = json.loads(Path(project, "wrangler.jsonc").read_text())
    assert cfg["name"] == f"paw-site-{site_id}"
    assert cfg["main"] == ".svelte-kit/cloudflare/_worker.js"
    assert cfg["workers_dev"] is True
    assert cfg["compatibility_flags"] == ["nodejs_compat"]
    assert cfg["compatibility_date"] == "2024-09-23"
    assert cfg["assets"] == {"binding": "ASSETS", "directory": ".svelte-kit/cloudflare"}
    # A STATIC site binds no database.
    assert "d1_databases" not in cfg

    assert url == "https://paw-site-507f1f77bcf86cd799439011.acct.workers.dev"


# ── dynamic sites: the D1 binding on the free workers.dev tier ───────────────


@pytest.mark.asyncio
async def test_dynamic_deploy_binds_d1_in_wrangler_jsonc(tmp_path, monkeypatch):
    """A dynamic site must reach its per-tenant D1 WITHOUT Workers-for-Platforms.

    WfP (``put_worker``'s dispatch namespace) is a paid add-on; an account without it
    gets CF error 10121 / HTTP 403. Binding the D1 in the workers-mode config is what
    lets the same built worker serve live data on the free tier."""
    project = _build_project(tmp_path)
    site_id = "507f1f77bcf86cd799439011"
    proc = _FakeProc(0, b"https://paw-site-507f1f77bcf86cd799439011.acct.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    await workers_deploy.deploy_workers(site_id, project, d1_database_id="d1-uuid-0001")

    cfg = json.loads(Path(project, "wrangler.jsonc").read_text())
    assert cfg["d1_databases"] == [
        {
            "binding": "DB",
            "database_name": f"paw-site-{site_id}",
            "database_id": "d1-uuid-0001",
        }
    ]


@pytest.mark.asyncio
async def test_dynamic_deploy_declares_no_queue_producers(tmp_path, monkeypatch):
    """Cloudflare Queues is paid and these queues are never created on this path.

    Declaring a producer for a nonexistent queue fails ``wrangler deploy`` outright,
    so the emitted config must carry none. Both site-side consumers already degrade
    gracefully on a missing binding."""
    project = _build_project(tmp_path)
    proc = _FakeProc(0, b"https://x.y.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    await workers_deploy.deploy_workers("507f1f77bcf86cd799439011", project, d1_database_id="d1-1")

    cfg = json.loads(Path(project, "wrangler.jsonc").read_text())
    assert "queues" not in cfg


# ── html engine: an assets-only Worker, no server script (HE-4) ──────────────


def _build_html_project(tmp_path: Path) -> str:
    """Create a minimal built HTML project. ``static_output_rel("html")`` is ``"."``,
    so the generator writes the raw static tree straight into the project dir (no
    ``.svelte-kit/cloudflare`` build subdir) — index.html at the root."""
    (tmp_path / "index.html").write_text("<!doctype html><h1>hi</h1>")
    (tmp_path / "styles.css").write_text("h1{color:#111}")
    return str(tmp_path)


@pytest.mark.asyncio
async def test_html_deploy_names_no_svelte_worker(tmp_path, monkeypatch):
    """An html site is a raw static tree with NO server worker, so its wrangler config
    takes the assets-only shape: no ``nodejs_compat`` (nothing runs the node runtime),
    no D1, and ``assets.directory`` is the project root itself
    (``static_output_rel("html") == "."``).

    SA-1 CHANGED WHAT THIS TEST CAN CLAIM. The config now DOES carry a ``main`` — the
    generated pageview counter — so the old assertion (``"main" not in cfg``) has been
    replaced rather than deleted. The fact it was protecting survives intact and is
    the one asserted here: ``main`` must never name a ``_worker.js`` the build does not
    produce, which is the RX-1 failure this branch exists to prevent. The counter's own
    shape is covered in ``test_sites_analytics_counter.py``."""
    project = _build_html_project(tmp_path)
    site_id = "507f1f77bcf86cd799439011"
    proc = _FakeProc(0, b"https://paw-site-507f1f77bcf86cd799439011.acct.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    url = await workers_deploy.deploy_workers(site_id, project, engine="html")

    raw = Path(project, "wrangler.jsonc").read_text()
    cfg = json.loads(raw)
    assert cfg["name"] == f"paw-site-{site_id}"
    assert cfg["workers_dev"] is True
    assert cfg["assets"]["directory"] == "."
    assert "_worker.js" not in raw
    assert ".svelte-kit" not in raw
    assert "compatibility_flags" not in cfg
    assert "d1_databases" not in cfg
    assert url == "https://paw-site-507f1f77bcf86cd799439011.acct.workers.dev"


@pytest.mark.asyncio
async def test_html_assetsignore_excludes_the_config_file(tmp_path, monkeypatch):
    """With ``assets.directory == "."`` the wrangler config sits INSIDE the asset
    dir, so wrangler would serve ``wrangler.jsonc`` as a public asset. The html
    ``.assetsignore`` (also at the root) keeps the deploy-scaffold files out of the
    served tree — the reason html needs an ``.assetsignore`` even though it has no
    ``_worker.js`` to ignore."""
    project = _build_html_project(tmp_path)
    proc = _FakeProc(0, b"https://x.y.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    await workers_deploy.deploy_workers("507f1f77bcf86cd799439011", project, engine="html")

    assetsignore = Path(project, ".assetsignore").read_text().splitlines()
    assert "wrangler.jsonc" in assetsignore
    # It never lists a _worker.js — that Pages-style entry does not exist on html.
    assert "_worker.js" not in assetsignore


@pytest.mark.asyncio
async def test_html_deploy_passes_config_flag(tmp_path, monkeypatch):
    """html still names its config explicitly, same as the svelte/ripple path."""
    project = _build_html_project(tmp_path)
    proc = _FakeProc(0, b"https://x.y.workers.dev\n", b"")
    captured = _patch_subprocess(monkeypatch, proc)
    monkeypatch.setenv("PAW_CF_WRANGLER_CMD", "bunx wrangler@4.101.0")

    await workers_deploy.deploy_workers("507f1f77bcf86cd799439011", project, engine="html")

    assert "--config" in captured["argv"]
    assert captured["argv"][-1] == "wrangler.jsonc"
    assert captured["cwd"] == project


# ── react engine: builds, yet still assets-only (RX-1) ───────────────────────
#
# The section that exists because "runs a Node build" stopped implying "emits a
# server entry". A react project has a package.json and a real Vite build, so under
# the old ``needs_node_build`` condition it would have taken the SvelteKit branch —
# writing ``main: "dist/_worker.js"`` for a file no react build produces, which
# wrangler rejects. The deploy shape now reads ``emits_server_worker``.


def _build_react_project(tmp_path: Path) -> str:
    """Create a minimal BUILT react project: the Vite client output at ``dist/``
    (``static_output_rel("react")``) holding the PRERENDERED index.html, plus the
    project-root package.json a react project genuinely carries — the file whose
    presence would tempt a deploy path into assuming a server worker exists.

    Deliberately writes NO ``_worker.js`` anywhere: that is the fact under test."""
    (tmp_path / "package.json").write_text('{"name":"paw-site-x","private":true}')
    out = tmp_path / "dist"
    (out / "assets").mkdir(parents=True)
    (out / "index.html").write_text("<!doctype html><div id=root><main><h1>hi</h1></main></div>")
    (out / "assets" / "index-abc.css").write_text("h1{color:#111}")
    return str(tmp_path)


@pytest.mark.asyncio
async def test_react_deploy_takes_the_assets_only_shape(tmp_path, monkeypatch):
    """A react site builds to a PRERENDERED static tree with no server worker, so its
    wrangler config takes the assets-only shape — despite the engine needing a full
    Node build.

    This is the assertion that separates the two capabilities. It used to read
    ``"main" not in cfg``; SA-1 puts the pageview counter on this branch too, so the
    claim is now the precise one: ``main`` is OUR generated entry at the project root
    and never a ``dist/_worker.js`` that react does not write, which is what wrangler
    would fail the deploy on."""
    project = _build_react_project(tmp_path)
    site_id = "507f1f77bcf86cd799439011"
    proc = _FakeProc(0, b"https://paw-site-507f1f77bcf86cd799439011.acct.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    url = await workers_deploy.deploy_workers(site_id, project, engine="react")

    cfg = json.loads(Path(project, "wrangler.jsonc").read_text())
    assert cfg["name"] == f"paw-site-{site_id}"
    assert cfg["workers_dev"] is True
    # Vite's client output, NOT the SvelteKit adapter path.
    assert cfg["assets"]["directory"] == "dist"
    assert cfg["main"] == "_paw_analytics.js"
    assert Path(project, cfg["main"]).is_file()
    assert "compatibility_flags" not in cfg
    assert "d1_databases" not in cfg
    assert url == "https://paw-site-507f1f77bcf86cd799439011.acct.workers.dev"


@pytest.mark.asyncio
async def test_react_deploy_never_names_a_worker_entry(tmp_path, monkeypatch):
    """Belt-and-braces on the same fact, stated as the failure it prevents: nothing
    in the emitted config may reference a ``_worker.js`` on the react track."""
    project = _build_react_project(tmp_path)
    proc = _FakeProc(0, b"https://x.y.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    await workers_deploy.deploy_workers("507f1f77bcf86cd799439011", project, engine="react")

    raw = Path(project, "wrangler.jsonc").read_text()
    assert "_worker.js" not in raw
    assert ".svelte-kit" not in raw


@pytest.mark.asyncio
async def test_react_assetsignore_lands_in_dist_and_excludes_itself(tmp_path, monkeypatch):
    """``.assetsignore`` is written INSIDE the asset dir on every engine, so on react
    it lands in ``dist/`` — where wrangler would otherwise serve it as a public
    asset. It must exclude itself, and must NOT list the Pages worker entry (there is
    no ``_worker.js`` to hide on this track)."""
    project = _build_react_project(tmp_path)
    proc = _FakeProc(0, b"https://x.y.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    await workers_deploy.deploy_workers("507f1f77bcf86cd799439011", project, engine="react")

    assetsignore = Path(project, "dist", ".assetsignore").read_text().splitlines()
    assert ".assetsignore" in assetsignore
    assert "_worker.js" not in assetsignore
    # The prerendered page is still there to serve — the deploy scaffold did not
    # clobber the build output.
    assert Path(project, "dist", "index.html").read_text().startswith("<!doctype html>")


@pytest.mark.asyncio
async def test_react_deploy_without_a_build_fails_cleanly(tmp_path, monkeypatch):
    """react needs its build to have RUN — unlike html, generate alone leaves no
    servable tree. A missing ``dist/`` must raise the clean ValidationError before
    any subprocess, not produce a config pointing at a directory that isn't there."""
    (tmp_path / "package.json").write_text('{"name":"paw-site-x"}')
    proc = _FakeProc(0, b"https://x.y.workers.dev\n", b"")
    captured = _patch_subprocess(monkeypatch, proc)

    with pytest.raises(ValidationError):
        await workers_deploy.deploy_workers(
            "507f1f77bcf86cd799439011", str(tmp_path), engine="react"
        )
    assert captured.get("argv") is None


@pytest.mark.asyncio
async def test_static_svelte_config_unchanged_by_engine_default(tmp_path, monkeypatch):
    """Regression guard: the default engine (ripple/svelte) keeps the exact
    server-worker config — ``main`` + ``nodejs_compat`` + the ``.svelte-kit/cloudflare``
    asset dir — byte-for-byte, so HE-4 does not touch the proven path."""
    project = _build_project(tmp_path)
    proc = _FakeProc(0, b"https://x.y.workers.dev\n", b"")
    _patch_subprocess(monkeypatch, proc)

    await workers_deploy.deploy_workers("507f1f77bcf86cd799439011", project)

    cfg = json.loads(Path(project, "wrangler.jsonc").read_text())
    assert cfg["main"] == ".svelte-kit/cloudflare/_worker.js"
    assert cfg["compatibility_flags"] == ["nodejs_compat"]
    assert cfg["assets"] == {"binding": "ASSETS", "directory": ".svelte-kit/cloudflare"}
    # The svelte/ripple .assetsignore still drops the Pages worker entry.
    assetsignore = Path(project, ".svelte-kit/cloudflare/.assetsignore").read_text().splitlines()
    assert assetsignore == ["_worker.js", "_routes.json", "_headers"]


@pytest.mark.asyncio
async def test_deploy_invokes_wrangler_with_deploy_in_project_dir(tmp_path, monkeypatch):
    project = _build_project(tmp_path)
    proc = _FakeProc(0, b"https://x.y.workers.dev\n", b"")
    captured = _patch_subprocess(monkeypatch, proc)
    # Pin the wrangler cmd so the assertion is deterministic.
    monkeypatch.setenv("PAW_CF_WRANGLER_CMD", "bunx wrangler@4.101.0")

    await workers_deploy.deploy_workers("507f1f77bcf86cd799439011", project)

    # On Windows the shared resolver rewrites `bunx` -> `bun x` (there is no bunx.exe
    # for create_subprocess_exec to launch); POSIX keeps the real `bunx`.
    # ``--config wrangler.jsonc`` is always passed: a DYNAMIC project dir also holds
    # the generator's wrangler.toml, and wrangler must not resolve that one.
    expected = (
        ["bun", "x", "wrangler@4.101.0", "deploy", "--config", "wrangler.jsonc"]
        if sys.platform == "win32"
        else ["bunx", "wrangler@4.101.0", "deploy", "--config", "wrangler.jsonc"]
    )
    assert captured["argv"] == expected
    assert captured["cwd"] == project
    # The CF creds wrangler reads itself ride through the env (full os.environ).
    assert captured["env"] is not None


# ── URL resolution ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_url_parsed_from_stdout(tmp_path, monkeypatch):
    project = _build_project(tmp_path)
    stdout = (
        b"Total Upload: 10 KiB\n"
        b"Uploaded paw-site-x (1.2 sec)\n"
        b"  https://paw-site-x.my-acct.workers.dev\n"
        b"Current Deployment ID: abc\n"
    )
    _patch_subprocess(monkeypatch, _FakeProc(0, stdout, b""))

    url = await workers_deploy.deploy_workers("x", project)
    assert url == "https://paw-site-x.my-acct.workers.dev"


@pytest.mark.asyncio
async def test_url_fallback_from_subdomain_env(tmp_path, monkeypatch):
    project = _build_project(tmp_path)
    # wrangler printed nothing parseable → construct from PAW_CF_WORKERS_SUBDOMAIN.
    _patch_subprocess(monkeypatch, _FakeProc(0, b"deployed, no url here\n", b""))
    monkeypatch.setenv("PAW_CF_WORKERS_SUBDOMAIN", "acme-team")

    url = await workers_deploy.deploy_workers("507f1f77bcf86cd799439011", project)
    assert url == "https://paw-site-507f1f77bcf86cd799439011.acme-team.workers.dev"


@pytest.mark.asyncio
async def test_url_empty_when_unparseable_and_no_subdomain(tmp_path, monkeypatch):
    project = _build_project(tmp_path)
    _patch_subprocess(monkeypatch, _FakeProc(0, b"deployed, no url\n", b""))
    monkeypatch.delenv("PAW_CF_WORKERS_SUBDOMAIN", raising=False)

    # The deploy still SUCCEEDS (returncode 0) — just with no resolved URL.
    url = await workers_deploy.deploy_workers("x", project)
    assert url == ""


# ── failure paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nonzero_exit_raises_internal_with_stderr_tail(tmp_path, monkeypatch):
    project = _build_project(tmp_path)
    _patch_subprocess(
        monkeypatch,
        _FakeProc(1, b"", b"Authentication error [code: 10000] - invalid token"),
    )

    with pytest.raises(Internal) as exc:
        await workers_deploy.deploy_workers("x", project)
    assert "invalid token" in str(exc.value)


@pytest.mark.asyncio
async def test_missing_build_dir_raises_validation_error(tmp_path, monkeypatch):
    # No .svelte-kit/cloudflare/ — the project was never built. Should raise a clean
    # ValidationError BEFORE any subprocess (the subprocess would be a NameError if
    # reached, since we don't patch it).
    project = str(tmp_path)
    with pytest.raises(ValidationError):
        await workers_deploy.deploy_workers("x", project)


@pytest.mark.asyncio
async def test_wrangler_not_on_path_raises_internal(tmp_path, monkeypatch):
    project = _build_project(tmp_path)

    async def _boom(*argv, **kwargs):
        raise FileNotFoundError("bunx: command not found")

    monkeypatch.setattr(workers_deploy.asyncio, "create_subprocess_exec", _boom)
    with pytest.raises(Internal) as exc:
        await workers_deploy.deploy_workers("x", project)
    assert "wrangler" in str(exc.value).lower()

"""Root pytest configuration: keeps every test hermetic from the machine and from
the tests that ran before it in the same process.

* Temp HOME. The block at the very top runs before any ``pocketpaw`` import and
  points ``HOME`` at a fresh per-process temp dir (each xdist worker imports this
  file itself), so nothing writes under the developer's real ``~/.pocketpaw``,
  ``~/.soul`` or ``~/.config``. Tool caches (uv, Playwright, XDG) and the global
  git config are exported from the real home first so they keep working.
* No ``.env``. ``PYTHON_DOTENV_DISABLED`` and ``Settings.env_file = None`` make
  every local run read the environment CI sees; Logfire is pinned off.
* Process globals reset before every test. ``_reset_process_globals`` runs the
  ``_RESETS`` table (lifecycle registry, settings/provider caches, every
  ``reset_*`` / ``_reset_for_tests`` hook) and blanks the ``_GLOBALS`` table with
  ``monkeypatch``. It only touches modules already in ``sys.modules``, it is the
  first function-scoped autouse fixture so it never wipes what the others set
  up, and a reset that raises fails the session once, by name. Set
  ``PP_RESET_TIMING=1`` to print its median/p99 cost at session end. Store
  caches are evicted per test even though ``tests/cloud``'s ``local_store_home``
  keeps one data dir for the session; handles reopen under that same dir.
* Other autouse isolation: connector state, audit log, ``SOUL_DATA_DIR`` and the
  decisions DB go to ``tmp_path``; the paw-bar per-IP limiter is emptied; catalog
  syncs are recorded, not run; spawning the real livekit call-bot is refused (it
  never exits under pytest and hangs the suite).
* mongomock's ``create_indexes`` is shimmed to keep ``partialFilterExpression``
  so partial unique indexes behave as in MongoDB.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Temp HOME, before anything can resolve ``Path.home()`` at import. Real-home tool
# paths are exported first: uv and Playwright would otherwise re-download into the
# temp dir, and subprocess git would lose its identity.
assert not any(m == "pocketpaw" or m.startswith("pocketpaw.") for m in sys.modules), (
    "tests/conftest.py must set HOME before the first pocketpaw import"
)
_REAL_HOME = Path.home()
os.environ.setdefault("XDG_CACHE_HOME", str(_REAL_HOME / ".cache"))
os.environ.setdefault("UV_CACHE_DIR", str(Path(os.environ["XDG_CACHE_HOME"]) / "uv"))
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(
        _REAL_HOME / "Library/Caches/ms-playwright"
        if sys.platform == "darwin"
        else Path(os.environ["XDG_CACHE_HOME"]) / "ms-playwright"
    ),
)
if (_REAL_HOME / ".gitconfig").exists():
    os.environ.setdefault("GIT_CONFIG_GLOBAL", str(_REAL_HOME / ".gitconfig"))
_TEST_HOME = tempfile.mkdtemp(prefix="pp-test-home-")
os.environ["HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, True)

import asyncio  # noqa: E402
import functools  # noqa: E402
import importlib.metadata  # noqa: E402
import importlib.util  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

# A test run reads NO ``.env`` file. This must run before the first ``pocketpaw``
# import: ``security/url_validators.py`` calls ``load_dotenv()`` at import, and
# python-dotenv walks UP from the calling file, so a worktree under
# ``pocketPaw/.worktrees/`` loads the parent checkout's operator ``.env``. The
# per-tree allowlists (``tests/ee/sites/conftest.py`` OPERATOR_ENV_VARS, the Logfire
# pins below) only cover the names someone thought of; anything else in the file
# (``POCKETPAW_SITES_BILLING_ENFORCED`` switched on the custom-domain cap and the
# concierge plan gate, ``PAW_SITES_GEN_CMD`` un-skipped the real-generator e2e) made
# the suite's result a property of the machine. CI has no ``.env``, so this changes
# nothing there — it makes every local run match CI. ``setdefault`` so a developer can
# export ``PYTHON_DOTENV_DISABLED=0`` for a deliberate integration run.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import pocketpaw._registry as _pp_registry  # noqa: E402
from pocketpaw.config import Settings  # noqa: E402
from pocketpaw.security.audit import AuditLogger  # noqa: E402

# ``_reset_process_globals`` clears the provider cache before every test, and a
# rebuild re-scans installed metadata (~17 ms per group). Installed entry points
# cannot change mid-run, so the scan is memoised here; the providers themselves
# are still re-instantiated per test.
# ponytail: a test that installs a dist mid-run would not see it; none does.
_scan_entry_points = functools.cache(lambda group: importlib.metadata.entry_points(group=group))
_pp_registry.entry_points = lambda *, group: _scan_entry_points(group)

# The pydantic-settings half of the same leak: ``env_file=".env"`` reads the CWD's
# file into Settings fields (``sites_billing_enforced`` among them) without touching
# os.environ, so the dotenv switch above cannot reach it. Same opt-in as above.
if os.environ["PYTHON_DOTENV_DISABLED"].casefold() in {"1", "true", "t", "yes", "y"}:
    Settings.model_config["env_file"] = None


def require_enterprise_install(suite: str) -> None:
    """Fail collection, never skip, when the enterprise package is missing.

    ``tests/cloud`` and ``tests/ee`` are the enterprise surface. A venv built with
    ``uv sync --dev --all-extras`` has beanie but not ``pocketpaw_ee``, and the
    old ``importorskip`` turned that into a wholesale silent skip: a run read
    green with hundreds of tests never collected (2026-08-06, 2026-08-17,
    2026-10-01). The OSS-only CI job never loads these conftests (it passes
    ``--ignore=tests/ee`` and ``tests/cloud`` is hidden by the pyproject addopts).
    """
    import importlib.util

    needed = ("pocketpaw_ee", "beanie", "mongomock_motor")
    missing = [m for m in needed if importlib.util.find_spec(m) is None]
    if missing:
        raise pytest.UsageError(
            f"{suite} needs the enterprise install but {', '.join(missing)} is not importable. "
            "Run `uv sync --group ee --group dev --extra knowledge` (`--all-extras` alone does not "
            "install pocketpaw_ee). Refusing to skip: a skipped enterprise suite reads green."
        )


# Tests run with loopback / RFC1918 URLs in many places (`http://localhost:*`
# ollama defaults, mock HTTP servers, etc). In production that's the exact
# SSRF shape blocked by security.url_validators.validate_external_url — here
# we relax the check so Settings() instantiates cleanly. Tests that need the
# strict behaviour monkeypatch POCKETPAW_ALLOW_INTERNAL_URLS=false themselves.
os.environ.setdefault("POCKETPAW_ALLOW_INTERNAL_URLS", "true")

# A test run must NEVER configure Logfire. Same class of dev-machine leak as the
# line above, and a worse blast radius, because it leaves the machine.
#
# ``security/url_validators.py`` calls ``load_dotenv()`` at import, and
# python-dotenv walks UP from the calling FILE when the frame is not
# interactive. From a git worktree that walk reaches the PARENT checkout's
# ``.env``, so an operator's real project token arrives in a test process that
# never asked for one. (``python -c`` counts as interactive and searches the cwd
# instead, which is why this does not reproduce from a one-liner.)
#
# It has to be popped here, at import, rather than in a fixture:
# ``logfire.configure`` is process-global and there is no un-configure, so one
# test reaching ``setup_logging`` before the fixture ran would arm exporting for
# the whole session. With POCKETPAW_LOGFIRE_INCLUDE_CONTENT also set, what ships
# is fixture text.
#
# SET to a safe value, not popped -- popping does not hold, and that was the
# first thing tried here. The pop runs at conftest import; ``load_dotenv`` runs
# whenever the module that calls it is first imported, which for
# ``url_validators`` is later, and for ``dashboard_lifecycle`` and
# ``uploads/factory`` is inside a function at run time. Any of those re-adds
# what was popped.
#
# Every one of those calls uses ``override=False`` (the default), so a value
# already present in the environment WINS. Setting a safe one is therefore the
# only form of this that survives a later load. Checked: no call in src/ or ee/
# passes override=True.
#
# The token is emptied rather than removed for the same reason -- an empty string
# is present, so it cannot be replaced, and it is falsy, so
# ``send_to_logfire="if-token-present"`` exports nothing even if something did
# manage to configure.
#
# A test that wants the ON path uses ``monkeypatch.setenv``, which restores the
# safe value afterwards.
#
# POCKETPAW_LOGFIRE_INSTRUMENT is deliberately NOT pinned here. It only selects
# which integrations attach, and nothing attaches unless configure ran, which the
# master switch already gates -- so pinning it would buy no safety and would
# fight the tests that exercise the selection.
for _var, _safe in (
    ("POCKETPAW_LOGFIRE_ENABLED", "false"),
    ("POCKETPAW_LOGFIRE_INCLUDE_CONTENT", "false"),
    ("LOGFIRE_TOKEN", ""),
):
    os.environ[_var] = _safe


def _forward_partial_filter_in_mongomock() -> None:
    """Make mongomock's ``create_indexes`` keep ``partialFilterExpression``.

    ``mongomock.Collection.create_indexes`` (what Beanie's ``init_beanie`` calls) rebuilds
    each ``IndexModel`` from ``key`` / ``unique`` / ``sparse`` / ``expireAfterSeconds`` /
    ``name`` only, although ``create_index`` itself supports the filter. A partial unique
    index then enforces uniqueness on EVERY row, nulls included -- the opposite of what
    MongoDB does -- and any model declaring one breaks every test that inserts two rows.
    """
    try:
        from mongomock.collection import Collection
    except ImportError:  # OSS-only install without the test DB stack
        return

    def create_indexes(self, indexes, session=None):
        return [
            self.create_index(
                index.document["key"].items(),
                session=session,
                expireAfterSeconds=index.document.get("expireAfterSeconds"),
                unique=index.document.get("unique", False),
                sparse=index.document.get("sparse", False),
                name=index.document.get("name"),
                partialFilterExpression=index.document.get("partialFilterExpression"),
            )
            for index in indexes
        ]

    Collection.create_indexes = create_indexes


_forward_partial_filter_in_mongomock()


def pytest_report_header() -> str | None:
    """Say so, loudly, when a mutation sweep is running in this worktree.

    Added 2026-08-09 after this cost real time twice in one day. ``scripts/mutate.py``
    applies each mutation IN PLACE, so while one is live the tree genuinely contains
    broken code and any test run against it fails for a reason that looks exactly like a
    regression. It happened to a reviewer, and then to me while verifying the fix.

    This hook is the reader's ACTUAL path — someone investigating a failure runs the
    suite; they do not necessarily run ``git status``. So the warning belongs in pytest's
    own header, where it is impossible to miss and where it still works with the marker
    gitignored.

    A header, deliberately, not a hard failure: a sweep runs the suite itself, over and
    over, and erroring out would make the tool unable to do its job.
    """
    marker = Path(__file__).resolve().parents[1] / ".mutation-sweep-active"
    if not marker.exists():
        return None
    current = ""
    try:
        for line in marker.read_text(encoding="utf-8").splitlines():
            if line.startswith("current"):
                current = line.partition(":")[2].strip()
                break
    except OSError:  # pragma: no cover - best effort
        pass
    return (
        "\n"
        "  ****************************************************************\n"
        "  *  A MUTATION SWEEP IS RUNNING IN THIS WORKTREE.               *\n"
        "  *  Failures below are EXPECTED and are NOT regressions.        *\n"
        "  *  Re-run once .mutation-sweep-active is gone.                 *\n"
        "  ****************************************************************\n"
        f"  currently mutated: {current or '(unknown)'}\n"
    )


@pytest.fixture(scope="session", autouse=True)
def _setup_asyncio_child_watcher():
    """Attach a child watcher so subprocess-based tests don't crash.

    On Python < 3.12 the default child watcher requires attachment to
    the running event loop.  On 3.12+ child watchers were removed, so
    this is a no-op.
    """
    if sys.version_info < (3, 12) and hasattr(asyncio, "ThreadedChildWatcher"):
        watcher = asyncio.ThreadedChildWatcher()
        asyncio.set_child_watcher(watcher)
    yield


# ---------------------------------------------------------------------------
# Process-global reset, before every test (see the module docstring)
# ---------------------------------------------------------------------------

# Zero-arg reset hooks, as (module, function). Import-time registries
# (meetings ``providers.base._REGISTRY``, ``ripple_resolver._REGISTRY``) are left
# out on purpose: they are filled when their modules import, and modules never
# re-import, so clearing them would drop providers for every later test.
_RESETS: tuple[tuple[str, str], ...] = (
    ("pocketpaw.lifecycle", "reset_all"),
    ("pocketpaw._registry", "clear_cache"),
    ("pocketpaw._store_locks", "reset_audit_locks"),
    ("pocketpaw.api.api_keys", "reset_api_key_manager"),
    ("pocketpaw.api.oauth2.server", "reset_oauth_server"),
    ("pocketpaw.deep_work", "reset_deep_work_session"),
    ("pocketpaw.journal_dep", "reset_journal_cache"),
    ("pocketpaw.kits.store", "reset_kit_store"),
    ("pocketpaw.mission_control.executor", "reset_mc_task_executor"),
    ("pocketpaw.mission_control.heartbeat", "reset_heartbeat_daemon"),
    ("pocketpaw.mission_control.manager", "reset_mission_control_manager"),
    ("pocketpaw.mission_control.store", "reset_mission_control_store"),
    ("pocketpaw.retrieval.router", "reset_store_cache"),
    ("pocketpaw.runtime.connector_bus", "reset_for_tests"),
    ("pocketpaw.security.pii", "reset_pii_scanner"),
    ("pocketpaw.soul._manager", "_reset_manager"),
    ("pocketpaw.stores", "reset_store_caches"),
    ("pocketpaw.widget.router", "reset_store_cache"),
    ("pocketpaw_ee.cloud._core.realtime.broadcast", "_reset_for_tests"),
    ("pocketpaw_ee.cloud._core.realtime.presence", "_reset_for_tests"),
    ("pocketpaw_ee.cloud._core.realtime.xproc", "_reset_for_tests"),
    ("pocketpaw_ee.cloud._core.redis_client", "_reset_for_tests"),
    ("pocketpaw_ee.cloud._core.request_log", "_reset_for_tests"),
    ("pocketpaw_ee.cloud._core.sweep_runtime", "reset"),
    ("pocketpaw_ee.cloud._core.temporal_scheduler", "_reset_for_tests"),
    ("pocketpaw_ee.cloud._core.timing", "reset_buffers"),
    ("pocketpaw_ee.cloud.auth.api_keys", "_reset_caches_for_tests"),
    ("pocketpaw_ee.cloud.auth.sso.crypto", "_reset_for_tests"),
    ("pocketpaw_ee.cloud.auth.sso.oidc", "_clear_discovery_cache"),
    ("pocketpaw_ee.cloud.chat.runs.executor", "_reset_for_tests"),
    ("pocketpaw_ee.cloud.chat.runs.transport", "_reset_for_tests"),
    ("pocketpaw_ee.cloud.chat.runs.worker", "_reset_bootstrap_for_tests"),
    ("pocketpaw_ee.cloud.codeagent.bridge", "_reset_for_tests"),
    ("pocketpaw_ee.cloud.composio.providers", "reset_cache_for_tests"),
    ("pocketpaw_ee.cloud.composio.service", "reset_client_cache_for_tests"),
    ("pocketpaw_ee.cloud.decisions.explain.cache", "reset_explain_cache_for_tests"),
    ("pocketpaw_ee.cloud.decisions.reconciler", "reset_reconciler_for_tests"),
    ("pocketpaw_ee.cloud.decisions.service", "reset_projection_for_tests"),
    ("pocketpaw_ee.cloud.embeddings.cost_tracker", "reset_cost_tracker_for_tests"),
    ("pocketpaw_ee.cloud.fabric_ingest.scheduler", "reset_scheduler_for_tests"),
    ("pocketpaw_ee.cloud.files.content_search", "reset_compiled_with_cache"),
    ("pocketpaw_ee.cloud.member_ingest.scheduler", "reset_scheduler_for_tests"),
    ("pocketpaw_ee.cloud.pockets._refresh_budget", "reset_budget"),
    ("pocketpaw_ee.cloud.pockets.layouts", "reset_user_template_store"),
    ("pocketpaw_ee.cloud.pockets.refresh_scheduler", "_reset_for_tests"),
    ("pocketpaw_ee.cloud.push.coalesce", "reset"),
    ("pocketpaw_ee.cloud.websandbox.githubapp", "_reset_client_for_tests"),
    ("pocketpaw_ee.cloud.websandbox.requirements", "_reset_cache_for_tests"),
    ("pocketpaw_ee.foresight.api.run_store", "reset_run_store"),
    ("pocketpaw_ee.foresight.insights_llm", "reset_cache"),
    ("pocketpaw_ee.foresight.persona", "reset_paw_social_agent_counter"),
    ("pocketpaw_ee.sites.artifact_store_s3", "reset_shared_adapter"),
)

# Singletons with no reset hook, as (module, attr, default), blanked with
# ``monkeypatch``. A one-shot "already subscribed" flag sits next to the thing it
# subscribed to: the message bus is dropped by ``lifecycle.reset_all`` and
# ``mount_cloud`` installs a new realtime bus per app, so a flag left True would
# skip subscribing on the new bus. ``security.audit._audit_logger`` is absent:
# ``_isolate_audit_log`` gives each test its own.
_GLOBALS: tuple[tuple[str, str, object], ...] = (
    ("pocketpaw.agents.plan_mode", "_plan_manager", None),
    ("pocketpaw.agents.pool", "_pool", None),
    ("pocketpaw.api.v1.connectors", "_registry", None),
    ("pocketpaw.audit.store", "_audit_store", None),
    ("pocketpaw.automations.evaluator", "_evaluator", None),
    ("pocketpaw.automations.store", "_instance", None),
    ("pocketpaw.bus.commands", "_handler", None),
    ("pocketpaw.bus.media", "_downloader", None),
    ("pocketpaw.daemon.context", "_context_hub", None),
    ("pocketpaw.daemon.intentions", "_intention_store", None),
    ("pocketpaw.daemon.proactive", "_daemon", None),
    ("pocketpaw.health", "_instance", None),
    ("pocketpaw.mcp.manager", "_ws_broadcast", None),
    ("pocketpaw.recent_files", "_tracker", None),
    ("pocketpaw.security.guardian", "_guardian", None),
    ("pocketpaw.security.injection_scanner", "_scanner", None),
    ("pocketpaw.security.rate_limiter", "_api_key_limiter", None),
    ("pocketpaw.skills.executor", "_skill_executor", None),
    ("pocketpaw.skills.loader", "_skill_loader", None),
    ("pocketpaw.tools.builtin.connector_tools", "_registry", None),
    ("pocketpaw.usage_tracker", "_tracker", None),
    ("pocketpaw.web_server", "_session_secret", None),
    ("pocketpaw.web_server", "_settings", None),
    # message bus (lifecycle) + its one-shot subscriber
    ("pocketpaw_ee.cloud.sessions.title_listener", "_subscribed", False),
    # realtime bus (replaced per mount_cloud) + its subscriber and its buffer
    ("pocketpaw_ee.cloud.activity.buffer", "_buffer", None),
    ("pocketpaw_ee.cloud.activity.buffer", "_registered", False),
    # per-test audit logger + the bridge installed on it
    ("pocketpaw_ee.cloud.audit.listeners", "_BRIDGE_REGISTERED", False),
    ("pocketpaw_ee.cloud.connectors.service", "_registry", None),
    ("pocketpaw_ee.cloud.license", "_cached_license", None),
    ("pocketpaw_ee.cloud.license", "_license_error", None),
    ("pocketpaw_ee.cloud.license", "_no_license_until", 0.0),
    ("pocketpaw_ee.cloud.shared.db", "_client", None),
    ("pocketpaw_ee.sites.local_server", "_server", None),
)

_reset_ns: list[int] = []
_reset_failures: dict[str, str] = {}


def _check_reset_tables() -> None:
    """A misspelt module would look exactly like "not imported yet" and silently
    no-op, so every name must exist on disk. Checked by path, not ``find_spec``
    on the dotted name, which would import every parent package (all of
    ``pocketpaw_ee.cloud``) at conftest load."""
    roots = {}
    for top in ("pocketpaw", "pocketpaw_ee"):
        spec = importlib.util.find_spec(top)
        if spec is not None and spec.origin:
            roots[top] = Path(spec.origin).parent
    bad = []
    for name in sorted({m for m, _ in _RESETS} | {m for m, _, _ in _GLOBALS}):
        top, _, rest = name.partition(".")
        if top not in roots:
            if top != "pocketpaw_ee":  # OSS-only install: ee rows are inert
                bad.append(name)
            continue
        path = roots[top].joinpath(*rest.split("."))
        if not (path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()):
            bad.append(name)
    if bad:
        raise pytest.UsageError(f"tests/conftest.py reset tables name unknown modules: {bad}")


_check_reset_tables()


@pytest.fixture(autouse=True)
def _reset_process_globals(monkeypatch):
    """Start every test from fresh process singletons (see the module docstring).

    First function-scoped autouse fixture in the tree, so the isolation fixtures
    below and every suite's own fixtures set up on top of a clean slate."""
    t0 = time.perf_counter_ns()
    modules = sys.modules
    # Looked up each time: some tests swap get_settings for a plain stub.
    config = modules.get("pocketpaw.config")
    clear = getattr(getattr(config, "get_settings", None), "cache_clear", None)
    if clear is not None:
        clear()
    for mod_name, func_name in _RESETS:
        mod = modules.get(mod_name)
        if mod is None:
            continue
        try:
            getattr(mod, func_name)()
        except Exception as exc:  # noqa: BLE001 — one broken hook must not fail every test
            _reset_failures.setdefault(f"{mod_name}.{func_name}", repr(exc))
    for mod_name, attr, default in _GLOBALS:
        mod = modules.get(mod_name)
        if mod is None:
            continue
        try:
            monkeypatch.setattr(mod, attr, default)
        except Exception as exc:  # noqa: BLE001
            _reset_failures.setdefault(f"{mod_name}.{attr}", repr(exc))
    _reset_ns.append(time.perf_counter_ns() - t0)
    yield


def pytest_sessionfinish(session, exitstatus):
    # Runs in every xdist worker and in the controller; workers' stdout is not
    # shown, so report on stderr, and only from a process that ran tests.
    if _reset_failures:
        lines = "\n".join(f"  {k}: {v}" for k, v in sorted(_reset_failures.items()))
        sys.stderr.write(f"\nERROR: process-global resets raised (tests/conftest.py):\n{lines}\n")
        if session.exitstatus == 0:
            session.exitstatus = 1
    if os.environ.get("PP_RESET_TIMING") == "1" and _reset_ns:
        ms = sorted(n / 1e6 for n in _reset_ns)
        p99 = ms[min(len(ms) - 1, int(len(ms) * 0.99))]
        sys.stderr.write(
            f"\n_reset_process_globals: {len(ms)} tests, median {statistics.median(ms):.4f} ms, "
            f"p99 {p99:.4f} ms, max {ms[-1]:.3f} ms\n"
        )


@pytest.fixture(autouse=True)
def _enable_test_full_access(request, monkeypatch):
    """Flip the require_scope testing-bypass on for all tests by default.

    Router-only tests (which mount FastAPI routers without the dashboard
    middleware) can't set request.state.full_access on their own — this
    fixture lets them exercise route logic without every fixture having
    to install middleware. Tests that explicitly verify fail-closed
    scope behaviour use the ``enforce_scope`` marker to opt out.
    """
    if "enforce_scope" in request.keywords:
        return
    monkeypatch.setattr("pocketpaw.api.deps._TESTING_FULL_ACCESS", True)


@pytest.fixture(autouse=True)
def _isolate_connector_state(tmp_path, monkeypatch):
    """Prevent tests from persisting connector config to the real ~/.pocketpaw.

    ConnectorRegistry.connect() is write-through to a state store that
    defaults to ~/.pocketpaw/connectors/state (CS-1). Point the default at a
    per-test temp dir so suites that build a registry with the default store
    stay hermetic. Tests that exercise the store directly pass an explicit
    ``base_dir`` instead.
    """
    monkeypatch.setattr(
        "pocketpaw.connectors.state_store._default_state_dir",
        lambda: tmp_path / "connector-state",
    )
    monkeypatch.setattr(
        "pocketpaw.connectors.registry._default_home_connectors_dir",
        lambda: tmp_path / "home-connectors",
    )


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path):
    """Prevent tests from writing to the real ~/.pocketpaw/audit.jsonl.

    Creates a temp audit logger per test and patches the singleton so
    ToolRegistry.execute() and any other callers write to a throwaway file.
    """
    temp_logger = AuditLogger(log_path=tmp_path / "audit.jsonl")
    with (
        patch("pocketpaw.security.audit._audit_logger", temp_logger),
        patch("pocketpaw.security.audit.get_audit_logger", return_value=temp_logger),
        patch("pocketpaw.tools.registry.get_audit_logger", return_value=temp_logger),
    ):
        yield temp_logger


def _clear_journal_cache() -> None:
    # sys.modules lookup, not an import; getattr because some tests swap the
    # lru_cache'd factory for a plain stub while they run.
    fn = getattr(sys.modules.get("pocketpaw.journal_dep"), "_cached_journal", None)
    if hasattr(fn, "cache_clear"):
        fn.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_soul_data_dir(tmp_path, monkeypatch):
    """Keep every test out of the developer's real ``~/.soul``.

    The org journal (``pocketpaw.journal_dep``) lives under ``SOUL_DATA_DIR`` or
    ``~/.soul``, and the decisions store's ``_DB_PATH`` global defaults to
    ``~/.soul/decisions.db``; tests that ``set_db_path(tmp_path)`` never restored it.
    The next ``mount_cloud`` then replayed the whole real journal (~137k events)
    into a fresh temp store: 1182 s in one census run. Both now point at this
    test's tmp dir and are restored afterwards.
    """
    soul_dir = tmp_path / "soul"
    monkeypatch.setenv("SOUL_DATA_DIR", str(soul_dir))
    _clear_journal_cache()
    if importlib.util.find_spec("pocketpaw_ee") is not None:
        # Imported here (lazy package, ~1 s once per process) so a store first
        # imported mid-test can't resolve the real default path.
        from pocketpaw_ee.cloud.decisions import store

        monkeypatch.setattr(store, "_DB_PATH", soul_dir / "decisions.db")
    yield
    _clear_journal_cache()


# ---------------------------------------------------------------------------
# Gated-proposal test seam (feat/growth-g4, security review F2)
# ---------------------------------------------------------------------------


def seed_gated_action(client, payload: dict):
    """Seed a PENDING Action carrying a gated blob, the way a real proposer does.

    ``POST /instinct/actions`` is the GENERIC propose route, open to any MEMBER.
    It now REFUSES reserved gated-blob parameter keys (``_ship_action``,
    ``_growth_send``, ``_admin_action``, …) with
    ``422 instinct.reserved_parameter_key`` — a member could otherwise file an
    innocuous Tray card whose blob dispatches a privileged executor the moment
    someone clicks Approve. Only the in-process helper that owns each kind
    (``ee.cloud.ship.propose``, ``ee.cloud.growth.propose``, …) may mint one;
    those call ``store.propose`` directly and never cross this route.

    Gate tests still need such an Action in the store. This helper reproduces
    the state the real helper leaves behind: POST the payload with the plain
    parameters, then write the gated blob onto the stored row. The write is a
    plain synchronous sqlite UPDATE (not ``store.update_parameters``) so the
    helper works identically inside and outside a running event loop.

    Returns the propose response, so a call site keeps reading
    ``resp.json()["id"]`` / ``resp.status_code`` exactly as before.
    """
    import json as _json
    import sqlite3

    from pocketpaw_ee.instinct import router as _instinct_router
    from pocketpaw_ee.instinct.router import RESERVED_GATED_PARAM_KEYS

    parameters = dict(payload.get("parameters") or {})
    gated = {k: v for k, v in parameters.items() if k in RESERVED_GATED_PARAM_KEYS}
    plain = {k: v for k, v in parameters.items() if k not in gated}

    resp = client.post("/instinct/actions", json={**payload, "parameters": plain})
    if not gated or resp.status_code != 201:
        return resp

    store = _instinct_router._store(payload.get("workspace_id") or "")
    with sqlite3.connect(store._db_path) as db:
        db.execute(
            "UPDATE instinct_actions SET parameters = ? WHERE id = ?",
            (_json.dumps(parameters), resp.json()["id"]),
        )
    return resp


async def aseed_gated_action(client, payload: dict):
    """``seed_gated_action`` for an httpx ``AsyncClient``. Same contract."""
    import json as _json
    import sqlite3

    from pocketpaw_ee.instinct import router as _instinct_router
    from pocketpaw_ee.instinct.router import RESERVED_GATED_PARAM_KEYS

    parameters = dict(payload.get("parameters") or {})
    gated = {k: v for k, v in parameters.items() if k in RESERVED_GATED_PARAM_KEYS}
    plain = {k: v for k, v in parameters.items() if k not in gated}

    resp = await client.post("/instinct/actions", json={**payload, "parameters": plain})
    if not gated or resp.status_code != 201:
        return resp

    store = _instinct_router._store(payload.get("workspace_id") or "")
    with sqlite3.connect(store._db_path) as db:
        db.execute(
            "UPDATE instinct_actions SET parameters = ? WHERE id = ?",
            (_json.dumps(parameters), resp.json()["id"]),
        )
    return resp


@pytest.fixture(autouse=True)
def _reset_paw_bar_public_ip_limiter():
    """Empty the paw-bar per-IP limiter before each test (see the module docstring).

    Looked up in ``sys.modules`` rather than imported, so tests that never load the
    EE router pay nothing and OSS-only runs do not need ``pocketpaw_ee``."""
    router = sys.modules.get("pocketpaw_ee.paw_bar.router")
    limiter = getattr(router, "_PUBLIC_IP_LIMITER", None)
    if limiter is not None:
        with limiter._lock:
            limiter._buckets.clear()
    yield


@pytest.fixture(autouse=True)
def scheduled_catalog_syncs(monkeypatch):
    """Record the background catalog syncs a knowledge sync schedules instead of
    running them, so no test leaves a site import (network) loose on the loop.
    Tests that care request this fixture and assert on the sites it holds; the
    sync itself is tested by calling it directly."""
    scheduled: list = []
    try:
        from pocketpaw_ee.paw_bar import catalog_sync
    except Exception:  # noqa: BLE001 — OSS-only install: nothing to stub
        yield scheduled
        return
    monkeypatch.setattr(catalog_sync, "_scheduler", scheduled.append)
    yield scheduled


_LIVEKIT_AGENT_MODULE = "pocketpaw_ee.cloud.livekit.agent"


@pytest.fixture(autouse=True)
def _refuse_real_livekit_agent(monkeypatch):
    """Refuse to spawn the real call-bot subprocess from any test.

    ``pocketpaw_ee.cloud.livekit.service._spawn_agent_process`` runs
    ``python -m pocketpaw_ee.cloud.livekit.agent``. Under pytest that child
    never exits (no room to join, nothing reaps it) and the suite hangs on it;
    on 2026-10-02 one such run held ``scripts/gate`` for 12 hours. The test
    must patch the spawn (see ``TestSpawnRace`` in
    ``tests/ee/test_livekit_service.py``); this guard makes forgetting that a
    failure instead of a hang.
    """
    real_exec = asyncio.create_subprocess_exec

    async def _guarded(*argv, **kwargs):
        if any(_LIVEKIT_AGENT_MODULE in str(a) for a in argv):
            raise RuntimeError(
                "test tried to spawn the real livekit call-bot "
                f"(`python -m {_LIVEKIT_AGENT_MODULE}`), which never exits under pytest "
                "and hangs the suite. Patch "
                "`pocketpaw_ee.cloud.livekit.service._spawn_agent_process` "
                "(and `_reap_agent_process`) in the test, as TestSpawnRace does."
            )
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _guarded)

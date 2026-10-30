# test_logfire_bridge.py — the Logfire wiring is off by default, and scrubbed when on.
# Created: 2026-09-05. Two properties, and the second is the reason the first can be
#   shipped before anyone decides on data residency:
#     1. With POCKETPAW_LOGFIRE_ENABLED unset, nothing is configured and no handler is
#        added. The process logs exactly as it did.
#     2. With it set, the Logfire handler carries the scrubbers. Logfire never scrubs a
#        log message itself — the formatted message lands in ``logfire.msg``, the first
#        entry in its SAFE_KEYS allowlist, and an exception lands in
#        ``exception.message`` / ``exception.stacktrace``, also SAFE_KEYS. So our
#        filters are the only thing in front of it, and a handler added AFTER
#        install_scrubbing_filters() would carry none.
#   The discriminating tests drive the REAL LogfireLoggingHandler with a fake Logfire
#   instance, so they assert on what Logfire would actually receive rather than on what
#   a StreamHandler prints.

from __future__ import annotations

import importlib.util
import io
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from pocketpaw.logging_setup import SecretFilter, install_scrubbing_filters

_FAKE_KEY = "sk-ant-" + "A" * 40

#: pydantic-ai is an EXTRA. CI runs an OSS-only install where it is absent by
#: design, and there ``_pydantic_ai_instrumentation`` raises and the integration
#: drops out — correctly. Tests that assert on what it passed have nothing to say
#: on that install.
_HAS_PYDANTIC_AI = importlib.util.find_spec("pydantic_ai") is not None
_needs_pydantic_ai = pytest.mark.skipif(
    not _HAS_PYDANTIC_AI, reason="pydantic-ai not installed (OSS-only install)"
)


class _FakeLogfireInstance:
    """Stands in for the Logfire instance the handler emits to."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def with_settings(self, **_: Any) -> _FakeLogfireInstance:
        return self

    def log(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def captured_root() -> Iterator[io.StringIO]:
    """Root logger with one readable handler, fully restored afterwards."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_filters = root.filters[:]

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    root.filters = []
    try:
        yield stream
    finally:
        root.handlers = saved_handlers
        root.level = saved_level
        root.filters = saved_filters


@pytest.fixture(autouse=True)
def reset_configured_flag() -> Iterator[None]:
    """``_CONFIGURED`` is process state; a leaked True makes later tests lie."""
    from pocketpaw import observability

    saved = observability._CONFIGURED
    observability._CONFIGURED = False
    try:
        yield
    finally:
        observability._CONFIGURED = saved


def _fake_logfire_module(calls: list[dict[str, Any]], instrumented: list[dict[str, Any]]):
    """A stand-in logfire module with the full surface we call.

    Deliberately not a bare ``SimpleNamespace(configure=...)``: a fake missing
    ``ScrubbingOptions`` would make the real call raise inside our own try/except,
    which warns and returns False — leaving Logfire silently unconfigured in
    production while the test stayed green. Same reason it carries every
    ``instrument_*`` name: the registry reads them all when it is built.
    """
    fake = _recording_logfire({})
    fake.configure = lambda **kw: calls.append(kw)
    fake.ScrubbingOptions = lambda **kw: SimpleNamespace(**kw)
    fake.instrument_pydantic_ai = lambda **kw: instrumented.append(kw)
    return fake


def test_nothing_is_wired_unless_the_flag_is_set(
    captured_root: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off by default is what makes this shippable before the residency decision.

    No configure, no handler, no telemetry. The process logs exactly as it did.
    """
    from pocketpaw import observability
    from pocketpaw.logging_setup import setup_logging

    monkeypatch.delenv("POCKETPAW_LOGFIRE_ENABLED", raising=False)
    configured: list[bool] = []
    monkeypatch.setattr(
        observability, "configure_observability", lambda: configured.append(True) or True
    )

    setup_logging(level="INFO")

    assert not configured, "logfire was configured with the master switch off"
    assert not any(
        type(h).__name__ == "LogfireLoggingHandler" for h in logging.getLogger().handlers
    ), "the bridge went on with the master switch off"


def test_the_off_switch_is_not_a_decoration(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bool(os.environ.get(...))`` makes the string "false" truthy.

    That is how an off switch turns out to be a label, so the parse is spelled out
    and tested rather than assumed.
    """
    from pocketpaw.observability import logfire_enabled

    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", value)
        assert logfire_enabled() is True, f"{value!r} should be on"
    for value in ("", "0", "false", "False", "no", "off", "maybe"):
        monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", value)
        assert logfire_enabled() is False, f"{value!r} should be off"


def test_the_bridge_is_added_alongside_the_console_handler(
    captured_root: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additive, not a replacement.

    Logfire's own docs say ``basicConfig(handlers=[LogfireLoggingHandler()])``,
    which REPLACES the root's handler list. That would delete the console handler
    and, more to the point, the handler the scrubbers are attached to.
    """
    pytest.importorskip("logfire")
    from pocketpaw import observability
    from pocketpaw.logging_setup import setup_logging

    monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", "true")
    monkeypatch.setattr(observability, "configure_observability", lambda: True)

    setup_logging(level="INFO")

    kinds = [type(h).__name__ for h in logging.getLogger().handlers]
    assert "LogfireLoggingHandler" in kinds, "the bridge was not installed"
    assert any(k != "LogfireLoggingHandler" for k in kinds), (
        "the bridge replaced the console handler instead of joining it"
    )


def test_the_bridge_handler_carries_the_scrubbers(
    captured_root: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ORDER GATE. The bridge goes on first, then the scrubbers are installed.

    ``install_scrubbing_filters()`` walks the root's handler list, so a handler
    added after it carries no filters. Reversing the two lines in
    ``_install_observability`` leaves the Logfire handler unscrubbed while every
    other test still passes, because the console handler's filter mutates the
    record in place before the bridge ever sees it.
    """
    pytest.importorskip("logfire")
    from pocketpaw import observability
    from pocketpaw.logging_setup import setup_logging

    monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", "true")
    monkeypatch.setattr(observability, "configure_observability", lambda: True)

    setup_logging(level="INFO")

    bridges = [
        h for h in logging.getLogger().handlers if type(h).__name__ == "LogfireLoggingHandler"
    ]
    assert bridges, "the bridge was not installed"
    assert any(isinstance(f, SecretFilter) for f in bridges[0].filters), (
        "the Logfire handler carries no scrubber, so it exports credentials in clear"
    )


def test_a_secret_in_args_reaches_logfire_redacted(captured_root: io.StringIO) -> None:
    """Driven through the REAL handler, asserting on what Logfire receives."""
    logging_integration = pytest.importorskip("logfire.integrations.logging")

    fake = _FakeLogfireInstance()
    handler = logging_integration.LogfireLoggingHandler(
        logfire_instance=fake, fallback=logging.NullHandler()
    )
    logging.getLogger().handlers = [handler]
    install_scrubbing_filters()

    logging.getLogger("pocketpaw.agents.provider").info("provider rejected %s", _FAKE_KEY)

    assert fake.calls, "the bridge emitted nothing"
    emitted = repr(fake.calls[0])
    assert _FAKE_KEY not in emitted, "the key reached Logfire in clear"
    assert "***REDACTED***" in emitted


def test_a_secret_in_a_traceback_reaches_logfire_redacted(captured_root: io.StringIO) -> None:
    """The bridge passes the RAW ``exc_info`` tuple and drops ``exc_text``.

    ``exc_text`` is in Logfire's RESERVED_ATTRS, so the scrubbed rendering never
    arrives; Logfire re-renders from the exception objects. Then
    ``exception.message`` and ``exception.stacktrace`` are SAFE_KEYS, so its own
    scrubber skips them. Scrubbing the exception OBJECT is what covers this, and
    this test is the only one that sees it from Logfire's side.
    """
    logging_integration = pytest.importorskip("logfire.integrations.logging")

    fake = _FakeLogfireInstance()
    handler = logging_integration.LogfireLoggingHandler(
        logfire_instance=fake, fallback=logging.NullHandler()
    )
    logging.getLogger().handlers = [handler]
    install_scrubbing_filters()

    log = logging.getLogger("pocketpaw.agents.provider")
    try:
        raise RuntimeError(f"upstream rejected token {_FAKE_KEY}")
    except RuntimeError:
        log.exception("provider call failed")

    assert fake.calls, "the bridge emitted nothing"
    exc_info = fake.calls[0]["exc_info"]
    assert exc_info is not None, "no exception reached Logfire; the test proves nothing"
    assert _FAKE_KEY not in str(exc_info[1]), "Logfire got the raw exception with the key in it"
    assert "***REDACTED***" in str(exc_info[1])


def test_configure_is_called_once_and_with_the_settings_that_matter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four kwargs each close a specific hole; none is decoration."""
    import sys

    from pocketpaw.observability import configure_observability

    calls: list[dict[str, Any]] = []
    instrumented: list[dict[str, Any]] = []
    monkeypatch.setitem(sys.modules, "logfire", _fake_logfire_module(calls, instrumented))
    monkeypatch.setenv("LOGFIRE_SERVICE_NAME", "pocketpaw-worker")
    monkeypatch.setenv("LOGFIRE_ENVIRONMENT", "prod")

    for _ in range(3):
        assert configure_observability() is True

    assert len(calls) == 1, f"logfire configured {len(calls)} times"
    kwargs = calls[0]
    assert kwargs["send_to_logfire"] == "if-token-present", "must be safe with no account"
    assert kwargs["console"] is False, "Logfire's console exporter would double every line"
    assert kwargs["distributed_tracing"] is False, (
        "the default EXTRACTS an incoming traceparent, so any caller could graft "
        "spans onto our traces on an internet-facing API"
    )
    assert kwargs["service_name"] == "pocketpaw-worker", (
        "backend and worker run the same image; a literal makes them one service"
    )
    assert kwargs["environment"] == "prod"


def test_the_extra_scrub_patterns_are_what_logfire_accepts() -> None:
    """``ScrubbingOptions.extra_patterns`` is ``Sequence[str]``, not compiled patterns.

    Passing the compiled objects raises inside logfire, where our own try/except
    swallows it and warns — leaving Logfire unconfigured in production while every
    mocked test stays green. So this one runs against the real class.
    """
    logfire = pytest.importorskip("logfire")

    from pocketpaw.observability import logfire_extra_scrub_patterns

    patterns = logfire_extra_scrub_patterns()
    assert patterns, "no credential patterns were exported to Logfire"
    assert all(isinstance(p, str) for p in patterns)
    logfire.ScrubbingOptions(extra_patterns=patterns)


def test_configure_passes_only_kwargs_real_logfire_accepts() -> None:
    """A typo'd kwarg is invisible against a mock that accepts ``**kw``."""
    logfire = pytest.importorskip("logfire")

    import inspect
    import sys

    from pocketpaw.observability import configure_observability

    calls: list[dict[str, Any]] = []
    instrumented: list[dict[str, Any]] = []
    saved = sys.modules["logfire"]
    sys.modules["logfire"] = _fake_logfire_module(calls, instrumented)
    try:
        configure_observability()
    finally:
        sys.modules["logfire"] = saved

    accepted = inspect.signature(logfire.configure).parameters
    unknown = [name for name in calls[0] if name not in accepted]
    assert not unknown, f"logfire.configure does not accept {unknown}"


@_needs_pydantic_ai
def test_agent_content_is_excluded_unless_it_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logfire's default is to send prompts and completions. This inverts it.

    Logfire does not scrub message content at all — every ``gen_ai.*`` message key
    is a SAFE_KEY — so leaving the default on would ship customer chat text to a
    hosted store as a side effect of turning on observability.
    """
    import sys

    from pocketpaw.observability import configure_observability

    calls: list[dict[str, Any]] = []
    instrumented: list[dict[str, Any]] = []
    monkeypatch.setitem(sys.modules, "logfire", _fake_logfire_module(calls, instrumented))
    monkeypatch.delenv("POCKETPAW_LOGFIRE_INCLUDE_CONTENT", raising=False)

    configure_observability()

    assert instrumented, "pydantic-ai was never instrumented"
    assert instrumented[0]["include_content"] is False, "customer prompts would leave the network"
    assert instrumented[0]["include_binary_content"] is False


@_needs_pydantic_ai
def test_agent_content_can_be_turned_on_deliberately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opting in is one env var, because debugging a bad run needs the prompt."""
    import sys

    from pocketpaw.observability import configure_observability

    calls: list[dict[str, Any]] = []
    instrumented: list[dict[str, Any]] = []
    monkeypatch.setitem(sys.modules, "logfire", _fake_logfire_module(calls, instrumented))
    monkeypatch.setenv("POCKETPAW_LOGFIRE_INCLUDE_CONTENT", "true")

    configure_observability()

    assert instrumented[0]["include_content"] is True


def test_a_broken_logfire_does_not_stop_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observability is never load-bearing.

    ``setup_logging`` runs at module scope in ``__main__``, so an exception here
    would stop the process booting rather than degrade its telemetry.
    """
    import sys

    from pocketpaw.observability import configure_observability

    def _boom(**_: Any) -> None:
        raise RuntimeError("no exporter")

    monkeypatch.setitem(
        sys.modules,
        "logfire",
        SimpleNamespace(configure=_boom, ScrubbingOptions=lambda **kw: SimpleNamespace(**kw)),
    )

    assert configure_observability() is False


def test_a_broken_bridge_does_not_stop_startup(
    captured_root: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule for the handler: a failure leaves the console logging intact."""
    import sys

    from pocketpaw.observability import install_logfire_bridge

    class _Boom:
        def __init__(self, **_: Any) -> None:
            raise RuntimeError("handler unavailable")

    monkeypatch.setitem(
        sys.modules, "logfire.integrations.logging", SimpleNamespace(LogfireLoggingHandler=_Boom)
    )

    assert install_logfire_bridge("INFO") is False
    assert logging.getLogger().handlers, "the console handler was lost"


# ---------------------------------------------------------------------------
# Coverage: which integrations attach, and what they are allowed to capture.
# ---------------------------------------------------------------------------


def _recording_logfire(calls: dict[str, Any]):
    """A logfire stand-in whose every instrument_* records the kwargs it got."""
    names = (
        "instrument_pydantic_ai",
        "instrument_claude_agent_sdk",
        "instrument_anthropic",
        "instrument_openai",
        "instrument_mcp",
        "instrument_httpx",
        "instrument_aiohttp_client",
        "instrument_redis",
        "instrument_pymongo",
        "instrument_system_metrics",
    )

    def recorder(name: str):
        def _call(**kwargs: Any) -> None:
            calls[name] = kwargs

        return _call

    return SimpleNamespace(**{name: recorder(name) for name in names})


def test_every_integration_in_the_default_set_attaches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is everything, and every entry has to actually be callable.

    A registry entry naming a function logfire does not have would be a silent
    hole: ``_instrument`` swallows the AttributeError and that integration is
    just quietly absent.

    ``_pydantic_ai_instrumentation`` is stubbed rather than run, because this
    asserts what the REGISTRY covers and pydantic-ai is an extra — otherwise the
    expected set would change with the install shape, and the OSS-only CI job
    would read a correct absence as a coverage regression.
    """
    import sys

    from pocketpaw import observability
    from pocketpaw.observability import instrument_everything

    calls: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "logfire", _recording_logfire(calls))
    monkeypatch.setattr(observability, "_pydantic_ai_instrumentation", lambda: None)
    monkeypatch.delenv("POCKETPAW_LOGFIRE_INSTRUMENT", raising=False)

    attached = instrument_everything()

    assert set(attached) == {
        "pydantic_ai",
        "claude_agent_sdk",
        "anthropic",
        "openai",
        "mcp",
        "httpx",
        "aiohttp_client",
        "pymongo",
        "system_metrics",
    }, f"the default coverage changed: {attached}"

    assert "redis" not in attached, (
        "redis is known but off by default: two arq lanes poll their queues on "
        "arq's timer, so an idle deployment emits roughly six command spans a "
        "second forever and buries the traces Logfire was turned on to read"
    )
    assert "redis" in observability._instrumentations(), (
        "it must stay SELECTABLE — POCKETPAW_LOGFIRE_INSTRUMENT is how someone "
        "debugging the queue reaches it for a session"
    )


def test_the_capture_flags_that_would_export_customer_data_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headers carry Authorization; httpx bodies are prompts; redis holds the job.

    Logfire's scrubber matches attribute NAMES against a keyword list, so a
    bearer token under a header name it does not recognise goes through
    untouched. These flags decide whether it ever gets the chance.
    """
    import sys

    from pocketpaw.observability import instrument_everything

    calls: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "logfire", _recording_logfire(calls))
    # redis is off by default, and its capture flag still has to be right for
    # the session someone turns it on for.
    monkeypatch.setenv("POCKETPAW_LOGFIRE_INSTRUMENT", "httpx,redis")

    instrument_everything()

    assert calls["instrument_httpx"]["capture_headers"] is False
    assert "capture_request_body" not in calls["instrument_httpx"], (
        "leave it at logfire's own default of False rather than setting it here"
    )
    assert calls["instrument_redis"]["capture_statement"] is False, (
        "the redis statement carries the arq job payload, which is the conversation"
    )


def test_the_instrument_list_can_be_trimmed_and_emptied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spans are metered, so dropping one integration must not need a code deploy."""
    import sys

    from pocketpaw.observability import instrument_everything

    calls: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "logfire", _recording_logfire(calls))

    # Two integrations that need no extra, so this measures the SELECTION and not
    # which optional packages the install happens to carry.
    monkeypatch.setenv("POCKETPAW_LOGFIRE_INSTRUMENT", "httpx, redis")
    assert set(instrument_everything()) == {"httpx", "redis"}

    calls.clear()
    monkeypatch.setenv("POCKETPAW_LOGFIRE_INSTRUMENT", "none")
    assert instrument_everything() == []
    assert not calls


def test_an_unknown_integration_name_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo in the env var must not read as "that integration is off"."""
    import sys

    from pocketpaw.observability import instrument_everything

    calls: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "logfire", _recording_logfire(calls))
    monkeypatch.setenv("POCKETPAW_LOGFIRE_INSTRUMENT", "htpx,redis")

    with caplog.at_level(logging.WARNING, logger="pocketpaw.observability"):
        attached = instrument_everything()

    assert attached == ["redis"]
    assert any("htpx" in record.getMessage() for record in caplog.records), (
        "a misspelled integration was skipped silently"
    )


def test_one_broken_integration_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instrumentors are optional packages, so a missing one is expected.

    An install without them should lose spans, never fail to boot.
    """
    import sys

    from pocketpaw.observability import instrument_everything

    calls: dict[str, Any] = {}
    fake = _recording_logfire(calls)

    def _boom(**_: Any) -> None:
        raise RuntimeError("requires the opentelemetry-instrumentation-httpx package")

    fake.instrument_httpx = _boom
    monkeypatch.setitem(sys.modules, "logfire", fake)
    monkeypatch.delenv("POCKETPAW_LOGFIRE_INSTRUMENT", raising=False)

    attached = instrument_everything()

    assert "httpx" not in attached
    # A witness that comes AFTER httpx in the registry, so it can only be here
    # if the loop carried on past the failure rather than stopping at it.
    assert "pymongo" in attached, "a missing optional package took the rest down with it"


def test_fastapi_is_not_instrumented_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both app builders call this unconditionally, so the gate lives here."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI

    from pocketpaw.observability import instrument_fastapi_app

    monkeypatch.delenv("POCKETPAW_LOGFIRE_ENABLED", raising=False)

    assert instrument_fastapi_app(FastAPI()) is False


def test_a_request_becomes_a_span_named_by_route_not_by_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end one, against real logfire and a real request.

    Two properties, and both fail quietly rather than loudly:

    * The span is named by route TEMPLATE. Instrument before the routers are
      mounted and there is no route table to match against, so every pocket id
      becomes its own span name and the dashboard is unusable.
    * ``/api/v1/version`` produces NO span. The compose healthcheck curls it
      every 30 seconds in both containers, which is thousands of metered spans a
      day that say nothing a container status does not.
    """
    logfire = pytest.importorskip("logfire")
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    testing = pytest.importorskip("logfire.testing")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from pocketpaw.observability import instrument_fastapi_app

    exporter = testing.TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", "true")

    app = FastAPI()

    @app.get("/api/v1/pockets/{pocket_id}")
    def read_pocket(pocket_id: str) -> dict[str, str]:
        return {"id": pocket_id}

    @app.get("/api/v1/version")
    def version() -> dict[str, str]:
        return {"version": "1.0.0"}

    assert instrument_fastapi_app(app) is True

    client = TestClient(app)
    client.get("/api/v1/pockets/abc123")
    client.get("/api/v1/version")

    names = [span.name for span in exporter.exported_spans]
    assert names, "the request produced no span at all"
    assert all("{pocket_id}" in name for name in names), (
        f"spans are named by path, not by route template: {names}"
    )
    assert not any("version" in name for name in names), (
        f"the healthcheck route was traced: {names}"
    )


def test_the_api_app_is_instrumented_after_every_router_is_mounted() -> None:
    """Ordering inside ``create_api_app``, checked structurally.

    The route-template test above proves the instrumentor uses templates when it
    has a route table. This proves it is given one: instrument before
    ``mount_v1_routers`` and there are no routes to match, so every pocket id
    becomes its own span name. That failure produces spans, so nothing else
    catches it — it just makes the dashboard useless.

    By AST over the function body rather than by substring, because the file
    mentions both names in prose.
    """
    import ast
    import inspect
    import textwrap

    from pocketpaw.api import serve

    tree = ast.parse(textwrap.dedent(inspect.getsource(serve.create_api_app)))
    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in {"instrument_fastapi_app", "mount_v1_routers", "install_cors"}:
                called.append(name)

    assert "instrument_fastapi_app" in called, (
        "the deployed API app is never instrumented, so requests produce no spans"
    )
    assert called.index("instrument_fastapi_app") > called.index("mount_v1_routers"), (
        "instrumented before the routers were mounted; spans would be named by path"
    )


# ---------------------------------------------------------------------------
# The test process must not inherit a real Logfire configuration.
# ---------------------------------------------------------------------------


def test_a_developers_own_logfire_config_never_reaches_a_test_run() -> None:
    """A real project token must not be live while the suite runs.

    ``security/url_validators.py`` calls ``load_dotenv()`` at import, and
    python-dotenv walks UP from the calling file, so from a git worktree it
    reaches the PARENT checkout's ``.env``. That is how an operator's real token
    arrives in a process that never asked for one. Measured, not theorised: it
    reproduced on this machine the moment a token was put in that file.

    ``logfire.configure`` is process-global with no un-configure, so a single
    test that reached ``setup_logging`` before any fixture ran would arm
    exporting for the entire session, and with content capture on what ships is
    fixture text. ``tests/conftest.py`` pops these at import for that reason.
    """
    import os

    from pocketpaw.observability import logfire_enabled, logfire_include_content

    assert not logfire_enabled(), (
        "the master switch is on during the test run, so this suite can "
        "configure Logfire and export to a real project. See tests/conftest.py."
    )
    assert not logfire_include_content(), "content capture is on during the test run"
    assert not os.environ.get("LOGFIRE_TOKEN"), (
        "a Logfire token is live during the test run, so anything that does "
        "configure will export to a real project"
    )


def test_setup_logging_is_inert_when_the_environment_is_clean() -> None:
    """The consequence of the guard above, asserted end to end.

    Not a restatement: this drives the real ``setup_logging`` and checks that no
    bridge handler appears. It is what would actually have shipped test noise.
    """
    import logging

    from pocketpaw.logging_setup import setup_logging

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        setup_logging(level="INFO")
        assert not any(
            type(h).__name__ == "LogfireLoggingHandler" for h in logging.getLogger().handlers
        ), "a test run installed the Logfire bridge on the root logger"
    finally:
        root.handlers = saved_handlers
        root.level = saved_level

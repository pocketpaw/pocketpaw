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

import io
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from pocketpaw.logging_setup import SecretFilter, install_scrubbing_filters

_FAKE_KEY = "sk-ant-" + "A" * 40


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
    production while the test stayed green.
    """
    return SimpleNamespace(
        configure=lambda **kw: calls.append(kw),
        ScrubbingOptions=lambda **kw: SimpleNamespace(**kw),
        instrument_pydantic_ai=lambda **kw: instrumented.append(kw),
    )


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

# test_log_scrubbing.py — the secret / PII log filters actually scrub.
# Created: 2026-09-05 — the filters were attached to the root LOGGER, so they
#   never ran for any record from a child logger, which is every module in the
#   codebase. These tests log the way real code logs (getLogger(__name__)) and
#   assert on what reaches the handler's stream, so a filter that is installed
#   but never consulted fails them.

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from pocketpaw.logging_setup import SecretFilter, install_scrubbing_filters

_FAKE_KEY = "sk-ant-" + "A" * 40


@pytest.fixture
def captured_root() -> Iterator[io.StringIO]:
    """Root logger with exactly one handler we can read, restored afterwards.

    The root's handler list and level are process state that pytest's own
    logging plugin also touches, so both are saved and put back.
    """
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


def test_a_child_logger_record_is_scrubbed(captured_root: io.StringIO) -> None:
    """The regression test for the bug: real modules log through a child logger.

    A filter on the root LOGGER is not consulted for these records — the record
    propagates to the root's HANDLERS, and a handler applies only its own
    filters. This is the assertion that was missing.
    """
    install_scrubbing_filters()

    logging.getLogger("pocketpaw.agents.some_backend").info(
        "provider rejected the key %s", _FAKE_KEY
    )

    out = captured_root.getvalue()
    assert _FAKE_KEY not in out, "a child logger's record reached the handler unscrubbed"
    assert "***REDACTED***" in out


def test_a_secret_inside_a_traceback_is_scrubbed(captured_root: io.StringIO) -> None:
    """``logger.exception`` is used 281 times here and carries the traceback.

    The filter used to touch only ``record.msg`` and ``record.args``, so a
    provider SDK that put a key in an exception message wrote it to the log in
    clear. That is the exact scenario the worker's logging setup exists for.
    """
    install_scrubbing_filters()

    log = logging.getLogger("pocketpaw.agents.provider")
    try:
        raise RuntimeError(f"upstream rejected token {_FAKE_KEY}")
    except RuntimeError:
        log.exception("provider call failed")

    out = captured_root.getvalue()
    assert "Traceback" in out, "the traceback was not rendered at all; test proves nothing"
    assert _FAKE_KEY not in out, "the key reached the log through exc_info"
    assert "***REDACTED***" in out


def test_the_exception_object_itself_is_scrubbed(captured_root: io.StringIO) -> None:
    """Renderers disagree about where they read the traceback from.

    A stdlib ``Formatter`` reuses ``record.exc_text``; ``RichHandler``
    re-renders from ``exc_info`` and ignores it; Logfire's logging handler
    passes the raw ``exc_info`` tuple and drops ``exc_text`` as a reserved
    attribute, and its own scrubber skips exception keys as SAFE_KEYS. So
    scrubbing the rendered text alone protects only one of the three. The
    exception object is the one thing all of them read.
    """
    install_scrubbing_filters()

    log = logging.getLogger("pocketpaw.agents.provider")
    caught: BaseException | None = None
    try:
        raise RuntimeError(f"upstream rejected token {_FAKE_KEY}")
    except RuntimeError as exc:
        caught = exc
        log.exception("provider call failed")

    assert caught is not None
    assert _FAKE_KEY not in str(caught), "the exception still carries the key after logging"
    assert "***REDACTED***" in str(caught)


def test_a_second_handler_without_the_filter_still_sees_a_redacted_exception(
    captured_root: io.StringIO,
) -> None:
    """This is the property the Logfire bridge depends on.

    Filters are per-handler, so a handler added without one is unprotected by
    anything attached elsewhere. Because the scrub rewrites the shared exception
    object rather than a per-record copy, a later handler reading ``exc_info``
    sees the redacted text regardless. That is what keeps a bridge handler safe
    when it renders the traceback itself.
    """
    install_scrubbing_filters()

    # A second handler, deliberately WITHOUT the filter, standing in for a
    # bridge handler that re-renders from exc_info.
    second = io.StringIO()
    bridge = logging.StreamHandler(second)
    bridge.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(bridge)
    try:
        log = logging.getLogger("pocketpaw.agents.provider")
        try:
            raise RuntimeError(f"upstream rejected token {_FAKE_KEY}")
        except RuntimeError:
            log.exception("provider call failed")
    finally:
        logging.getLogger().removeHandler(bridge)

    assert _FAKE_KEY not in second.getvalue(), (
        "an unfiltered handler rendered the key; the exception object was not scrubbed"
    )


def test_an_exception_without_a_secret_is_left_alone(captured_root: io.StringIO) -> None:
    """Only rewrite args when a pattern matched.

    Code that inspects an exception after logging it must see exactly what it
    raised, so the scrub must be a no-op on ordinary exceptions.
    """
    install_scrubbing_filters()

    log = logging.getLogger("pocketpaw.agents.provider")
    original = ("connection refused", 42)
    caught: BaseException | None = None
    try:
        raise RuntimeError(*original)
    except RuntimeError as exc:
        caught = exc
        log.exception("provider call failed")

    assert caught is not None
    assert caught.args == original, "an exception with no secret in it was rewritten"


def test_a_non_tty_does_not_get_the_rich_handler() -> None:
    """Rich renders tracebacks from exc_info and ignores the scrubbed exc_text.

    On a TTY that is a developer's own screen. In a container it is the log
    collector, so the plain handler is what keeps the traceback scrubbed there.
    """
    import sys
    from unittest.mock import patch

    from pocketpaw.logging_setup import _use_rich

    class _Stream:
        def __init__(self, tty: bool) -> None:
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    with patch.object(sys, "stderr", _Stream(tty=False)):
        assert _use_rich() is False, "a non-TTY must not select Rich"
    with patch.object(sys, "stderr", _Stream(tty=True)):
        assert _use_rich() is True, "a TTY should still get Rich for humans"


def test_the_secret_is_scrubbed_when_it_is_in_the_message_itself(
    captured_root: io.StringIO,
) -> None:
    """The filter rewrites ``record.msg`` as well as ``record.args``.

    Both paths matter: ``logger.info("... %s", key)`` puts it in args, and an
    f-string or a formatted exception message puts it in msg.
    """
    install_scrubbing_filters()

    logging.getLogger("pocketpaw.cloud.worker").error(f"boom, using {_FAKE_KEY} upstream")

    out = captured_root.getvalue()
    assert _FAKE_KEY not in out
    assert "***REDACTED***" in out


def test_installing_twice_does_not_stack_duplicate_filters(
    captured_root: io.StringIO,
) -> None:
    """Idempotent by filter type, so a re-init cannot pile up scrubbers."""
    install_scrubbing_filters()
    install_scrubbing_filters()
    install_scrubbing_filters()

    handler = logging.getLogger().handlers[0]
    secret_filters = [f for f in handler.filters if isinstance(f, SecretFilter)]
    assert len(secret_filters) == 1


def test_setup_logging_leaves_the_handler_carrying_a_scrubber(
    captured_root: io.StringIO,
) -> None:
    """``setup_logging`` must end with the filters on the handlers.

    This is the pairing the worker depends on: the entrypoint calls
    setup_logging and gets scrubbing as a consequence.
    """
    from pocketpaw.logging_setup import setup_logging

    setup_logging(level="INFO")

    handlers = logging.getLogger().handlers
    assert handlers, "setup_logging installed no handler"
    assert any(any(isinstance(f, SecretFilter) for f in h.filters) for h in handlers), (
        "no handler carries a SecretFilter, so nothing is scrubbed"
    )


def test_the_worker_supervisor_calls_setup_logging_not_basicconfig() -> None:
    """The worker entrypoint must go through setup_logging.

    The worker is the process that spends money and whose provider SDKs surface
    credentials in exception text. ``basicConfig`` gives the root a handler and
    installs no filters, which is what this used to call.

    Checked by AST over the *calls* in ``main``, not by substring: the source
    text mentions basicConfig in a comment explaining why it is not used.
    """
    import ast
    import inspect
    import textwrap

    from pocketpaw_ee.cloud import worker_supervisor

    tree = ast.parse(textwrap.dedent(inspect.getsource(worker_supervisor.main)))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }

    assert "setup_logging" in called, "the worker supervisor no longer installs scrubbing"
    assert "basicConfig" not in called, "basicConfig installs no filters; it cannot scrub"

"""
Beautiful logging setup using Rich.

Created: 2026-02-02
Changes:
  - 2026-09-05: SecretFilter now scrubs the EXCEPTION and its traceback, not
    just the message and args. A provider SDK that puts a key in an exception
    reached the log through exc_info, which the filter ignored entirely, and
    logger.exception is used 281 times in this codebase. It rewrites the
    exception's args, because renderers disagree about where they read the
    traceback from: Formatter reuses record.exc_text, RichHandler re-renders
    from exc_info, and Logfire's logging handler passes the raw exc_info tuple
    while dropping exc_text as a reserved attribute. The exception object is the
    one thing all of them read. Args are only rewritten when a pattern actually
    matched, so an exception with no secret is left exactly as raised.
    Separately, Rich is now used only on a TTY — a log-format decision, not a
    security one: the previous non-TTY fallback emitted no timestamp, level or
    logger name, so a container line could not be correlated with a report.
  - 2026-09-05: Both filters are now attached to the root logger's HANDLERS
    rather than to the root logger. They were attached to the logger, which
    meant they never ran: a logger's filters apply only to records logged
    directly on it, and every module here logs via getLogger(__name__), whose
    records reach the root's handlers without consulting the root's filters. So
    no log line has been scrubbed in any process since the filters were added.
    Extracted install_scrubbing_filters() so a process that configures its own
    logging (the cloud worker supervisor) can install them too.
  - 2026-02-06: Added SecretFilter to scrub API key patterns from log output.
  - 2026-02-16: Added PIILogFilter for opt-in PII scrubbing in log output.
  - Initial setup with Rich console handler for beautiful logs.
"""

import logging
import re
import sys
import traceback
from collections.abc import Callable, Mapping
from typing import Any

# Patterns that match known API key / token formats
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[a-zA-Z0-9_-]+"),  # Anthropic
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),  # OpenAI
    re.compile(r"xoxb-[a-zA-Z0-9_-]+"),  # Slack bot
    re.compile(r"xapp-[a-zA-Z0-9_-]+"),  # Slack app
    re.compile(r"\b\d+:AA[a-zA-Z0-9_-]{30,}"),  # Telegram bot token
    re.compile(r"pp_[a-zA-Z0-9_-]{20,}"),  # PocketPaw API key
    re.compile(r"ppat_[a-zA-Z0-9_-]{20,}"),  # PocketPaw OAuth access token
    re.compile(r"pprt_[a-zA-Z0-9_-]{20,}"),  # PocketPaw OAuth refresh token
]


def _scrub(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***REDACTED***", text)
    return text


def scrub_log_args(args: Any, scrub: Callable[[str], str]) -> Any:
    """Scrub a ``LogRecord.args`` **preserving its shape**.

    ``record.args`` is a tuple, or a Mapping when the caller used named
    placeholders: ``logger.info("%(user)s", {"user": name})``. logging unwraps a
    single mapping argument, so the attribute is the dict itself.

    Rebuilding that as a tuple is not a cosmetic error. ``getMessage`` does
    ``msg % self.args``, and ``"%(user)s" % ({...},)`` raises "format requires a
    mapping" inside logging, which swallows it and **drops the record entirely**.
    So a filter that mishandles this does not merely fail to scrub, it destroys
    the log line — including records from third-party libraries, since these
    filters sit on the root handler.
    """
    if not args:
        return args
    if isinstance(args, Mapping):
        return {k: scrub(v) if isinstance(v, str) else v for k, v in args.items()}
    items = args if isinstance(args, tuple) else (args,)
    return tuple(scrub(a) if isinstance(a, str) else a for a in items)


def _scrub_args(args: Any) -> Any:
    return scrub_log_args(args, _scrub)


class SecretFilter(logging.Filter):
    """Scrub API key patterns from log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        # The traceback, not just the message. A provider SDK that puts a key in
        # an exception reaches the log through ``exc_info``, which this used to
        # ignore entirely — and ``logger.exception`` is used 281 times here.
        #
        # Scrub the EXCEPTION OBJECT first, then the rendered text. Rewriting
        # ``args`` is the only thing every renderer sees, because they do not
        # agree on where to read the traceback from: ``Formatter`` reuses
        # ``record.exc_text``, ``RichHandler`` re-renders from ``exc_info`` and
        # ignores ``exc_text``, and Logfire's logging handler passes the raw
        # ``exc_info`` tuple while dropping ``exc_text`` as a reserved
        # attribute — and its own scrubber skips ``exception.message`` and
        # ``exception.stacktrace`` as SAFE_KEYS, so nothing downstream would
        # catch it either. Scrubbing at the source covers all three.
        #
        # ``args`` is only reassigned when a pattern actually matched, so an
        # exception carrying no secret is left untouched and code that inspects
        # it after logging sees exactly what it raised.
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            original = getattr(exc, "args", ())
            if original:
                scrubbed = tuple(_scrub(a) if isinstance(a, str) else a for a in original)
                if scrubbed != original:
                    exc.args = scrubbed
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = _scrub(record.exc_text)
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        record.args = _scrub_args(record.args)
        return True


def _build_pii_filter() -> logging.Filter | None:
    """The PII scrubbing filter, or ``None`` when it is off or unavailable.

    Returns ``None`` rather than raising during early bootstrap, when settings
    are not loadable yet.
    """
    try:
        from pocketpaw.config import get_settings

        settings = get_settings()
        if not (settings.pii_scan_enabled and settings.pii_scan_logs):
            return None

        from pocketpaw.security.pii import PIIAction, PIIScanner

        scanner = PIIScanner(default_action=PIIAction.MASK)

        def _mask(text: str) -> str:
            result = scanner.scan(text)
            return result.sanitized_text if result.has_pii else text

        class PIILogFilter(logging.Filter):
            """Scrub PII patterns from log output."""

            def filter(self, record: logging.LogRecord) -> bool:
                if isinstance(record.msg, str):
                    record.msg = _mask(record.msg)
                # Shape-preserving, for the same reason as SecretFilter: turning a
                # mapping into a tuple makes logging raise and drop the record.
                record.args = scrub_log_args(record.args, _mask)
                return True

        return PIILogFilter()
    except Exception:
        return None  # Config not available during early bootstrap


def install_scrubbing_filters() -> None:
    """Attach the secret and PII filters to the root logger's HANDLERS.

    Not to the root logger itself, which is what this used to do and why the
    scrubbing never ran. A filter attached to a logger is consulted only for
    records logged *directly on that logger*: ``Logger.handle`` applies the
    logger's own filters, then ``callHandlers`` walks the hierarchy invoking each
    ancestor's *handlers*, and a handler applies only its own filters. Every
    module here logs through ``logging.getLogger(__name__)``, so its records
    reach the root's handlers without the root logger's filters ever running.

    Attaching to the handlers is what actually scrubs. Idempotent by filter type
    so repeated calls (tests, a re-init) do not stack duplicates.
    """
    filters: list[logging.Filter] = [SecretFilter()]
    pii = _build_pii_filter()
    if pii is not None:
        filters.append(pii)

    for handler in logging.getLogger().handlers:
        installed = {type(existing) for existing in handler.filters}
        for scrubber in filters:
            if type(scrubber) not in installed:
                handler.addFilter(scrubber)


def _use_rich() -> bool:
    """Rich only for a human at a terminal.

    The reason is log format, not scrubbing. Rich's box drawing and ANSI are
    noise in a container's log collector, and a line with no timestamp, level or
    logger name cannot be correlated with a customer report — which is what the
    previous fallback produced.

    Scrubbing is handled at the source instead, by rewriting the exception's
    ``args`` in ``SecretFilter``, because ``RichHandler`` re-renders tracebacks
    from ``exc_info`` and ignores ``record.exc_text``. That makes Rich safe
    either way; this function is about readability for machines.
    """
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


def setup_logging(level: str = "INFO") -> None:
    """Configure logging: Rich for a terminal, a plain scrubbed handler otherwise.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
    """
    if not _use_rich():
        # No force=True. It removes and CLOSES every handler already on the root
        # logger, which under pytest is the capture handler other tests assert
        # against — it changed what unrelated suites recorded. Without it this is
        # a no-op when logging is already configured, and install_scrubbing_filters
        # below then attaches the scrubbers to whatever handlers are actually
        # there, which is the behaviour we want either way.
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            handlers=[logging.StreamHandler(sys.stderr)],
        )
        for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "websockets"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        install_scrubbing_filters()
        return

    try:
        from rich.console import Console
        from rich.logging import RichHandler

        # Create console for rich output
        console = Console(stderr=True)

        # Configure root logger with Rich handler
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(message)s",
            datefmt="[%X]",
            handlers=[
                RichHandler(
                    console=console,
                    show_time=True,
                    show_path=False,  # Cleaner output
                    rich_tracebacks=True,
                    tracebacks_show_locals=False,
                    markup=True,
                )
            ],
        )

        # Reduce noise from third-party libraries
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)

    except ImportError:
        # Fallback to basic logging if rich not installed
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler(sys.stderr)],
        )
        logging.warning("Rich not installed, using basic logging")

    install_scrubbing_filters()

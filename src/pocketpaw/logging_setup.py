"""
Beautiful logging setup using Rich.

Created: 2026-02-02
Changes:
  - 2026-09-05: SecretFilter now scrubs the TRACEBACK too, not just the message
    and args. A provider SDK that puts a key in an exception reached the log
    through exc_info, which the filter ignored entirely, and logger.exception is
    used 281 times in this codebase. Rich is now used only when stderr is a TTY:
    RichHandler renders tracebacks from exc_info and ignores record.exc_text, so
    it would discard the scrubbed traceback and print the raw one. The non-TTY
    handler also carries a timestamp, level and logger name, which the
    lastResort fallback did not.
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


class SecretFilter(logging.Filter):
    """Scrub API key patterns from log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        # The traceback, not just the message. A provider SDK that puts a key in
        # an exception reaches the log through ``exc_info``, which this used to
        # ignore entirely — and ``logger.exception`` is used 281 times here.
        #
        # ``Formatter.format`` renders the traceback only when ``exc_text`` is
        # empty and reuses it when set, so filling it in with a scrubbed render
        # is what actually reaches the handler. This is also why setup_logging
        # only uses Rich on a TTY: RichHandler renders from ``exc_info`` itself
        # and ignores ``exc_text``, so in a container it would print the key.
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = _scrub(record.exc_text)
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            new_args = []
            for arg in args:
                if isinstance(arg, str):
                    arg = _scrub(arg)
                new_args.append(arg)
            record.args = tuple(new_args)
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

        class PIILogFilter(logging.Filter):
            """Scrub PII patterns from log output."""

            def filter(self, record: logging.LogRecord) -> bool:
                if isinstance(record.msg, str):
                    result = scanner.scan(record.msg)
                    if result.has_pii:
                        record.msg = result.sanitized_text
                if record.args:
                    args = record.args if isinstance(record.args, tuple) else (record.args,)
                    new_args = []
                    for arg in args:
                        if isinstance(arg, str):
                            r = scanner.scan(arg)
                            if r.has_pii:
                                arg = r.sanitized_text
                        new_args.append(arg)
                    record.args = tuple(new_args)
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

    Two reasons, and the second is a security one. Rich's box drawing and ANSI
    are noise in a container's log collector, and `%(asctime)s` with the logger
    name is what makes a line correlatable with a customer report. More
    importantly ``RichHandler`` renders tracebacks from ``exc_info`` itself and
    ignores ``record.exc_text``, so the scrubbed traceback ``SecretFilter``
    prepares would be discarded and the raw one printed. On a TTY that is a
    developer's own screen; in production it is the log collector.
    """
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


def setup_logging(level: str = "INFO") -> None:
    """Configure logging: Rich for a terminal, a plain scrubbed handler otherwise.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
    """
    if not _use_rich():
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            handlers=[logging.StreamHandler(sys.stderr)],
            force=True,
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

"""
Beautiful logging setup using Rich.

Created: 2026-02-02
Changes:
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


class SecretFilter(logging.Filter):
    """Scrub API key patterns from log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern in _SECRET_PATTERNS:
                record.msg = pattern.sub("***REDACTED***", record.msg)
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            new_args = []
            for arg in args:
                if isinstance(arg, str):
                    for pattern in _SECRET_PATTERNS:
                        arg = pattern.sub("***REDACTED***", arg)
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


def setup_logging(level: str = "INFO") -> None:
    """Configure beautiful logging with Rich.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
    """
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

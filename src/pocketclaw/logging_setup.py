"""
Beautiful logging setup using Rich.

Created: 2026-02-02
Changes:
  - 2026-02-06: Added SecretFilter to scrub API key patterns from log output.
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

    # Attach secret scrubbing filter to root logger
    logging.getLogger().addFilter(SecretFilter())

# Shared ANSI colors and output helpers used by all CLI commands.

import json
import sys

# ANSI helpers
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# Mask secrets that match these patterns
_SECRET_SUBSTRINGS = ("key", "token", "secret", "password")


def print_header(title: str, subtitle: str = "") -> None:
    """Print a styled CLI header."""
    print(f"\n  {BOLD}{title}{RESET}")
    if subtitle:
        print(f"  {DIM}{subtitle}{RESET}")
    print()


def print_row(label: str, value: str, indent: int = 2) -> None:
    """Print a key-value row."""
    pad = " " * indent
    print(f"{pad}{label:<24} {value}")


def print_ok(msg: str) -> None:
    print(f"  {GREEN}[OK]{RESET}   {msg}")


def print_warn(msg: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def print_fail(msg: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")


def output_json(data: object) -> None:
    """Print JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


def is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def mask_value(key: str, value: str) -> str:
    """Mask sensitive config values, showing only first 4 and last 4 chars."""
    if not value or not isinstance(value, str):
        return str(value) if value else ""
    is_secret = any(s in key.lower() for s in _SECRET_SUBSTRINGS)
    if is_secret and len(value) > 12:
        return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
    if is_secret and value:
        return "****"
    return value

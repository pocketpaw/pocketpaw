"""Optional dependency helpers."""


def require_extra(package: str, extra: str) -> None:
    """Raise ImportError with install instructions for a missing optional dependency."""
    raise ImportError(
        f"'{package}' is required but not installed. "
        f"Install it with: pip install 'pocketpaw[{extra}]'"
    )

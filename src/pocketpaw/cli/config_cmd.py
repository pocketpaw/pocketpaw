# CLI config command - show, set, validate config.
# Named config_cmd.py to avoid shadowing the pocketpaw.config module.

from __future__ import annotations

from pocketpaw.cli.utils import (
    BOLD,
    DIM,
    GREEN,
    RESET,
    mask_value,
    output_json,
    print_fail,
    print_header,
    print_ok,
    print_warn,
)


def run_config_cmd(
    action: str | None = None,
    key: str | None = None,
    value: str | None = None,
    as_json: bool = False,
) -> int:
    """Manage PocketPaw config.

    - No action: show current config (secrets masked)
    - set <key> <value>: set a config field
    - validate: validate API keys and settings
    - path: print config file path
    """
    if action == "set":
        return _set_config(key, value)
    if action == "validate":
        return _validate_config(as_json)
    if action == "path":
        return _show_path()
    return _show_config(as_json)


def _show_config(as_json: bool) -> int:
    from pocketpaw.config import get_settings

    settings = get_settings()
    data = settings.model_dump()

    if as_json:
        # Mask secrets in JSON output too
        masked = {k: mask_value(k, str(v)) if v else v for k, v in data.items()}
        output_json(masked)
        return 0

    print_header("Configuration")

    # Group by prefix for readability
    groups: dict[str, list[tuple[str, str]]] = {}
    for k, v in sorted(data.items()):
        if v is None or v == "" or v == [] or v == {}:
            continue
        # Skip internal/computed fields
        if k.startswith("_"):
            continue
        prefix = k.split("_")[0] if "_" in k else "general"
        masked = mask_value(k, str(v))
        groups.setdefault(prefix, []).append((k, masked))

    for group, items in groups.items():
        print(f"  {BOLD}{group}{RESET}")
        for k, v in items:
            display_v = v if len(v) < 80 else v[:77] + "..."
            print(f"    {k:<36} {DIM}{display_v}{RESET}")
        print()

    return 0


def _set_config(key: str | None, value: str | None) -> int:
    if not key or value is None:
        print_fail("Usage: pocketpaw config set <key> <value>")
        return 1

    from pocketpaw.config import Settings

    settings = Settings.load()

    if not hasattr(settings, key):
        print_fail(f"Unknown config key: '{key}'")
        print(f"  {DIM}Run 'pocketpaw config' to see available keys.{RESET}")
        return 1

    # Coerce value to the right type
    current = getattr(settings, key)
    try:
        if isinstance(current, bool):
            coerced = value.lower() in ("true", "1", "yes")
        elif isinstance(current, int):
            coerced = int(value)
        elif isinstance(current, float):
            coerced = float(value)
        elif isinstance(current, list):
            # Comma-separated list
            coerced = [v.strip() for v in value.split(",") if v.strip()]
        else:
            coerced = value
    except (ValueError, TypeError) as e:
        print_fail(f"Invalid value for '{key}': {e}")
        return 1

    setattr(settings, key, coerced)
    settings.save()

    display = mask_value(key, str(coerced))
    print(f"  {GREEN}Set{RESET} {BOLD}{key}{RESET} = {display}")
    return 0


def _validate_config(as_json: bool) -> int:
    from pocketpaw.config import Settings, validate_api_keys

    settings = Settings.load()
    warnings = validate_api_keys(settings)

    if as_json:
        output_json({"valid": len(warnings) == 0, "warnings": warnings})
        return 0 if not warnings else 1

    print_header("Config Validation")

    if not warnings:
        print_ok("All API keys and settings look good.")
    else:
        for w in warnings:
            print_warn(w)

    print()
    return 0 if not warnings else 1


def _show_path() -> int:
    from pocketpaw.config import get_config_path

    print(get_config_path())
    return 0

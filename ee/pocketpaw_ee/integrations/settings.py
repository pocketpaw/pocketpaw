# ee/pocketpaw_ee/integrations/settings.py: per-site settings checked against a
# provider's ``settings_fields``.
#
#   merge_put(provider, incoming, current)  the PUT body's ``settings`` -> stored dict
#   missing_required(provider, settings)    required keys with no valid value
#   field_views(provider)                   the ``settings_fields`` wire list
#   visible(provider, settings)             stored settings minus hidden fields
#
# PUT rules: an unknown key is a 422 ``invalid_settings``; a readonly (server-set)
# key from a client is ignored and the stored value kept; "" or null clears an
# editable key; values are strings. ``url`` must be http(s) with a host, and one of
# the field's ``allowed_hosts`` exactly when it lists any. ``select`` must be one of
# the options (comma-separated for a ``multi`` field, normalised to "a,b"). Every
# value is capped at the field's ``max_length``.

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.integrations.providers.base import IntegrationProvider, SettingsField

INVALID = "invalid_settings"
MAX_URL = 2048


def _fields(provider: IntegrationProvider) -> dict[str, SettingsField]:
    return {f.key: f for f in getattr(provider, "settings_fields", ()) or ()}


def check_value(spec: SettingsField, value: Any) -> str:
    """The normalised value, or ValueError with an owner-facing reason."""
    if not isinstance(value, str):
        raise ValueError(f"{spec.label} must be text.")
    value = value.strip()
    cap = min(spec.max_length, MAX_URL) if spec.type in ("url", "link") else spec.max_length
    if len(value) > cap:
        raise ValueError(f"{spec.label} is too long.")
    if spec.type in ("url", "link"):
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            raise ValueError(f"{spec.label} must be a web address starting with https://.")
        if spec.allowed_hosts and host not in spec.allowed_hosts:
            raise ValueError(f"{spec.label} must be a {' or '.join(spec.allowed_hosts)} link.")
    elif spec.type == "select":
        allowed = {o.value for o in spec.options}
        picked = [v.strip() for v in value.split(",")] if spec.multi else [value]
        picked = list(dict.fromkeys(v for v in picked if v))
        if not picked or any(v not in allowed for v in picked):
            raise ValueError(f"Pick {spec.label} from the list.")
        value = ",".join(picked)
    return value


def merge_put(
    provider: IntegrationProvider, incoming: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    fields = _fields(provider)
    unknown = sorted(k for k in incoming if k not in fields)
    if unknown:
        raise ValidationError(INVALID, f"{provider.name} has no setting named {unknown[0]!r}.")
    merged = dict(current or {})
    for key, value in incoming.items():
        spec = fields[key]
        if spec.readonly or spec.hidden:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            merged.pop(key, None)
            continue
        try:
            merged[key] = check_value(spec, value)
        except ValueError as exc:
            raise ValidationError(INVALID, str(exc)) from None
    return merged


def missing_required(provider: IntegrationProvider, settings: dict[str, Any]) -> list[str]:
    missing = []
    for spec in _fields(provider).values():
        if not spec.required:
            continue
        value = (settings or {}).get(spec.key)
        try:
            if value is None or not check_value(spec, value):
                missing.append(spec.key)
        except ValueError:
            missing.append(spec.key)
    return missing


def readonly_keys(provider: IntegrationProvider) -> set[str]:
    return {k for k, f in _fields(provider).items() if f.readonly or f.hidden}


def visible(provider: IntegrationProvider, settings: dict[str, Any]) -> dict[str, Any]:
    hidden = {k for k, f in _fields(provider).items() if f.hidden}
    return {k: v for k, v in (settings or {}).items() if k not in hidden}


def field_views(provider: IntegrationProvider) -> list[dict[str, Any]]:
    return [
        {
            "key": f.key,
            "label": f.label,
            "type": f.type,
            "help": f.help or None,
            "required": f.required,
            "options": [{"value": o.value, "label": o.label} for o in f.options] or None,
            "readonly": f.readonly,
        }
        for f in _fields(provider).values()
        if not f.hidden
    ]

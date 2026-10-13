# ee/pocketpaw_ee/integrations/settings.py: per-site settings checked against a
# provider's ``settings_fields``.
#
#   merge_put(provider, incoming, current)  the PUT body's ``settings`` -> stored dict
#   missing_required(provider, settings)    required keys with no valid value
#   field_views(provider)                   the ``settings_fields`` wire list
#   visible(provider, settings)             stored settings minus hidden fields
#   server_values(provider, written)        what a site_setup result may store
#
# PUT rules: an unknown key is a 422 ``invalid_settings``; a readonly (server-set)
# key from a client is ignored and the stored value kept; "" or null clears an
# editable key; values are strings. ``url`` / ``link`` must be http(s) with an ASCII
# host, no userinfo (no ``@`` in the authority), no port, and no backslash,
# whitespace, control character, quote or angle bracket anywhere (so a browser
# can't read a different host than ``urlsplit`` does), and one of the field's
# ``allowed_hosts`` exactly when it lists any. ``select`` must be one of
# the options (comma-separated for a ``multi`` field, normalised to "a,b"). Every
# value is capped at the field's ``max_length``.

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.integrations.providers.base import IntegrationProvider, SettingsField

INVALID = "invalid_settings"
MAX_URL = 2048
_URL_BANNED = frozenset("\\\"'<>`")


def _url_host(value: str) -> str | None:
    """The lowercased host of a safe absolute http(s) URL, else None."""
    if any(c in _URL_BANNED or c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    if not value.isascii():
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or "@" in parts.netloc or port is not None:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    host = (parts.hostname or "").lower()
    return host or None


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
        host = _url_host(value)
        if host is None:
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


def server_values(provider: IntegrationProvider, written: dict[str, Any]) -> dict[str, Any]:
    """What ``site_setup`` may store: declared readonly/hidden keys whose value passes
    the field's check. Anything else is dropped."""
    kept: dict[str, Any] = {}
    for key, value in written.items():
        spec = _fields(provider).get(key)
        if spec is None or not (spec.readonly or spec.hidden):
            continue
        try:
            kept[key] = check_value(spec, value)
        except ValueError:
            continue
    return kept


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
            "multi": f.multi,
            "allowed_hosts": list(f.allowed_hosts) or None,
            "max_length": f.max_length,
        }
        for f in _fields(provider).values()
        if not f.hidden
    ]

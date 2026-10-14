# ee/pocketpaw_ee/integrations/settings.py: per-site settings checked against a
# provider's ``settings_fields``.
#
#   merge_put(provider, incoming, current, remote)  the PUT body's ``settings`` -> stored dict
#   remote_keys_to_check(provider, incoming, current)  remote selects a PUT changes
#   drop_remote(provider, settings)         settings minus remote selects (on relink)
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
# the options (comma-separated for a ``multi`` field, normalised to "a,b"). A
# ``remote`` select is checked against the provider's live options, which the
# caller loads (field_options.py) for every remote key the PUT changes and passes
# as ``remote``; a changed remote key with no loaded list is refused (fail closed).
# A stored remote value is only re-checked when it changes; a relink to another
# connection drops stored remote values first (``drop_remote``), so every remote
# key in that PUT is new and a required one reads missing until re-picked. Every
# value is capped at the field's ``max_length``.

from __future__ import annotations

from collections.abc import Collection, Mapping
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


def field(provider: IntegrationProvider, key: str) -> SettingsField | None:
    return _fields(provider).get(key)


def is_remote_select(spec: SettingsField) -> bool:
    return spec.type == "select" and spec.options_source == "remote"


def check_value(spec: SettingsField, value: Any, allowed: Collection[str] | None = None) -> str:
    """The normalised value, or ValueError with an owner-facing reason.

    ``allowed`` overrides a select's static options (a remote select's live list).
    A remote select with no ``allowed`` is only normalised: its membership was
    checked when it was stored."""
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
        if allowed is None and not is_remote_select(spec):
            allowed = {o.value for o in spec.options}
        picked = [v.strip() for v in value.split(",")] if spec.multi else [value]
        picked = list(dict.fromkeys(v for v in picked if v))
        if not picked or (allowed is not None and any(v not in allowed for v in picked)):
            raise ValueError(f"Pick {spec.label} from the list.")
        value = ",".join(picked)
    return value


def _clears(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _unchanged(value: Any, stored: Any) -> bool:
    return isinstance(value, str) and isinstance(stored, str) and value.strip() == stored


def drop_remote(provider: IntegrationProvider, settings: dict[str, Any]) -> dict[str, Any]:
    """``settings`` without remote select values: they belong to the old account."""
    fields = _fields(provider)
    return {
        k: v
        for k, v in (settings or {}).items()
        if not ((spec := fields.get(k)) is not None and is_remote_select(spec))
    }


def remote_keys_to_check(
    provider: IntegrationProvider, incoming: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    """Remote select keys whose PUT value is new, so their live options must load."""
    fields = _fields(provider)
    return [
        key
        for key, value in incoming.items()
        if (spec := fields.get(key)) is not None
        and is_remote_select(spec)
        and not (spec.readonly or spec.hidden)
        and not _clears(value)
        and not _unchanged(value, (current or {}).get(key))
    ]


def merge_put(
    provider: IntegrationProvider,
    incoming: dict[str, Any],
    current: dict[str, Any],
    remote: Mapping[str, Collection[str]] | None = None,
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
        if _clears(value):
            merged.pop(key, None)
            continue
        allowed = None
        if is_remote_select(spec) and not _unchanged(value, merged.get(key)):
            allowed = (remote or {}).get(key)
            if allowed is None:
                raise ValidationError(INVALID, f"Pick {spec.label} from the list.")
        try:
            merged[key] = check_value(spec, value, allowed)
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
            "options_source": f.options_source,
        }
        for f in _fields(provider).values()
        if not f.hidden
    ]

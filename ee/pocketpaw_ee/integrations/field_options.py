# ee/pocketpaw_ee/integrations/field_options.py: live choices for remote selects.
#
# A provider ``select`` declared with ``options_source="remote"`` (an account's
# audiences, bases, databases) gets its choices from the provider's
# ``field_options(binding, ctx, key)`` hook. ``load`` is the one caller, used by
# the owner route ``GET .../integrations/{provider}/fields/{key}/options`` and by
# the PUT validation in bindings.upsert, so both see the same list.
#
# Rules:
#   * a connect kind needs an active connection, else 409 ``not_connected``
#   * any provider failure is 502 ``provider_error`` with a generic message (the
#     cause is logged, never returned); failures are not cached
#   * the list is normalised: string values only, blank / over-long / duplicate
#     values dropped (and values with a comma for a ``multi`` field), a missing
#     label falls back to the value, capped at ``MAX_OPTIONS``
#   * cached in process for ``TTL_SECONDS`` per (workspace, pocket, provider,
#     connection, key, the effective values of the field's ``depends_on``: saved,
#     or a validated draft from the options route), i.e. per binding + key;
#     relinking another connection or picking another base is a new key. The
#     cache is bounded (``MAX_ENTRIES``, oldest out first).
#   * single-flight: concurrent misses for one key share one provider call

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.integrations import vault
from pocketpaw_ee.integrations.providers.base import (
    IntegrationProvider,
    ProviderError,
    SettingOption,
    SettingsField,
    SetupContext,
)

logger = logging.getLogger(__name__)

MAX_OPTIONS = 500
MAX_LABEL = 200
TTL_SECONDS = 60.0
MAX_ENTRIES = 2048

_cache: dict[tuple[str, ...], tuple[float, list[SettingOption]]] = {}
_inflight: dict[tuple[str, ...], asyncio.Task[list[SettingOption]]] = {}


def not_connected() -> CloudError:
    return CloudError(409, "not_connected", "Connect this integration first.")


def provider_error() -> CloudError:
    return CloudError(502, "provider_error", "The service didn't respond. Try again in a moment.")


def setup_context(
    *, workspace_id: str, pocket_id: str, user_id: str, provider: str, conn: Any | None
) -> SetupContext:
    """The context ``site_setup`` and ``field_options`` get (token under the vault lease)."""
    return SetupContext(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
        provider=provider,
        connection=conn,
        get_token=(
            (lambda rejected="": vault.get_valid_token(conn, rejected=rejected))
            if conn is not None
            else None
        ),
    )


def clear_cache() -> None:
    _cache.clear()
    _inflight.clear()


def _normalise(raw: Any, spec: SettingsField) -> list[SettingOption]:
    out: list[SettingOption] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list | tuple) else ():
        if isinstance(item, SettingOption):
            value, label = item.value, item.label
        elif isinstance(item, Mapping):
            value, label = item.get("value"), item.get("label")
        else:
            continue
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or len(value) > spec.max_length or value in seen:
            continue
        if spec.multi and "," in value:
            continue
        label = label.strip() if isinstance(label, str) else ""
        seen.add(value)
        out.append(SettingOption(value=value, label=(label or value)[:MAX_LABEL]))
        if len(out) >= MAX_OPTIONS:
            break
    return out


def _store(key: tuple[str, ...], expires: float, options: list[SettingOption]) -> None:
    if len(_cache) >= MAX_ENTRIES:
        now = time.monotonic()
        for stale in [k for k, (until, _) in _cache.items() if until <= now]:
            _cache.pop(stale, None)
        while len(_cache) >= MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))
    _cache[key] = (expires, options)


async def load(
    provider: IntegrationProvider,
    spec: SettingsField,
    *,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    binding: Any | None,
    conn: Any | None,
) -> list[SettingOption]:
    """The remote select's choices (cached), or a 409 / 502 ``CloudError``."""
    if provider.kind == "connect" and (conn is None or conn.status != "active"):
        raise not_connected()
    stored = dict(getattr(binding, "settings", None) or {})
    key = (
        workspace_id,
        pocket_id,
        provider.id,
        str(conn.id) if conn is not None else "",
        spec.key,
        *(str(stored.get(dep) or "") for dep in spec.depends_on),
    )
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(
            _fetch(provider, spec, key, workspace_id, pocket_id, user_id, binding, conn)
        )
        _inflight[key] = task
        task.add_done_callback(
            lambda done: _inflight.pop(key, None) if _inflight.get(key) is done else None
        )
    # shield: one caller going away must not cancel the call the others wait on.
    return await asyncio.shield(task)


async def _fetch(
    provider: IntegrationProvider,
    spec: SettingsField,
    key: tuple[str, ...],
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    binding: Any | None,
    conn: Any | None,
) -> list[SettingOption]:
    hook = getattr(provider, "field_options", None)
    if hook is None:
        logger.info("integrations: %s has a remote field but no field_options", provider.id)
        raise provider_error()
    ctx = setup_context(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
        provider=provider.id,
        conn=conn,
    )
    try:
        raw = await hook(binding, ctx, spec.key)
    except vault.NotConnected:
        raise not_connected() from None
    except ProviderError as exc:
        logger.info("integrations: %s field_options(%s) failed: %s", provider.id, spec.key, exc)
        raise provider_error() from None
    except Exception:
        logger.exception("integrations: %s field_options(%s) crashed", provider.id, spec.key)
        raise provider_error() from None
    options = _normalise(raw, spec)
    _store(key, time.monotonic() + TTL_SECONDS, options)
    return options

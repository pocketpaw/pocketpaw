"""Platform settings — read the resolved deployment config, write one field at a time.

Created: 2026-09-16 (feat/platform-settings-health) — chunk 11 of the Paw Admin
PRD. Design: docs/design/drafts/2026-09-15-paw-admin-screen-settings-health.md
(the binding spec for this screen, see especially §3, §4.4, §6.5) and
docs/design/drafts/2026-09-15-paw-admin-prd-corrections.md (L1/L7/L8, quoted
inline below where each one is relevant).

WHY ``platform.settings.read`` IS OPERATOR, NOT SUPPORT. Every other read in
this namespace is SUPPORT — this one is deliberately not, and that is not a
gap to "fix". The payload below renders resolved configuration including
credential SHAPE (never content) and deployment topology, which is a strictly
higher-privilege view than "what is going on with this one tenant". See
``ee/pocketpaw_ee/guards/platform.py``.

NEVER A SECRET VALUE. Every field in ``pocketpaw.credentials.SECRET_FIELDS``
reports presence/absence and shape only (``is_set``, ``provenance``) —
``value``/``default`` are always ``None`` for those fields, in both the read
response AND the audit trail recorded for a write (see ``_mask_for_audit``).
The audit collection is queryable at the SUPPORT rung
(``platform.audit.read``), a LOWER rung than ``platform.settings.write``
(OPERATOR) — recording a raw secret value in ``before``/``after`` would let a
support operator read it back out through a route they cannot reach directly.
Assume every response here can end up on an operator's screen-share.

L7/L8 (config.py) — WHAT THIS ROUTE DOES AND DOES NOT FIX. ``Settings.save()``
dumps the ENTIRE model via ``model_dump()`` and rewrites every field in
config.json plus all secrets in the credential store on any single save (L7);
``Settings.load()`` swallows a corrupt config.json and a whole-file pydantic
schema rejection with bare ``except: pass`` and no log line (L8). This route
is a NEW caller of that machinery and does not inherit either bug: the write
path below never calls ``Settings.save()`` or ``Settings.load()``. It parses
config.json itself (``_read_config_json``, surfacing "unparseable" vs.
"rejected_by_schema" as distinct, visible states instead of swallowing both)
and writes back only the changed key(s), leaving every other key in the file
untouched. ``config.py`` itself is UNCHANGED — every other caller of
``Settings.save()``/``.load()`` (the OSS settings route, the onboarding
wizard, etc.) still has L7/L8 exactly as before. Fixing those at the source is
a separate, larger change against shared machinery with many callers and is
out of scope for this chunk; see the PR description for the explicit call-out.
"""

from __future__ import annotations

import json
import logging
import os
import types
import typing
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from pydantic import TypeAdapter
from pydantic_core import PydanticUndefined

# `_IMMUTABLE_FIELDS` is imported rather than redefined: it is the one place in
# the codebase that names which fields control file-system boundaries, the
# permission gate, injection/PII scanning and the terminal — duplicating that
# list here would drift the moment either copy changes.
from pocketpaw.api.v1.settings import _IMMUTABLE_FIELDS
from pocketpaw.config import Settings, _chmod_safe, get_config_path, get_settings
from pocketpaw.credentials import SECRET_FIELDS, get_credential_store

from pocketpaw_ee.cloud._core.errors import ConflictError, Forbidden, Internal, ValidationError
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.platform import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["platform"])

_ENV_PREFIX = "POCKETPAW_"

# These two ARE `Settings` fields, but editing them here would lie to the
# operator: `ee/pocketpaw_ee/catalog/config.py` reads
# POCKETPAW_LITELLM_API_BASE / POCKETPAW_LITELLM_API_KEY directly from
# `os.environ`, independently of this process's `Settings` object, so a write
# through this route would never reach the one subsystem operators are
# usually checking this screen for. Locked regardless of provenance. See
# design doc §6.5.
_CATALOG_LOCKED_FIELDS: frozenset[str] = frozenset({"litellm_api_base", "litellm_api_key"})

# Curated groups (design doc §4.4). "Limits and thresholds" is deliberately
# NOT built here: the design doc's own §9 open questions call its membership
# "a judgement call over roughly 300 candidates" that wants an operator pass
# before the allowlist is frozen. Shipping a guessed list would be worse than
# shipping none — every field left out of a group still surfaces in
# `other_fields`, so nothing is hidden, and the group can be added later
# without touching the read/write machinery below.
_GROUPS: list[tuple[str, str, str, list[str]]] = [
    (
        "billing",
        "Billing and enforcement",
        "Whether spend limits and paid-tier gates are actually enforced on this deployment.",
        [
            "billing_enforced",
            "sites_billing_enforced",
            "billing_markup",
            "billing_dunning_grace_days",
        ],
    ),
    (
        "llm_proxy",
        "The LLM proxy",
        "The self-hosted LiteLLM proxy every backend routes through.",
        [
            "litellm_api_base",
            "litellm_api_key",
            "litellm_model",
            "litellm_max_tokens",
            "litellm_spend_ingest_enabled",
            "litellm_spend_mode",
            "litellm_reconcile_gap_threshold_credits",
            "litellm_search_api_base",
            "litellm_search_tool_name",
        ],
    ),
    (
        "payments",
        "Payments",
        "Dodo Payments credentials and product wiring.",
        [
            "dodo_payments_api_key",
            "dodo_environment",
            "dodo_billing_country",
            "dodo_webhook_secret",
            "dodo_credit_product_id",
            "dodo_plan_products",
            "dodo_checkout_return_base",
            "credit_usd",
        ],
    ),
]

_GROUPED_FIELD_NAMES: frozenset[str] = frozenset(
    name for _, _, _, names in _GROUPS for name in names
)


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class FieldOut(BaseModel):
    name: str
    label: str
    type: str
    literals: list[str] | None
    value: Any
    default: Any
    is_secret: bool
    is_set: bool
    provenance: str
    env_var: str
    shadowed_config_value: Any
    editable: bool
    locked_reason: str | None
    danger: bool
    description: str


class GroupOut(BaseModel):
    key: str
    title: str
    description: str
    danger: bool
    fields: list[FieldOut]


class ConfigJsonStatusOut(BaseModel):
    path: str
    status: str  # ok | missing | unparseable | rejected_by_schema
    detail: str | None


class CredentialStoreStatusOut(BaseModel):
    readable: bool
    detail: str | None


class SettingsPageOut(BaseModel):
    config_json: ConfigJsonStatusOut
    ignore_config_json: bool
    credential_store: CredentialStoreStatusOut
    groups: list[GroupOut]
    other_fields: list[FieldOut]


class SettingsWriteIn(BaseModel):
    changes: dict[str, Any]
    reason: str
    # Accepted but not yet deduplicated against — see the write handler's
    # docstring for why that gap is reported rather than silently no-op'd.
    idempotency_key: str | None = None


class SettingsWriteOut(BaseModel):
    applied: list[str]
    settings: SettingsPageOut


# ---------------------------------------------------------------------------
# config.json + credential store introspection
#
# Deliberately NOT `Settings.load()`. That call merges env, credential-store
# secrets and config.json into one object and (L8) discards the ENTIRE file on
# either a parse failure or a schema rejection, via two bare
# `except Exception: pass` blocks with no log line — the two are
# indistinguishable to a caller. This screen exists so an operator can tell
# "the file has content pydantic won't accept" apart from "there is no file",
# so it parses and validates config.json itself instead.
# ---------------------------------------------------------------------------


def _read_config_json() -> tuple[ConfigJsonStatusOut, dict[str, Any]]:
    path = get_config_path()
    if not path.exists():
        return ConfigJsonStatusOut(path=str(path), status="missing", detail=None), {}

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ConfigJsonStatusOut(path=str(path), status="unparseable", detail=str(exc)), {}

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return ConfigJsonStatusOut(path=str(path), status="unparseable", detail=str(exc)), {}

    if not isinstance(data, dict):
        return (
            ConfigJsonStatusOut(
                path=str(path),
                status="unparseable",
                detail="config.json does not contain a JSON object",
            ),
            {},
        )

    try:
        Settings(**data)
    except PydanticValidationError as exc:
        return (
            ConfigJsonStatusOut(path=str(path), status="rejected_by_schema", detail=str(exc)),
            data,
        )

    return ConfigJsonStatusOut(path=str(path), status="ok", detail=None), data


def _read_credential_store() -> tuple[CredentialStoreStatusOut, dict[str, str]]:
    try:
        secrets = get_credential_store().get_all()
    except Exception as exc:  # pragma: no cover - store failures are rare and defensive
        return CredentialStoreStatusOut(readable=False, detail=str(exc)), {}
    return CredentialStoreStatusOut(readable=True, detail=None), secrets


def _ignore_config_json() -> bool:
    return os.environ.get("POCKETPAW_IGNORE_CONFIG_JSON", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_env_set(name: str) -> bool:
    """Mirrors the exact expression `Settings.load()` uses to decide env wins.

    Computed against `os.environ` directly, never against a resolved value —
    a field can equal its env value by coincidence, which must not read as
    "set by env" when it was not.
    """
    return os.environ.get(f"{_ENV_PREFIX}{name.upper()}") is not None


# ---------------------------------------------------------------------------
# Field classification
# ---------------------------------------------------------------------------


def _unwrap_optional(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _field_kind(annotation: Any) -> str:
    ann = _unwrap_optional(annotation)
    if typing.get_origin(ann) is typing.Literal:
        return "literal"
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "str"
    if ann is Path:
        return "path"
    return "other"


def _literal_options(annotation: Any) -> list[str] | None:
    ann = _unwrap_optional(annotation)
    if typing.get_origin(ann) is typing.Literal:
        return [str(a) for a in typing.get_args(ann)]
    return None


def _json_safe(value: Any) -> Any:
    # Fields with no static default (required, or default_factory-only) carry
    # pydantic's internal `PydanticUndefined` sentinel in `FieldInfo.default`.
    # It is not JSON-serializable and must never reach a response body.
    if value is PydanticUndefined:
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _label(name: str) -> str:
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else text


def _mask_for_audit(name: str, value: Any) -> Any:
    """Never a secret value in the audit trail. See module docstring."""
    if name in SECRET_FIELDS:
        return {"is_set": bool(value)}
    return _json_safe(value)


# ---------------------------------------------------------------------------
# Field assembly
# ---------------------------------------------------------------------------


def _build_field(
    name: str,
    *,
    live: Settings,
    config_json: dict[str, Any],
    secrets: dict[str, str],
) -> FieldOut:
    field_info = Settings.model_fields[name]
    is_secret = name in SECRET_FIELDS

    if _is_env_set(name):
        provenance = "env"
    elif is_secret and bool(secrets.get(name)):
        provenance = "credential_store"
    elif name in config_json:
        provenance = "config_json"
    else:
        provenance = "default"

    resolved = getattr(live, name)

    shadowed = None
    if provenance == "env" and not is_secret and name in config_json:
        shadowed = _json_safe(config_json[name])

    locked_reason: str | None = None
    editable = True
    if name in _IMMUTABLE_FIELDS:
        editable = False
        locked_reason = "security-critical field; change config.json or the environment directly"
    elif name in _CATALOG_LOCKED_FIELDS:
        editable = False
        locked_reason = (
            "read directly from the environment by the model catalog; change the environment"
        )
    elif provenance == "env":
        editable = False
        locked_reason = (
            f"set by {_ENV_PREFIX}{name.upper()} in the environment; "
            "a write here would be silently overridden on the next reload"
        )

    return FieldOut(
        name=name,
        label=_label(name),
        type=_field_kind(field_info.annotation),
        literals=_literal_options(field_info.annotation),
        value=None if is_secret else _json_safe(resolved),
        default=None if is_secret else _json_safe(field_info.default),
        is_secret=is_secret,
        is_set=provenance != "default",
        provenance=provenance,
        env_var=f"{_ENV_PREFIX}{name.upper()}",
        shadowed_config_value=shadowed,
        editable=editable,
        locked_reason=locked_reason,
        danger=name in _IMMUTABLE_FIELDS,
        description=field_info.description or "",
    )


def _catalog_rows() -> list[FieldOut]:
    """The two catalog knobs that are not `Settings` fields at all.

    `ee/pocketpaw_ee/catalog/config.py` reads these directly from env
    (one prefixed, one not — see the docstring there) independently of
    `Settings`, so without this they would not appear anywhere on a screen
    meant to show every knob that shapes this deployment.
    """
    from pocketpaw_ee.catalog import config as catalog_config

    ttl_env_set = bool(os.environ.get("CATALOG_CACHE_TTL_SECONDS", "").strip())
    models_dev_env_set = bool(os.environ.get("CATALOG_MODELS_DEV_ENABLED", "").strip())

    return [
        FieldOut(
            name="catalog_cache_ttl_seconds",
            label="Catalog cache TTL seconds",
            type="int",
            literals=None,
            value=catalog_config.cache_ttl_seconds(),
            default=catalog_config.DEFAULT_CACHE_TTL_SECONDS,
            is_secret=False,
            is_set=ttl_env_set,
            provenance="env" if ttl_env_set else "default",
            env_var="CATALOG_CACHE_TTL_SECONDS",
            shadowed_config_value=None,
            editable=False,
            locked_reason=(
                "read from an unprefixed environment variable by the catalog module, "
                "not a Settings field; change the environment"
            ),
            danger=False,
            description="In-process TTL for the assembled model catalog.",
        ),
        FieldOut(
            name="catalog_models_dev_enabled",
            label="Catalog models.dev enrichment enabled",
            type="bool",
            literals=None,
            value=catalog_config.models_dev_enabled(),
            default=True,
            is_secret=False,
            is_set=models_dev_env_set,
            provenance="env" if models_dev_env_set else "default",
            env_var="CATALOG_MODELS_DEV_ENABLED",
            shadowed_config_value=None,
            editable=False,
            locked_reason=(
                "read from an unprefixed environment variable by the catalog module, "
                "not a Settings field; change the environment"
            ),
            danger=False,
            description=(
                "Best-effort models.dev enrichment for the catalog. "
                "False runs LiteLLM-only, with no outbound models.dev fetch."
            ),
        ),
    ]


async def _build_settings_page() -> SettingsPageOut:
    config_status, config_json = _read_config_json()
    cred_status, secrets = _read_credential_store()
    live = get_settings()

    field_names = sorted(name for name in Settings.model_fields if not name.startswith("_"))
    fields_by_name = {
        name: _build_field(name, live=live, config_json=config_json, secrets=secrets)
        for name in field_names
    }

    groups = [
        GroupOut(
            key=key,
            title=title,
            description=description,
            danger=any(fields_by_name[n].danger for n in names if n in fields_by_name),
            fields=[fields_by_name[n] for n in names if n in fields_by_name],
        )
        for key, title, description, names in _GROUPS
    ]

    other_fields = [
        fields_by_name[name] for name in field_names if name not in _GROUPED_FIELD_NAMES
    ]
    other_fields.extend(_catalog_rows())

    return SettingsPageOut(
        config_json=config_status,
        ignore_config_json=_ignore_config_json(),
        credential_store=cred_status,
        groups=groups,
        other_fields=other_fields,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=SettingsPageOut)
async def get_platform_settings(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.settings.read"))],
) -> SettingsPageOut:
    """The resolved configuration for this deployment.

    Gated at OPERATOR — see the module docstring for why this read, alone
    among reads in this namespace, is not SUPPORT.
    """
    page = await _build_settings_page()

    await audit.record_read(
        operator=operator,
        action="platform.settings.read",
        query="settings",
        target_type="platform_settings",
        request=request,
    )

    return page


def _validate_change_value(name: str, raw_value: Any) -> Any:
    field_info = Settings.model_fields[name]
    adapter = TypeAdapter(field_info.annotation)
    try:
        return adapter.validate_python(raw_value)
    except PydanticValidationError as exc:
        raise ValidationError(
            "platform.settings.invalid_value",
            f"{name}: value does not match the expected type ({field_info.annotation})",
        ) from exc


@router.put("", response_model=SettingsWriteOut)
async def update_platform_settings(
    body: SettingsWriteIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.settings.write"))],
) -> SettingsWriteOut:
    """Change one or more settings fields, field-scoped.

    Deliberately NOT ``Settings.load()`` -> ``setattr`` -> ``.save()`` (the
    pattern the OSS route uses, and the source of L7): that dumps and
    rewrites every one of ~330 fields, plus all 31 secrets, on any single
    save. This handler reads config.json itself, changes only the requested
    key(s), and writes the rest of the file back untouched. Secrets never
    touch config.json — they go straight to the encrypted credential store.

    ``idempotency_key`` is accepted on the wire per the design contract but is
    NOT yet deduplicated against — there is no store for it in this chunk. A
    retried request with the same key currently just re-applies (idempotently
    fine for a plain field assignment, but not detected/short-circuited as a
    duplicate). Flagged here and in the PR description rather than silently
    dropped.
    """
    reason = (body.reason or "").strip()
    if not reason:
        raise ValidationError(
            "platform.settings.reason_required", "A reason is required to change settings"
        )
    if not body.changes:
        raise ValidationError("platform.settings.no_changes", "No changes were supplied")

    config_status, config_json = _read_config_json()
    if config_status.status == "unparseable":
        raise ConflictError(
            "platform.settings.config_unparseable",
            "config.json is not valid JSON; fix or remove it before writing settings here",
        )

    validated: dict[str, Any] = {}
    for name, raw_value in body.changes.items():
        if name not in Settings.model_fields or name.startswith("_"):
            raise ValidationError(
                "platform.settings.unknown_field", f"Unknown settings field: {name}"
            )
        if name in _IMMUTABLE_FIELDS:
            raise Forbidden(
                "platform.settings.immutable_field",
                f"{name} cannot be changed through this API",
            )
        if name in _CATALOG_LOCKED_FIELDS:
            raise Forbidden(
                "platform.settings.catalog_locked_field",
                f"{name} is read directly from the environment by the model catalog "
                "and cannot be changed here",
            )
        if _is_env_set(name):
            raise ConflictError(
                "platform.settings.env_shadowed",
                f"{name} is set by {_ENV_PREFIX}{name.upper()} in the environment; "
                "a write here would be silently overridden on the next reload",
            )
        validated[name] = _validate_change_value(name, raw_value)

    live_before = get_settings()
    before = {name: _mask_for_audit(name, getattr(live_before, name)) for name in validated}

    event = await audit.begin(
        operator=operator,
        action="platform.settings.write",
        reason=reason,
        target_type="platform_settings",
        before=before,
        request=request,
    )

    try:
        store = get_credential_store()
        updated_config_json = dict(config_json)
        for name, value in validated.items():
            if name in SECRET_FIELDS:
                store.set(name, value)
                # Never plaintext in config.json, even if an older write left
                # it there before this route existed.
                updated_config_json.pop(name, None)
            else:
                updated_config_json[name] = _json_safe(value)

        config_path = get_config_path()
        config_path.write_text(json.dumps(updated_config_json, indent=2))
        _chmod_safe(config_path, 0o600)

        get_settings.cache_clear()
    except Exception as exc:
        await audit.settle(event, ok=False)
        raise Internal(
            "platform.settings.write_failed", "Failed to write settings"
        ) from exc

    live_after = get_settings()
    after = {name: _mask_for_audit(name, getattr(live_after, name)) for name in validated}
    await audit.settle(event, ok=True, after=after)

    page = await _build_settings_page()
    return SettingsWriteOut(applied=sorted(validated), settings=page)


__all__ = ["router"]
